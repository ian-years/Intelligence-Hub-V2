"""`tools/rescan_local.py`：从 `data/media/` 那棵树把丢掉的作品/博主行重建回来。

信条（本文件通篇）：**断言写成关系，不写成样本**。

- 「盘上有几棵作品目录」由本文件自己 `glob` 数一遍，再与库里的行数比相等 ——
  不是"我断言 5 条"（那样改坏实现只要恰好还产出 5 条就仍然绿）。
- 幂等写成"跑两遍，第二遍 created=0 且行数一字不差"。
- 跳过写成"每一条被跳过的条目都必须带原因原文出现在报告里"，并且**目录原地不动**。
- dry-run 写成"跑完数一遍库里的行数，与跑前相同"，外加"库文件根本没被创建"。

与 `tests/integration/test_migrate_from_v1.py` 同一判据：**本文件的 storage fixture
故意不预置 `platforms` 镜像行**（`tests/conftest.py` 那个会预置四家且 `enabled=True`）。
预置等于替脚本把外键前置条件做完，脚本自己补不补、补成 `enabled` 是 True 还是 False
就永远测不到 —— 而那是这个工具唯一一处会碰到"平台开关"的地方。

媒体目录一律用 `FileStorage.media_dir()` 造，不在测试里手拼名字：
测试树与被测实现共用同一个路径真源，`FileStorage` 改了约定这里跟着动，
不会出现"测试自己那套拼法绿了、实现却按另一套读"（§7.11 那一族）。
"""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.core.config import AppConfig, DataSection
from intelligence_hub_v2.models.creator import Creator, CreatorDraft
from intelligence_hub_v2.models.video import Video
from intelligence_hub_v2.platforms import supported_platforms
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage
from tools import migrate_from_v1 as mig
from tools import rescan_local as tool

CREATOR = "姜胡说"
NO_CREATOR = "无身份博主"
DB_NAME = "intelligence_hub.sqlite3"

# --------------------------------------------------------------------------- #
# 造树 / 数树
# --------------------------------------------------------------------------- #


