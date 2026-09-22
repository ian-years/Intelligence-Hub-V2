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
3. **不许臆造成功**（V1 §1.3）。每一条映射失败都进 `report.errors` 带原文；
   dry-run 绝不写库、绝不落媒体。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 脚本入口：把仓库根（tools/ 的上一级）放进 sys.path，好 `import intelligence_hub_v2`。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.transcript import TranscriptDraft
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

V1_DB_RELPATH = Path("downloads") / "local.sqlite3"
V1_HIDDEN_RELPATH = Path("launcher-state") / "hidden-videos.json"
STATE_FILENAME = ".migration_state.json"


@dataclass
class MigrationReport:
    dry_run: bool
    creators: int = 0
    videos: int = 0
    transcripts: int = 0
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


def _metadata_with_provenance(vrow: dict[str, Any]) -> str:
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
    return json.dumps(payload, ensure_ascii=False)


async def _creator_pk(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = _table_columns(conn, "creators")
    rows = conn.execute("SELECT * FROM creators").fetchall()
    return [_row_to_dict(r, cols) for r in rows]


async def _videos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = _table_columns(conn, "videos")
    rows = conn.execute("SELECT * FROM videos").fetchall()
    return [_row_to_dict(r, cols) for r in rows]


def read_hidden_keys(v1_root: Path) -> set[str]:
    """V1 的墓碑文件 = 一组"身份值"（platform_video_id / url / id 都可能）。拿不到就空集。"""
    path = v1_root / V1_HIDDEN_RELPATH
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    items = data if isinstance(data, list) else data.get("keys") or data.get("videos") or []
    keys: set[str] = set()
    for item in items:
        if isinstance(item, str):
            keys.add(item)
        elif isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str) and value:
                    keys.add(value)
    return keys


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


async def migrate(
    v1_root: Path,
    storage: SqliteStorage,
    files: FileStorage,
    *,
    dry_run: bool = False,
    resume: bool = True,
    report: MigrationReport | None = None,
) -> MigrationReport:
    """把 V1 的一次性搬进 V2。幂等：已存在的 (platform, platform_id / platform_video_id) 跳过。"""
    report = report or MigrationReport(dry_run=dry_run)
    v1_db = v1_root / V1_DB_RELPATH
    conn = open_v1_readonly(v1_db)
    done = _load_state(files.root) if resume else set()
    platform_key_to_id: dict[tuple[str, str], int] = {}

    for crow in await _creator_pk(conn):
        platform = str(crow.get("platform") or "").strip()
        platform_id = str(crow.get("platform_id") or "").strip()
        if not platform or not platform_id:
            report.errors.append(f"creator 缺 platform/platform_id：{crow.get('id')}")
            continue
        draft = CreatorDraft(
            platform=platform,
            platform_id=platform_id,
            name=str(crow.get("name") or platform_id),
            avatar_url=(str(crow["avatar_url"]).strip() or None)
            if crow.get("avatar_url")
            else None,
            profile_url=str(
                crow.get("homepage_url") or crow.get("profile_url") or f"{platform}://{platform_id}"
            ),
            is_tracking=bool(crow.get("is_tracking", 1)),
            metadata_json=str(crow.get("metadata_json") or "{}"),
        )
        if dry_run:
            report.creators += 1
            continue
        created = await storage.creators.insert_or_get(draft)
        platform_key_to_id[(platform, platform_id)] = created[0].id
        report.creators += 1

    hidden_keys = read_hidden_keys(v1_root)

    for vrow in await _videos(conn):
        platform = str(vrow.get("platform") or "").strip()
        pvid = str(vrow.get("platform_video_id") or "").strip()
        if not platform or not pvid:
            report.errors.append(f"video 缺 platform/platform_video_id：{vrow.get('id')}")
            continue
        vkey = f"{platform}:{pvid}"
        if vkey in done:
            report.skipped_existing += 1
            continue

        creator_pid = str(
            vrow.get("creator_id") or vrow.get("mid") or vrow.get("creator_platform_id") or ""
        ).strip()
        creator_id = platform_key_to_id.get((platform, creator_pid))

        media_rel, _source = _stage_media(v1_root, files, vrow, report, dry_run)

        title = str(vrow.get("video_title") or vrow.get("title") or pvid)
        draft = VideoDraft(
            platform=platform,
            platform_video_id=pvid,
            creator_id=creator_id,
            title=title,
            duration_seconds=vrow.get("duration_seconds"),
            media_path=media_rel,
            # V1 没记这条媒体实际走的哪条路 → 照实留 None（枚举列不许塞 "migrated_from_v1"，
            # 那是编一个 DB CHECK 不认的取值）。迁移来源写进 metadata_json。
            media_source=None,
            metadata_json=_metadata_with_provenance(vrow),
        )
        if dry_run:
            report.videos += 1
            status = str(vrow.get("transcript_status") or "")
            if _clean_transcript_of(vrow) and status in {"已转写", "done", "transcribed"}:
                report.transcripts += 1
            continue

        video, created = await storage.videos.insert_or_get(draft)
        if not created:
            report.skipped_existing += 1
        else:
            report.videos += 1

        await _maybe_attach_transcript(storage, files, video.id, vrow, media_rel, report, dry_run)

        identities = {pvid, str(vrow.get("id") or ""), str(vrow.get("video_url") or "")}
        if identities & hidden_keys and not video.is_hidden:
            await storage.videos.hide(video.id, "迁移自 V1 墓碑（hidden-videos.json）")
            report.hidden += 1
        done.add(vkey)

    conn.close()
    if not dry_run:
        _save_state(files.root, done)
    return report


def _stage_media(
    v1_root: Path, files: FileStorage, vrow: dict[str, Any], report: MigrationReport, dry_run: bool
) -> tuple[str | None, Path | None]:
    source = _media_source_path(v1_root, str(vrow.get("video_path") or ""))
    if source is None:
        if str(vrow.get("video_path") or "").strip():
            report.media_missing += 1
        return None, None
    platform = str(vrow.get("platform") or "")
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
    media_rel: str | None,
    report: MigrationReport,
    dry_run: bool,
) -> None:
    text = _clean_transcript_of(vrow)
    status = str(vrow.get("transcript_status") or "")
    if not text or status not in {"已转写", "done", "transcribed"}:
        return
    if dry_run:
        report.transcripts += 1
        return
    if media_rel is None:
        # 没有媒体目录，稿子无处安（V2 稿子与媒体同住）→ 建一个以 video_id 命名的目录放它。
        media_dir = files.media_dir(
            str(vrow.get("platform") or ""),
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
    print(f"  creators={report.creators} videos={report.videos} transcripts={report.transcripts}")
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
