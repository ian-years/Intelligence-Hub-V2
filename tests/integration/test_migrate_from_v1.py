"""`tools/migrate_from_v1.py`：对**按 V1 真写入路径造的** V1 库跑迁移。

**这份文件以前证明不了任何事**：它照 `local_store.py` 的 DDL 建了表（结构是真的），
却往里写 V2 形状的**值** —— `platform='douyin'`。V1 的 `local_store.normalize_platform()`
存的始终是**显示名** `抖音 / B站 / 小红书 / YouTube`，而 V2 的 `creators.platform`
是 `ForeignKey("platforms.name")`，真数据的行插进来第一条就 FK 失败。
教训：**fixture 的 DDL 忠实不够，值也必须来自源系统的真写入路径。**

所以这里的 V1 侧常量一律**不 import 被测模块**，逐字抄 V1 源码：

| 事实 | V1 出处 |
|---|---|
| 平台取值是显示名 | `local_store.py:60-73 normalize_platform()` |
| 建表 DDL | `local_store.py:135-180 _init_db()`（含 NOT NULL / DEFAULT） |
| 库文件位置 | `launcher_server.py:1882` → `downloads/local.sqlite3` |
| 墓碑文件位置 | `launcher_server.py:2346` → `downloads/launcher-state/hidden-videos.json` |
| 墓碑条目字段 | `launcher_server.py:1871-1878`（`id` 是 `token_hex(6)` 随机串，**不是**作品身份） |
| 墓碑取键口径 | `launcher_server.py:1548` 只认 platform_video_id 与 record_id |

本模块的 storage fixture **故意不预置 `platforms` 镜像行**（`tests/conftest.py` 那个会预置）：
预置等于替脚本把 FK 前置条件做完，脚本自己补不补就永远测不到。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from errno import EXDEV
from pathlib import Path
from typing import NoReturn

import pytest
from tools.migrate_from_v1 import main, migrate, open_v1_readonly

from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

pytestmark = pytest.mark.integration

# --- 以下 V1 侧常量抄 V1，不从被测模块 import（那等于用被测代码的假设验证被测代码） ---

V1_DB_RELPATH = Path("downloads") / "local.sqlite3"
V1_TOMBSTONE_RELPATH = Path("downloads") / "launcher-state" / "hidden-videos.json"

V1_DOUYIN = "抖音"  # normalize_platform() 的真实输出
V1_BILIBILI = "B站"
V1_UNMAPPED = "视频号"  # normalize_platform 认不出时原样返回；V2 没有这个平台

NOW = "2026-01-01 00:00:00"

_V1_DDL = """
CREATE TABLE creators (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    name TEXT NOT NULL,
    homepage_url TEXT DEFAULT '',
    cross_platform_identity TEXT DEFAULT '',
    collection_strategy TEXT DEFAULT '',
    is_tracking INTEGER DEFAULT 1,
    avatar_url TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, platform_id)
);
CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_video_id TEXT NOT NULL,
    creator_id TEXT DEFAULT '',
    creator_name TEXT DEFAULT '',
    video_title TEXT NOT NULL,
    video_url TEXT DEFAULT '',
    video_path TEXT DEFAULT '',
    metadata_path TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    duration_seconds REAL DEFAULT NULL,
    published_at TEXT DEFAULT '',
    downloaded_at TEXT DEFAULT '',
    video_download_status TEXT DEFAULT '已下载',
    transcript_status TEXT DEFAULT '待转写',
    content_summary TEXT DEFAULT '',
    key_points TEXT DEFAULT '',
    user_pain_points TEXT DEFAULT '',
    expandable_topics TEXT DEFAULT '',
    representative_comments TEXT DEFAULT '',
    raw_transcript TEXT DEFAULT '',
    clean_transcript TEXT DEFAULT '',
    metrics_json TEXT DEFAULT '{}',
    raw_data_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, platform_video_id)
);
"""


#: 两条 INSERT 写成静态字符串常量：列名不来自任何输入（也就没有 f-string 拼 SQL 这回事），
#: 值全部走参数绑定。
_INSERT_CREATOR = (
    "INSERT INTO creators (id, platform, platform_id, name, homepage_url, is_tracking,"
    " metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_V1_VIDEO_COLS = (
    "id",
    "platform",
    "platform_video_id",
    "creator_id",
    "creator_name",
    "video_title",
    "video_url",
    "video_path",
    "metadata_path",
    "cover_url",
    "duration_seconds",
    "published_at",
    "downloaded_at",
    "video_download_status",
    "transcript_status",
    "content_summary",
    "key_points",
    "user_pain_points",
    "expandable_topics",
    "representative_comments",
    "raw_transcript",
    "clean_transcript",
    "metrics_json",
    "raw_data_json",
    "created_at",
    "updated_at",
)
# 静态字面量（不是拼出来的）：S608 的判据是"SQL 文本由字符串构造"，这里没有构造。
_INSERT_VIDEO = """
INSERT INTO videos (
    id, platform, platform_video_id, creator_id, creator_name, video_title, video_url,
    video_path, metadata_path, cover_url, duration_seconds, published_at, downloaded_at,
    video_download_status, transcript_status, content_summary, key_points, user_pain_points,
    expandable_topics, representative_comments, raw_transcript, clean_transcript,
    metrics_json, raw_data_json, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
# 列顺序必须与 _INSERT_VIDEO 一致 —— 改了这边要改那边，这条断言在收集期就响。
assert len(_V1_VIDEO_COLS) == 26, "_V1_VIDEO_COLS 与 _INSERT_VIDEO 的列数漂了"

#: V1 建表时各列的 DEFAULT（`local_store.py:156-179`）。缺列时按 V1 自己的默认补，
#: 而不是让 fixture 偷偷发明取值。
_V1_VIDEO_DEFAULTS: dict[str, object] = {
    "creator_id": "",
    "creator_name": "",
    "video_url": "",
    "video_path": "",
    "metadata_path": "",
    "cover_url": "",
    "duration_seconds": None,
    "published_at": "",
    "downloaded_at": "",
    "video_download_status": "已下载",
    "transcript_status": "待转写",
    "content_summary": "",
    "key_points": "",
    "user_pain_points": "",
    "expandable_topics": "",
    "representative_comments": "",
    "raw_transcript": "",
    "clean_transcript": "",
    "metrics_json": "{}",
    "raw_data_json": "{}",
    "created_at": NOW,
    "updated_at": NOW,
}


def _creator(platform: str, platform_id: str, name: str) -> tuple[object, ...]:
    return (
        f"creator:{platform}:{platform_id}",
        platform,
        platform_id,
        name,
        f"https://example.invalid/user/{platform_id}",
        1,
        "{}",
        NOW,
        NOW,
    )


def _video(**kw: object) -> tuple[object, ...]:
    row = {**_V1_VIDEO_DEFAULTS, **kw}
    return tuple(row[name] for name in _V1_VIDEO_COLS)


def _build_v1(
    root: Path, *, creators: list[tuple[object, ...]], videos: list[tuple[object, ...]]
) -> Path:
    """按 V1 的真实 DDL + 真实取值口径建一份 V1 工作区（只建库，媒体/墓碑另有函数）。"""
    db = root / V1_DB_RELPATH
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(_V1_DDL)
    conn.executemany(_INSERT_CREATOR, creators)
    conn.executemany(_INSERT_VIDEO, videos)
    conn.commit()
    conn.close()
    return root


def _write_tombstones(root: Path, entries: list[dict[str, str]]) -> None:
    """写到 V1 真正读的那个路径，条目形状抄 `launcher_server.py:1871`。"""
    path = root / V1_TOMBSTONE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _media(root: Path, rel: str, payload: bytes) -> None:
    path = root / "downloads" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _one_douyin_creator_and_video(root: Path) -> Path:
    """最干净的单平台样本：给只关心「写没写盘」的用例用，免得掺进平台翻译的噪声。"""
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
                transcript_status="未转写",
            ),
        ],
    )


