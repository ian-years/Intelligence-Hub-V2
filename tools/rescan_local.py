"""磁盘重扫恢复：把 `data/media/` 那棵树里还在、库里却丢了的作品/博主行重建回来。

契约来源：`docs/specs/data-model.md §1`（目录布局）+ `§2.2/§2.3`（两张表）+
ADR-0018 决定 1（这一条**必须真适配**：它写的是 V2 的库与 V2 的目录约定，
V1 那份 `local_store.migrate_all_local_data()` 搬的是 V1 的扁平表和 V1 的
`downloads/` 布局，表结构对不上，所以这里只借它"从磁盘重建库"这件事，不借它的代码）。

**跑法**（`--data-dir` 指的是"那一棵 `data/`"，媒体树 = 其下的 `media/`；
不指就用配置的 `data.dir`，所以**验证时务必显式指到 `.scratch/` 下造出来的树**）：

```bash
# 预演（默认）：只扫、只记账，库里一行都不写，也不建任何目录/库文件
uv run python -X utf8 tools/rescan_local.py --data-dir .scratch/rescan-demo/data
# 真写库
uv run python -X utf8 tools/rescan_local.py --data-dir .scratch/rescan-demo/data --apply
# 机器可读的完整报告（跳过原因全量；日志走 stderr，stdout 只有这一份 JSON）
uv run python -X utf8 tools/rescan_local.py --data-dir .scratch/rescan-demo/data --apply --json
```

退出码：**0** = 跑完了且没有任何"东西没做成"；**1** = `report.errors` 非空
（元数据读不出来、某行入库失败、库不可用）；用法错误走 `SystemExit` 带文案
（媒体根目录不存在 = `--data-dir` 指错了）。与 `tools/migrate_from_v1.py` 同一套含义。

## 五条硬规矩（每条都有一条会红的用例对着）

1. **默认 dry-run，要 `--apply` 才写库。** 这个工具往库里**加行**，误跑的代价
   不比误删小：满库的"来自磁盘的假作品"。dry-run 连目标库文件都不创建
   （库里还没有它时用内存库比对，并如实说明"下面的计数是按空库算的"）。
2. **磁盘只读。** 一个字节都不写、不改名、不删除 —— 认不出来的目录**原地不动**，
   只在报告里说清为什么认不出来。库里已有的行也**一个字段都不改**
   （查重命中就返回既有行，见规矩 3）。
3. **查重只走 `insert_or_get()`。** 不自己拼 SQL、不写 `INSERT`、不做"先查后插"的
   手写版本：那是 §7.11 那一族"同一个东西两个实现"。幂等（同一棵树扫两遍
   第二遍 `created=0`）完全靠 `(platform, platform_video_id)` / `(platform, platform_id)`
   这两个唯一索引。
4. **路径只有 `FileStorage` 一个算得出者。** 认目录不是靠猜分隔规则，而是
   **拿 `files.media_dir(platform, creator, id, title)` 反算一遍再和盘上比**：
   相等才认。媒体/封面/metadata/口播稿的位置同样只用
   `media_file()` / `cover_file()` / `metadata_file()` / `transcript_path()` 算。
   进库的路径一律 `files.rel()`（相对 `data/`、posix 分隔符）。
5. **认不出来就说，不猜。** 每条跳过都有 `reason` 原文；每条重建出来的行都带
   `metadata_json."rescanned_from_disk"` 凭据（与 V1 迁移标记怎么区分见下面
   `PROVENANCE_KEY` 那段）。
   拿不到博主身份时把作品记成**无归属**（`creator_id IS NULL`），
   而不是编一个 `platform_id`；`media_source` 这种带 CHECK 枚举的列照实留 NULL。

## 故意不做的事

- **不重建 `transcripts` 行。** 那一行的 `engine` 是有 CHECK 枚举的列，磁盘上只有
  `transcript/speech-clean.txt` 这个事实，说不出它是 sherpa 还是字幕或人工 ——
  编一个取值就是把 `failed` 记成 0。工具只在报告里给出"目录里有稿子但库里没建索引"
  的条数（`transcripts_on_disk`），要接回去是 T4.x 另一件事。
- **不打开任何开关。** 新建的 `platforms` 镜像行 `enabled=False`（开关的权威源是
  `platforms.yaml`）；新建的博主行 `is_tracking=False`（勾上它等于替用户决定
  "从明天起自动采集这位"，那是有代价的决定，得由人做）。报告里两条都点名。
- **不认没有配置 schema 的平台目录。** 清单只问 `supported_platforms()`（=
  `PLATFORM_CONFIG_SCHEMAS` 的 key，谁被注册就认谁，本文件里不写死名字）。
  理由不是洁癖：`creators.platform` 外键指向 `platforms` 镜像表，而服务启动时的
  `prune_unknown()` 会删掉本构建不支持的行 —— 底下还挂着博主时
  `ON DELETE RESTRICT` 会让服务**起不来**。红在扫描阶段比红在启动阶段便宜。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 脚本入口：把仓库根（tools/ 的上一级）放进 sys.path，好 `import intelligence_hub_v2`。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import ValidationError

from intelligence_hub_v2.core.config import AppConfig, load_app_config
from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.creator import Creator, CreatorDraft
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.platforms import supported_platforms
from intelligence_hub_v2.storage.db import SqliteStorage, resolve_db_path
from intelligence_hub_v2.storage.files import FileStorage

__all__ = ["RescanReport", "main", "rescan", "scan_tree"]

PROVENANCE_KEY = "rescanned_from_disk"
"""`metadata_json` 里的凭据键（值恒为 `True`）。

