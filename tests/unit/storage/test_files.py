"""`FileStorage` 的路径计算测试。

这个类是**产物路径的唯一真源**（docs/specs/data-model.md §1 Locked 的目录布局）。
它不算大小、不写文件（除 `ensure_dirs` 与 `tmp_dir(create=True)`），
所以测试不需要真磁盘参与 —— `tmp_path` 只用来验 `rel()`/`abs()` 的换算。

三条值得写测试的：

- **`video_id` 前缀**（V1 §7.8）：两个标题净化后撞名时，没有前缀就是"第二条覆盖第一条"。
- **`transcript_path(media_dir)` 全平台同一条**（V1 §7.5 的结构性解决）。
- **`rel()` 存的是相对 `data/` 的 posix 路径**：整棵数据目录要能搬走 / 备份 / 换机器。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from intelligence_hub_v2.storage.files import FileStorage

CREATOR = "姜胡说"
VIDEO_ID = "7642363455722229042"
TITLE = "如何做一份能复用的知识系统"


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    return FileStorage(tmp_path / "data")


# ---------------------------------------------------------------------------
# 根与骨架目录
# ---------------------------------------------------------------------------


def test_subdirectories_hang_off_the_root(files: FileStorage, tmp_path: Path) -> None:
    root = tmp_path / "data"
    assert files.root == root
    assert files.media_root == root / "media"
    assert files.manifests_dir == root / "manifests"
    assert files.cookies_dir == root / "cookies"
    assert files.logs_dir == root / "logs"
    assert files.tmp_root == root / "tmp"
    assert files.bridge_profile_dir == root / "cdp-bridge-profile"
    assert files.asr_models_dir == root / "asr-models"


def test_subdir_names_are_configurable(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path / "data", media_subdir="作品", manifests_subdir="清单")
    assert storage.media_root == tmp_path / "data" / "作品"
    assert storage.manifests_dir == tmp_path / "data" / "清单"


def test_ensure_dirs_creates_the_skeleton_and_is_idempotent(files: FileStorage) -> None:
    """V1 §4.1 的结论：那棵树不需要人手工建，首次运行自己长出来。"""
    files.ensure_dirs()
    files.ensure_dirs()  # 第二次不能炸
    for directory in (
        files.root,
        files.media_root,
        files.manifests_dir,
        files.cookies_dir,
        files.logs_dir,
        files.tmp_root,
    ):
        assert directory.is_dir()


def test_ensure_dirs_does_not_create_bridge_profile_or_asr_models(files: FileStorage) -> None:
    """这两个不是"空目录就能用"的东西：一个是 Chrome 自己建的 profile，
    一个是要人下载的 233 MB 权重。预建空目录只会让预检误判成"已就位"。"""
    files.ensure_dirs()
    assert not files.bridge_profile_dir.exists()
    assert not files.asr_models_dir.exists()


# ---------------------------------------------------------------------------
# 媒体目录
# ---------------------------------------------------------------------------


def test_media_dir_layout(files: FileStorage) -> None:
    directory = files.media_dir("douyin", CREATOR, VIDEO_ID, TITLE)
    assert directory == files.media_root / "douyin" / CREATOR / f"{VIDEO_ID}-{TITLE}"
    assert directory.relative_to(files.media_root).parts == (
        "douyin",
        CREATOR,
        f"{VIDEO_ID}-{TITLE}",
    )


def test_media_dir_prefixes_the_video_id_so_titles_cannot_collide(files: FileStorage) -> None:
    """**V1 §7.8**。`"测试:上"` 与 `"测试_上"` 净化后是同一个名字，
    没有 video_id 前缀就是"第二条作品的媒体覆盖第一条"。"""
    first = files.media_dir("douyin", CREATOR, "7000000000000000001", "测试:上")
    second = files.media_dir("douyin", CREATOR, "7000000000000000002", "测试_上")
    assert first.name != second.name
    assert first.parent == second.parent  # 同一博主，只差 leaf


def test_media_dir_is_platform_scoped(files: FileStorage) -> None:
    """`platform_video_id` 只在**平台内**唯一。抖音与 B站 撞号时要靠外层目录隔开。"""
    douyin = files.media_dir("douyin", CREATOR, "123456789", "同标题")
    bilibili = files.media_dir("bilibili", "某UP", "123456789", "同标题")
    assert douyin != bilibili
    assert douyin.relative_to(files.media_root).parts[0] == "douyin"


def test_media_dir_sanitizes_every_external_input(files: FileStorage) -> None:
    """昵称与标题都是外部输入。`../../etc/passwd` 当昵称不能穿越出 `media/`。"""
    directory = files.media_dir("douyin", "../../etc", "1", "../../passwd")
    assert ".." not in directory.relative_to(files.media_root).as_posix().split("/")
    assert directory.is_relative_to(files.media_root)


def test_media_dir_keeps_chinese_readable(files: FileStorage) -> None:
    """不做 unicode 转义、不剥中文 —— 目录名给人看的（V1 §6 实测 `姜胡说` 比 sec_uid 友好）。"""
    directory = files.media_dir("douyin", CREATOR, "1", "姜胡说的第 12 期")
    assert CREATOR in directory.parts
    assert "姜胡说的第 12 期" in directory.name


def test_media_dir_truncates_a_long_title(files: FileStorage) -> None:
    """Windows 单个路径段上限 255，加上前缀很容易超。"""
    directory = files.media_dir("douyin", CREATOR, "1", "标" * 300)
    assert len(directory.name) <= len("1-") + 60
    assert len(directory.parent.name) <= 60


def test_media_file_cover_and_metadata_share_one_dir(files: FileStorage) -> None:
    media = files.media_dir("douyin", CREATOR, VIDEO_ID, TITLE)
    assert files.media_file("douyin", CREATOR, VIDEO_ID, TITLE) == media / "media.mp4"
    assert files.cover_file("douyin", CREATOR, VIDEO_ID, TITLE) == media / "cover.jpg"
    assert files.metadata_file("douyin", CREATOR, VIDEO_ID, TITLE) == media / "metadata.json"


def test_dash_part_file_matches_yt_dlp_naming(files: FileStorage) -> None:
    """**V1 §7.21**：合并失败时留下 `media.f137.mp4` + `media.f140.m4a`。
    命名与 yt-dlp 自己的产物一致，后处理才不需要区分是谁写的。"""
    media = files.media_dir("bilibili", "某UP", "BV1cSec6tEux", "标题")
    video = files.dash_part_file(
        "bilibili", "某UP", "BV1cSec6tEux", "标题", format_id="137", ext="mp4"
    )
    audio = files.dash_part_file(
        "bilibili", "某UP", "BV1cSec6tEux", "标题", format_id="140", ext="m4a"
    )
    assert video == media / "media.f137.mp4"
    assert audio == media / "media.f140.m4a"


def test_dash_part_file_strips_a_leading_dot_from_the_ext(files: FileStorage) -> None:
    """`.m4a` 与 `m4a` 都要落到同一个名字，否则同一轨会出现两个文件。"""
    with_dot = files.dash_part_file("bilibili", "某UP", "BV1", "标题", format_id="140", ext=".m4a")
    without_dot = files.dash_part_file(
        "bilibili", "某UP", "BV1", "标题", format_id="140", ext="m4a"
    )
    assert with_dot == without_dot
    assert with_dot.name == "media.f140.m4a"


# ---------------------------------------------------------------------------
# 口播稿
# ---------------------------------------------------------------------------


def test_transcript_path_is_identical_across_platforms(files: FileStorage) -> None:
    """**V1 §7.5 的结构性解决**。

    V1 里三份 `postprocess_*.py` 各写各的目录、启动器与前端按平台分支去读，
    改错一处就读不到稿子，而"顺手统一"被明确禁止（两边都有消费者）。
    V2 只有这一个答案：`<media_dir>/transcript/speech-clean.txt`。
    """
    douyin = files.media_dir("douyin", CREATOR, VIDEO_ID, TITLE)
    bilibili = files.media_dir("bilibili", "某UP", "BV1cSec6tEux", "B站标题")

    dy = files.transcript_path(douyin)
    bili = files.transcript_path(bilibili)

    assert dy.parent.name == bili.parent.name == "transcript"
    assert dy.name == bili.name == "speech-clean.txt"
    assert dy == douyin / "transcript" / "speech-clean.txt"


def test_segments_path_sits_beside_the_transcript(files: FileStorage) -> None:
    media = files.media_dir("douyin", CREATOR, VIDEO_ID, TITLE)
    segments = files.segments_path(media)
    assert segments == files.transcript_dir(media) / "segments.json"
    assert segments.parent == files.transcript_path(media).parent


def test_audio_dir_is_not_under_transcript(files: FileStorage) -> None:
    """**V1 §7.21**：自己产出的 `postprocess/audio/part-001.m4a` 曾被当成源媒体、
    一条作品转两遍。中间产物必须放在源媒体扫描**不会**去看的地方。"""
    media = files.media_dir("bilibili", "某UP", "BV1", "标题")
    audio = files.audio_dir(media)
    assert audio == media / "audio"
    assert audio != files.transcript_dir(media)
    # 后处理扫的是媒体目录下的 *.mp4/*.m4a；audio 是它的兄弟目录，
    # 所以媒体目录本身不会把 audio 里的文件当源媒体。
    assert audio.parent == media


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def test_manifest_path_uses_a_utc_stamp(files: FileStorage) -> None:
    """文件名要能排序。V1 用本地时间，V2 的清单还要和 DB 里 UTC 的
    `written_at` 对齐 —— 两套时区并存是自找麻烦。"""
    started = datetime(2026, 9, 22, 5, 56, 26, tzinfo=UTC)
    path = files.manifest_path(started, "douyin_collect", "task-1")
    assert path == files.manifests_dir / "20260922-055626-douyin_collect-task-1.json"


def test_two_tasks_started_in_the_same_second_get_different_files(files: FileStorage) -> None:
    """**这条是防数据覆盖的，不是防难看**。

    只带"秒 + kind"的话，`all_platforms`（故意并行起跑）会让后一份
    `os.replace` 静默盖掉前一份：DB 两条索引指向同一个文件，
    前一个任务的审计凭据消失且不报错。
    """
    same = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
    first = files.manifest_path(same, "douyin_collect", "aaaaaaaa-1111")
    second = files.manifest_path(same, "douyin_collect", "bbbbbbbb-2222")
    assert first != second


def test_manifest_path_sorts_lexically_like_it_sorts_chronologically(files: FileStorage) -> None:
    earlier = files.manifest_path(datetime(2026, 9, 22, 9, 0, 0, tzinfo=UTC), "douyin", "t1")
    later = files.manifest_path(datetime(2026, 12, 1, 8, 0, 0, tzinfo=UTC), "douyin", "t2")
    assert earlier.name < later.name


def test_manifest_path_sanitizes_the_kind(files: FileStorage) -> None:
    """`kind` 现在来自代码内的白名单，过 `safe_filename` 是防"哪天它来自请求体"。

    判据是**目录没变 + 只剩一个文件名段**（`../../escape` 净化成 `.._.._escape`，
    残留的两个点是名字的一部分，不是"上一级"）。
    """
    path = files.manifest_path(datetime(2026, 1, 1, tzinfo=UTC), "../../escape", "t")
    assert path.parent == files.manifests_dir
    assert len(path.name.split("/")) == 1
    assert "\\" not in path.name


def test_manifest_path_drops_the_timezone_offset(files: FileStorage) -> None:
    """只取年月日时分秒。带 `+08:00` 的名字既不能排序也不好看。"""
    path = files.manifest_path(datetime(2026, 9, 22, 13, 56, 26, tzinfo=UTC), "douyin", "t")
    assert "+" not in path.name and "T" not in path.name


# ---------------------------------------------------------------------------
# cookie
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("douyin.com", "douyin.com.txt"),
        ("www.douyin.com", "www.douyin.com.txt"),
        ("https://www.douyin.com/", "www.douyin.com.txt"),
        ("https://space.bilibili.com/12345/video", "space.bilibili.com.txt"),
        ("bilibili.com:443", "bilibili.com.txt"),
        ("Bilibili.COM", "bilibili.com.txt"),
        ("  douyin.com  ", "douyin.com.txt"),
        ("douyin.com.", "douyin.com.txt"),
    ],
)
def test_cookies_path_accepts_a_url_and_keeps_only_the_host(
    files: FileStorage, given: str, expected: str
) -> None:
    """静默生成 `cookies/https:__www.douyin.com_user_x.txt` 比报错难查得多。"""
    assert files.cookies_path(given) == files.cookies_dir / expected


@pytest.mark.parametrize("given", ["", "   ", "not a domain", "http://", "localhost", "douyin"])
def test_cookies_path_rejects_anything_that_is_not_a_host(files: FileStorage, given: str) -> None:
    with pytest.raises(ValueError, match="域名"):
        files.cookies_path(given)


def test_cookies_path_is_one_file_per_platform(files: FileStorage) -> None:
    """V1 §7.3：Windows 上 yt-dlp 读不了 Chrome 的 cookie 库，唯一稳定路径就是这个导出文件。
    三家各一份，不能共用。"""
    paths = {
        files.cookies_path("douyin.com"),
        files.cookies_path("bilibili.com"),
        files.cookies_path("xiaohongshu.com"),
    }
    assert len(paths) == 3
    assert all(p.parent == files.cookies_dir for p in paths)


# ---------------------------------------------------------------------------
# 临时目录与日志
# ---------------------------------------------------------------------------


def test_tmp_dir_has_no_side_effects_by_default(files: FileStorage) -> None:
    """算路径不该有副作用 —— 顺手 mkdir 会让一个拼错的 task_id 留一个空目录。"""
    path = files.tmp_dir("task-1")
    assert path == files.tmp_root / "task-1"
    assert not path.exists()


def test_tmp_dir_create_makes_the_directory(files: FileStorage) -> None:
    path = files.tmp_dir("task-1", create=True)
    assert path.is_dir()
    assert files.tmp_dir("task-1", create=True).is_dir()  # 幂等


def test_tmp_dir_is_scoped_per_task(files: FileStorage) -> None:
    """两个任务同时跑时不能共用临时目录 —— 一个任务收尾删目录会把另一个的中间产物带走。"""
    assert files.tmp_dir("a") != files.tmp_dir("b")


def test_log_file_defaults_to_server_log(files: FileStorage) -> None:
    assert files.log_file() == files.logs_dir / "server.log"
    assert files.log_file("collector.log") == files.logs_dir / "collector.log"


# ---------------------------------------------------------------------------
# rel / abs
# ---------------------------------------------------------------------------


def test_rel_produces_a_posix_path_relative_to_data(files: FileStorage, tmp_path: Path) -> None:
    """**存相对路径**：整个数据目录要能搬走、备份、换机器。"""
    target = files.media_file("douyin", CREATOR, VIDEO_ID, TITLE)
    relative = files.rel(target)
    assert relative.startswith("media/")
    assert "\\" not in relative
    assert not Path(relative).is_absolute()
    assert relative.count(tmp_path.name) == 0


def test_rel_roundtrips_through_abs(files: FileStorage) -> None:
    target = files.transcript_path(files.media_dir("douyin", CREATOR, VIDEO_ID, TITLE))
    assert files.abs(files.rel(target)) == files.root / target.relative_to(files.root)


def test_rel_falls_back_to_absolute_outside_the_root(files: FileStorage, tmp_path: Path) -> None:
    """用户把媒体目录挂到别的盘是合法配置，为这个把整轮采集搞挂不值得。"""
    elsewhere = tmp_path / "other-mount" / "media.mp4"
    elsewhere.parent.mkdir(parents=True)
    relative = files.rel(elsewhere)
    assert Path(relative).is_absolute()
    assert files.abs(relative) == elsewhere.resolve()


def test_abs_returns_an_absolute_path_unchanged(files: FileStorage, tmp_path: Path) -> None:
    absolute = tmp_path / "somewhere" / "media.mp4"
    assert files.abs(absolute) == absolute


def test_abs_joins_a_relative_stored_value(files: FileStorage) -> None:
    assert files.abs("media/douyin/x/media.mp4") == files.media_root / "douyin" / "x" / "media.mp4"


def test_rel_works_on_a_string_input(files: FileStorage) -> None:
    """`rel()` 吃 `Path` 也吃 `str` —— 从 DB 里读回来的本来就是字符串。"""
    target = files.media_file("douyin", CREATOR, VIDEO_ID, TITLE)
    assert files.rel(str(target)) == files.rel(target)


def test_rel_of_a_string_read_back_from_the_db_roundtrips(files: FileStorage) -> None:
    """采集器把 `rel()` 写进 DB，下一次读回来是 `str`，再 `abs()` 要能落回同一个文件。"""
    original = files.metadata_file("bilibili", "某UP", "BV1cSec6tEux", "标题")
    stored = files.rel(original)
    assert files.abs(files.rel(Path(stored))) == files.abs(Path(stored))
    assert files.abs(stored) == files.root / stored


def test_paths_do_not_require_the_directories_to_exist(files: FileStorage) -> None:
    """`FileStorage` 只算路径。采集器在建目录之前就要能拿到目标路径写进清单。"""
    media = files.media_dir("douyin", CREATOR, VIDEO_ID, TITLE)
    assert not media.exists()
    assert files.transcript_path(media).name == "speech-clean.txt"
