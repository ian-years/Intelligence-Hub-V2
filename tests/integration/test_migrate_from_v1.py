"""`tools/migrate_from_v1.py`：对**造出来的 V1 库**跑 dry-run 与真迁移。

这台机器上没有真实 V1 数据（V1 的 `data/` 是 gitignore 的），所以按 `local_store.py` 的真实
schema 造一份 V1 SQLite + 媒体文件 + `hidden-videos.json`，验证迁移的行为：
只读 V1、幂等、墓碑内化、transcript 落盘、媒体 hardlink、dry-run 一个字节都不写。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tools.migrate_from_v1 import (
    V1_DB_RELPATH,
    V1_HIDDEN_RELPATH,
    migrate,
    open_v1_readonly,
)

pytestmark = pytest.mark.integration


def _build_v1(root: Path) -> Path:
    """按 V1 `local_store.py` 的 creators / videos 真实列造一份库 + 一份媒体 + 一份墓碑。"""
    db = root / V1_DB_RELPATH
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE creators (
            id TEXT PRIMARY KEY, platform TEXT NOT NULL, platform_id TEXT NOT NULL,
            name TEXT, homepage_url TEXT, cross_platform_identity TEXT, collection_strategy TEXT,
            is_tracking INTEGER, avatar_url TEXT, metadata_json TEXT,
            created_at TEXT, updated_at TEXT,
            UNIQUE(platform, platform_id)
        );
        CREATE TABLE videos (
            id TEXT PRIMARY KEY, platform TEXT NOT NULL, platform_video_id TEXT NOT NULL,
            creator_id TEXT, creator_name TEXT, video_title TEXT, video_url TEXT, video_path TEXT,
            metadata_path TEXT, cover_url TEXT, duration_seconds REAL, published_at TEXT,
            downloaded_at TEXT, video_download_status TEXT, transcript_status TEXT,
            content_summary TEXT, key_points TEXT, user_pain_points TEXT, expandable_topics TEXT,
            representative_comments TEXT, raw_transcript TEXT, clean_transcript TEXT,
            metrics_json TEXT, raw_data_json TEXT, created_at TEXT, updated_at TEXT,
            UNIQUE(platform, platform_video_id)
        );
        """
    )
    now = "2026-01-01 00:00:00"
    conn.execute(
        "INSERT INTO creators (id,platform,platform_id,name,homepage_url,is_tracking,metadata_json,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "creator:douyin:sec1",
            "douyin",
            "sec1",
            "姜胡说",
            "https://douyin.com/user/sec1",
            1,
            "{}",
            now,
            now,
        ),
    )
    # 有媒体 + 已转写的作品
    (db.parent / "media" / "v1.mp4").parent.mkdir(parents=True, exist_ok=True)
    (db.parent / "media" / "v1.mp4").write_bytes(b"fake-video-bytes")
    conn.execute(
        "INSERT INTO videos (id,platform,platform_video_id,creator_id,creator_name,video_title,"
        "video_url,video_path,duration_seconds,transcript_status,clean_transcript,created_at,"
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "v1id",
            "douyin",
            "pv1",
            "sec1",
            "姜胡说",
            "怎么选题",
            "https://douyin.com/video/pv1",
            "media/v1.mp4",
            42.5,
            "已转写",
            "第一句。第二句。第三句。",
            now,
            now,
        ),
    )
    # 会被墓碑命中的作品（无媒体、未转写）
    conn.execute(
        "INSERT INTO videos (id,platform,platform_video_id,creator_id,video_title,"
        "transcript_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("v2id", "douyin", "pv2", "sec1", "要删掉的", "待转写", now, now),
    )
    conn.commit()
    conn.close()

    (root / V1_HIDDEN_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (root / V1_HIDDEN_RELPATH).write_text(json.dumps(["pv2", "v2id"]), encoding="utf-8")
    return root


async def test_dry_run_writes_nothing(tmp_path, storage, files) -> None:
    v1 = _build_v1(tmp_path / "v1")

    report = await migrate(v1, storage, files, dry_run=True)

    assert report.creators == 1
    assert report.videos == 2
    assert report.transcripts == 1  # pv1 有 clean_transcript
    # 一个字节都没落到 V2
    assert await storage.creators.count() == 0
    assert await storage.videos.count() == 0


async def test_real_migration_moves_everything(tmp_path, storage, files) -> None:
    v1 = _build_v1(tmp_path / "v1")

    report = await migrate(v1, storage, files, dry_run=False)

    assert (await storage.creators.find("douyin", "sec1")).name == "姜胡说"

    pv1 = await storage.videos.find_by_platform_id("douyin", "pv1")
    assert pv1 is not None
    # 迁移来源记在 metadata_json（media_source 是有 CHECK 枚举的列，V1 没记 → None）
    assert pv1.media_source is None
    assert "migrated_from_v1" in pv1.metadata_json
    # 媒体落到了 V2 的 data 树下（hardlink 或 copy 都算），库里存的是相对 posix 路径
    assert pv1.media_path and not Path(pv1.media_path).is_absolute()
    assert files.abs(pv1.media_path).is_file()

    # transcript 落盘 + 入库
    rec = await storage.transcripts.get_for_video(pv1.id)
    assert rec is not None and rec.engine == "manual"  # 有 CHECK 枚举，迁移稿归 manual
    assert files.abs(rec.text_path).read_text(encoding="utf-8").startswith("第一句")

    # 墓碑内化成 is_hidden
    pv2 = await storage.videos.find_by_platform_id("douyin", "pv2")
    assert pv2 is not None and pv2.is_hidden is True
    assert report.hidden == 1
    assert report.media_linked + report.media_copied == 1  # 只搬了 pv1 的媒体
    assert (files.root / ".migration_state.json").is_file()


async def test_second_run_is_idempotent(tmp_path, storage, files) -> None:
    v1 = _build_v1(tmp_path / "v1")

    first = await migrate(v1, storage, files, dry_run=False)
    second = await migrate(v1, storage, files, dry_run=False)

    assert first.videos == 2
    assert second.videos == 0  # 两条都在 done 状态里，直接跳过
    assert second.skipped_existing == 2
    assert await storage.videos.count() == 2


async def test_v1_handle_is_genuinely_read_only(tmp_path) -> None:
    v1 = _build_v1(tmp_path / "v1")
    conn = open_v1_readonly(v1 / V1_DB_RELPATH)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM videos")  # 只读 URI：写会当场抛，不靠约定
    finally:
        conn.close()