**与 `migrated_from_v1` 是两回事，两边都不许混：**
`tools/migrate_from_v1.py --rollback` 的判据是 `payload["migrated_from_v1"] is True`，
重扫行没有那个键 → 不会被它删掉；反过来本工具只插不改，也碰不到迁移行。
光有键名不够，同一份 payload 里必须留得出来自哪一棵目录（`rescan_source`），
否则以后仍然分不出"这条是谁灌的"。
"""

CREATOR_ID_KEYS: tuple[str, ...] = (
    "uploader_id",
    "sec_uid",
    "mid",
    "channel_id",
    "user_id",
    "owner_id",
)
"""`metadata.json` 里可能带博主原生 ID 的键，按顺序取第一个非空值。

**不 import V1 的那份键清单**（V1 `migrate_all_local_data()` 里是一串内联 `or`）：
这份是 yt-dlp 出口里"确实是博主身份"的那几个，逐个点名写在这里是为了能被证伪 ——
V1 那份还顺带用了 `vdir.parent.name`（目录名当 ID），那正是本工具拒绝的猜法。
"""

CREATOR_NAME_KEYS: tuple[str, ...] = ("uploader", "channel", "creator", "artist")
VIDEO_TITLE_KEYS: tuple[str, ...] = ("title", "fulltitle")

VIDEO_EXTS = frozenset({".mp4", ".webm", ".mkv", ".m4v", ".mov", ".flv"})
"""能当"这条作品有画面"的扩展名。

只有**恰好一个**这样的文件时才敢把它当主媒体；多个就不猜（见 `_media_of`）。
"""

AUDIO_EXTS = frozenset({".m4a", ".mp3", ".m4b", ".aac", ".opus", ".ogg"})
"""音频轨。DASH 未合并时目录里是 `media.f137.mp4` + `media.f140.m4a` 两个文件
（V1 §7.21），本工具**不猜哪一条是视频轨**：主媒体留 NULL，两条都进
`media_aux_paths_json`，并在报告里单独计数。"""

MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

TITLE_FROM_METADATA = "metadata.json"
TITLE_FROM_DIR = "目录名"
TITLE_FROM_ID = "作品 ID 占位"
"""标题的出处。`TITLE_FROM_ID` = 目录名后半段与作品 ID 相同
（`tasks/single_link.py` 就是 `media_dir(platform, "single-link", id, id)` 这个形状），
库里那格 `title` 存的是 ID 而不是真标题，必须能被数出来。"""

OUTCOME_CREATED = "新建"
OUTCOME_EXISTING = "已存在"
OUTCOME_BY_NAME = "按昵称认出"
OUTCOME_UNKNOWN = "认不出"
"""博主那一侧的四种走法（`_CreatorIndex.resolve` 的返回值）。