def _standard_v1(root: Path) -> Path:
    """两平台三作品（pv1 有媒体有稿 / pv2 待转写无媒体 / pv3 是 B站）+ 一条 V2 没有的平台。"""
    _media(root, "media/v1.mp4", b"fake-video-bytes")
    _media(root, "media/v3.m4a", b"fake-audio-bytes")
    return _build_v1(
        root,
        creators=[
            _creator(V1_DOUYIN, "sec1", "姜胡说"),
            _creator(V1_BILIBILI, "mid9", "阿B说"),
            _creator(V1_UNMAPPED, "sph1", "没有这个平台"),
        ],
        videos=[
            _video(
                id="row1",
                platform=V1_DOUYIN,
                platform_video_id="pv1",
                creator_id="sec1",
                creator_name="姜胡说",
                video_title="怎么选题",
                video_url="https://x.invalid/pv1",
                video_path="media/v1.mp4",
                duration_seconds=42.5,
                published_at="2026-01-02 03:04:05",
                metrics_json='{"view_count": 999}',
                transcript_status="已转写",
                clean_transcript="第一句。第二句。第三句。",
            ),
            _video(
                id="row2",
                platform=V1_DOUYIN,
                platform_video_id="pv2",
                creator_id="sec1",
                creator_name="姜胡说",
                video_title="要删掉的",
                transcript_status="待转写",
            ),
            _video(
                id="row3",
                platform=V1_BILIBILI,
                platform_video_id="pv3",
                creator_id="mid9",
                creator_name="阿B说",
                video_title="B站作品",
                video_path="media/v3.m4a",
                transcript_status="未转写",
            ),
            _video(
                id="row4",
                platform=V1_UNMAPPED,
                platform_video_id="pv4",
                creator_id="sph1",
                video_title="V2 没这个平台",
                transcript_status="未转写",
            ),
        ],
    )


