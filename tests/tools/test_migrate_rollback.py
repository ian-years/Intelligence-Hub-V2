"""`--rollback` 与 `--media-strategy`（计划 T6.2，ADR-0010 §行为契约 3 与 6 的欠账）。

V1 那一侧的形状**不在这儿重造** —— 直接从 `tests/integration/test_migrate_from_v1.py`
import 那套 builder，两边共用一份定义。否则"V1 长什么样"就有两个真源，
而迁移脚本的正确性完全取决于它像不像真的 V1（同 [[经验 44]] 那一族）。

这一文件里最值钱的是 `test_remigrating_after_rollback_actually_migrates`：
回滚只删数据行、留着 `.migration_state.json` 的话，症状不是报错而是**下一次迁移
静默搬 0 条** —— 看板看起来一切正常，数据却永远补不回来。
"""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest
from tests.integration.test_migrate_from_v1 import (
    V1_DOUYIN,
    _build_v1,
    _creator,
    _media,
    _video,
)

from intelligence_hub_v2.core.config import AppConfig, load_app_config
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage
from tools import migrate_from_v1 as mig


def _one_video_v1(root: Path) -> Path:
    """一位博主 + 一条有媒体的作品（`--media-strategy` 的四种走法都要它）。"""
    _media(root, "media/v1.mp4", b"v1-payload-bytes")
    return _build_v1(
        root,
        creators=[_creator(V1_DOUYIN, "sec1", "姜胡说")],
        videos=[
            _video(
                id="row1",
                platform=V1_DOUYIN,
                platform_video_id="pv1",
                creator_id="sec1",
                creator_name="姜胡说",
                video_title="怎么选题",
                video_path="media/v1.mp4",
                transcript_status="已转写",
                clean_transcript="第一句。第二句。第三句。",
            )
        ],
    )


def _v1_media(tmp_path: Path) -> Path:
    """V1 那份媒体文件在哪：`_media()` 一律落在 `downloads/` 下面（V1 的真实布局）。"""
    return tmp_path / "v1" / "downloads" / "media" / "v1.mp4"


def _v1_root(tmp_path: Path) -> Path:
    """V1 工作区**只建一次**：`_build_v1` 里是 `executescript(DDL)`，
    同一个目录建第二遍会 `table creators already exists`。"""
    root = tmp_path / "v1"
    if not (root / "downloads" / "local.sqlite3").is_file():
        _one_video_v1(root)
    return root


async def _migrate(
    tmp_path: Path, *, strategy: str = "hardlink"
) -> tuple[SqliteStorage, FileStorage, mig.MigrationReport]:
    files = FileStorage(tmp_path / "v2data")
    files.ensure_dirs()
    storage = SqliteStorage(files.root / "intelligence_hub.sqlite3")
    await storage.initialize()
    report = await mig.migrate(
        _v1_root(tmp_path), storage, files, dry_run=False, media_strategy=strategy
    )
    return storage, files, report


# --------------------------------------------------------------------------- #
# _is_migrated：判据只认布尔真值
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"migrated_from_v1": true}', True),
        ('{"a": 1, "migrated_from_v1": true}', True),
        ('{"migrated_from_v1": "true"}', False),  # 字符串不算
        ('{"migrated_from_v1": 1}', False),
        ('{"note": "migrated_from_v1"}', False),  # 值里提到这个词不算
        ('{"migrated_from_v1": false}', False),
        ("", False),
        ("不是 JSON", False),
        ("[1, 2]", False),
        (None, False),
    ],
)
def test_only_an_explicit_true_marks_a_migrated_row(payload: str | None, expected: bool) -> None:
    """回滚是按这个判据**删数据**的，判据松一格的后果不可逆。

    用 `metadata_json LIKE '%migrated_from_v1%'` 会把上面第三条样本也删掉 ——
    而那一行可能是人自己写的。
    """
    assert mig._is_migrated(payload) is expected


# --------------------------------------------------------------------------- #
# --media-strategy 四种走法
# --------------------------------------------------------------------------- #


async def test_hardlink_and_copy_differ_by_behavior_not_by_inode(tmp_path: Path) -> None:
    """区分 hardlink 与 copy 的**可靠**判据：改源文件，看目标跟不跟。

    比 `st_ino` 稳：Windows 上 inode 号与 `st_nlink` 的表现依赖文件系统与 Python 版本，
    而"是不是同一个 inode"这件事的**后果**就是共享数据块 —— 直接量后果。
    """
    storage, files, report = await _migrate(tmp_path, strategy="hardlink")
    try:
        assert report.media_linked == 1 and report.media_copied == 0
        dest = await _media_of(storage, files)
        src = _v1_media(tmp_path)
        src.write_bytes(b"CHANGED")
        assert dest.read_bytes() == b"CHANGED", "hardlink 没链上：目标是一份独立副本"
    finally:
        await storage.close()


