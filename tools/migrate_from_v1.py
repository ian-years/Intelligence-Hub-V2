"""V1 → V2 一次性数据迁移（只读 V1 SQLite，写进 V2 主库 + data/ 树）。

契约来源：`docs/specs/data-model.md §6`（字段映射）+ `AGENTS.md §3`（V1 工作区一行不动）。

**跑法**：

```bash
# 预演：只统计、只打印"会做什么"，一个字节都不写
uv run python -X utf8 tools/migrate_from_v1.py --v1-root "E:/08-Codework/Intelligence-Hub" --dry-run
# 真迁移（幂等；中断后据 data/.migration_state.json 续跑）
uv run python -X utf8 tools/migrate_from_v1.py --v1-root "E:/08-Codework/Intelligence-Hub"
```

三条硬规矩（V1 的经验直接搬过来）：
1. **V1 只读**。V1 的库以 `mode=ro` URI 打开，任何写都会 `OperationalError` —— 不是"我们
   记得不写"，是**写不了**。
2. **媒体 hardlink，跨卷退回 copy**（`docs/lessons.md` 经验里 V1→V2 那条）。同一 data/ 卷上
   hardlink 不占额外空间；跨盘 `os.link` 抛 `EXDEV` → `shutil.copy2`。找不到源文件不算失败，
   记 `media_missing` 并把视频行照写（V2 里"有作品行没媒体"是合法中间态）。
3. **不许臆造成功**（V1 §1.3）。每一条映射失败都进 `report.errors` 带原文，且**单行失败不炸整跑**；
   dry-run 连目标目录都不创建。

还有一条只在踩过之后才会写下来的：**V1 与 V2 说的不是同一种平台语言**。V1 的
`local_store.normalize_platform()` 存显示名（`抖音` / `B站` / `小红书` / `YouTube`），V2 的
`platforms.name` 存 slug（`douyin` / `bilibili` / …），而 `creators.platform` 是指向后者的外键。
原样搬 = 第一条真数据就 FK 失败。见 `V1_PLATFORM_TO_SLUG`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 脚本入口：把仓库根（tools/ 的上一级）放进 sys.path，好 `import intelligence_hub_v2`。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import ValidationError

from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.transcript import TranscriptDraft
from intelligence_hub_v2.models.video import Video, VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

V1_DB_RELPATH = Path("downloads") / "local.sqlite3"
# V1 的 `launcher_server.load_state_list()` 走 `root / "downloads" / "launcher-state" / name`
# （launcher_server.py:2346）。少一层 `downloads/` 就是读不到 → 静默空集 → 用户删过的作品整批复活。
V1_HIDDEN_RELPATH = Path("downloads") / "launcher-state" / "hidden-videos.json"
STATE_FILENAME = ".migration_state.json"

_LOCAL_RECORD_PREFIX = "local:"
_LOCAL_RECORD_PARTS = 3

#: V1 平台值 → V2 slug。左列逐字来自 V1 `local_store.py:60-73 normalize_platform()` 的出口；
#: 右列也作为左列收进来，是为了容忍手改过的 V1 库（幂等重跑时也会命中自己写过的值）。
V1_PLATFORM_TO_SLUG: Mapping[str, str] = {
    "抖音": "douyin",
    "B站": "bilibili",
    "小红书": "xiaohongshu",
    "YouTube": "youtube",
    "douyin": "douyin",
    "bilibili": "bilibili",
    "xiaohongshu": "xiaohongshu",
    "youtube": "youtube",
}


def to_v2_platform(raw: str) -> str | None:
    """V1 的平台值翻成 V2 slug；认不出来返回 None，由调用方如实记一条 —— 不替它猜一个平台。"""
    return V1_PLATFORM_TO_SLUG.get(raw.strip())


@dataclass
class MigrationReport:
    dry_run: bool
    creators: int = 0
    videos: int = 0
    transcripts: int = 0
    summaries: int = 0
    hidden: int = 0
    media_linked: int = 0
    media_copied: int = 0
    media_missing: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _row_to_dict(row: sqlite3.Row, cols: set[str]) -> dict[str, Any]:
    # `sqlite3.Row.keys()` 是取**列名**的正确访问器；SIM118（"用 `in dict` 别 `in dict.keys()`"）
    # 在这认错了对象 —— Row 不是 dict，去掉 .keys() 会迭代到**值**上去。
    return {k: row[k] for k in row.keys() if k in cols}  # noqa: SIM118


def open_v1_readonly(db_path: Path) -> sqlite3.Connection:
    """以**只读 URI** 打开 V1 库。写它会在 sqlite 层直接抛，不靠约定。"""
    if not db_path.is_file():
        msg = f"找不到 V1 库：{db_path}"
        raise FileNotFoundError(msg)
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _clean_transcript_of(video: dict[str, Any]) -> str:
    return (video.get("clean_transcript") or video.get("raw_transcript") or "").strip()


def _v1_text(video: dict[str, Any], key: str) -> str | None:
    """V1 的文本列：空串与空白归 None —— V2 用 NULL 表示"没做过"，空串是另一种"有值"。"""
    raw = str(video.get(key) or "").strip()
    return raw or None


def _reference_of(
    vrow: dict[str, Any], pvid: str, report: MigrationReport
) -> tuple[str | None, str | None]:
    """V1 的 `content_summary` / `key_points`，**前提是这条作品会换来一个 `transcripts` 行**。

    ADR-0015 把这两列放在 `transcripts` 上（它们是"一份稿子的派生字段"）。所以 V1 行
    有摘要却没有可搬的稿子时，它在 V2 里**没有落脚点** —— 这时必须响。
    实测本机 V1 库这种行是 0 条（13/21 有摘要，全部落在已转写的行上），但那条 0 是
    今天这一份库的抽样，不是契约：丢了要写进 `report.errors`，不能让"迁移完成"里
    悄悄含着一批永远不会出现在看板上的数据（V1 §1.3）。
    """
    summary = _v1_text(vrow, "content_summary")
    points = _v1_text(vrow, "key_points")
    if not (summary or points):
        return None, None
    if not _transcript_worth_moving(vrow):
        report.errors.append(
            f"video {pvid} 带着 V1 摘要/要点，却没有可搬的稿子"
            "（这两列在 V2 挂在 transcripts 上，见 ADR-0015）—— 原文已丢弃，需要就去 V1 库手抄"
        )
        return None, None
    report.summaries += 1
    return summary, points


def _metadata_with_provenance(vrow: dict[str, Any], *, extra: dict[str, Any] | None = None) -> str:
    """把 V1 的 raw_data_json 原样留着，再打一个"来自 V1 迁移"的标记。

    不动 `videos.media_source`（那是有 CHECK 枚举的列），provenance 走自由格式的 metadata_json。
    """
    raw = str(vrow.get("raw_data_json") or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {"v1_raw_data": payload}
    except ValueError:
        payload = {"v1_raw_data_unparsed": raw[:500]}
    payload["migrated_from_v1"] = True
    payload.update(extra or {})
    return json.dumps(payload, ensure_ascii=False)


#: V1 把四个计数塞在一个 `metrics_json` 文本列里；V2 是四个整型列。
_V1_COUNT_FIELDS = ("view_count", "like_count", "comment_count", "share_count")


def _metrics_of(vrow: dict[str, Any]) -> tuple[dict[str, int], str | None]:
    """`metrics_json` → 四个计数列。返回 (取值, 解析不出来的原文)。

    认不出的形状不猜 0：原文交给调用方记进 metadata 与 errors，数据本身不丢。
    """
    raw = str(vrow.get("metrics_json") or "").strip()
    if not raw or raw == "{}":
        return {}, None
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}, raw[:500]
    if not isinstance(payload, dict):
        return {}, raw[:500]
    counts = {
        name: payload[name] for name in _V1_COUNT_FIELDS if isinstance(payload.get(name), int)
    }
    return counts, None


def _parse_published_at(raw: str) -> tuple[datetime | None, bool]:
    """V1 的时间列是 `datetime.now().strftime("%Y-%m-%d %H:%M:%S")` —— 无时区的本机时间。

    按**本机时区**解读成 aware datetime，并把"这是推断出来的"随数据一起记下来：
    留 NULL 会让迁移来的整批行在 Feed 排序里沉底且看不出异常，
    而直接当成 UTC 则是凭空指定一个偏移（实测本机 +08:00，差 8 小时）。
    返回 (值, 是否按本机时区推断)；解析不出来返回 (None, False)，由调用方记账。
    """
    text = raw.strip()
    if not text:
        return None, False
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, False
    if parsed.tzinfo is not None:
        return parsed, False
    return parsed.astimezone(), True


async def _ensure_platform_row(storage: SqliteStorage, slug: str, seeded: set[str]) -> None:
    """补 `platforms` 镜像行 —— `creators.platform` / `videos.platform` 是指向它的外键。

    这张镜像平时由 FastAPI 的 lifespan 灌（`main._sync_platform_mirror`），而脚本不走 lifespan；
    不补就是第一条真数据 FK 失败。已有行原样不动（用户可能已在配置里打开了该平台）。

    `enabled=False`：开关的权威源是 `platforms.yaml`，**迁移不许顺手打开任何平台**。
    """
    if slug in seeded:
        return
    if await storage.platforms.get(slug) is None:
        await storage.platforms.upsert(slug, enabled=False)
    seeded.add(slug)


async def _creator_pk(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = _table_columns(conn, "creators")
    rows = conn.execute("SELECT * FROM creators").fetchall()
    return [_row_to_dict(r, cols) for r in rows]


async def _videos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = _table_columns(conn, "videos")
    rows = conn.execute("SELECT * FROM videos").fetchall()
    return [_row_to_dict(r, cols) for r in rows]


def read_hidden_keys(v1_root: Path) -> tuple[set[str], list[str]]:
    """V1 的删除名单，返回 (身份值集合, 错误行)。

    取键口径逐字照 V1 `launcher_server.py:1548-1558 hidden_video_keys()`：只认
    `platform_video_id` 与 `record_id`（外加 `local:<平台>:<vid>` 的尾段）。
    条目里那个 `id` 是 `secrets.token_hex(6)` 随机串，**不是**作品身份 —— V1 自己注释过：
    把它收进来，任何一条 record_id 撞上它都会静默隐藏一条活得好好的作品。

    文件不存在 → 空集且不算错误（V1 对缺失同样返回空列表，语义就是"没人删过作品"）。
    文件存在但读不出来 → **必须响**：此时无法知道用户删过什么，静默返回空集等于
    把"删除复活"伪装成"从来没删过"。
    """
    path = v1_root / V1_HIDDEN_RELPATH
    if not path.is_file():
        return set(), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return set(), [f"读不出 V1 墓碑名单 {path}：{type(exc).__name__}: {exc}"]
    items = data if isinstance(data, list) else []
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        vid = str(item.get("platform_video_id") or "").strip()
        if vid:
            keys.add(vid)
        rid = str(item.get("record_id") or "").strip()
        if not rid:
            continue
        keys.add(rid)
        # V1 本地行的 record_id 是 `local:<平台>:<作品 ID>`（launcher_server.py:1866）。
        # split 上限 2：平台名理论上可能带冒号，尾段要整块拿。
        parts = rid.split(":", 2)
        if rid.startswith(_LOCAL_RECORD_PREFIX) and len(parts) == _LOCAL_RECORD_PARTS:
            keys.add(parts[2])
    return keys, []


def _media_source_path(v1_root: Path, video_path: str) -> Path | None:
    """V1 记的 video_path 可能是绝对、也可能相对 V1 的 downloads/data 目录。都试一遍。"""
    if not video_path:
        return None
    candidate = Path(video_path)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    for base in (v1_root, v1_root / "downloads", v1_root / "data"):
        probe = base / video_path
        if probe.is_file():
            return probe
    return None


def _link_or_copy(src: Path, dest: Path, report: MigrationReport) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os_link(src, dest)
        report.media_linked += 1
    except OSError:
        # 跨卷（EXDEV）或 V1 与 V2 不在同一文件系统：退回复制，代价是磁盘翻倍，如实记。
        shutil.copy2(src, dest)
        report.media_copied += 1


def os_link(src: Path, dest: Path) -> None:
    """hardlink；已存在同内容就当成功（幂等重跑）。用 `os.link`（`Path.link_to` 是本机
    Python 版本没有的方法，跨卷时它抛 `EXDEV` → 调用方退回 copy）。"""
    if dest.exists():
        return
    os.link(src, dest)


def _load_state(v2_data: Path) -> set[str]:
    path = v2_data / STATE_FILENAME
    if not path.is_file():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("done_videos", []))
    except (OSError, ValueError):
        return set()


def _save_state(v2_data: Path, done: set[str]) -> None:
    (v2_data / STATE_FILENAME).write_text(
        json.dumps({"done_videos": sorted(done)}, ensure_ascii=False), encoding="utf-8"
    )


def _creator_draft(crow: dict[str, Any], report: MigrationReport) -> CreatorDraft | None:
    """V1 `creators` 行 → `CreatorDraft`；认不出的平台记一条错误并返回 None（不猜平台）。"""
    raw_platform = str(crow.get("platform") or "").strip()
    platform_id = str(crow.get("platform_id") or "").strip()
    slug = to_v2_platform(raw_platform)
    if slug is None:
        report.errors.append(
            f"creator {crow.get('id')} 未迁入：V1 平台 {raw_platform!r} 在 V2 没有对应 slug"
        )
        return None
    if not platform_id:
        report.errors.append(f"creator {crow.get('id')} 未迁入：缺 platform/platform_id")
        return None
    return CreatorDraft(
        platform=slug,
        platform_id=platform_id,
        name=str(crow.get("name") or platform_id),
        avatar_url=(str(crow["avatar_url"]).strip() or None) if crow.get("avatar_url") else None,
        profile_url=str(
            crow.get("homepage_url") or crow.get("profile_url") or f"{slug}://{platform_id}"
        ),
        is_tracking=bool(crow.get("is_tracking", 1)),
        metadata_json=str(crow.get("metadata_json") or "{}"),
    )


def _video_draft(
    vrow: dict[str, Any],
    platform: str,
    creator_id: int | None,
    media_rel: str | None,
    report: MigrationReport,
) -> VideoDraft:
    """V1 `videos` 行 → `VideoDraft`。V1 把发布时间与四个计数存在文本列里，这里拆开映射。"""
    pvid = str(vrow.get("platform_video_id") or "")
    published_raw = str(vrow.get("published_at") or "").strip()
    published_at, assumed_local = _parse_published_at(published_raw)
    metrics, metrics_unparsed = _metrics_of(vrow)
    if published_raw and published_at is None:
        report.errors.append(f"video {pvid} 的 published_at 解析不出来，留空：{published_raw!r}")
    if metrics_unparsed:
        report.errors.append(f"video {pvid} 的 metrics_json 解析不出来，原文留在 metadata_json")
    extra: dict[str, Any] = {}
    if assumed_local:
        extra["published_at_assumed_tz"] = "local"
    if metrics_unparsed:
        extra["v1_metrics_json"] = metrics_unparsed
    return VideoDraft(
        platform=platform,
        platform_video_id=pvid,
        creator_id=creator_id,
        title=str(vrow.get("video_title") or vrow.get("title") or pvid),
        duration_seconds=vrow.get("duration_seconds"),
        published_at=published_at,
        media_path=media_rel,
        # V1 没记这条媒体走的哪条路 → 照实留 None（枚举列不许塞 "migrated_from_v1"，
        # 那是编一个 DB CHECK 不认的取值）。迁移来源写进 metadata_json。
        media_source=None,
        metadata_json=_metadata_with_provenance(vrow, extra=extra),
        # 逐个显式传，不用 `**metrics`：字典展开会让 mypy 放弃校验这个构造调用的其余参数
        # （它曾因此放过一个真正的类型错）。
        view_count=metrics.get("view_count"),
        like_count=metrics.get("like_count"),
        comment_count=metrics.get("comment_count"),
        share_count=metrics.get("share_count"),
    )


def _tombstone_hit(vrow: dict[str, Any], pvid: str, hidden_keys: set[str]) -> bool:
    """这条 V1 作品在不在删除名单上。只比 `platform_video_id` / V1 行 id / video_url
    三个值 —— 与 `read_hidden_keys` 的取键口径成一对，两边都不塞"任何字符串"。"""
    identities = {pvid, str(vrow.get("id") or ""), str(vrow.get("video_url") or "")}
    return bool(identities & hidden_keys)


async def _apply_tombstone(
    storage: SqliteStorage,
    video: Video,
    vrow: dict[str, Any],
    pvid: str,
    hidden_keys: set[str],
    report: MigrationReport,
) -> None:
    """命中 V1 墓碑 → 内化成 `is_hidden`，不再另存一份名单（V1 §7.25 的"墓碑散落"）。"""
    if _tombstone_hit(vrow, pvid, hidden_keys) and not video.is_hidden:
        await storage.videos.hide(video.id, "迁移自 V1 墓碑（hidden-videos.json）")
        report.hidden += 1


def _video_target(vrow: dict[str, Any], report: MigrationReport) -> tuple[str, str] | None:
    """(slug, platform_video_id)；平台认不出就记一条并返回 None（与 `_creator_draft` 同一口径）。

    认不出的平台**不写行、也不替它建 platforms 镜像行** —— 落一个 V2 没有的平台名，
    等于给下次启动的 `prune_unknown` 埋雷（那行会被清，而引用它的行清不掉）。
    """
    raw_platform = str(vrow.get("platform") or "").strip()
    slug = to_v2_platform(raw_platform)
    if slug is None:
        report.errors.append(
            f"video {vrow.get('id')} 未迁入：V1 平台 {raw_platform!r} 在 V2 没有对应 slug"
        )
        return None
    pvid = str(vrow.get("platform_video_id") or "").strip()
    if not pvid:
        report.errors.append(f"video {vrow.get('id')} 未迁入：缺 platform_video_id")
        return None
    return slug, pvid


async def migrate(
    v1_root: Path,
    storage: SqliteStorage,
    files: FileStorage,
    *,
    dry_run: bool = False,
    resume: bool = True,
    report: MigrationReport | None = None,
) -> MigrationReport:
    """把 V1 的一次性搬进 V2。幂等：已存在的 (platform, platform_id / platform_video_id) 跳过。

    单行失败只记一条 `report.errors`，不炸整跑 —— 一次跑不完比跑错一半便宜得多，
    而状态文件让下一次接着跑。
    """
    report = report or MigrationReport(dry_run=dry_run)
    v1_db = v1_root / V1_DB_RELPATH
    conn = open_v1_readonly(v1_db)
    done = _load_state(files.root) if resume else set()
    hidden_keys, hidden_errors = read_hidden_keys(v1_root)
    report.errors.extend(hidden_errors)
    platform_key_to_id: dict[tuple[str, str], int] = {}
    seeded: set[str] = set()

    for crow in await _creator_pk(conn):
        draft = _creator_draft(crow, report)
        if draft is None:
            continue
        if dry_run:
            report.creators += 1
            continue
        try:
            await _ensure_platform_row(storage, draft.platform, seeded)
            created = await storage.creators.insert_or_get(draft)
        except (StorageError, ValidationError) as exc:
            report.errors.append(f"creator {crow.get('id')} 未迁入：{type(exc).__name__}: {exc}")
            continue
        platform_key_to_id[(draft.platform, draft.platform_id)] = created[0].id
        report.creators += 1

    for vrow in await _videos(conn):
        target = _video_target(vrow, report)
        if target is None:
            continue
        platform, pvid = target
        vkey = f"{platform}:{pvid}"
        if vkey in done:
            report.skipped_existing += 1
            continue

        creator_pid = str(
            vrow.get("creator_id") or vrow.get("mid") or vrow.get("creator_platform_id") or ""
        ).strip()
        creator_id = platform_key_to_id.get((platform, creator_pid))

        media_rel, _source = _stage_media(v1_root, files, vrow, platform, report, dry_run)
        video_draft = _video_draft(vrow, platform, creator_id, media_rel, report)
        if dry_run:
            _project_dry_run(vrow, pvid, hidden_keys, report)
            continue

        try:
            await _ensure_platform_row(storage, platform, seeded)
            video, is_new = await storage.videos.insert_or_get(video_draft)
        except (StorageError, ValidationError) as exc:
            report.errors.append(f"video {vkey} 未迁入：{type(exc).__name__}: {exc}")
            continue
        if not is_new:
            report.skipped_existing += 1
        else:
            report.videos += 1

        await _maybe_attach_transcript(storage, files, video.id, vrow, platform, media_rel, report)

        await _apply_tombstone(storage, video, vrow, pvid, hidden_keys, report)
        done.add(vkey)

    conn.close()
    if not dry_run:
        _save_state(files.root, done)
    return report


#: V1 的 `transcript_status` 真实取值里只有这一个表示"稿子是完整的"。
#: （实测本机 V1 库：已转写 16 条全部带稿子，待转写 5 条全部没有。）
_V1_TRANSCRIBED = "已转写"


def _transcript_worth_moving(vrow: dict[str, Any]) -> bool:
    return bool(_clean_transcript_of(vrow)) and str(vrow.get("transcript_status") or "") == (
        _V1_TRANSCRIBED
    )


def _project_dry_run(
    vrow: dict[str, Any], pvid: str, hidden_keys: set[str], report: MigrationReport
) -> None:
    """预演只记账、不写库。三项都要投影：漏报"会隐藏 2 条"，看计划的人就以为
    这次跑不会碰到任何被删过的作品。"""
    report.videos += 1
    _reference_of(vrow, pvid, report)  # 预演就要报出"哪些摘要会丢"，不是到真跑才发现
    if _transcript_worth_moving(vrow):
        report.transcripts += 1
    if _tombstone_hit(vrow, pvid, hidden_keys):
        report.hidden += 1


def _stage_media(
    v1_root: Path,
    files: FileStorage,
    vrow: dict[str, Any],
    platform: str,
    report: MigrationReport,
    dry_run: bool,
) -> tuple[str | None, Path | None]:
    """把 V1 的媒体文件挂进 V2 的 data/ 树。`platform` 是**已翻译好的 slug**（目录名要与
    V2 自己采集出来的路径同构，不能再拿 V1 的显示名去拼一层）。"""
    source = _media_source_path(v1_root, str(vrow.get("video_path") or ""))
    if source is None:
        if str(vrow.get("video_path") or "").strip():
            report.media_missing += 1
        return None, None
    pvid = str(vrow.get("platform_video_id") or "")
    creator_name = str(vrow.get("creator_name") or "unknown")
    title = str(vrow.get("video_title") or pvid)
    dest_dir = files.media_dir(platform, creator_name, pvid, title)
    dest = dest_dir / ("media" + source.suffix or ".mp4")
    if dry_run:
        report.media_linked += 1
        return files.rel(dest), source
    try:
        _link_or_copy(source, dest, report)
    except OSError as exc:
        report.errors.append(f"媒体搬运失败 {source} → {dest}: {type(exc).__name__}: {exc}")
        return None, source
    return files.rel(dest), source


async def _maybe_attach_transcript(
    storage: SqliteStorage,
    files: FileStorage,
    video_id: int,
    vrow: dict[str, Any],
    platform: str,
    media_rel: str | None,
    report: MigrationReport,
) -> None:
    """V1 的稿子是内联文本列；V2 是 `transcripts` 表 + 磁盘文件。dry-run 走不到这里。"""
    text = _clean_transcript_of(vrow)
    # 先问摘要（它自己判"这条有没有稿子可搬"，放不下时已经把那条响记进 report），
    # 再决定要不要往下走 —— 反过来写就会在"没有稿子"那一支上悄悄跳过整次记账。
    summary, points = _reference_of(vrow, str(vrow.get("platform_video_id") or ""), report)
    if not _transcript_worth_moving(vrow):
        return
    if media_rel is None:
        # 没有媒体目录，稿子无处安（V2 稿子与媒体同住）→ 另建一个目录放它。
        media_dir = files.media_dir(
            platform,
            "unknown",
            str(vrow.get("platform_video_id") or ""),
            str(vrow.get("video_title") or "video"),
        )
    else:
        media_dir = files.abs(media_rel).parent
    path = files.transcript_path(media_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    await storage.transcripts.attach(
        video_id,
        TranscriptDraft(
            # transcripts.engine 是有 CHECK 枚举的列，V1 没记稿子出自哪个引擎 → 归 manual
            # （"人工/来源不明"），不塞一个枚举不认的 "migrated_from_v1"。
            engine="manual",
            char_count=len(text),
            sentence_count=text.count("。") + text.count("！") + text.count("？") or 1,
            text_path=files.rel(path),
            # **正文原样搬**：V1 那份摘要最长实测 2982 字，不截到 V2 抽取式的 600 字上限，
            # 也不改写 —— 迁移不许编辑数据。来源如实写 v1-imported（V1 侧四个写入口都查不清
            # 是模型整理还是原稿片段，那就不能说它是 local-extractive）。
            content_summary=summary,
            key_points=points,
            summary_method="v1-imported" if (summary or points) else None,
        ),
    )
    report.transcripts += 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V1 → V2 数据迁移（只读 V1）")
    parser.add_argument("--v1-root", type=Path, required=True, help="V1 工作区根目录")
    parser.add_argument("--config-dir", type=Path, default=Path("config"), help="V2 配置目录")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写")
    parser.add_argument(
        "--no-resume", action="store_true", help="忽略 .migration_state.json 全量重跑"
    )
    return parser


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    manager = ConfigManager(args.config_dir)
    config = manager.load()
    files = FileStorage.from_config(config)
    if args.dry_run:
        # 预演**不碰目标树**：`ensure_dirs()` 会建出五个目录，`from_config().initialize()`
        # 会建库并跑 Alembic —— 那之前"dry-run 未写任何东西"是句假话（打印它也一样假）。
        # dry_run 路径下 `migrate()` 一次都不碰 storage，所以内存库足够。
        storage = SqliteStorage.in_memory()
    else:
        files.ensure_dirs()
        storage = SqliteStorage.from_config(config)
    await storage.initialize()
    try:
        report = await migrate(
            args.v1_root, storage, files, dry_run=args.dry_run, resume=not args.no_resume
        )
    finally:
        await storage.close()
    _print_report(report)
    return 1 if report.errors else 0


def _print_report(report: MigrationReport) -> None:
    mode = "DRY-RUN（未写任何东西）" if report.dry_run else "已写入"
    print(f"== V1→V2 迁移 · {mode} ==")
    print(
        f"  creators={report.creators} videos={report.videos} "
        f"transcripts={report.transcripts} 带摘要={report.summaries}"
    )
    print(
        f"  hidden墓碑={report.hidden} 媒体 link={report.media_linked} copy={report.media_copied} "
        f"missing={report.media_missing} 已存在跳过={report.skipped_existing}"
    )
    if report.errors:
        print(f"  ⚠️ {len(report.errors)} 条错误：")
        for line in report.errors[:50]:
            print(f"    - {line}")


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