为什么要走法而不是直接 `+1`：**计数必须在事务提交之后**才落到报告上，
所以"认出"这一步只能把结果交回去、不能自己记账。"""


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """一条"哪个目录 + 为什么"。跳过原因与无归属原因共用这个形状。"""

    path: str
    reason: str


@dataclass
class RescanReport:
    dry_run: bool
    entries_seen: int = 0
    """扫到的**每一个**条目（目录与散文件）。

    不变量：`entries_seen == recognized + len(skipped)`。用例按这条判，
    不按"我数了 3 条"判 —— 少扫一项却报"全部处理完"是这条工具最坏的失败方式。
    """

    recognized: int = 0
    skipped: list[Finding] = field(default_factory=list)
    videos_created: int = 0
    videos_existing: int = 0
    creators_created: int = 0
    creators_existing: int = 0
    creators_matched_by_name: int = 0
    videos_without_creator: int = 0
    unattributed: list[Finding] = field(default_factory=list)
    """`videos_without_creator` 的原文清单（哪棵目录、为什么认不出博主）。"""

    placeholder_titles: int = 0
    without_main_media: int = 0
    transcripts_on_disk: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 扫树（同步：全部文件系统动作都在这一半，异步那半只碰库）
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    skips: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def entries_seen(self) -> int:
        return len(self.candidates) + len(self.skips)


@dataclass(frozen=True)
class Candidate:
    """一棵认得出来的作品目录。

    **所有文件系统事实在这里就结清**（`media_path` / `aux_paths` / `cover_path` /
    `has_transcript`），异步那半只做库的事 —— 既不需要 `asyncio.to_thread`，
    也不会出现"库里写了个路径、稍后再去看它还在不在"的竞态。
    """

    platform: str
    creator_name: str
    platform_video_id: str
    title: str
    title_source: str
    media_dir: Path
    metadata: dict[str, Any]
    media_path: str | None
    aux_paths: tuple[str, ...]
    cover_path: str | None
    has_transcript: bool
    identity_from_metadata: bool


def scan_tree(files: FileStorage) -> ScanResult:
    """扫 `data/media/<platform>/<creator>/<id>-<title>/`。约定见 `FileStorage.media_dir`。

    层级是**定死三层**：多出来的深度（`transcript/`、`audio/`）住在作品目录里面，
    不参与候选；少一层的散文件逐条记跳过，不留成"静默少一半"。
    """
    result = ScanResult()
    supported = set(supported_platforms())
    try:
        platform_dirs = _children(files.media_root)
    except OSError as exc:
        # 根都读不出来就别往下装：这轮的任何一个数字都没有意义，退出码必须非 0。
        result.errors.append(
            f"媒体根目录读不出来（{files.media_root}）：{type(exc).__name__}: {exc}"
        )
        return result
    for platform_dir in platform_dirs:
        name = platform_dir.name
        if not platform_dir.is_dir():
            result.skips.append(
                Finding(_rel(files, platform_dir), "媒体根下的散文件（这一层只认平台目录）")
            )
            continue
        if name not in supported:
            # 整棵子树一次说清，不再逐条报（否则 200 条目录刷出 200 行同一个原因）。
            result.skips.append(
                Finding(
                    _rel(files, platform_dir),
                    f"平台 {name!r} 不在本构建的支持清单里，整个子树未进入"
                    f"（支持：{', '.join(sorted(supported)) or '无'}）。"
                    "理由见模块 docstring 的『不认没有配置 schema 的平台目录』",
                )
            )
            continue
        for creator_dir in _children_of(files, result, platform_dir):
            _scan_creator(files, result, name, creator_dir)
    return result


def _listing(directory: Path) -> tuple[list[Path], str | None]:
    """列一层目录，并把"读不出来"翻成**原文**而不是异常。

    一棵坏掉的目录（权限、正在被删、符号链接成环）不该掀掉整轮 —— 剩下那些还要有结论，
    而失败原文得留在报告里，不能变成"扫到 0 条、完成"（V1 §1.3）。
    """
    try:
        return _children(directory), None
    except OSError as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _children_of(files: FileStorage, result: ScanResult, directory: Path) -> list[Path]:
    """`_listing` + "读不出来就记一条带原文的跳过"。用在非致命的那两层。"""
    entries, unreadable = _listing(directory)
    if unreadable is not None:
        result.skips.append(Finding(_rel(files, directory), f"读不出这一层目录：{unreadable}"))
    return entries


def _scan_creator(files: FileStorage, result: ScanResult, platform: str, creator_dir: Path) -> None:
    if not creator_dir.is_dir():
        result.skips.append(
            Finding(_rel(files, creator_dir), f"{platform}/ 下的散文件（这一层只认博主目录）")
        )
        return
    if _is_hidden(creator_dir):
        result.skips.append(Finding(_rel(files, creator_dir), "隐藏目录（以 . 开头）"))
        return
    for leaf in _children_of(files, result, creator_dir):
        _scan_leaf(files, result, platform, creator_dir.name, leaf)


def _scan_leaf(
    files: FileStorage, result: ScanResult, platform: str, creator_name: str, leaf: Path
) -> None:
    where = _rel(files, leaf)
    if not leaf.is_dir():
        result.skips.append(Finding(where, "不是目录：作品必须是 <作品 ID>-<标题>/ 这一级"))
        return
    if _is_hidden(leaf):
        result.skips.append(Finding(where, "隐藏目录（以 . 开头）"))
        return
    video_id, sep, title = leaf.name.partition("-")
    if not sep or not video_id or not title:
        result.skips.append(
            Finding(where, "目录名不是 <作品 ID>-<标题> 形状（分隔符缺失，或 ID/标题为空）")
        )
        return
    expected = files.media_dir(platform, creator_name, video_id, title)
    if expected != leaf:
        # 这一句就是"路径逻辑只有 FileStorage 一个真源"的落点：不对就不认，不动文件。
        result.skips.append(
            Finding(
                where,
                f"目录名与 FileStorage.media_dir() 反算出来的 {expected.name!r} 不一致 —— "
                "不是本仓库写出来的树（手改过 / 外部工具建的），原地保持不动",
            )
        )
        return

    meta, meta_error = _read_metadata(files.metadata_file(platform, creator_name, video_id, title))
    if meta_error is not None:
        # 不丢这条作品：按目录名照样能重建，只是元数据没采用。但这不是"成功"，进 errors。
        result.errors.append(f"{where} 的 metadata.json 未被采用（已按目录名重建）：{meta_error}")

    entries, unreadable = _listing(leaf)
    if unreadable is not None:
        # 记成跳过就到此为止：这棵目录既不该被重建，也不该同时被算成候选。
        result.skips.append(Finding(where, f"读不出这棵目录：{unreadable}"))
        return
    media_path, aux_paths = _media_of(files, entries, platform, creator_name, video_id, title)
    cover = files.cover_file(platform, creator_name, video_id, title)
    cover_path = _rel(files, cover) if cover.is_file() else None
    has_transcript = files.transcript_path(leaf).is_file()
    if not (media_path or aux_paths or meta or meta_error or cover_path or has_transcript):
        result.skips.append(
            Finding(where, "目录里没有任何产物（媒体 / cover.jpg / metadata.json / 口播稿都没有）")
        )
        return

    title_final, title_source = _title_of(meta, title, video_id)
    result.candidates.append(
        Candidate(
            platform=platform,
            creator_name=creator_name,
            platform_video_id=video_id,
            title=title_final,
            title_source=title_source,
            media_dir=leaf,
            metadata=meta,
            media_path=media_path,
            aux_paths=aux_paths,
            cover_path=cover_path,
            has_transcript=has_transcript,
            identity_from_metadata=meta_error is None and bool(meta),
        )
    )


def _children(directory: Path) -> list[Path]:
    """目录下的条目，按名字排序（报告顺序稳定 = 两次跑出来的东西可 diff）。"""
    return sorted(directory.iterdir(), key=lambda item: item.name)


def _is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


def _rel(files: FileStorage, path: Path) -> str:
    return files.rel(path)


def _read_metadata(path: Path) -> tuple[dict[str, Any], str | None]:
    """读 `metadata.json`。返回 (内容, 读不出来的原文)。

    文件不存在是**正常路径**（V2 今天的采集不写它，见 `platforms/douyin/adapter.py`
    模块 docstring 那句"V1 写、V2 不写"）→ 空 dict 且不是错误。
    """
    if not path.is_file():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return {}, f"顶层不是 JSON 对象（是 {type(payload).__name__}）"
    return payload, None


def _media_of(
    files: FileStorage,
    entries: list[Path],
    platform: str,
    creator_name: str,
    video_id: str,
    title: str,
) -> tuple[str | None, tuple[str, ...]]:
    """(主媒体 rel 路径, 其余媒体 rel 路径)。`entries` 是这棵目录里列出来的条目。

    三条判据，按优先级：
    1. `files.media_file()` 算出来的 `media.mp4` 在 → 它是主媒体（V2 的合并产物名）。
    2. 否则目录里**恰好一个**媒体文件、且它是视频扩展名 → 它是主媒体
       （`media.webm` 这类换封装的产物）。
    3. 否则没有主媒体：0 个 = 只有 metadata/稿子；≥2 个 = DASH 分片没合并
       （V1 §7.21 的形状，`media.f137.mp4` + `media.f140.m4a` 里认不出哪条是画面轨，
       **不猜**）；只有音频 = 根本没有画面。媒体文件全部进
       `media_aux_paths_json`，一条都不丢。
    """
    main = files.media_file(platform, creator_name, video_id, title)
    present = [item for item in entries if item.is_file() and item.suffix.lower() in MEDIA_EXTS]
    if main.is_file():
        return _rel(files, main), tuple(_rel(files, p) for p in present if p != main)
    if len(present) == 1 and present[0].suffix.lower() in VIDEO_EXTS:
        return _rel(files, present[0]), ()
    return None, tuple(_rel(files, p) for p in present)


def _title_of(meta: dict[str, Any], dir_title: str, video_id: str) -> tuple[str, str]:
    """标题与"它是从哪来的"。元数据里的优先（目录名那半段可能被截到 60 字）。"""
    for key in VIDEO_TITLE_KEYS:
        raw = str(meta.get(key) or "").strip()
        if raw:
            return raw, TITLE_FROM_METADATA
    if dir_title == video_id:
        return dir_title, TITLE_FROM_ID
    return dir_title, TITLE_FROM_DIR


def _first_meta_str(meta: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        raw = str(meta.get(key) or "").strip()
        if raw:
            return raw
    return None


def _int_of(meta: dict[str, Any], key: str) -> int | None:
    """只收真整数。`True` 是 `int` 的子类，所以显式排掉布尔 —— 别把标志位存成播放量。"""
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _float_of(meta: dict[str, Any], key: str) -> float | None:
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch_published_at(meta: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """`timestamp`（epoch 秒）。这是**唯一无歧义**的那种：它自带绝对时刻，不含时区猜测。"""
    stamp = meta.get("timestamp")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None, None
    try:
        return datetime.fromtimestamp(float(stamp), UTC), None
    except (OverflowError, OSError, ValueError):  # pragma: no cover - 极端时间戳
        return None, str(stamp)


def _text_published_at(meta: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """文本形状：只有**带 tz** 的 ISO 串才认。

    `upload_date`（`"20240501"`）既没有时间也没有时区，补成本机午夜就是凭空指定一个偏移
    （`tools/migrate_from_v1.py:_parse_published_at` 为此专门把假设随数据记下来）——
    这里更保守：列留 NULL、原文进 metadata_json。宁可 Feed 排序看不见它，
    也不写一个假装是 UTC 的时刻。
    """
    raw = str(meta.get("publication_date") or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None, raw
        return (parsed, None) if parsed.tzinfo is not None else (None, raw)
    date_only = str(meta.get("upload_date") or "").strip()
    return (None, date_only) if date_only else (None, None)


def _published_at_of(meta: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """发布时间。返回 (值, 认不出来但有的原文)。判据在上面两个 helper 里。"""
    value, raw = _epoch_published_at(meta)
    if value is not None or raw is not None:
        return value, raw
    return _text_published_at(meta)


# ---------------------------------------------------------------------------
# 草稿
# ---------------------------------------------------------------------------


def _provenance_payload(*, source: str, source_abs: str, extra: dict[str, Any]) -> dict[str, Any]:
    """凭据：来源 = 磁盘重扫 + 得出来的那棵目录（相对 + 绝对各一份）+ 什么时候扫的。

    **不把整份 `metadata.json` 塞进来**（几十 KB 一行的教训见 `api/v1/export.py`
    那段注释）：映射不进列的信息，原文要就去那棵目录里看 —— 那棵树才是原始产物。
    绝对路径只是给人核对用的旁注，库里承重的那一列 (`media_path`) 仍走 `files.rel()`。
    """
    return {
        PROVENANCE_KEY: True,
        "rescan_source": source,
        "rescan_source_abs": source_abs,
        "rescan_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **extra,
    }


def _video_draft(files: FileStorage, cand: Candidate, creator_id: int | None) -> VideoDraft:
    published_at, unmapped = _published_at_of(cand.metadata)
    extra: dict[str, Any] = {
        "rescan_identity": TITLE_FROM_METADATA if cand.identity_from_metadata else TITLE_FROM_DIR,
        "rescan_title_source": cand.title_source,
        "rescan_has_transcript": cand.has_transcript,
    }
    if cand.media_path is None and cand.aux_paths:
        extra["rescan_media_note"] = "多个媒体文件，主媒体指针未猜（可能是未合并的 DASH 分片）"
    if unmapped:
        extra["rescan_published_at_unmapped"] = unmapped
    payload = _provenance_payload(
        source=_rel(files, cand.media_dir), source_abs=str(cand.media_dir), extra=extra
    )
    return VideoDraft(
        platform=cand.platform,
        platform_video_id=cand.platform_video_id,
        creator_id=creator_id,
        title=cand.title,
        description=_first_meta_str(cand.metadata, ("description",)),
        published_at=published_at,
        duration_seconds=_float_of(cand.metadata, "duration"),
        view_count=_int_of(cand.metadata, "view_count"),
        like_count=_int_of(cand.metadata, "like_count"),
        comment_count=_int_of(cand.metadata, "comment_count"),
        share_count=_int_of(cand.metadata, "share_count"),
        media_path=cand.media_path,
        # 这条媒体当年走的是 yt-dlp 还是页面播放直链，磁盘上问不出来 —— 照实 NULL，
        # 不塞一个 CHECK 枚举认的取值来"让它显示成有来源"（V1 §7.2 要的恰恰是原文）。
        media_source=None,
        media_aux_paths_json=json.dumps(list(cand.aux_paths), ensure_ascii=False),
        cover_path=cand.cover_path,
        metadata_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _creator_draft(files: FileStorage, cand: Candidate, platform_id: str) -> CreatorDraft:
    name = _first_meta_str(cand.metadata, CREATOR_NAME_KEYS) or cand.creator_name
    payload = _provenance_payload(
        source=_rel(files, cand.media_dir.parent),
        source_abs=str(cand.media_dir.parent),
        extra={
            "rescan_platform_id_from": "metadata.json",
            "rescan_platform_id": platform_id,
            "rescan_creator_dir": cand.creator_name,
        },
    )
    return CreatorDraft(
        platform=cand.platform,
        platform_id=platform_id,
        name=name,
        profile_url=f"{cand.platform}://{platform_id}",
        # 重扫不代用户打开自动采集：`is_tracking=True` 的下一天定时任务就会去收这位
        # （开关的权威源是配置/Settings 页，一次性脚本不许顺手改，同 migrate 的
        # `enabled=False` 那条判据）。
        is_tracking=False,
        metadata_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


# ---------------------------------------------------------------------------
# 写库
# ---------------------------------------------------------------------------


class _CreatorIndex:
    """博主行的"认出 → 补建"。查重只走 `creators.insert_or_get()` / `find()`。

    身份来源两档，**顺序不能反**：
    1. `metadata.json` 里的平台原生 ID（`CREATOR_ID_KEYS`）→ 那是真身份，可以建行；
    2. 目录名 = 昵称，且库里该平台**恰好一位**同名博主 → 认领既有行。
    昵称不是身份（V1 §7.11 的教训就是昵称撞车），所以第 3 档不存在：
    既没有 ID 又对不上唯一同名时，作品按 `creator_id = None` 入库并进
    `report.unattributed`，绝不凭空造一个 `platform_id`。
    """

    def __init__(self, storage: SqliteStorage, files: FileStorage, report: RescanReport) -> None:
        self._storage = storage
        self._files = files
        self._report = report
        self._by_name: dict[str, list[Creator]] = {}
        self._seeded: set[str] = set()

    async def resolve(self, cand: Candidate, *, dry_run: bool) -> tuple[int | None, str]:
        """返回 `(creator_id, 认出的走法)`。

        **这里一行都不记到 report 的计数里**（只往 `unattributed` 里留原因）：
        计数要等事务提交成功才算数 —— 在 `with storage.transaction()` 里面加，
        万一后面的作品写入炸了、整个事务回滚，报告就会声称"新建了 1 位博主"
        而库里其实一行都没有。那就是"臆造成功"最小的一个变体。
        """
        platform_id = _first_meta_str(cand.metadata, CREATOR_ID_KEYS)
        if platform_id is None:
            return await self._lookup_by_name(cand)
        if dry_run:
            existing = await self._storage.creators.find(cand.platform, platform_id)
            return (None, OUTCOME_CREATED) if existing is None else (existing.id, OUTCOME_EXISTING)
        row, created = await self._storage.creators.insert_or_get(
            _creator_draft(self._files, cand, platform_id)
        )
        if created:
            self._by_name.pop(cand.platform, None)  # 刚补的这位也可能被后面的目录按名字认出来
        return (row.id, OUTCOME_CREATED if created else OUTCOME_EXISTING)

    async def ensure_platform_row(self, platform: str) -> None:
        """补 `platforms` 镜像行（`creators.platform` 的外键目标）。

        **每条候选都要**，不是只在"要建博主行"的时候才补：库里有一条
        `videos.platform='douyin'` 而镜像表里没有 douyin，前端那个平台的列表就是空的，
        看着像"重扫压根没生效"。

        **不 import `migrate_from_v1._ensure_platform_row`**：那是一次性工具里的私有函数，
        搬完就要退役，跨工具引私有符号等于把两个工具的生命周期绑在一起。
        真正的单一实现是 `PlatformRepository.upsert()`，两边都调它。
        `enabled=False`：镜像不是权威源，重扫不许顺手打开任何平台。
        """
        if platform in self._seeded:
            return
        if await self._storage.platforms.get(platform) is None:
            await self._storage.platforms.upsert(platform, enabled=False)
        self._seeded.add(platform)

    async def _lookup_by_name(self, cand: Candidate) -> tuple[int | None, str]:
        """没有平台 ID 时唯一的兜底：库里该平台**恰好一位**同名博主。"""
        rows = self._by_name.get(cand.platform)
        if rows is None:
            rows = await self._storage.creators.list_all(platform=cand.platform)
            self._by_name[cand.platform] = rows
        matches = [row for row in rows if row.name == cand.creator_name]
        if len(matches) == 1:
            return matches[0].id, OUTCOME_BY_NAME
        where = _rel(self._files, cand.media_dir)
        if not matches:
            self._report.unattributed.append(
                Finding(
                    where,
                    f"认不出博主：{cand.platform} 下库里没有名为 {cand.creator_name!r} 的博主，"
                    "目录里也没有带平台 ID 的 metadata.json",
                )
            )
        else:
            self._report.unattributed.append(
                Finding(
                    where,
                    f"认不出博主：{cand.platform} 下有 {len(matches)} 位同名博主 "
                    f"{cand.creator_name!r}，昵称不足以判定是哪一位",
                )
            )
        return None, OUTCOME_UNKNOWN


async def rescan(
    storage: SqliteStorage,
    files: FileStorage,
    scan: ScanResult,
    *,
    dry_run: bool = False,
    report: RescanReport | None = None,
) -> RescanReport:
    """把扫出来的候选逐个进库（`dry_run=True` 时只投影计数）。

    单条失败只记一条 `report.errors`，不炸整跑：一棵坏目录不该让剩下 200 棵不扫了，
    而"跑完但漏了三条"和"跑挂在一半"对用户的动作完全不同 —— 后者根本不知道还剩什么。
    """
    out = report or RescanReport(dry_run=dry_run)
    out.dry_run = dry_run
    out.entries_seen = scan.entries_seen
    out.recognized = len(scan.candidates)
    out.skipped = list(scan.skips)
    out.errors = list(scan.errors)
    index = _CreatorIndex(storage, files, out)
    for cand in scan.candidates:
        try:
            await _handle_one(storage, files, cand, index, out, dry_run=dry_run)
        except (StorageError, ValidationError) as exc:
            out.errors.append(f"{_rel(files, cand.media_dir)} 未入库：{type(exc).__name__}: {exc}")
    return out


async def _handle_one(
    storage: SqliteStorage,
    files: FileStorage,
    cand: Candidate,
    index: _CreatorIndex,
    report: RescanReport,
    *,
    dry_run: bool,
) -> None:
    """一棵目录。dry-run 与真跑**共用同一套识别与计数**，只在最后一步分岔：
    真跑走 `insert_or_get`（幂等查重在那里面），dry-run 走 `find_by_platform_id` 投影。
    """
    if cand.media_path is None:
        report.without_main_media += 1
    if cand.has_transcript:
        report.transcripts_on_disk += 1
    if cand.title_source == TITLE_FROM_ID:
        report.placeholder_titles += 1

    if dry_run:
        creator_id, outcome = await index.resolve(cand, dry_run=True)
        known = await storage.videos.find_by_platform_id(cand.platform, cand.platform_video_id)
        report.videos_existing += int(known is not None)
        report.videos_created += int(known is None)
        _tally_creator(report, outcome)
        return

    # 一条目录一个事务：博主行与作品行要么一起进去，要么一起没有
    # （半条 = 库里多出一个谁也不挂的博主，或一条 `creator_id` 指向没建成的行）。
    async with storage.transaction():
        await index.ensure_platform_row(cand.platform)
        creator_id, outcome = await index.resolve(cand, dry_run=False)
        _row, created = await storage.videos.insert_or_get(_video_draft(files, cand, creator_id))
    # 计数在事务**外面**：走到这一行说明提交成功了。放在里面时，后面的作品写入一炸、
    # 整笔回滚，报告就留下"新建了 1 位博主"而库里一行都没有 —— 那是最省的一次谎。
    _tally_creator(report, outcome)
    report.videos_existing += int(not created)
    report.videos_created += int(created)


def _tally_creator(report: RescanReport, outcome: str) -> None:
    """博主那一侧的四种走法各归一个计数；`认不出` 只体现在 `videos_without_creator` 上。"""
    if outcome == OUTCOME_CREATED:
        report.creators_created += 1
    elif outcome == OUTCOME_EXISTING:
        report.creators_existing += 1
    elif outcome == OUTCOME_BY_NAME:
        report.creators_matched_by_name += 1
    else:
        report.videos_without_creator += 1


# ---------------------------------------------------------------------------
# 报告打印
# ---------------------------------------------------------------------------


def print_report(report: RescanReport) -> None:
    mode = "DRY-RUN（库里一行都没写）" if report.dry_run else "已写入"
    print(f"== 磁盘重扫 · {mode} ==")
    print(
        f"  条目={report.entries_seen} 认出作品={report.recognized} "
        f"跳过={len(report.skipped)}（不变量：条目 = 认出 + 跳过）"
    )
    print(
        f"  作品 新建={report.videos_created} 已存在={report.videos_existing} | "
        f"博主 新建={report.creators_created} 已存在={report.creators_existing} "
        f"按昵称认出={report.creators_matched_by_name} 无归属={report.videos_without_creator}"
    )
    print(
        f"  标题是 ID 占位={report.placeholder_titles} "
        f"无主媒体指针={report.without_main_media} "
        f"目录里有口播稿={report.transcripts_on_disk}（稿子行不在本工具职责内，见模块 docstring）"
    )
    for note in report.notes:
        print(f"  ! {note}")
    # 跳过与无归属**全量打印、不截断**：`migrate_from_v1` 那处 `[:50]` 的截断
    # 让"静默少一半"有了藏身处，而这条工具的失败方式正是"少了一半没人发现"。
    for item in report.skipped:
        print(f"  - 跳过 {item.path}：{item.reason}")
    for item in report.unattributed:
        print(f"  - 无归属 {item.path}：{item.reason}")
    if report.creators_created:
        print(
            f"  · 新建的 {report.creators_created} 位博主 is_tracking=False（重扫不代你打开采集）"
        )
    if report.errors:
        print(f"  ⚠️ {len(report.errors)} 条错误：")
        for line in report.errors:
            print(f"    - {line}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="磁盘重扫：从 data/media/ 那棵树重建丢掉的作品/博主行（默认 dry-run）"
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="产物根目录，其下的 media/ 就是扫描对象；默认取配置 data.dir（相对仓库根）。"
        "验证用请显式指到 .scratch/ 下造出来的树，别指 live data/",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=Path("config"), help="配置目录（读 app.yaml）"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真写库。不给这个开关就是 dry-run：只扫只打印，不写库、不建目录、不建库文件",
    )
    parser.add_argument("--json", action="store_true", help="把整份报告打成 JSON 到 stdout")
    return parser.parse_args(argv)


def resolve_files(args: argparse.Namespace, config: AppConfig) -> FileStorage:
    """`--data-dir` 给了就用它，否则从配置算。**两条路都不在这里手拼 `media/`**。"""
    raw = str(args.data_dir or "").strip()
    if raw:
        return FileStorage(Path(raw).expanduser().resolve())
    # root=仓库根：`data.dir` 在配置里是相对路径，相对的是仓库而不是当前工作目录。
    return FileStorage.from_config(config, root=_REPO_ROOT)


def db_path_for(config: AppConfig, files: FileStorage) -> Path:
    """这一棵 `data/` 对应的主库在哪。

    只有一个算得出者：`storage.db.resolve_db_path()`（V1 §7.12「预检的那个库不是主库」
    的结构性消除）。`--data-dir` 的语义靠"把 `data.dir` 换成它再问同一个函数"实现，
    而不是在本文件里再拼一次 `<dir>/<sqlite_file>`。
    """
    pointed = config.model_copy(update={"data": config.data.model_copy(update={"dir": files.root})})
    return resolve_db_path(pointed)


async def _amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        report = await _rescan_from_args(args)
    # 一次性工具的 stdout 只放报告：`--json` 那一档要能直接 `| jq`，而 structlog
    # 没被配置过时默认往 **stdout** 打（`storage.initialized` 那一行就是这么混进来的）。
    # 不用 `setup_logging()` 治它：它会摘掉 root 上别人的 handler（含 pytest 的 caplog），
    # 一个跑完就退的脚本没资格改全局日志配置（见 `logging.setup_logging` 的 docstring）。
    noise = captured.getvalue()
    if noise:
        sys.stderr.write(noise)
    if args.json:
        print(report.to_json())
    else:
        print_report(report)
    return 1 if report.errors else 0


async def _rescan_from_args(args: argparse.Namespace) -> RescanReport:
    config = load_app_config(args.config_dir / "app.yaml")
    files = resolve_files(args, config)
    if not files.media_root.is_dir():
        msg = (
            f"媒体根目录不存在：{files.media_root}"
            "（--data-dir 指对了吗？没有这棵树就别报『扫到 0 条、完成』）"
        )
        raise SystemExit(msg)

    scan = scan_tree(files)
    report = RescanReport(dry_run=not args.apply)
    db = db_path_for(config, files)
    if not db.is_file() and not args.apply:
        # dry-run 且库还没有 → 用内存库比对，**不创建它**。否则打印"未写任何东西"是句假话：
        # `initialize()` 会跑 Alembic，盘上立刻多出 `intelligence_hub.sqlite3` + WAL。
        storage = SqliteStorage.in_memory()
        report.notes.append(
            f"目标库还不存在（{db}）：dry-run 不创建它，改用内存库比对，"
            "所以下面的『新建』计数是按空库算的"
        )
    else:
        if args.apply:
            # 真写才建骨架目录（dry-run 一个字节都不落盘）。
            files.ensure_dirs()
        storage = SqliteStorage(
            db, wal_mode=config.storage.wal_mode, busy_timeout_ms=config.storage.busy_timeout_ms
        )
    await storage.initialize()
    try:
        await rescan(storage, files, scan, dry_run=not args.apply, report=report)
    finally:
        await storage.close()
    return report


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover - 入口由 main() 的用例覆盖
    raise SystemExit(main())