def _leaf(
    files: FileStorage,
    platform: str,
    creator: str,
    video_id: str,
    title: str | None = None,
    *,
    meta: dict[str, Any] | None = None,
    media: tuple[str, ...] = ("media.mp4",),
    with_cover: bool = False,
    with_transcript: bool = False,
    raw_metadata: str | None = None,
) -> Path:
    """按 V2 的目录约定（`data-model.md §1`）造一棵作品目录，返回它的路径。"""
    directory = files.media_dir(platform, creator, video_id, title or video_id)
    directory.mkdir(parents=True, exist_ok=True)
    for name in media:
        (directory / name).write_bytes(b"payload")
    if with_cover:
        (directory / "cover.jpg").write_bytes(b"jpeg")
    if meta is not None:
        (directory / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    if raw_metadata is not None:
        (directory / "metadata.json").write_text(raw_metadata, encoding="utf-8")
    if with_transcript:
        transcript = files.transcript_path(directory)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("第一句。第二句。", encoding="utf-8")
    return directory


def _dirs_on_disk(files: FileStorage) -> list[Path]:
    """`<media>/<平台>/<博主>/<作品>/` 这一级的**独立**计数（不借被测实现的眼睛）。"""
    return sorted(p for p in files.media_root.glob("*/*/*") if p.is_dir())


def _identity_of(directory: Path) -> tuple[str, str]:
    """(平台, 作品 ID) —— 按 spec §1 的层级从路径尾巴上数，不借实现的眼睛。"""
    parts = directory.parts
    return parts[-3], parts[-1].partition("-")[0]


def _db_counts(db: Path) -> tuple[int, int]:
    """直接开一条 SQLite 连接数行数：**绕开被测实现**，证明"真写进了磁盘上那个库文件"。

    只发 SELECT。库文件不存在时 `sqlite3.connect` 会当场建一个空文件 —— 所以这一句
    只在已经 `--apply` 过的用例里出现（dry-run 那条用例断言的是"文件不存在"）。
    """
    conn = sqlite3.connect(db)
    try:
        videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        creators = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
    finally:
        conn.close()
    return int(videos), int(creators)


def _meta(row: Video | Creator) -> dict[str, Any]:
    return json.loads(row.metadata_json)


async def _video_of(storage: SqliteStorage, platform: str, video_id: str) -> Video:
    row = await storage.videos.find_by_platform_id(platform, video_id)
    assert row is not None, f"库里没有 {platform}:{video_id} —— 重扫没把它重建出来"
    return row


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    return FileStorage(tmp_path / "data")


@pytest.fixture
async def storage() -> AsyncIterator[SqliteStorage]:
    """内存库，**不预置 platforms 镜像行**（理由见模块 docstring）。"""
    store = SqliteStorage.in_memory()
    await store.initialize()
    yield store
    await store.close()


async def _run(
    storage: SqliteStorage, files: FileStorage, *, dry_run: bool = False
) -> tool.RescanReport:
    return await tool.rescan(storage, files, tool.scan_tree(files), dry_run=dry_run)


def _cli(files: FileStorage, tmp_path: Path, *extra: str) -> int:
    """走 CLI 那一层：`--data-dir` 显式指树，`--config-dir` 指一个不存在的目录（全默认值）。

    两个都必须给：不给 `--data-dir` 会读到仓库里的 live `data/`（真凭证 + 真主库），
    不给 `--config-dir` 会去读 `config/app.yaml`。测试一行都不该碰。
    """
    return tool.main(
        [
            "--data-dir",
            str(files.root),
            "--config-dir",
            str(tmp_path / "no-config-here"),
            *extra,
        ]
    )


# --------------------------------------------------------------------------- #
# 不变量 1：盘上有什么 → 库里就有对应多少行
# --------------------------------------------------------------------------- #


async def test_rows_in_the_library_equal_the_directories_on_disk(
    storage: SqliteStorage, files: FileStorage
) -> None:
    _leaf(files, "douyin", CREATOR, "7123", "怎么选题")
    _leaf(files, "douyin", CREATOR, "7124", "怎么起号", with_cover=True)
    _leaf(files, "douyin", NO_CREATOR, "7125", "无身份的一条")
    _leaf(files, "bilibili", "某UP", "BV1cSec6tEux", "B站的一条")
    _leaf(files, "bilibili", "某UP", "BV1other0000", "另一条", media=("media.webm",))

    report = await _run(storage, files)

    disk = _dirs_on_disk(files)
    assert report.recognized == len(disk), "认出的作品数必须等于盘上的作品目录数"
    assert await storage.videos.count(filters=None) == len(disk)
    for directory in disk:
        platform, video_id = _identity_of(directory)
        assert await storage.videos.find_by_platform_id(platform, video_id) is not None
    # 报告自身的账也要平：每一条被检视过的条目要么被认出、要么被解释
    assert report.entries_seen == report.recognized + len(report.skipped)
    assert report.videos_created == len(disk)
    assert report.errors == []


async def test_paths_in_the_library_are_relative_posix_and_resolvable(
    storage: SqliteStorage, files: FileStorage
) -> None:
    directory = _leaf(files, "douyin", CREATOR, "7123", "标题", with_cover=True)

    await _run(storage, files)

    row = await _video_of(storage, "douyin", "7123")
    # 期望值按 spec §1 的布局手拼（不从实现里推导），否则实现写错了也测不出来
    assert row.media_path == f"media/douyin/{CREATOR}/7123-标题/media.mp4"
    assert "\\" not in (row.media_path or ""), "库里存反斜杠 = 换写法/换机器就查不到"
    assert files.abs(row.media_path or "").resolve() == (directory / "media.mp4").resolve()
    assert row.cover_path == f"media/douyin/{CREATOR}/7123-标题/cover.jpg"


# --------------------------------------------------------------------------- #
# 不变量 2：幂等
# --------------------------------------------------------------------------- #


async def test_scanning_the_same_tree_twice_creates_nothing(
    storage: SqliteStorage, files: FileStorage
) -> None:
    _leaf(files, "douyin", CREATOR, "7123", "标题", meta={"uploader_id": "sec1"})
    _leaf(files, "bilibili", "某UP", "BV1cSec6tEux", "另一条")

    first = await _run(storage, files)
    videos_after = await storage.videos.count(filters=None)
    creators_after = await storage.creators.count()
    second = await _run(storage, files)

    assert (first.videos_created, first.videos_existing) == (2, 0)
    assert second.videos_created == 0, "第二遍还在新建 = 查重没生效"
    assert second.creators_created == 0
    assert second.videos_existing == videos_after
    assert await storage.videos.count(filters=None) == videos_after
    assert await storage.creators.count() == creators_after


async def test_dry_run_writes_no_rows_into_an_existing_library(
    storage: SqliteStorage, files: FileStorage
) -> None:
    _leaf(files, "douyin", CREATOR, "7123", "标题", meta={"uploader_id": "sec1"})
    written = await _run(storage, files)
    before_videos = await storage.videos.count(filters=None)
    before_creators = await storage.creators.count()

    report = await _run(storage, files, dry_run=True)

    assert written.videos_created == 1
    assert report.dry_run is True
    assert await storage.videos.count(filters=None) == before_videos, "dry-run 往库里写了行"
    assert await storage.creators.count() == before_creators
    assert (report.videos_created, report.videos_existing) == (0, before_videos)
    # dry-run 的博主投影也要报"已存在"，否则预演说会新建、真跑却不新建
    assert (report.creators_created, report.creators_existing) == (0, before_creators)


async def test_dry_run_against_an_empty_library_still_writes_nothing(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """上一条命中的是"已存在"那一半；这一条才是"闸门"本身。

    空库 + dry-run：预演**必须**说"会新建 2 条作品 / 2 位博主"，同时库里一条都不许多。
    只有这个形状能证伪"把 dry-run 的闸去掉" —— 已存在的行会让 `insert_or_get`
    什么都不写，那种情况下闸门没了也看不出来（第一轮写这条用例时就栽在这儿）。
    """
    _leaf(files, "douyin", CREATOR, "7123", "标题", meta={"uploader_id": "sec1"})
    _leaf(files, "bilibili", "某UP", "BV1cSec6tEux", "另一条", meta={"mid": "12345"})

    report = await _run(storage, files, dry_run=True)

    assert (report.videos_created, report.creators_created) == (2, 2), "预演得先说出会做什么"
    assert await storage.videos.count(filters=None) == 0, "dry-run 真的往库里插行了"
    assert await storage.creators.count() == 0, "dry-run 顺手补了博主行"
    assert await storage.platforms.count() == 0, "dry-run 顺手补了 platforms 镜像行"


def test_dry_run_creates_no_database_file_and_says_so(
    tmp_path: Path, files: FileStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    """dry-run 那句"未写任何东西"必须是真的：连库文件与骨架目录都不许留下。"""
    _leaf(files, "douyin", CREATOR, "7123", "标题")

    code = _cli(files, tmp_path)

    assert code == 0
    assert not (files.root / DB_NAME).exists(), "dry-run 把目标库建出来了"
    assert not (files.root / "cookies").exists(), "dry-run 不该 ensure_dirs()"
    out = capsys.readouterr().out
    assert "DRY-RUN（库里一行都没写）" in out
    assert "目标库还不存在" in out, "『按空库算出的新建数』这件事要说出来，不能闷着报新建 1 条"


# --------------------------------------------------------------------------- #
# 不变量 3：跳过必有原因，且磁盘保持原样
# --------------------------------------------------------------------------- #


async def test_what_it_cannot_read_stays_put_and_is_explained(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """认不出来的条目：逐条留下原因，且**磁盘一字不动**。

    "没有配置 schema 的那个平台"**不能写死名字** —— 别人今天把 xiaohongshu 注册进
    `PLATFORM_CONFIG_SCHEMAS`（2026-09-24 真发生了），写死名字的用例就从"看护契约"
    退化成"看护某个人的提交顺序"。这里现取 `supported_platforms()` 的补集。
    """
    absent = next(
        (name for name in ("shipinhao", "weixin", "kuaishou") if name not in supported_platforms()),
        "",
    )
    assert absent, "造不出一个'本构建没实现'的平台名了，这条用例的前提需要重写"
    junk = {
        "没有分隔符": files.media_root / "douyin" / CREATOR / "纯标题没有横线",
        "空目录": files.media_root / "douyin" / CREATOR / "9000-什么都没有",
        "改名过": files.media_root / "douyin" / CREATOR / "9001-标题  带双空格",
        "平台没实现": files.media_root / absent,
        "隐藏的博主目录": files.media_root / "douyin" / ".creator",
        "隐藏的作品目录": files.media_root / "douyin" / CREATOR / ".9100-隐藏",
    }
    for path in junk.values():
        path.mkdir(parents=True, exist_ok=True)
    stray = files.media_root / "douyin" / CREATOR / "stray.txt"
    stray.write_text("散", encoding="utf-8")
    junk["博主目录下的散文件"] = stray
    (files.media_root / "README.txt").write_text("散文件", encoding="utf-8")
    (files.media_root / "douyin" / "stray.jpg").write_bytes(b"j")
    # 未实现的平台下面还有完整一棵作品目录：也要一次说清，而不是逐条静默丢弃
    _leaf(files, absent, "某位博主", "64abcdef1234567890", "那底下的一条")

    report = await _run(storage, files, dry_run=True)

    reasons = {item.path: item.reason for item in report.skipped}
    assert report.recognized == 0 and report.videos_created == 0
    assert report.entries_seen == report.recognized + len(report.skipped)
    assert report.errors == []
    for name, path in junk.items():
        rel = path.relative_to(files.root).as_posix()
        assert rel in reasons, f"{name} 被静默丢掉了：报告里没有它的跳过原因"
        assert reasons[rel].strip(), f"{name} 的跳过原因是空的"
        assert path.exists(), f"{name} 被删掉了 —— 重扫工具一个文件都不许动"
    assert "media/README.txt" in reasons and "media/douyin/stray.jpg" in reasons
    assert (files.media_root / absent).exists()
    assert len(list((files.media_root / absent).rglob("*"))) == 3, "那棵子树被动过了"
    # 没实现的平台不许被顺手建进镜像表：下次启动 prune_unknown 会被 RESTRICT 挡住
    assert await storage.platforms.get(absent) is None


async def test_an_unreadable_leaf_becomes_a_skip_with_the_original_error(
    storage: SqliteStorage, files: FileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读不出来的目录：**记一条带原文的跳过**，既不 crash 也不静默少一半。

    两层都要试：作品目录（`_listing` 直接接）与博主目录（`_children_of` 那一层接）。
    """
    good = _leaf(files, "douyin", CREATOR, "7123", "正常的一条")
    bad_leaf = _leaf(files, "douyin", CREATOR, "7124", "读不出的一条")
    bad_creator_dir = _leaf(files, "bilibili", "某UP主", "BV1cSec6tEux", "整位博主都读不出")
    real_iterdir = Path.iterdir

    def fake_iterdir(self: Path) -> Iterator[Path]:
        if self in {bad_leaf, bad_creator_dir.parent}:
            msg = "拒绝访问"
            raise PermissionError(13, msg)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    report = await _run(storage, files)

    assert report.recognized == 1, f"只有 {good.name} 该被认出"
    assert report.entries_seen == report.recognized + len(report.skipped)
    assert await storage.videos.count(filters=None) == 1
    by_path = {item.path: item.reason for item in report.skipped}
    leaf_rel = good.parent.relative_to(files.root).as_posix()
    assert "拒绝访问" in by_path[f"{leaf_rel}/7124-读不出的一条"]
    assert "PermissionError" in by_path[f"{leaf_rel}/7124-读不出的一条"]
    creator_rel = bad_creator_dir.parent.relative_to(files.root).as_posix()
    assert "拒绝访问" in by_path[creator_rel], "博主目录那一层的读不出来也要有原文"


def test_a_media_root_that_cannot_be_listed_is_an_error_not_a_zero(
    files: FileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """根都读不出来时，任何一个数字都没意义 → 进 `errors`（退出码因此是 1）。"""
    files.media_root.mkdir(parents=True, exist_ok=True)

    def boom(self: Path) -> Iterator[Path]:
        msg = "拒绝访问"
        raise PermissionError(13, msg)

    monkeypatch.setattr(Path, "iterdir", boom)

    scan = tool.scan_tree(files)

    assert (scan.candidates, scan.skips) == ([], [])
    assert len(scan.errors) == 1 and "媒体根目录读不出来" in scan.errors[0]
    assert "拒绝访问" in scan.errors[0]


async def test_a_leaf_that_does_not_round_trip_is_left_alone(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """`safe_filename` 会把连续空白折成一个空格，所以带双空格的目录名不可能是本仓库写的。"""
    odd = files.media_root / "douyin" / CREATOR / "9001-标题  带双空格"
    odd.mkdir(parents=True)
    (odd / "media.mp4").write_bytes(b"x")
    good = _leaf(files, "douyin", CREATOR, "9002", "正常的一条")

    report = await _run(storage, files)

    assert report.recognized == 1 and len(report.skipped) == 1
    assert good.is_dir() and odd.is_dir(), "认不出来的目录必须原地不动"
    assert [p.name for p in odd.iterdir()] == ["media.mp4"], "目录里的文件被动过"
    assert "FileStorage.media_dir()" in report.skipped[0].reason
    assert await storage.videos.find_by_platform_id("douyin", "9001") is None


# --------------------------------------------------------------------------- #
# 凭据：与 V1 迁移的标记分开
# --------------------------------------------------------------------------- #


async def test_rescan_rows_carry_disk_provenance_and_survive_v1_rollback(
    storage: SqliteStorage, files: FileStorage
) -> None:
    directory = _leaf(
        files, "douyin", CREATOR, "7123", "标题", meta={"uploader_id": "sec1", "uploader": CREATOR}
    )

    await _run(storage, files)

    video = await _video_of(storage, "douyin", "7123")
    creator = await storage.creators.find("douyin", "sec1")
    assert creator is not None
    for row, source in ((video, directory), (creator, directory.parent)):
        payload = _meta(row)
        assert payload[tool.PROVENANCE_KEY] is True
        assert payload["rescan_source"] == source.relative_to(files.root).as_posix()
        assert payload["rescan_source_abs"] == str(source)
        assert payload["rescan_at"]
        # 与 `migrate_from_v1 --rollback` 的判据不撞：重扫行不该被它删掉
        assert "migrated_from_v1" not in payload
        assert mig._is_migrated(row.metadata_json) is False
    assert creator.is_tracking is False, "重扫不许替用户打开自动采集"
    assert creator.profile_url == "douyin://sec1"
    assert creator.name == CREATOR


async def test_the_platform_mirror_row_is_seeded_disabled(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """没有博主身份也要补镜像行：`creators.platform` 是外键，而缺镜像行的平台在前端是空的。"""
    _leaf(files, "douyin", NO_CREATOR, "7123", "标题")

    await _run(storage, files)

    record = await storage.platforms.get("douyin")
    assert record is not None
    assert record.enabled is False, "开关的权威源是 platforms.yaml，重扫不许顺手打开平台"


# --------------------------------------------------------------------------- #
# 博主归属的三条路
# --------------------------------------------------------------------------- #


async def test_two_leaves_of_one_creator_make_one_creator_row(
    storage: SqliteStorage, files: FileStorage
) -> None:
    meta = {"uploader_id": "sec1", "uploader": CREATOR}
    _leaf(files, "douyin", CREATOR, "7123", "a", meta=meta)
    _leaf(files, "douyin", CREATOR, "7124", "b", meta=meta)

    report = await _run(storage, files)

    assert (report.creators_created, report.creators_existing) == (1, 1)
    assert await storage.creators.count() == 1
    ids = {(await _video_of(storage, "douyin", vid)).creator_id for vid in ("7123", "7124")}
    assert len(ids) == 1 and None not in ids


async def test_a_known_creator_is_linked_by_nickname_without_new_rows(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await storage.platforms.upsert("douyin", enabled=False)
    hand = await storage.creators.insert(
        CreatorDraft(
            platform="douyin", platform_id="sec9", name=CREATOR, profile_url="douyin://sec9"
        )
    )
    _leaf(files, "douyin", CREATOR, "7123", "标题")

    report = await _run(storage, files)

    assert (report.creators_created, report.creators_matched_by_name) == (0, 1)
    assert await storage.creators.count() == 1, "按昵称认出就已经是新加了一位 = 双份博主"
    assert (await _video_of(storage, "douyin", "7123")).creator_id == hand.id


async def test_an_ambiguous_nickname_is_unattributed_and_says_why(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await storage.platforms.upsert("douyin", enabled=False)
    for platform_id in ("sec-a", "sec-b"):
        await storage.creators.insert(
            CreatorDraft(
                platform="douyin",
                platform_id=platform_id,
                name=CREATOR,
                profile_url=f"douyin://{platform_id}",
            )
        )
    _leaf(files, "douyin", CREATOR, "7123", "标题")

    report = await _run(storage, files)

    assert (await _video_of(storage, "douyin", "7123")).creator_id is None, "昵称撞车时不许猜一位"
    assert report.videos_without_creator == 1
    assert (report.creators_created, report.creators_matched_by_name) == (0, 0)
    assert len(report.unattributed) == 1 and "同名" in report.unattributed[0].reason
    assert report.unattributed[0].path.startswith("media/douyin/")


async def test_no_creator_evidence_at_all_still_rebuilds_the_video(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """`creator_id` 可空是 schema 明写的（`ON DELETE SET NULL`）：无归属不是丢数据的理由。"""
    _leaf(files, "douyin", NO_CREATOR, "7123", "标题")

    report = await _run(storage, files)

    assert report.videos_created == 1 and report.videos_without_creator == 1
    assert len(report.unattributed) == 1 and "认不出博主" in report.unattributed[0].reason


# --------------------------------------------------------------------------- #
# 元数据映射：认得出来的才写，认不出来不编
# --------------------------------------------------------------------------- #


async def test_metadata_maps_what_is_unambiguous_and_leaves_the_rest_null(
    storage: SqliteStorage, files: FileStorage
) -> None:
    _leaf(
        files,
        "douyin",
        CREATOR,
        "7123",
        "目录里的截断标题",
        meta={
            "title": "完整标题，比目录名长得多得多得多得多",
            "description": "  ",
            "duration": 93.5,
            "view_count": 100,
            "like_count": True,  # 布尔是 int 的子类，绝不该被存成点赞数
            "timestamp": 1_700_000_000,
            "uploader_id": "sec1",
        },
    )

    await _run(storage, files)

    row = await _video_of(storage, "douyin", "7123")
    assert row.title.startswith("完整标题"), "目录名那半段被截断过，元数据里的标题才可信"
    assert row.description is None
    assert row.duration_seconds == 93.5
    assert row.view_count == 100
    assert row.like_count is None
    assert row.media_source is None, "磁盘问不出媒体来源，不许塞一个枚举认的取值"
    assert row.published_at is not None and row.published_at.year == 2023
    assert _meta(row)["rescan_identity"] == "metadata.json"


async def test_a_date_without_time_or_zone_is_not_invented(
    storage: SqliteStorage, files: FileStorage
) -> None:
    _leaf(files, "douyin", CREATOR, "7123", "标题", meta={"upload_date": "20240501"})

    await _run(storage, files)

    row = await _video_of(storage, "douyin", "7123")
    assert row.published_at is None, "只有日期、没有时间与时区 → 不许假装成某一个时刻"
    assert _meta(row)["rescan_published_at_unmapped"] == "20240501"


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"timestamp": 1_700_000_000}, (datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC), None)),
        # 带 tz 的 ISO 串才认
        (
            {"publication_date": "2024-05-01T20:00:00+08:00"},
            (datetime(2024, 5, 1, 12, tzinfo=UTC), None),
        ),
        # naive ISO / 只有日期 / 认不出来：值都是 None，但**原文要留着**
        ({"publication_date": "2024-05-01T20:00:00"}, (None, "2024-05-01T20:00:00")),
        ({"publication_date": "五月的一天"}, (None, "五月的一天")),
        ({"upload_date": "20240501"}, (None, "20240501")),
        ({"timestamp": "不是数字"}, (None, None)),
        ({}, (None, None)),
    ],
)
def test_published_at_only_accepts_shapes_that_carry_an_instant(
    meta: dict[str, Any], expected: tuple[datetime | None, str | None]
) -> None:
    """`published_at` 排空 = Feed 排序与 `since` 过滤看不见它；排错 = 整批行时间漂走。

    两种都难看，但**漂走的那种不会红**，所以这里宁缺毋滥，并把原文随数据留下。
    """
    assert tool._published_at_of(meta) == expected


async def test_metadata_that_is_not_an_object_falls_back_to_the_directory_name(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """`metadata.json` 顶层是数组（V1 那种整表 dump）→ 不采用、不猜，但要说出来。"""
    directory = _leaf(files, "douyin", CREATOR, "7123", "标题")
    (directory / "metadata.json").write_text("[1, 2]", encoding="utf-8")

    report = await _run(storage, files)

    assert report.recognized == 1, "元数据读不出来不该连作品都不重建"
    assert len(report.errors) == 1 and "metadata.json 未被采用" in report.errors[0]
    row = await _video_of(storage, "douyin", "7123")
    assert row.title == "标题"
    assert _meta(row)["rescan_identity"] == "目录名"


async def test_single_link_shape_counts_its_placeholder_title(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """`tasks/single_link.py` 写的目录是 `<id>-<id>`：那格 `title` 里其实是 ID。"""
    _leaf(files, "douyin", "single-link", "7123")

    report = await _run(storage, files)

    assert report.placeholder_titles == 1
    row = await _video_of(storage, "douyin", "7123")
    assert row.title == "7123"
    assert _meta(row)["rescan_title_source"] == tool.TITLE_FROM_ID


async def test_unmerged_dash_shards_get_no_main_media_pointer(
    storage: SqliteStorage, files: FileStorage
) -> None:
    _leaf(
        files,
        "bilibili",
        "某UP",
        "BV1cSec6tEux",
        "标题",
        media=("media.f137.mp4", "media.f140.m4a"),
    )
    # 对照项：只有一条轨时它确实就是主媒体（"认不出画面轨"这件事只在多条轨时成立）
    _leaf(files, "bilibili", "某UP", "BV1single000", "单文件的一条", media=("media.webm",))

    report = await _run(storage, files)

    row = await _video_of(storage, "bilibili", "BV1cSec6tEux")
    assert row.media_path is None, "两条轨里认不出画面轨，就不该指一条当主媒体（V1 §7.21）"
    aux = json.loads(row.media_aux_paths_json)
    assert [Path(item).name for item in aux] == ["media.f137.mp4", "media.f140.m4a"]
    assert all(files.abs(item).is_file() for item in aux), "分片一条都不许丢"
    assert report.without_main_media == 1
    assert "DASH" in _meta(row)["rescan_media_note"]
    single = await _video_of(storage, "bilibili", "BV1single000")
    assert single.media_path is not None and single.media_path.endswith("media.webm")
    assert json.loads(single.media_aux_paths_json) == []


async def test_transcript_on_disk_is_counted_but_does_not_become_a_row(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """`transcripts.engine` 是有 CHECK 枚举的列：磁盘上问不出引擎，就不许编一行。"""
    _leaf(files, "douyin", CREATOR, "7123", "标题", with_transcript=True)

    report = await _run(storage, files)

    assert report.transcripts_on_disk == 1
    assert await storage.transcripts.count() == 0
    assert _meta(await _video_of(storage, "douyin", "7123"))["rescan_has_transcript"] is True


# --------------------------------------------------------------------------- #
# 失败原文：单条坏目录不炸整跑
# --------------------------------------------------------------------------- #


def test_broken_metadata_is_loud_but_the_rows_all_get_rebuilt(
    tmp_path: Path, files: FileStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    """认不出元数据 ≠ 丢掉这条作品：行照建，但**必须**留下原文并以退出码 1 收尾。"""
    _leaf(files, "douyin", CREATOR, "7123", "标题", raw_metadata="{不是 JSON")
    _leaf(files, "douyin", CREATOR, "7124", "好的那条")

    code = _cli(files, tmp_path, "--apply", "--json")

    assert code == 1, "有东西没做成必须以非 0 收尾（V1 §1.3）"
    printed = json.loads(capsys.readouterr().out)
    assert len(printed["errors"]) == 1 and "metadata.json 未被采用" in printed["errors"][0]
    assert printed["recognized"] == 2
    assert _db_counts(files.root / DB_NAME) == (2, 0), "坏一条不该拖累整跑"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_it_refuses_to_claim_success_without_a_tree(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="媒体根目录不存在"):
        tool.main(
            ["--data-dir", str(tmp_path / "nothing"), "--config-dir", str(tmp_path / "no-cfg")]
        )


def test_the_json_report_carries_every_skip_reason(
    tmp_path: Path, files: FileStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    _leaf(files, "douyin", CREATOR, "7123", "标题")
    (files.media_root / "douyin" / CREATOR / "没有横线").mkdir(parents=True)

    code = _cli(files, tmp_path, "--json")

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["recognized"] == 1
    assert len(report["skipped"]) == report["entries_seen"] - report["recognized"]
    assert all(item["reason"].strip() for item in report["skipped"])


def test_the_human_report_prints_every_skip_and_every_error(
    tmp_path: Path, files: FileStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    """人读那一档也要全量打印：`migrate_from_v1` 的 `errors[:50]` 截断是"少一半"的藏身处。"""
    _leaf(files, "douyin", CREATOR, "7123", "标题", raw_metadata="{坏")
    _leaf(files, "douyin", CREATOR, "7124", "没有媒体指针的一条", media=("a.mp4", "b.m4a"))
    (files.media_root / "douyin" / CREATOR / "没有横线").mkdir(parents=True)

    code = _cli(files, tmp_path, "--apply")

    out = capsys.readouterr().out
    assert code == 1
    assert "跳过 media/douyin/姜胡说/没有横线" in out
    assert "metadata.json 未被采用" in out
    assert "1 条错误" in out
    assert "无归属 media/douyin/" in out


def test_without_data_dir_the_root_comes_from_the_config(tmp_path: Path) -> None:
    """`--data-dir` 没给时走 `FileStorage.from_config(root=仓库根)` 那一条路。

    配置里的 `data.dir` 指到 tmp：**这条用例的意义就在于它不需要 live `data/`**，
    所以断言的是"算出来的根 = 配置里那一个"，而不是去打真库。
    """
    config = AppConfig(data=DataSection(dir=tmp_path))

    files = tool.resolve_files(Namespace(data_dir="   "), config)
    db = tool.db_path_for(config, files)

    assert files.media_root == tmp_path / "media"
    assert db == tmp_path / "intelligence_hub.sqlite3"


def test_apply_prints_written_and_a_second_apply_stays_put(
    tmp_path: Path, files: FileStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    _leaf(
        files,
        "douyin",
        CREATOR,
        "7123",
        "标题",
        meta={"uploader_id": "sec1", "uploader": CREATOR},
    )

    first = _cli(files, tmp_path, "--apply")

    out = capsys.readouterr().out
    assert first == 0
    assert "== 磁盘重扫 · 已写入 ==" in out
    assert "新建=1" in out
    assert "is_tracking=False" in out, "重扫新建的博主不该被顺手打开自动采集，得说出来"
    assert _db_counts(files.root / DB_NAME) == (1, 1), "报告说写了，磁盘上就得真有"

    again = _cli(files, tmp_path, "--apply")

    second = capsys.readouterr().out
    assert again == 0
    assert _db_counts(files.root / DB_NAME) == (1, 1), "第二遍把同一个目录又灌了一遍"
    assert "已存在=1" in second and "新建=0" in second


async def test_one_failing_row_does_not_abort_the_run(
    storage: SqliteStorage,
    files: FileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _leaf(files, "douyin", CREATOR, "7123", "会失败的那条")
    _leaf(files, "douyin", CREATOR, "7124", "正常的那条")
    real = tool._video_draft

    def explode(files_arg: FileStorage, cand: tool.Candidate, creator_id: int | None) -> Video:
        if cand.platform_video_id == "7123":
            msg = "假装的入库失败"
            raise tool.StorageError(msg)
        return real(files_arg, cand, creator_id)

    monkeypatch.setattr(tool, "_video_draft", explode)

    report = await _run(storage, files)

    assert len(report.errors) == 1 and "7123-会失败的那条 未入库" in report.errors[0]
    assert "假装的入库失败" in report.errors[0], "失败原文要留着"
    assert report.videos_created == 1
    assert await storage.videos.find_by_platform_id("douyin", "7124") is not None