async def test_copy_strategy_leaves_an_independent_file(tmp_path: Path) -> None:
    storage, files, report = await _migrate(tmp_path, strategy="copy")
    try:
        assert report.media_copied == 1 and report.media_linked == 0
        dest = await _media_of(storage, files)
        assert dest.read_bytes() == b"v1-payload-bytes"
        _v1_media(tmp_path).write_bytes(b"CHANGED")
        assert dest.read_bytes() == b"v1-payload-bytes", "copy 出来的文件不该跟着源变"
    finally:
        await storage.close()


async def test_reference_strategy_moves_no_bytes_and_stores_no_absolute_path(
    tmp_path: Path,
) -> None:
    """`reference` = 库里记 V1 绝对路径，**但 `media_path` 留 NULL**。

    那条列的契约是"相对 `data/`"（`files.abs()` 靠它），塞绝对路径等于把
    "换机器就废"写进库。V1 的位置记在 `metadata_json.v1_media_path`，
    代价要如实：这条作品在 V2 里没有可读媒体。
    """
    storage, files, report = await _migrate(tmp_path, strategy="reference")
    try:
        assert report.media_referenced == 1
        assert report.media_linked == 0 and report.media_copied == 0
        video = await storage.videos.get_or_raise(await _video_id(storage))
        assert video.media_path is None, "reference 不该给 media_path 填东西"
        meta = json.loads(video.metadata_json)
        assert meta["v1_media_path"] == str(_v1_media(tmp_path))  # V1 的原始绝对路径，原样记
        assert meta["migrated_from_v1"] is True
        assert list((files.root / "media").rglob("*.mp4")) == [], "reference 不该落任何文件"
    finally:
        await storage.close()


async def test_a_non_default_strategy_never_silently_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点名要 symlink，建不出来就**报错**，不许偷偷变成 copy（ADR-0010 只给默认策略降级权）。

    Windows 上建符号链接要特权，所以这条不是假想场景：真跑 `--media-strategy=symlink`
    的人本来就想省磁盘，悄悄换成 copy 会得到"磁盘翻倍而文件却是副本"的双重意外。
    """
    monkeypatch.setattr(
        Path, "symlink_to", lambda *a, **k: (_ for _ in ()).throw(OSError(131, "需要特权"))
    )
    storage, _files, report = await _migrate(tmp_path, strategy="symlink")
    try:
        assert report.media_symlinked == 0
        assert report.media_copied == 0 and report.media_downgraded == 0, "悄悄降级了"
        assert report.media_missing == 0  # 不是"源文件找不到"，是"建链失败"，两回事
        assert any("媒体搬运失败" in line and "需要特权" in line for line in report.errors)
        video = await storage.videos.get_or_raise(await _video_id(storage))
        assert video.media_path is None, "搬运失败还写路径 = 库里指着不存在的文件"
    finally:
        await storage.close()


async def test_the_default_hardlink_downgrade_is_a_cost_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨卷时 hardlink 退 copy 是**契约里写明的默认行为**：单独计数，不进 `errors`。

    把它记成 error 会让每次跨卷迁移都以退出码 1 收尾，而 1 的意思是"有东西没做成" ——
    真正的失败（比如上面那条 symlink）就会被淹没在噪声里。
    """

    def fake_link(src: Path, dest: Path) -> None:
        msg = "[WinError 135] 找不到有效的卷"
        raise OSError(msg)

    monkeypatch.setattr(mig, "os_link", fake_link)
    storage, files, report = await _migrate(tmp_path, strategy="hardlink")
    try:
        assert report.errors == [], "降级不是失败，不该占用错误清单"
        assert report.media_copied == 1 and report.media_downgraded == 1
        assert report.media_linked == 0
        dest = await _media_of(storage, files)
        assert dest.read_bytes() == b"v1-payload-bytes", "退 copy 之后文件得真的在"
    finally:
        await storage.close()


# --------------------------------------------------------------------------- #
# --rollback
# --------------------------------------------------------------------------- #


async def test_rollback_dry_run_lists_rows_and_touches_nothing(tmp_path: Path) -> None:
    storage, files, _first = await _migrate(tmp_path)
    try:
        report = await mig.rollback(storage, files, dry_run=True)
        assert report.rolled_back_videos == 1 and report.rolled_back_creators == 1
        assert any("douyin:pv1" in line for line in report.preview)
        assert await storage.videos.count() == 1, "dry-run 删了东西"
        assert await storage.creators.count() == 1
        assert (files.root / mig.STATE_FILENAME).is_file(), "dry-run 不该动状态文件"
    finally:
        await storage.close()


