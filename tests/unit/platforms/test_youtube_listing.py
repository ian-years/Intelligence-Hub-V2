"""YouTube 的枚举解析与 **时间窗**（V1 `--recent-days` 的语义）。

`tests/fixtures/youtube/flat_playlist.jsonl` 是**合成样本**（本机到 YouTube 不通，
没有真抓取），判据与"它挡不住哪一类回归"写在那个目录的 `README.md` 里。
本文件里凡是"照真输出形状写"的地方都用**关系**表达（谁进谁不出、两份解析器必须一致），
而不是把样本数量当结论。

最深的那一组是时间窗：`since` 传与不传、日期缺失、正好压在边界上、
`upload_date` 只有日期没有时分 —— 这四种各对应一个**后果不同的判法**。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.infra import subprocess as infra_subprocess
from intelligence_hub_v2.infra import ytdlp as infra_ytdlp
from intelligence_hub_v2.infra.ytdlp import (
    FLAT_PLAYLIST_DUMP_FLAG,
    YtDlpRunner,
    plan_cookie_variants,
)
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.platforms.bilibili import listing as bili_listing
from intelligence_hub_v2.platforms.youtube import listing

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "youtube"
CHANNEL_ID = "UCKC8SZ0cdXPnnkKkQQLJmsQ"

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def flat(video_id: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": video_id, "type": "video", "title": f"作品 {video_id}"}
    payload.update(fields)
    return payload


def ids(videos: Sequence[listing.FlatVideo]) -> set[str]:
    return {video.video_id for video in videos}


# =========================================================================== #
# 自证：fixture 必须真的能被本模块吃下
# =========================================================================== #


class TestFixtureIsActuallyTheShapeTheParserReads:
    """这几条不测行为，测的是"我照着结构造的东西有没有落空"。"""

    def test_the_fixture_is_line_oriented_not_one_object(self) -> None:
        raw = fixture_text("flat_playlist.jsonl")
        lines = [line for line in raw.splitlines() if line.strip()]
        assert len(lines) >= 4, "fixture 缩成一条就没有'逐行'这件事可测了"
        # 整段 json.loads 必须**失败**：那正是 `-J` 与 `-j` 的分界
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
        assert len(listing.parse_dump_json_lines(raw)) == len(lines)

    def test_the_fixture_carries_all_three_date_shapes_on_purpose(self) -> None:
        """三种日期形态（timestamp / upload_date / 都没有）必须都在，
        否则'日期缺失怎么办'那条判据只是我想出来的一句话，没有任何东西钉着。"""
        entries = listing.parse_dump_json_lines(fixture_text("flat_playlist.jsonl"))
        videos, rejected = listing.to_videos(entries)
        assert len(videos) + len(rejected) == len(entries), "有既没进也没被点名的条目"
        dated = [v for v in videos if v.published_at is not None]
        undated = [v for v in videos if v.published_at is None]
        assert len(dated) >= 2
        assert len(undated) >= 1
        # `upload_date` 那一条必须落在 UTC 零点（V1 是本地零点，差别见 docstring）
        by_date = next(v for v in videos if v.video_id == "bQw4w9WgXc1")
        assert by_date.published_at == datetime(2026, 9, 10, tzinfo=UTC)


# =========================================================================== #
# `-j` 这个 flag：命令行与解析器共用同一份契约常量
# =========================================================================== #


class TestFlatPlaylistFlagAndParserShareOneContract:
    """2026-09-24 的 P0 形状：argv 写 `-J`、解析器吃 `-j`，两边各自绿、B站 枚举恒空。

    所以这里把三件事钉死在**同一条用例**里：
    真 `YtDlpRunner` 构造出的 argv 含 `FLAT_PLAYLIST_DUMP_FLAG`；
    该 flag 的输出形状（逐行）能被 YouTube 解析器抽出条目；
    而另一个 flag（`-J`）的输出形状喂进来必须**抽不出**条目。
    少任何一角，这条都不算数。
    """

    @staticmethod
    async def _capture(argv: Sequence[str], **_kw: Any) -> infra_subprocess.SubprocessResult:
        return infra_subprocess.SubprocessResult(
            argv=tuple(str(part) for part in argv),
            returncode=0,
            stdout=fixture_text("flat_playlist.jsonl"),
            stderr="",
            duration_seconds=0.1,
        )

    async def test_argv_and_parser_agree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        del tmp_path
        monkeypatch.setattr(infra_ytdlp, "run_subprocess", self._capture)
        ladder = plan_cookie_variants(("none",))
        runner = YtDlpRunner(default_variants=ladder, timeout_seconds=30.0)
        result = await runner.flat_playlist(
            f"https://www.youtube.com/channel/{CHANNEL_ID}/videos", variants=ladder
        )
        argv = list(result.stdout.splitlines())  # 只为了确认假件真的回了 fixture
        assert argv and argv[0].startswith("{")

        entries = listing.parse_dump_json_lines(result.stdout)
        videos, _rejected = listing.to_videos(entries)
        assert videos, "解析器吃不下自己那条命令的输出形状"

    async def test_the_real_runner_puts_the_shared_flag_on_the_command_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, ...]] = []

        async def spy(argv: Sequence[str], **_kw: Any) -> infra_subprocess.SubprocessResult:
            captured.append(tuple(str(part) for part in argv))
            return infra_subprocess.SubprocessResult(
                argv=tuple(argv), returncode=0, stdout="", stderr="", duration_seconds=0.0
            )

        monkeypatch.setattr(infra_ytdlp, "run_subprocess", spy)
        ladder = plan_cookie_variants(("none",))
        await YtDlpRunner(default_variants=ladder).flat_playlist(
            "https://www.youtube.com/channel/x/videos", variants=ladder, playlist_items=9
        )
        assert len(captured) == 1
        argv = list(captured[0])
        assert "--flat-playlist" in argv
        assert FLAT_PLAYLIST_DUMP_FLAG in argv, "命令行没带上那份契约常量"
        assert "--playlist-items" in argv and "1:9" in argv
        # `-J` 与 `-j` 不能同时出现：那是一份 argv 里两种 dump 形状
        assert "-J" not in argv and "--dump-single-json" not in argv

    def test_the_single_object_shape_yields_no_usable_entry_when_parsed(self) -> None:
        """`-J` 的真实形状（整体一个对象，`entries` 是数组）喂进来会怎样。

        逐行解析器**会**把它当成一行认下来（它确实是一个以 `{` 开头的 JSON 对象），
        所以真正挡在门口的是条目层：那个对象的 `id` 是**播放列表/频道的 ID**
        （`UC…`，24 位），不是 11 位的作品 ID。两侧都要钉住：
        少了形状闸，B站 那条 P0 的"枚举恒空"在这里就会变成"库里多一条孤儿"。
        """
        single = json.dumps(
            {"id": CHANNEL_ID, "entries": [flat("aQw4w9WgXc0"), flat("bQw4w9WgXc1")]},
            ensure_ascii=False,
        )
        assert listing.entry_to_video(json.loads(single)) is None, "整体对象不是作品"
        kept, rejected = listing.to_videos(listing.parse_dump_json_lines(single))
        assert kept == []
        assert len(rejected) == 1 and CHANNEL_ID in rejected[0]

    def test_debug_and_info_lines_do_not_break_the_batch(self) -> None:
        noise = (
            "[debug] User config cmd args: []\n"
            "[info] [youtube:tab] Extracting URL: https://www.youtube.com/@x/videos\n"
            + fixture_text("flat_playlist.jsonl")
        )
        assert len(listing.parse_dump_json_lines(noise)) >= 4


# =========================================================================== #
# 反漂移：与 B站 那份同名解析器必须一致
# =========================================================================== #


@pytest.mark.parametrize(
    "stdout",
    [
        fixture_text("flat_playlist.jsonl"),
        "",
        "\n\n   \n",
        "not json at all\n",
        "{broken\n",
        '{"id":"x"} trailing junk\n',
        '["an", "array"]\n{"id":"kept"}\n',
        '{"nested":{"a":1}}\n[1,2,3]\n"a string"\nnull\n',
    ],
)
def test_the_two_flat_playlist_parsers_still_agree(stdout: str) -> None:
    """`youtube.listing` 与 `bilibili.listing` 里各有一份 `parse_dump_json_lines`。

    重复是已知的（正解是提到 `infra/ytdlp.py`，那次要连着改 B站 的引用）。
    在这一格把它做掉不值当，但**不许它漂**：同一批样本喂两份，结果必须逐字相等。
    """
    assert listing.parse_dump_json_lines(stdout) == bili_listing.parse_dump_json_lines(stdout)


# =========================================================================== #
# 条目 → FlatVideo
# =========================================================================== #


class TestEntryToVideo:
    def test_non_entries_are_dropped_and_named(self) -> None:
        for entry in (None, "a string", 7, [], {"type": "playlist", "id": "x"}):
            video = listing.entry_to_video(entry)
            assert video is None
        _kept, rejected = listing.to_videos([{"type": "playlist", "id": "x"}])
        assert rejected and "playlist" in rejected[0]

    def test_missing_type_is_treated_as_a_video(self) -> None:
        """V1 的判据是 `item.get("type") or "video"`。判紧的代价是**整位博主空手**：
        单条作品的 dump 从来不带 `type`。"""
        assert listing.entry_to_video(flat("aQw4w9WgXc0")) is not None
        assert listing.entry_to_video({"id": "aQw4w9WgXc0"}) is not None

    def test_relative_urls_are_upgraded_and_absurd_ones_fall_back_to_the_id(self) -> None:
        relative = listing.entry_to_video(flat("aQw4w9WgXc0", url="/watch?v=aQw4w9WgXc0"))
        assert relative is not None
        assert relative.webpage_url.startswith("https://")
        bogus = listing.entry_to_video(flat("aQw4w9WgXc0", url="javascript:alert(1)"))
        assert bogus is not None
        assert bogus.webpage_url == "https://www.youtube.com/watch?v=aQw4w9WgXc0"

    def test_title_never_comes_out_empty(self) -> None:
        """空标题在前端是"这条作品没有名字"，而 id 至少能让人认出是数据缺了。"""
        video = listing.entry_to_video({"id": "aQw4w9WgXc0", "title": "   "})
        assert video is not None and video.title == "aQw4w9WgXc0"

    def test_missing_fields_stay_none_instead_of_becoming_zero(self) -> None:
        video = listing.entry_to_video({"id": "aQw4w9WgXc0"})
        assert video is not None
        assert video.duration_seconds is None
        assert video.view_count is None
        assert video.published_at is None
        assert not video.has_date

    def test_negative_or_absurd_numbers_are_not_read_as_measurements(self) -> None:
        video = listing.entry_to_video(flat("aQw4w9WgXc0", duration="abc", view_count=-5))
        assert video is not None
        assert video.duration_seconds is None
        assert video.view_count is None

    def test_duplicates_are_collapsed_and_named(self) -> None:
        kept, rejected = listing.to_videos(
            [flat("aQw4w9WgXc0"), flat("aQw4w9WgXc0"), flat("bQw4w9WgXc1")]
        )
        assert ids(kept) == {"aQw4w9WgXc0", "bQw4w9WgXc1"}
        assert len(rejected) == 1 and "重复" in rejected[0]


# =========================================================================== #
# 时间形态
# =========================================================================== #


class TestTimestamps:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1790064000, datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)),
            ("1790064000", datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)),
            (1790064000.5, datetime(2026, 9, 22, 8, 0, 0, 500000, tzinfo=UTC)),
            ("20260922", datetime(2026, 9, 22, tzinfo=UTC)),
            ("2026-09-22 08:00:00", datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)),
        ],
    )
    def test_the_four_accepted_shapes(self, value: Any, expected: datetime) -> None:
        got = listing.timestamp_to_utc(value)
        assert got == expected
        assert got is not None and got.tzinfo is UTC, "naive 时间进库 = 每台机器读出一个结果"

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            True,
            False,
            "不是时间",
            "2026-09-22",
            "202609",  # 6 位：不是 YYYYMMDD
            "20261301",  # 月份不合法
            "1758",  # 4 位：epoch 至少要 9 位
            "1758585600000",  # 13 位毫秒 —— **故意不认**，见 docstring
            0,
        ],
    )
    def test_anything_else_is_none_not_a_guess(self, value: Any) -> None:
        assert listing.timestamp_to_utc(value) is None

    def test_a_millisecond_stamp_is_undated_rather_than_forever_in_window(self) -> None:
        """这一格是"两种判法后果不同"的样本：把 13 位当秒会得出公元五万年，
        于是**所有**条目都窗内，时间窗整个失效而没有任何一条用例会红。

        字符串形态被"9~11 位"那道闸挡掉，整数形态被 `fromtimestamp` 的范围检查挡掉
        （Windows 上是 `OSError`，别的平台是 `OverflowError`）—— 两条都必须落到 None。
        """
        assert listing.timestamp_to_utc("1790064000000") is None
        assert listing.timestamp_to_utc(1790064000000) is None
        assert listing.timestamp_to_utc(-1) is None, "负数不是'公元前一分钟'，是没给时间"

    def test_absurd_epoch_does_not_blow_up_the_batch(self) -> None:
        assert listing.timestamp_to_utc(10**18) is None


# =========================================================================== #
# 时间窗（V1 `--recent-days` 的语义本体）
# =========================================================================== #


class TestWithinWindow:
    @staticmethod
    def corpus(now: datetime) -> list[listing.FlatVideo]:
        """newest-first，覆盖三种日期形态。顺序即 yt-dlp 给的顺序。"""
        return [
            listing.FlatVideo(video_id="newest", title="t", webpage_url="u", published_at=now),
            listing.FlatVideo(
                video_id="edge",
                title="t",
                webpage_url="u",
                published_at=now - timedelta(days=7),
            ),
            listing.FlatVideo(
                video_id="just-out",
                title="t",
                webpage_url="u",
                published_at=now - timedelta(days=7, seconds=1),
            ),
            listing.FlatVideo(
                video_id="oldest",
                title="t",
                webpage_url="u",
                published_at=now - timedelta(days=400),
            ),
            listing.FlatVideo(video_id="undated-a", title="t", webpage_url="u"),
            listing.FlatVideo(video_id="undated-b", title="t", webpage_url="u"),
        ]

    def test_the_window_is_exactly_n_days_and_the_boundary_is_inclusive(self) -> None:
        since = listing.days_to_since(7, now=NOW)
        outcome = listing.within_window(self.corpus(NOW), since=since, limit=100)
        kept = ids(outcome.selected)
        # 关系：`>= since` 的进、`< since` 的出，**由条目自己的时间决定而不是我数出来的名单**
        assert {"newest", "edge"} <= kept
        assert "just-out" not in kept and "oldest" not in kept
        assert since == NOW - timedelta(days=7)

    def test_undated_entries_are_kept_and_counted_not_dropped(self) -> None:
        """V1 那句"按原顺序取用（未编造时间）"。判"缺失即丢"的后果是整位博主空手。"""
        outcome = listing.within_window(self.corpus(NOW), since=NOW - timedelta(days=7), limit=100)
        assert {"undated-a", "undated-b"} <= ids(outcome.selected)
        assert outcome.undated == 2
        assert outcome.older == 2

    def test_no_filter_reports_zero_because_it_judged_nothing(self) -> None:
        outcome = listing.within_window(self.corpus(NOW), since=None, limit=3)
        assert ids(outcome.selected) == {"newest", "edge", "just-out"}
        assert outcome.undated == 0 and outcome.older == 0

    def test_limit_is_a_ceiling_and_stops_scanning_once_full(self) -> None:
        outcome = listing.within_window(self.corpus(NOW), since=NOW - timedelta(days=7), limit=1)
        assert len(outcome.selected) <= 1
        # 第一条就够：窗内第一条是 newest
        assert ids(outcome.selected) == {"newest"}
        big = listing.within_window(self.corpus(NOW), since=None, limit=0)
        assert big.selected == ()

    def test_empty_input_is_not_reported_as_fresh_but_empty(self) -> None:
        outcome = listing.within_window((), since=NOW - timedelta(days=7), limit=5)
        assert outcome.selected == ()
        assert outcome.older == 0 and outcome.undated == 0
        assert outcome.nothing_new is False, "一条都没喂进来 ≠ '窗内没有新的'"

    def test_nothing_new_only_when_something_was_actually_excluded(self) -> None:
        all_out = listing.within_window(
            [
                listing.FlatVideo("a", "t", "u", published_at=NOW - timedelta(days=400)),
                listing.FlatVideo("b", "t", "u", published_at=NOW - timedelta(days=500)),
            ],
            since=NOW - timedelta(days=7),
            limit=5,
        )
        assert all_out.nothing_new is True
        only_undated = listing.within_window(
            [listing.FlatVideo("a", "t", "u")], since=NOW - timedelta(days=7), limit=5
        )
        assert only_undated.nothing_new is False, "全是缺日期 ≠ '确实没有新作'，文案不一样"

    def test_a_naive_since_is_refused_before_it_becomes_a_typeerror(self) -> None:
        naive = NOW.replace(tzinfo=None)
        with pytest.raises(ValueError, match="必须带时区"):
            listing.within_window(self.corpus(NOW), since=naive, limit=5)

    def test_days_to_since_is_the_only_arithmetic_behind_the_window(self) -> None:
        assert listing.days_to_since(1, now=NOW) == NOW - timedelta(days=1)
        assert listing.days_to_since(90, now=NOW) == NOW - timedelta(days=90)
        with pytest.raises(ValueError, match="days 必须"):
            listing.days_to_since(0)
        with pytest.raises(ValueError, match="必须带时区"):
            listing.days_to_since(7, now=NOW.replace(tzinfo=None))

    def test_the_default_now_is_aware_so_the_real_call_site_cannot_go_naive(self) -> None:
        got = listing.days_to_since(3)
        assert got.tzinfo is not None and got.tzinfo.utcoffset(got) is not None


# =========================================================================== #
# 枚举预算 / VideoMeta 装配 / 身份反查
# =========================================================================== #


class TestScanBudgetAndMeta:
    @pytest.mark.parametrize("limit", [1, 5, 30])
    def test_unfiltered_asks_for_exactly_the_limit(self, limit: int) -> None:
        assert listing.scan_budget(limit, filtered=False) == limit

    def test_filtered_asks_for_more_but_is_capped(self) -> None:
        assert listing.scan_budget(5, filtered=True) == 15
        assert listing.scan_budget(40, filtered=True) == 50
        for limit in (1, 3, 20, 200):
            budget = listing.scan_budget(limit, filtered=True)
            assert listing._SCAN_CEILING >= budget >= min(listing._SCAN_CEILING, limit)

    def test_zero_or_negative_limit_still_produces_a_usable_range(self) -> None:
        """`--playlist-items 1:0` 是一个不存在的范围，yt-dlp 会沉默地一条不给。"""
        assert listing.scan_budget(0, filtered=False) >= 1
        assert listing.scan_budget(-5, filtered=True) >= 1

    def test_video_meta_is_well_formed_and_keeps_the_requested_identity(self) -> None:
        ref = CreatorRef(
            platform="youtube",
            platform_id=CHANNEL_ID,
            profile_url=f"https://www.youtube.com/channel/{CHANNEL_ID}",
        )
        video = listing.FlatVideo(
            video_id="aQw4w9WgXc0",
            title="标题",
            webpage_url="/watch?v=aQw4w9WgXc0",
            channel_id="UCanotherchannel00000000000",
            channel="别人",
        )
        meta = listing.video_meta_for(video, ref=ref)
        assert meta.platform == "youtube"
        assert meta.creator_ref.platform_id == CHANNEL_ID, "页面自报的频道不许改写入库身份"
        assert meta.extra["channel_matches_creator"] is False
        assert str(meta.webpage_url).startswith("http")
        assert meta.duration_seconds is None

    def test_channel_id_from_entries_needs_one_unambiguous_id(self) -> None:
        assert (
            listing.channel_id_from_entries(
                [flat("a", channel_id=CHANNEL_ID), flat("b", channel_id=CHANNEL_ID)]
            )
            == CHANNEL_ID
        )
        assert listing.channel_id_from_entries([flat("a"), flat("b")]) == ""
        assert (
            listing.channel_id_from_entries(
                [flat("a", channel_id=CHANNEL_ID), flat("b", channel_id="UCzzzzzzzzzzzzzzzzzzzzzz")]
            )
            == ""
        ), "跨频道播放列表里挑第一个当身份 = 把 A 的作品挂到 B 名下"
        assert listing.channel_id_from_entries([flat("a", channel_id="@handle")]) == ""
        assert listing.channel_id_from_entries(["不是对象"]) == ""