@pytest.fixture
def v1(tmp_path: Path) -> Path:
    root = _standard_v1(tmp_path / "v1")
    _write_tombstones(
        root,
        [
            {
                "id": "a1b2c3",  # V1 用 secrets.token_hex(6)：随机串，不是作品身份
                "platform": V1_DOUYIN,
                "platform_video_id": "pv2",
                "record_id": f"local:{V1_DOUYIN}:pv2",
                "video_title": "要删掉的",
                "created_at": NOW,
            }
        ],
    )
    return root


@pytest.fixture
async def unseeded_storage():
    """干净的 V2 内存库，`platforms` 镜像里一行都没有。"""
    store = SqliteStorage.in_memory()
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    fs = FileStorage(tmp_path / "v2data")
    fs.ensure_dirs()
    return fs


def _digest_tree(root: Path) -> dict[str, str]:
    """整棵树的 内容→指纹。md5 在这里是**变更检测**，不是安全用途（显式声明以免误报）。"""
    return {
        str(path.relative_to(root)): hashlib.md5(
            path.read_bytes(), usedforsecurity=False
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------- 平台翻译（C1）


async def test_v1_display_names_land_as_v2_slugs(v1, unseeded_storage, files) -> None:
    """V1 存显示名、V2 存 slug：不翻译的话 FK 当场炸，一条都迁不进去。"""
    await migrate(v1, unseeded_storage, files, dry_run=False)

    douyin = await unseeded_storage.creators.find("douyin", "sec1")
    assert douyin is not None and douyin.name == "姜胡说"
    assert await unseeded_storage.creators.find("bilibili", "mid9") is not None
    assert await unseeded_storage.videos.find_by_platform_id("douyin", "pv1") is not None
    assert await unseeded_storage.videos.find_by_platform_id("bilibili", "pv3") is not None


async def test_migration_seeds_the_platform_rows_it_needs(v1, unseeded_storage, files) -> None:
    """`creators.platform` 是 FK → platforms.name，而镜像行平时由 lifespan 灌；
    脚本不走 lifespan，不自己补就是第一条 FK 失败。"""
    assert await unseeded_storage.platforms.count() == 0

    await migrate(v1, unseeded_storage, files, dry_run=False)

    names = {row.name for row in await unseeded_storage.platforms.list_all()}
    assert {"douyin", "bilibili"} <= names


async def test_seeding_the_mirror_never_turns_a_platform_on(v1, unseeded_storage, files) -> None:
    """迁移不打开任何平台：开关的权威源是 platforms.yaml，脚本补出来的行只能是不启用。"""
    await migrate(v1, unseeded_storage, files, dry_run=False)

    seeded = {row.name: row.enabled for row in await unseeded_storage.platforms.list_all()}
    assert seeded == {"douyin": False, "bilibili": False}


async def test_unmappable_platform_is_reported_not_fatal(v1, unseeded_storage, files) -> None:
    """认不出的平台：如实记一条带原值的错误并跳过该行，其余照迁 ——
    不许整跑炸掉，也不许替它猜一个平台。"""
    report = await migrate(v1, unseeded_storage, files, dry_run=False)

    assert any(V1_UNMAPPED in line for line in report.errors)
    assert await unseeded_storage.videos.find_by_platform_id("douyin", "pv1") is not None
    assert await unseeded_storage.creators.find(V1_UNMAPPED, "sph1") is None
    assert await unseeded_storage.videos.find_by_platform_id(V1_UNMAPPED, "pv4") is None


# ---------------------------------------------------------------- 墓碑（C2）


async def test_tombstone_at_v1s_real_path_hides_the_video(v1, unseeded_storage, files) -> None:
    """V1 的墓碑在 `downloads/launcher-state/`。路径写错 → 读不到 → 静默空集，
    用户删过的作品整批复活。"""
    report = await migrate(v1, unseeded_storage, files, dry_run=False)

    pv2 = await unseeded_storage.videos.find_by_platform_id("douyin", "pv2")
    assert pv2 is not None and pv2.is_hidden is True
    assert report.hidden == 1


async def test_unparseable_tombstone_file_is_an_error(tmp_path, unseeded_storage, files) -> None:
    """墓碑文件在、但解析不出来 = 无法知道用户删过什么。这时静默返回空集
    等于把「删除复活」伪装成「从来没删过」，必须如实报。

    （文件**不存在**则不是错误：V1 的 `load_state_list` 对缺失同样返回空列表，
    含义就是"没人删过任何东西"。）"""
    root = _one_douyin_creator_and_video(tmp_path / "v1")
    path = root / V1_TOMBSTONE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")

    report = await migrate(root, unseeded_storage, files, dry_run=False)

    assert any("hidden-videos" in line for line in report.errors)


async def test_tombstone_random_id_is_not_a_video_identity(
    tmp_path, unseeded_storage, files
) -> None:
    """V1 墓碑条目的 `id` 是随机 hex 串。把它当作品身份收进来，任何 record_id 撞上它
    都会静默隐藏一条活得好好的作品 —— V1 在 `hidden_video_keys()` 里专门为此写了注释。"""
    root = _build_v1(
        tmp_path / "v1",
        creators=[_creator(V1_DOUYIN, "sec1", "姜胡说")],
        videos=[
            _video(
                id="rowA",
                platform=V1_DOUYIN,
                platform_video_id="a1b2c3",
                creator_id="sec1",
                video_title="活得好好的",
                transcript_status="未转写",
            ),
        ],
    )
    _write_tombstones(
        root,
        [
            {
                "id": "a1b2c3",
                "platform": V1_DOUYIN,
                "platform_video_id": "zzz-gone",
                "record_id": f"local:{V1_DOUYIN}:zzz-gone",
                "video_title": "别的",
                "created_at": NOW,
            }
        ],
    )

    report = await migrate(root, unseeded_storage, files, dry_run=False)

    live = await unseeded_storage.videos.find_by_platform_id("douyin", "a1b2c3")
    assert live is not None and live.is_hidden is False
    assert report.hidden == 0


# ---------------------------------------------------------------- 搬运与保真


async def test_real_migration_moves_media_transcript_and_state(v1, unseeded_storage, files) -> None:
    await migrate(v1, unseeded_storage, files, dry_run=False)

    pv1 = await unseeded_storage.videos.find_by_platform_id("douyin", "pv1")
    assert pv1 is not None
    # 迁移来源记在 metadata_json（media_source 是有 CHECK 枚举的列，V1 没记 → None）
    assert pv1.media_source is None
    assert "migrated_from_v1" in pv1.metadata_json
    assert pv1.media_path and not Path(pv1.media_path).is_absolute()
    assert files.abs(pv1.media_path).is_file()

    rec = await unseeded_storage.transcripts.get_for_video(pv1.id)
    assert rec is not None and rec.engine == "manual"  # 有 CHECK 枚举，迁移稿归 manual
    assert files.abs(rec.text_path).read_text(encoding="utf-8").startswith("第一句")
    assert (files.root / ".migration_state.json").is_file()


async def test_second_run_is_idempotent(v1, unseeded_storage, files) -> None:
    first = await migrate(v1, unseeded_storage, files, dry_run=False)
    second = await migrate(v1, unseeded_storage, files, dry_run=False)

    assert first.videos == 3
    assert second.videos == 0  # 全在 done 状态里，直接跳过
    assert second.skipped_existing == 3
    assert await unseeded_storage.videos.count() == 3


async def test_migrate_videos_preserves_title(v1, unseeded_storage, files) -> None:
    """标题既是外部输入也是人眼唯一会核对的字段。V1 §7.4 的塌陷形态就是它被默认值抹了。"""
    nasty = "选题/方法：为什么 90% 的人…<script>&"
    root = _build_v1(
        v1.parent / "v1-title",
        creators=[_creator(V1_DOUYIN, "sec1", "姜胡说")],
        videos=[
            _video(
                id="rowN",
                platform=V1_DOUYIN,
                platform_video_id="pvN",
                creator_id="sec1",
                video_title=nasty,
                transcript_status="未转写",
            )
        ],
    )

    await migrate(root, unseeded_storage, files, dry_run=False)

    row = await unseeded_storage.videos.find_by_platform_id("douyin", "pvN")
    assert row is not None
    assert row.title == nasty


async def test_published_at_and_metrics_survive_the_move(v1, unseeded_storage, files) -> None:
    """真 V1 库里 21/21 行都有 `published_at`。不映射它，Feed 的排序和 `since` 过滤
    对迁移来的整批行同时失效，而库里看不出任何异常（列就是 NULL）。"""
    await migrate(v1, unseeded_storage, files, dry_run=False)

    row = await unseeded_storage.videos.find_by_platform_id("douyin", "pv1")
    assert row is not None
    assert row.view_count == 999
    assert row.published_at is not None
    # V1 的 `now_text()` 是无时区的本机时间：按本机时区解读，并把这个假设随数据记下来，
    # 不假装它是没有来源的 UTC。
    assert row.published_at.utcoffset() is not None
    assert json.loads(row.metadata_json)["published_at_assumed_tz"] == "local"


async def test_unparseable_published_at_is_reported_not_stored_as_null_silently(
    tmp_path, unseeded_storage, files
) -> None:
    root = _build_v1(
        tmp_path / "v1",
        creators=[_creator(V1_DOUYIN, "sec1", "姜胡说")],
        videos=[
            _video(
                id="rowB",
                platform=V1_DOUYIN,
                platform_video_id="pvB",
                creator_id="sec1",
                video_title="脏时间",
                published_at="not-a-date",
                transcript_status="未转写",
            )
        ],
    )

    report = await migrate(root, unseeded_storage, files, dry_run=False)

    assert any("pvB" in line and "published_at" in line for line in report.errors)


async def test_hardlink_failure_falls_back_to_copy(
    v1, unseeded_storage, files, monkeypatch
) -> None:
    """跨卷时 `os.link` 抛 EXDEV → 退回 `shutil.copy2`，并如实计入 media_copied。"""

    def _exdev(*_args: object) -> NoReturn:
        raise OSError(EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", _exdev)
    report = await migrate(v1, unseeded_storage, files, dry_run=False)

    assert report.media_copied == 2 and report.media_linked == 0
    row = await unseeded_storage.videos.find_by_platform_id("douyin", "pv1")
    assert row is not None and row.media_path is not None
    assert files.abs(row.media_path).read_bytes() == b"fake-video-bytes"


async def test_whole_v1_tree_is_byte_identical_after_a_real_run(
    v1, unseeded_storage, files
) -> None:
    """硬约束「V1 一行不动」。只证明 DB 句柄拒写不够 —— 媒体搬运走的是文件系统。"""
    before = _digest_tree(v1)

    await migrate(v1, unseeded_storage, files, dry_run=False)

    assert _digest_tree(v1) == before


# ---------------------------------------------------------------- dry-run 纯度（C3）


async def test_dry_run_projects_the_tombstones_it_would_apply(v1, unseeded_storage, files) -> None:
    """预演打印的是"会做什么"。真 V1 库上有 2 条墓碑，而 dry-run 报 `hidden墓碑=0`
    —— 因为写库的那步在 dry_run 分支里被跳过了。计划表漏报一项，等于让人以为
    这次跑不会碰到任何"删过的作品"。"""
    report = await migrate(v1, unseeded_storage, files, dry_run=True)

    assert report.hidden == 1
    assert await unseeded_storage.videos.count() == 0  # 但确实一行都没写


def test_dry_run_creates_no_database_and_no_directories(tmp_path, monkeypatch) -> None:
    """`--dry-run` 印的是「未写任何东西」。当前 `_amain` 无条件 `ensure_dirs()` +
    `storage.initialize()`（会跑 Alembic），于是在目标目录建出 sqlite3 + 五个目录。"""
    root = _one_douyin_creator_and_video(tmp_path / "v1")
    _write_tombstones(root, [])
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text("data:\n  dir: v2data\n", encoding="utf-8")
    (config_dir / "platforms.yaml").write_text(
        "douyin:\n  enabled: true\n  display_name: 抖音\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    code = main(["--v1-root", str(root), "--config-dir", "config", "--dry-run"])

    assert code == 0
    target = tmp_path / "v2data"
    found = sorted(str(p.relative_to(target)) for p in target.rglob("*")) if target.exists() else []
    assert found == [], f"dry-run 写了东西：{found}"


async def test_a_failing_row_does_not_abort_the_rest(
    tmp_path: Path,
    unseeded_storage: SqliteStorage,
    files: FileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逐行隔离是 C1 的孪生保险：一行炸不能让 21 行的库变成"跑到第三条停了"。

    这层 `except (StorageError, ValidationError)` 是本轮新加的，而**它自己一次都没被
    驱动过**（unmappable 平台在更早的地方就被拦了）—— 未测的兜底等于没有兜底。
    """
    root = _build_v1(
        tmp_path / "v1",
        creators=[
            _creator(V1_DOUYIN, "sec1", "会失败的那个"),
            _creator(V1_DOUYIN, "sec2", "正常的"),
        ],
        videos=[],
    )
    original = unseeded_storage.creators.insert_or_get
    calls = {"n": 0}

    async def flaky(draft: object) -> tuple[object, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise StorageError("模拟：约束撞了")
        return await original(draft)

    monkeypatch.setattr(unseeded_storage.creators, "insert_or_get", flaky)

    report = await migrate(root, unseeded_storage, files, dry_run=False)

    assert calls["n"] == 2, "第一行失败之后不许提前收摊"
    assert (
        any("会失败的那个" not in line and "模拟：约束撞了" in line for line in report.errors)
        or report.errors
    )
    assert await unseeded_storage.creators.find("douyin", "sec2") is not None


def test_the_real_cli_run_writes_into_the_target_tree(tmp_path: Path, monkeypatch) -> None:
    """`main()` 的非 dry-run 那一支：以前只有 dry-run 走过 `_amain`，
    也就是说"用户真敲的那条命令"从来没被执行过。"""
    root = _one_douyin_creator_and_video(tmp_path / "v1")
    _write_tombstones(root, [])
    before = _digest_tree(root)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text("data:\n  dir: v2data\n", encoding="utf-8")
    (config_dir / "platforms.yaml").write_text(
        "douyin:\n  enabled: true\n  display_name: 抖音\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    code = main(["--v1-root", str(root), "--config-dir", "config"])

    assert code == 0, "没有映射失败就该退出码 0（有 errors 时必须非 0）"
    assert (tmp_path / "v2data" / "intelligence_hub.sqlite3").is_file()
    assert (tmp_path / "v2data" / ".migration_state.json").is_file()
    assert _digest_tree(root) == before  # V1 一行不动
    # 第二次跑是幂等的：状态文件命中，不再新增行
    assert main(["--v1-root", str(root), "--config-dir", "config"]) == 0


async def test_a_row_without_an_identity_is_reported_and_skipped(
    tmp_path: Path, unseeded_storage: SqliteStorage, files: FileStorage
) -> None:
    """缺 `platform_video_id` 的行：记一条带 V1 行 id 的错误并跳过，不写半条数据。"""
    root = _build_v1(
        tmp_path / "v1",
        creators=[_creator(V1_DOUYIN, "sec1", "姜胡说")],
        videos=[
            _video(
                id="rowNoId",
                platform=V1_DOUYIN,
                platform_video_id="",
                creator_id="sec1",
                video_title="没有身份的行",
                transcript_status="未转写",
            ),
            _video(
                id="rowOk",
                platform=V1_DOUYIN,
                platform_video_id="pvOk",
                creator_id="sec1",
                video_title="正常的",
                transcript_status="未转写",
            ),
        ],
    )

    report = await migrate(root, unseeded_storage, files, dry_run=False)

    assert any("rowNoId" in line for line in report.errors)
    assert await unseeded_storage.videos.find_by_platform_id("douyin", "pvOk") is not None


async def test_moving_media_that_fails_both_ways_is_reported_not_silent(
    tmp_path: Path,
    unseeded_storage: SqliteStorage,
    files: FileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hardlink 与 copy 都失败时：记 errors、`media_path` 留 None，但**视频行照写**
    （V2 里"有作品行没媒体"是合法中间态，静默丢一条作品才是错）。"""
    root = _standard_v1(tmp_path / "v1")
    real_link = os.link

    def _boom(*_a: object) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "link", _boom)
    monkeypatch.setattr(shutil, "copy2", lambda *_a, **_k: (_ for _ in ()).throw(OSError(5, "io")))
    try:
        report = await migrate(root, unseeded_storage, files, dry_run=False)
    finally:
        monkeypatch.setattr(os, "link", real_link)

    assert any("媒体搬运失败" in line for line in report.errors)
    row = await unseeded_storage.videos.find_by_platform_id("douyin", "pv1")
    assert row is not None and row.media_path is None


async def test_a_failing_video_row_does_not_abort_the_rest(
    tmp_path: Path,
    unseeded_storage: SqliteStorage,
    files: FileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """creator 那一侧同形的兜底已经测过，视频这一半也要驱动一次 —— 否则"漏测的那半"
    正是下一次改动会碰到的那半。"""
    root = _build_v1(
        tmp_path / "v1",
        creators=[_creator(V1_DOUYIN, "sec1", "姜胡说")],
        videos=[
            _video(
                id="bad",
                platform=V1_DOUYIN,
                platform_video_id="pvBad",
                creator_id="sec1",
                video_title="会失败的",
                transcript_status="未转写",
            ),
            _video(
                id="good",
                platform=V1_DOUYIN,
                platform_video_id="pvGood",
                creator_id="sec1",
                video_title="正常的",
                transcript_status="未转写",
            ),
        ],
    )
    original = unseeded_storage.videos.insert_or_get

    async def flaky(draft: object) -> tuple[object, bool]:
        if getattr(draft, "platform_video_id", "") == "pvBad":
            raise StorageError("模拟：视频行撞约束")
        return await original(draft)

    monkeypatch.setattr(unseeded_storage.videos, "insert_or_get", flaky)

    report = await migrate(root, unseeded_storage, files, dry_run=False)

    assert any("pvBad" in line for line in report.errors)
    assert await unseeded_storage.videos.find_by_platform_id("douyin", "pvGood") is not None


def test_the_cli_reports_mapping_failures_instead_of_hiding_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """有 errors 时退出码必须非 0，且把错误打出来 —— 一次性脚本"静默少迁一半"最难查。"""
    root = _standard_v1(tmp_path / "v1")  # 含一条 V2 没有的平台
    _write_tombstones(root, [])
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text("data:\n  dir: v2data\n", encoding="utf-8")
    (config_dir / "platforms.yaml").write_text(
        "douyin:\n  enabled: true\n  display_name: 抖音\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    code = main(["--v1-root", str(root), "--config-dir", "config"])

    out = capsys.readouterr().out
    assert code == 1
    assert V1_UNMAPPED in out and "未迁入" in out


# ---------------------------------------------------------------- 只读句柄（原有看护）


def test_v1_handle_is_genuinely_read_only(v1) -> None:
    conn = open_v1_readonly(v1 / V1_DB_RELPATH)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM videos")  # 只读 URI：写当场抛，不靠约定
    finally:
        conn.close()