async def test_rollback_removes_migrated_rows_and_keeps_human_rows(tmp_path: Path) -> None:
    storage, files, _first = await _migrate(tmp_path)
    try:
        hand = await storage.creators.insert(
            CreatorDraft(
                platform="douyin",
                platform_id="human1",
                name="人手加的",
                profile_url="douyin://human1",
            )
        )
        await storage.videos.insert(
            VideoDraft(
                platform="douyin",
                platform_video_id="human-video",
                creator_id=hand.id,
                title="人手收的作品",
            )
        )
        media_abs = await _media_of(storage, files)
        assert media_abs.is_file()

        report = await mig.rollback(storage, files, dry_run=False)

        assert report.rolled_back_videos == 1 and report.rolled_back_creators == 1
        assert report.rolled_back_transcripts == 1, "transcripts 要随外键级联走"
        assert report.files_left_on_disk == 1, "留在盘上的媒体数要报出来（它没被删，这是有意的）"
        assert await storage.transcripts.count() == 0
        # 人手写的两行必须还在：判据是 metadata 里那个布尔，不是"这库里所有行"
        assert await storage.creators.count() == 1
        human = await storage.videos.find_by_platform_id("douyin", "human-video")
        assert human is not None and human.title == "人手收的作品"
        # 媒体文件一个都不删（hardlink 那侧还连着 V1；copy/symlink 删了不可逆）
        assert media_abs.is_file()
        assert not (files.root / mig.STATE_FILENAME).exists()
    finally:
        await storage.close()


async def test_remigrating_after_rollback_actually_migrates(tmp_path: Path) -> None:
    """**这一条是这次改动存在的理由**。

    状态文件 `.migration_state.json` 记的是"哪些 V1 行已经搬过"。回滚只删数据行而留着它，
    下一次迁移就会把整库当成已完成 —— 报告写着 `videos=0 skipped_existing=1`，
    退出码 0，看起来"没什么要搬的"，而库里其实是空的。
    """
    storage, files, first = await _migrate(tmp_path)
    try:
        assert first.videos == 1
        await mig.rollback(storage, files, dry_run=False)
        assert await storage.videos.count() == 0

        again = await mig.migrate(_v1_root(tmp_path), storage, files, dry_run=False, resume=True)
        assert again.videos == 1, f"回滚后重新迁移只搬了 {again.videos} 条 —— 状态文件没跟着回滚"
        assert again.skipped_existing == 0
    finally:
        await storage.close()


async def test_rollback_on_an_empty_library_reports_no_fabricated_success(
    tmp_path: Path,
) -> None:
    """库里一行都没有时，回滚交回的计数必须全是 0（不是 None、不是"成功删除 1 条"）。"""
    files = FileStorage(tmp_path / "v2data")
    files.ensure_dirs()
    storage = SqliteStorage(files.root / "intelligence_hub.sqlite3")
    await storage.initialize()
    try:
        report = await mig.rollback(storage, files, dry_run=False)
        assert (
            report.rolled_back_videos,
            report.rolled_back_creators,
            report.rolled_back_transcripts,
            report.files_left_on_disk,
        ) == (0, 0, 0, 0)
        assert report.errors == []
    finally:
        await storage.close()


# --------------------------------------------------------------------------- #
# CLI 那一层：`--v1-root` 与 `--rollback` 的必填关系、缺库时不建库
# --------------------------------------------------------------------------- #


def test_the_cli_requires_v1_root_only_when_not_rolling_back() -> None:
    """必填关系是 `_amain` 判的，不是 argparse：`--rollback` 合法地不需要 `--v1-root`，
    所以 argparse 那层不能写 required=True（会让回滚那条支路进不来）。"""
    parser = mig._build_parser()
    assert parser.parse_args(["--rollback", "--dry-run"]).v1_root is None
    assert parser.parse_args(["--v1-root", "x"]).media_strategy == "hardlink"
    with pytest.raises(SystemExit, match="--v1-root"):
        asyncio.run(mig._amain(["--dry-run"]))  # 两个都没有 → 红在业务入口，不静默


async def test_rollback_without_a_database_does_not_create_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """目标库不存在时"没东西可回滚"是真话，**顺手建一个空库**不是。"""
    files = FileStorage(tmp_path / "never")
    code = await mig._amain_rollback(
        _ns(rollback=True, dry_run=True), _config_pointing_at(files.root), files
    )
    assert code == 0
    assert not (files.root / "intelligence_hub.sqlite3").exists()
    assert "没有可回滚的东西" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _ns(**kw: object) -> Namespace:
    return Namespace(**kw)


def _config_pointing_at(root: Path) -> AppConfig:
    """把 `data.dir` 指到 tmp 的配置：**改模型而不是写一份 yaml**，
    这样测的就是 `resolve_db_path` 真实读的那条路。"""
    config = load_app_config()
    return config.model_copy(update={"data": config.data.model_copy(update={"dir": str(root)})})


async def _video_id(storage: SqliteStorage, pvid: str = "pv1", platform: str = "douyin") -> int:
    row = await storage.videos.find_by_platform_id(platform, pvid)
    return int(row.id)


async def _media_of(storage: SqliteStorage, files: FileStorage) -> Path:
    video = await storage.videos.get_or_raise(await _video_id(storage))
    assert video.media_path is not None
    return files.abs(video.media_path)
