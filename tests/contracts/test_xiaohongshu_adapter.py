"""小红书 Adapter 契约测试。**深水区在"身份"与"图文不是失败"这两族**。

全 mock：不碰网络、不碰浏览器、不碰真二进制。

- 桥用 `tests/contracts/_doubles.py:FakeBridge`，回的是
  `tests/fixtures/xiaohongshu/*.json` —— **照实现结构手工合成的样本，未与现网核对**，
  它挡不住哪一类回归写在那个目录的 `README.md` 里，这里不重复。
- HTTP 用 `httpx.MockTransport`：CDN 403 与"几百字节错误页"那两条实测结论
  在假响应里复现，真链路 `make test-real` 才碰。
- yt-dlp 换掉 `adapter._ytdlp_runner()` 这个替换点。

与抖音那份文件的分工：跨平台都成立的通用契约在 `test_platform_adapter.py` 的
`PlatformAdapterContractTests`（本平台的子类是 `TestXiaohongshuContract`，在文件末尾），
这里只收小红书独有的判据。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.contracts._doubles import FakeBridge, FakeYtDlpRunner

from intelligence_hub_v2.errors import (
    BridgeError,
    ListError,
    MediaDownloadError,
    PlatformError,
)
from intelligence_hub_v2.infra.cdp_bridge import BridgeHealth
from intelligence_hub_v2.infra.cookies import COOKIE_FILE_HEADER, CookieManager
from intelligence_hub_v2.infra.pacing import RatePacer
from intelligence_hub_v2.infra.ytdlp import (
    YtDlpCookieVariant,
    YtDlpResult,
    YtDlpRunner,
    progress_from_ytdlp_line,
)
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.video import VideoDraft, VideoMeta
from intelligence_hub_v2.platforms.base import AdapterDeps
from intelligence_hub_v2.platforms.bilibili.config import BilibiliConfig
from intelligence_hub_v2.platforms.registry import PLATFORM_CONFIG_SCHEMAS, PLATFORMS
from intelligence_hub_v2.platforms.urls import parse_cn_count
from intelligence_hub_v2.platforms.xiaohongshu import adapter as adapter_module
from intelligence_hub_v2.platforms.xiaohongshu import detail as detail_module
from intelligence_hub_v2.platforms.xiaohongshu import listing, media
from intelligence_hub_v2.platforms.xiaohongshu.adapter import (
    LIKES_LOOKAHEAD_CARDS,
    XiaohongshuAdapter,
)
from intelligence_hub_v2.platforms.xiaohongshu.config import XiaohongshuConfig
from intelligence_hub_v2.platforms.xiaohongshu.detail import (
    NOTE_DETAIL_PAGE_FUNCTION,
    SEARCH_USER_PAGE_FUNCTION,
    fetch_note_detail,
    fetch_search_candidates,
    parse_detail_payload,
    parse_search_payload,
    render_detail_js,
    render_search_js,
    search_creators_by_name,
)
from intelligence_hub_v2.platforms.xiaohongshu.listing import (
    CARD_HELPERS,
    COLLECT_AFTER_SCROLL_FUNCTION,
    PROFILE_PAGE_FUNCTION,
    NoteCard,
    PageBudget,
    card_to_video_meta,
    decode_page_result,
    find_placeholders,
    is_image_note,
    is_placeholder_nickname,
    merge_note_cards,
    parse_cards_payload,
    parse_profile_payload,
    render_page_js,
    sort_cards,
)
from intelligence_hub_v2.platforms.xiaohongshu.urls import build_note_url
from intelligence_hub_v2.storage.files import FileStorage
from intelligence_hub_v2.tasks.collect import build_video_draft

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "xiaohongshu"

USER_ID = "5fb1a2c30000000000100001"
OTHER_USER_ID = "5fb1a2c30000000000100099"
NOTE_1 = "665f1c7e0000000000104a01"
NOTE_2 = "665f1c7f0000000000104a02"
NOTE_3 = "665f1c80000000000104a03"
NOTE_4 = "665f1c810000000000104a04"
PLATFORM_NAME = "xiaohongshu"
TOKEN_1 = "AB2oYh3kQ8dXp0vMfaBm5c8Jg0KgY="
TOKEN_2 = "AB3pZi4lR9eYq1wN/2HcYUp1Ld4="

_PROFILE_KEYS = ("display_name", "media_strategy", "list_strategy", "use_cdp_bridge")


def page_payload(name: str, *, as_json_string: bool = True) -> Any:
    """读一份页函数会返回的模拟结果（默认给字符串，因为页函数真的 stringify）。"""
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return json.dumps(payload, ensure_ascii=False) if as_json_string else payload


def fast_config(**overrides: object) -> XiaohongshuConfig:
    """出厂配置 + 把限速闸拧到 practically 不等（用例里等不起 2 秒）。"""
    payload: dict[str, object] = {
        "display_name": "小红书",
        "rate_limit": {"per_minute": 100_000, "per_creator_seconds": 0.0},
        **overrides,
    }
    return XiaohongshuConfig.model_validate(payload)


def advanced_config(**advanced: object) -> XiaohongshuConfig:
    return fast_config(advanced=dict(advanced))


def refusing_client() -> httpx.AsyncClient:
    """任何请求都会炸的客户端：忘了传 `http=` 的用例必须当场红，而不是"运气好过了"。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(_refuse))


def _refuse(request: httpx.Request) -> httpx.Response:
    msg = f"契约测试发出了真请求：{request.method} {request.url}"
    raise AssertionError(msg)


def collecting_client(
    routes: Sequence[tuple[str, httpx.Response]],
) -> tuple[httpx.AsyncClient, list[str]]:
    """按 URL 子串匹配的假 CDN，外加它收到的请求列表。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        for needle, response in routes:
            if needle in url:
                return response
        msg = f"没有为 {url!r} 配路由；已配 {[needle for needle, _ in routes]}"
        raise AssertionError(msg)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def make_deps(
    tmp_path: Path,
    *,
    config: XiaohongshuConfig | None = None,
    bridge: Any = ...,
    http: httpx.AsyncClient | None = None,
) -> AdapterDeps:
    return AdapterDeps(
        config=config or fast_config(),
        # 适配器不碰库、不发事件（模块 docstring 第 1 条），所以这两样故意给假的。
        storage=None,  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        http=http or refusing_client(),
        logger=get_logger("test.xiaohongshu"),
        cookies=CookieManager(FileStorage(tmp_path / "data")),
        bridge=None if bridge is None else (FakeBridge() if bridge is ... else bridge),
    )


def fast_budget() -> PageBudget:
    """所有轮询/滚动预算都压到 1 轮 0 毫秒：契约测试不该等十几秒。"""
    return PageBudget(
        max_scroll_rounds=1,
        settle_ms=0,
        search_poll_rounds=1,
        search_poll_ms=0,
        detail_poll_rounds=1,
        detail_poll_ms=0,
    )


def make_adapter(
    tmp_path: Path,
    *,
    config: XiaohongshuConfig | None = None,
    bridge: Any = ...,
    http: httpx.AsyncClient | None = None,
) -> XiaohongshuAdapter:
    adapter = XiaohongshuAdapter(
        config or fast_config(), make_deps(tmp_path, config=config, bridge=bridge, http=http)
    )
    adapter.page_budget = fast_budget()
    adapter.direct_budget_seconds = 5.0
    adapter.ytdlp_budget_seconds = 5.0
    return adapter


def profile_ref() -> CreatorRef:
    return CreatorRef(
        platform="xiaohongshu",
        platform_id=USER_ID,
        profile_url=f"https://www.xiaohongshu.com/user/profile/{USER_ID}",
    )


def make_video(note_id: str = NOTE_1, *, token: str = TOKEN_1) -> VideoMeta:
    return VideoMeta(
        platform="xiaohongshu",
        platform_video_id=note_id,
        creator_ref=profile_ref(),
        title="如何用三秒抓住观众",
        webpage_url=build_note_url(note_id, token),
        extra={"note_id": note_id, "xsec_token": token},
    )


def ytdlp_ok(*, artifacts: Sequence[Path]) -> YtDlpResult:
    stdout = "".join(f"[download] Destination: {path}\n" for path in artifacts)
    return YtDlpResult(
        ok=True,
        variant=YtDlpCookieVariant("exported_file", (), "带导出的登录 cookie"),
        stdout=stdout,
        stderr="",
        returncode=0,
        artifacts=tuple(artifacts),
        attempts=(("带导出的登录 cookie", 0, ""),),
    )


def ytdlp_blocked() -> YtDlpResult:
    """V1 §7.2 的常态：小红书这一侧 yt-dlp 通常产不出片。"""
    return YtDlpResult(
        ok=False,
        variant=YtDlpCookieVariant("none", (), "匿名（登录档画质不可用）"),
        stdout="",
        stderr="ERROR: [xiaohongshu] unable to extract video url\n",
        returncode=1,
        attempts=(("带导出的登录 cookie", 1, "unable to extract"),),
    )


def note_card(note_id: str, **fields: str) -> NoteCard:
    """一张卡片，缺的字段就是"这一半边没给"。"""
    return NoteCard(note_id=note_id, **fields)


async def collect(
    adapter: XiaohongshuAdapter, *, limit: int = 30, since: Any = None
) -> list[VideoMeta]:
    out: list[VideoMeta] = []
    async for meta in adapter.list_creator_videos(profile_ref(), since=since, limit=limit):
        out.append(meta)
    return out


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 防空转看护：下面这些参数化的枚举值一旦被误删，pytest 会**零收集且静默通过**。
# --------------------------------------------------------------------------- #

PROFILE_FIXTURES = [
    "profile_page.json",
    "scroll_page.json",
    "profile_page_user_id_mismatch.json",
    "profile_page_self_reported_other_uid.json",
    "profile_page_login_wall.json",
    "profile_page_user_not_found.json",
    "profile_page_empty_ok.json",
    "detail_video_note.json",
    "detail_image_note.json",
    "detail_note_id_mismatch.json",
    "detail_self_reported_other_id.json",
    "detail_login_wall.json",
    "detail_empty_note.json",
    "search_candidates.json",
    "search_login_wall.json",
    "search_no_candidates.json",
]

PAGE_TEMPLATES = [
    ("PROFILE_PAGE_FUNCTION", PROFILE_PAGE_FUNCTION),
    ("COLLECT_AFTER_SCROLL_FUNCTION", COLLECT_AFTER_SCROLL_FUNCTION),
    ("NOTE_DETAIL_PAGE_FUNCTION", NOTE_DETAIL_PAGE_FUNCTION),
    ("SEARCH_USER_PAGE_FUNCTION", SEARCH_USER_PAGE_FUNCTION),
]


class TestNoVacuousGuards:
    """参数化不许为空 —— 本仓库出现过两次的看护形状。"""

    def test_every_declared_fixture_exists_and_parses(self) -> None:
        assert PROFILE_FIXTURES, "fixture 清单被清空了，参数化会静默零收集"
        missing = [name for name in PROFILE_FIXTURES if not (FIXTURES / name).is_file()]
        assert not missing, f"少了 fixture：{missing}"
        for name in PROFILE_FIXTURES:
            assert isinstance(json.loads((FIXTURES / name).read_text(encoding="utf-8")), dict)

    def test_every_page_template_is_still_a_page_template(self) -> None:
        assert PAGE_TEMPLATES, "模板清单被清空了"
        for label, template in PAGE_TEMPLATES:
            assert "async () =>" in template, f"{label} 不再是页函数的形状"
            assert "window.__INITIAL_STATE__" in template or "location." in template, label


# =========================================================================== #
# 主页解析：身份、登录墙、占位昵称
# =========================================================================== #


class TestParseProfilePayload:
    def test_happy_path_carries_the_creators_own_identity(self) -> None:
        parsed = parse_profile_payload(
            page_payload("profile_page.json", as_json_string=False), expected_user_id=USER_ID
        )
        assert parsed.user_id == USER_ID
        assert parsed.nickname == "影视飓风"
        assert parsed.nickname_is_placeholder is False
        # "217.5万" → 2175000，而不是 0、也不是 None、也不是 217。
        assert parsed.follower_count == 2_175_000
        assert parsed.to_profile(profile_ref()).follower_count == 2_175_000

    def test_avatar_without_a_protocol_is_repaired_not_dropped(self) -> None:
        parsed = parse_profile_payload(
            page_payload("profile_page.json", as_json_string=False), expected_user_id=USER_ID
        )
        profile = parsed.to_profile(profile_ref())
        assert profile.avatar_url is not None
        assert str(profile.avatar_url).startswith("https://")

    def test_user_id_mismatch_discards_the_whole_page(self) -> None:
        """打开 A 主页被重定向到 B：整条丢弃，**不许**退回页面自报的 uid。"""
        payload = page_payload("profile_page_user_id_mismatch.json", as_json_string=False)
        assert payload["actual"] == OTHER_USER_ID
        with pytest.raises(PlatformError) as caught:
            parse_profile_payload(payload, expected_user_id=USER_ID, stage="list")
        message = str(caught.value)
        assert USER_ID in message and OTHER_USER_ID in message
        # 若退回页面自报的 uid，这里拿到的会是 ParsedProfile(user_id=OTHER) 而不是异常。
        assert not isinstance(caught.value, MediaDownloadError)

    def test_stage_decides_whether_the_scheduler_skips_or_the_task_goes_red(self) -> None:
        """`list` 要 `ListError`（这一位跳过），其它 stage 要单条红。同一个 payload。"""
        payload = page_payload("profile_page_user_id_mismatch.json", as_json_string=False)
        with pytest.raises(ListError):
            parse_profile_payload(payload, expected_user_id=USER_ID, stage="list")
        with pytest.raises(PlatformError) as caught:
            parse_profile_payload(payload, expected_user_id=USER_ID, stage="profile")
        assert not isinstance(caught.value, ListError)

    def test_a_bypassed_page_guard_still_cannot_reassign_the_notes(self) -> None:
        """页函数那道闸被绕过（`ok:true` 却自报别人的 uid）时的第二道闸。

        这一条是上一条的第二种走法：`parse_profile_payload` 拿到的是
        "看起来成功"的 payload，退回 `claimed` 就等于把 B 的笔记登记给 A。
        """
        payload = page_payload("profile_page_self_reported_other_uid.json", as_json_string=False)
        assert payload["ok"] is True, "前提：这份 payload 长得像成功"
        with pytest.raises(PlatformError) as caught:
            parse_profile_payload(payload, expected_user_id=USER_ID)
        assert OTHER_USER_ID in str(caught.value)
        assert "串号" in str(caught.value)

    def test_login_wall_and_user_not_found_are_two_different_reds(self) -> None:
        """**这一条最值钱**：混起来会让人去改一个本来没错的链接，而要做的只有扫码。"""
        wall = parse_error_of("profile_page_login_wall.json")
        gone = parse_error_of("profile_page_user_not_found.json")
        assert str(wall) != str(gone), "两种失败合成一句话，等于让人猜"
        assert "扫码" in str(wall)
        assert "查无此人" in str(gone)
        # 「扫码解决不了」这句话必须出现在查无此人那一侧，而不是只有登录墙那侧提到扫码。
        assert "扫码解决不了" in str(gone)
        assert "过期" in str(wall)

    def test_zero_notes_is_not_an_error(self) -> None:
        """小红书真有零笔记的账号：卡片为空**不是**失败（与抖音那侧口径不同）。"""
        parsed = parse_profile_payload(
            page_payload("profile_page_empty_ok.json", as_json_string=False),
            expected_user_id=USER_ID,
        )
        assert parsed.cards == ()
        assert parsed.nickname == "一个没发过笔记的号"

    def test_unknown_page_error_keeps_the_stopped_at_url(self) -> None:
        payload = {"ok": False, "error": "captcha", "page_url": "https://www.xiaohongshu.com/x"}
        with pytest.raises(PlatformError) as caught:
            parse_profile_payload(payload, expected_user_id=USER_ID)
        assert "captcha" in str(caught.value)
        assert "https://www.xiaohongshu.com/x" in str(caught.value), "不带原文就得重放才能看到"


def parse_error_of(fixture: str, *, expected_user_id: str = USER_ID) -> PlatformError:
    """跑一次主页解析，把那条红捞出来。"""
    with pytest.raises(PlatformError) as caught:
        parse_profile_payload(
            page_payload(fixture, as_json_string=False), expected_user_id=expected_user_id
        )
    return caught.value


class TestPlaceholderNicknames:
    @pytest.mark.parametrize(
        "name",
        ["", "   ", "小红书创作者", "小红书用户", "用户", USER_ID, "https://xhslink.com/a/AbC"],
    )
    def test_these_never_overwrite_a_real_name(self, name: str) -> None:
        assert is_placeholder_nickname(name) is True

    @pytest.mark.parametrize("name", ["影视飓风", "小A", "小红书搬运工二号"])
    def test_these_are_real_names(self, name: str) -> None:
        assert is_placeholder_nickname(name) is False

    def test_the_placeholder_flag_reaches_the_persistence_layer(self) -> None:
        """判据只在 `extra` 里留痕，覆盖与否由入库层决定（V1 §7.24）。"""
        payload = page_payload("profile_page_login_wall.json", as_json_string=False)
        payload["login_wall"] = False
        parsed = parse_profile_payload(payload, expected_user_id=USER_ID)
        assert parsed.nickname == "小红书创作者"
        profile = parsed.to_profile(profile_ref())
        assert profile.extra["nickname_is_placeholder"] is True
        assert profile.name == "小红书创作者"

    def test_parametrised_lists_are_not_empty(self) -> None:
        assert len(PROFILE_FIXTURES) >= 12
        assert USER_ID not in {"影视飓风", "小A"}


# =========================================================================== #
# 卡片：两条来路互补合并
# =========================================================================== #


class TestCardHarvestAndMerge:
    def test_non_note_id_rows_are_never_cards(self) -> None:
        """推荐位 / 广告位长得像卡片，但不是笔记：宁可不收。"""
        profile = parse_profile_payload(
            page_payload("profile_page.json", as_json_string=False), expected_user_id=USER_ID
        )
        assert {c.note_id for c in profile.cards} == {NOTE_1, NOTE_2, NOTE_3}
        assert all(len(c.note_id) >= 18 for c in profile.cards)

    def test_duplicate_watermark_keeps_the_first_row(self) -> None:
        """同一条笔记被水合扫到两次：留第一条，第二行的标题不许串进来。"""
        profile = parse_profile_payload(
            page_payload("profile_page.json", as_json_string=False), expected_user_id=USER_ID
        )
        first = next(c for c in profile.cards if c.note_id == NOTE_1)
        assert first.title == "如何用三秒抓住观众"
        assert "异步水合" not in str([c.title for c in profile.cards])

    def test_two_halves_merge_into_cards_with_nothing_missing(self) -> None:
        """**互补合并**：DOM 那半边有标题，状态树那半边有 token 与类型。

        断言写成关系：对两条来路**都有**的那批 ID，合并后每个字段都非空。
        覆盖式合并会让 token 变空（详情跳风控页），丢弃式会让类型变空（图文被当视频）。
        """
        primary = list(profile_cards())
        extra = list(parse_cards_payload(page_payload("scroll_page.json", as_json_string=False)))
        shared = {c.note_id for c in primary} & {c.note_id for c in extra}
        assert shared, "前提没成立：两份 fixture 没有任何重叠 ID，这条用例就成了空转"
        merged = {c.note_id: c for c in merge_note_cards(primary, extra)}
        assert shared <= set(merged)
        for note_id in sorted(shared):
            card = merged[note_id]
            filled = {
                "title": card.title,
                "likes": card.likes,
                "cover_url": card.cover_url,
                "xsec_token": card.xsec_token,
                "note_type": card.note_type,
            }
            assert all(filled.values()), (
                f"{note_id} 合完缺了 {sorted(k for k, v in filled.items() if not v)}"
            )

    def test_merge_is_not_a_blanket_overwrite(self) -> None:
        """两边**都有值**时先到先得；只有"这一边空着"才从另一边补。

        这条是上面那条的补集：两份 fixture 的字段刚好错开，所以"整条覆盖"那种改法
        在互补合并那条用例里活得下来 —— 这里让同一个字段两边都非空、且取值不同。
        """
        primary = [note_card(NOTE_1, title="页面给的标题", likes="1.2万", note_type="video")]
        extra = [
            note_card(
                NOTE_1, title="状态树那一份", likes="", xsec_token=TOKEN_1, note_type="normal"
            )
        ]
        merged = merge_note_cards(primary, extra)
        assert len(merged) == 1
        assert merged[0].title == "页面给的标题", "后到的不许覆盖先到的"
        assert merged[0].note_type == "video", "类型被覆盖 = 图文被判成视频 = 白跑一场 yt-dlp"
        assert merged[0].likes == "1.2万", "空值不许抹掉已有值"
        assert merged[0].xsec_token == TOKEN_1, "缺的还是要补上"

    def test_page_order_survives_the_merge(self) -> None:
        """顺序是"新在最前"，日更采集靠它：primary 在前，extra 里新的排后面。"""
        primary = [note_card(NOTE_1), note_card(NOTE_3)]
        extra = [note_card(NOTE_4), note_card(NOTE_1)]
        assert [c.note_id for c in merge_note_cards(primary, extra)] == [
            NOTE_1,
            NOTE_3,
            NOTE_4,
        ]

    def test_scroll_payload_cards_are_parsed_the_same_way(self) -> None:
        extra = parse_cards_payload(page_payload("scroll_page.json", as_json_string=False))
        assert {c.note_id for c in extra} == {NOTE_1, NOTE_2, NOTE_4}


def profile_cards() -> tuple[NoteCard, ...]:
    return parse_profile_payload(
        page_payload("profile_page.json", as_json_string=False), expected_user_id=USER_ID
    ).cards


# =========================================================================== #
# 顺序、图文分流、`xsec_token` 的位置
# =========================================================================== #


class TestSortAndTypeFilter:
    def test_likes_order_is_by_value_and_missing_metrics_last_not_dropped(self) -> None:
        """缺指标的卡片排最后但**不消失**：直接 int() 会让一位博主整轮红掉。

        "真 0 赞"必须排在"读不出赞数"**之前** —— 那两档的差别就是 `(1, v)`/`(0, 0)`
        两级键存在的全部理由，只测"缺的排最后"抓不到把两档对调的改法。
        """
        cards = [
            note_card(NOTE_1, likes="3456"),
            note_card(NOTE_2, likes="1.2万"),
            note_card(NOTE_3, likes="赞"),
            note_card(NOTE_4, likes="0"),
        ]
        ordered = sort_cards(cards, "likes")
        assert [c.note_id for c in ordered] == [NOTE_2, NOTE_1, NOTE_4, NOTE_3]
        assert len(ordered) == len(cards), "排序不许丢条目"
        assert parse_cn_count("赞") is None and parse_cn_count("0") == 0, "前提：两档真的能分开"

    def test_equal_likes_keep_the_pages_own_order(self) -> None:
        """稳定排序：同赞的那批页面给的是发布时间倒序，我们不该发明第二个顺序。"""
        cards = [note_card(NOTE_1, likes="10"), note_card(NOTE_2, likes="10")]
        assert [c.note_id for c in sort_cards(cards, "likes")] == [NOTE_1, NOTE_2]

    @pytest.mark.parametrize("sort_by", ["page_order", "anything-else", ""])
    def test_anything_but_likes_is_a_no_op(self, sort_by: str) -> None:
        cards = [note_card(NOTE_2, likes="1"), note_card(NOTE_1, likes="999")]
        assert sort_cards(cards, sort_by) == tuple(cards)

    def test_only_an_explicit_normal_counts_as_an_image_note(self) -> None:
        """空串既可能是图文也可能是"没渲染出来"：判不了就放行（V1 同一口径）。"""
        assert is_image_note("normal") is True
        assert is_image_note("video") is False
        assert is_image_note("") is False
        assert is_image_note(None) is False
        assert is_image_note(" NORMAL ") is False, "大小写不是我们发明的取值"

    def test_card_url_is_a_ticket_not_an_identity(self) -> None:
        """ADR-0016：token 只出现在 query 上，`platform_video_id` 里没有它。"""
        card = note_card(NOTE_1, xsec_token=TOKEN_2)
        assert "AB3pZi4lR9eYq1wN" in card.url
        meta = card_to_video_meta(card, ref=profile_ref())
        assert meta.platform_video_id == NOTE_1
        assert TOKEN_1 not in meta.platform_video_id
        assert meta.extra["xsec_token"] == TOKEN_2
        # token 里的 `/` `=` 必须转义，否则 query 会被截断成另一条链接。
        assert "%2F" in meta.webpage_url.path or "%2F" in str(meta.webpage_url)

    def test_a_card_without_a_token_still_yields_a_usable_url(self) -> None:
        meta = card_to_video_meta(note_card(NOTE_1), ref=profile_ref())
        assert str(meta.webpage_url) == f"https://www.xiaohongshu.com/explore/{NOTE_1}"

    def test_published_at_comes_from_the_note_id_and_is_aware(self) -> None:
        """小红书的 `since` 是真过滤（与抖音相反）：note_id 前 8 位十六进制就是时间。"""
        meta = card_to_video_meta(note_card(NOTE_1), ref=profile_ref())
        assert meta.published_at is not None
        assert meta.published_at.tzinfo is not None
        assert meta.extra["published_at_source"] == "note_id"

    def test_a_note_id_that_is_not_a_timestamp_gives_no_time(self) -> None:
        """前 8 位不像时间的 ID 交回 None，而不是 1970 年那一条安静的前排。"""
        meta = card_to_video_meta(note_card("zzzzzzzz0000000000ab"), ref=profile_ref())
        assert meta.published_at is None


# =========================================================================== #
# 页面 JS 的渲染纪律（V1 §2 契约三 + §7.13 那一族）
# =========================================================================== #


class TestPageJsRendering:
    def test_no_placeholder_survives_rendering(self) -> None:
        injected = PageBudget().placeholder_values()
        for label, template in PAGE_TEMPLATES:
            rendered = render_page_js(template, budget=fast_budget(), user_id=USER_ID)
            left = [name for name in injected if name in rendered]
            assert not left, f"{label} 里还剩没替换的占位符：{left}"

    def test_an_unknown_placeholder_raises_instead_of_reaching_the_page(self) -> None:
        """漏一个占位符 = 页面 `ReferenceError`，而 Python 只看到"evaluate 失败"。"""
        with pytest.raises(ValueError, match="未替换的占位符"):
            render_page_js("async () => __NEW_THING__;", budget=fast_budget())

    def test_the_initial_state_name_is_exempt_from_the_leftover_check(self) -> None:
        """`__INITIAL_STATE__` 长得就是一个占位符 —— 不豁免的话每份正常模板都误报。"""
        assert "__INITIAL_STATE__" in PROFILE_PAGE_FUNCTION
        assert "__INITIAL_STATE__" not in find_placeholders(*[t for _, t in PAGE_TEMPLATES])
        render_page_js(PROFILE_PAGE_FUNCTION, budget=fast_budget(), user_id=USER_ID)

    def test_every_injected_name_has_a_value(self) -> None:
        """模板加了占位符、`placeholder_values()` 得跟着加 —— 渲染之前先问一句。"""
        used = find_placeholders(*[t for _, t in PAGE_TEMPLATES])
        declared = set(PageBudget().placeholder_values())
        assert used <= declared, f"模板要的是 {sorted(used - declared)}，那张表里没有"

    @pytest.mark.parametrize(
        ("user_id", "note_id", "wanted"),
        [(USER_ID, NOTE_1, 30), ("", "", None), ('5fb/quote"', "665f1c7e", 1)],
    )
    def test_values_are_injected_with_their_own_quotes(
        self, user_id: str, note_id: str, wanted: int | None
    ) -> None:
        """模板里**不带引号**，注入值自带：写成 `"__X__"` 会拼成 `""5fb…""` → SyntaxError。

        判据写成"注入点后面正好是 json 那一份字面量"，不是"整份模板里没有空串"——
        模板里本来就有 JS 的 `""`，那种文本扫描会永远红。
        """
        profile = render_page_js(
            PROFILE_PAGE_FUNCTION, budget=fast_budget(), user_id=user_id, wanted=wanted
        )
        detail = render_page_js(NOTE_DETAIL_PAGE_FUNCTION, budget=fast_budget(), note_id=note_id)
        assert f"const expected = {json.dumps(user_id)};" in profile
        assert f"const expected = {json.dumps(note_id)};" in detail
        assert f"const expected = {json.dumps(json.dumps(user_id))};" not in profile

    def test_the_anchor_contract_survives_in_both_harvesters(self) -> None:
        """**源码级看护，不是行为看护**：必须读 `anchor.href` 而不是属性。

        属性上是相对路径（`/user/profile/xxx`），带域名的正则一条都匹配不到，
        于是"0 条卡片"，而 0 条卡片长得和"这个博主没发过笔记"一模一样。
        fixture 挡不住这一条（见 `tests/fixtures/xiaohongshu/README.md`），所以在这里钉文本。
        """
        for label, template in (
            ("CARD_HELPERS", CARD_HELPERS),
            ("SEARCH", SEARCH_USER_PAGE_FUNCTION),
        ):
            assert re.search(r"anchor\.href\s*\|\|\s*anchor\.getAttribute", template), (
                f"{label} 里 anchor.href 那一手不见了：改回 getAttribute('href') 会收到 0 条"
            )

    def test_the_search_page_readiness_signal_is_pc_search(self) -> None:
        """就绪判据必须是 `xsec_source=pc_search`，不是"页面上有 /user/profile/ 链接"。

        页头「我」的头像链接一进页就在，用宽松那条会立刻通过、然后收下 0 个候选。
        """
        assert "xsec_source=pc_search" in SEARCH_USER_PAGE_FUNCTION
        assert "a[href*='/user/profile/']" in SEARCH_USER_PAGE_FUNCTION
        # 宽松那一档只在候选为 0 时兜底，所以它必须在严格筛**之后**才出现。
        assert SEARCH_USER_PAGE_FUNCTION.index("xsec_source=pc_search") < (
            SEARCH_USER_PAGE_FUNCTION.index("if (!candidates.size) {")
        )

    def test_the_creator_identity_reads_user_page_data_not_the_logged_in_user(self) -> None:
        """`user.userInfo` 是「当前登录的人」：拿错那一份 → 全程绿灯、零条作品。"""
        assert "unwrap(user.userPageData)" in PROFILE_PAGE_FUNCTION
        assert "unwrap(user.userInfo)" not in PROFILE_PAGE_FUNCTION, (
            "userInfo 是当前登录的人，拿它当博主会让每个正常主页都被判成串号"
        )

    def test_budgets_reach_the_templates(self) -> None:
        budget = PageBudget(max_scroll_rounds=3, settle_ms=7, detail_poll_rounds=2)
        rendered = render_page_js(COLLECT_AFTER_SCROLL_FUNCTION, budget=budget, wanted=9)
        assert "const rounds = 3;" in rendered
        assert "const settle = 7;" in rendered
        assert "const wanted = 9;" in rendered
        assert render_detail_js(note_id=NOTE_1, budget=budget).count("const rounds = 2;") == 1
        assert "const rounds = 12;" in render_search_js(budget=PageBudget())


# =========================================================================== #
# 桥回的东西不合法时
# =========================================================================== #


class TestDecodePageResult:
    def test_a_json_string_is_the_normal_shape(self) -> None:
        payload = decode_page_result('{"ok": true}', stage="list")
        assert payload == {"ok": True}

    def test_a_dict_from_the_bridge_is_accepted_too(self) -> None:
        assert decode_page_result({"ok": True}, stage="media") == {"ok": True}

    def test_unparseable_text_keeps_the_original_snippet(self) -> None:
        with pytest.raises(ListError, match="不是合法 JSON"):
            decode_page_result("<html>登录</html>", stage="list")

    @pytest.mark.parametrize("junk", [None, 42, ["a"], True])
    def test_non_object_results_raise_with_their_type(self, junk: object) -> None:
        with pytest.raises(PlatformError, match="未返回可解析结果"):
            decode_page_result(junk, stage="profile")

    def test_stage_is_carried_into_the_error(self) -> None:
        with pytest.raises(ListError):
            decode_page_result("nope", stage="list")
        with pytest.raises(PlatformError) as caught:
            decode_page_result("nope", stage="media")
        assert caught.value.stage == "media"
        assert caught.value.platform == "xiaohongshu"


class TestPacingPlumbing:
    def test_rate_pacer_is_still_built_from_the_shipped_default(self, tmp_path: Path) -> None:
        """`fast_config()` 把闸门拧开了，所以专门用出厂值钉一句"闸门还在"。"""
        config = XiaohongshuConfig.model_validate({"display_name": "小红书"})
        instance = make_adapter(tmp_path, config=config)
        assert isinstance(instance._pacer, RatePacer)
        assert config.rate_limit.per_minute == 30
        assert config.rate_limit.per_creator_seconds == 1.0


# =========================================================================== #
# 笔记详情：串号、登录墙 / 本身没内容、时间
# =========================================================================== #


class TestParseDetailPayload:
    def test_a_video_note_is_a_video_note(self) -> None:
        detail = parse_detail_payload(
            page_payload("detail_video_note.json", as_json_string=False), expected_note_id=NOTE_1
        )
        assert detail.note_id == NOTE_1
        assert detail.title == "如何用三秒抓住观众"
        assert detail.author_id == USER_ID
        assert detail.has_video is True
        assert detail.is_image_note is False
        assert detail.duration_seconds == 59.0
        assert detail.like_count == 12_000
        assert detail.collect_count == 321
        assert detail.comment_count == 45
        assert detail.tags == ("剪辑", "摄影"), "空 tag 不占位"

    def test_the_millis_timestamp_becomes_an_aware_datetime(self) -> None:
        """毫秒串是页面最常见的那一种；裸时间戳按本机时区解释（ADR-0016）。"""
        detail = parse_detail_payload(
            page_payload("detail_video_note.json", as_json_string=False), expected_note_id=NOTE_1
        )
        assert detail.published_at is not None
        assert detail.published_at.tzinfo is not None
        assert detail.published_at.timestamp() == pytest.approx(1_717_509_246.0)

    def test_a_bare_date_string_is_also_aware(self) -> None:
        detail = parse_detail_payload(
            page_payload("detail_image_note.json", as_json_string=False), expected_note_id=NOTE_2
        )
        assert detail.published_at is not None
        assert detail.published_at.tzinfo is not None

    def test_no_time_at_all_falls_back_to_the_note_id(self) -> None:
        """两条来路：页面给的优先，给不出退到 note_id 前 8 位十六进制。"""
        payload = page_payload("detail_video_note.json", as_json_string=False)
        payload["published_at"] = ""
        detail = parse_detail_payload(payload, expected_note_id=NOTE_1)
        assert detail.published_at is not None
        assert detail.published_at.timestamp() == pytest.approx(1_717_509_246.0)

    def test_an_image_note_is_not_a_failure(self) -> None:
        """ADR-0019：没有视频直链但有原图 = 图文，不是"这条失败了"。"""
        detail = parse_detail_payload(
            page_payload("detail_image_note.json", as_json_string=False), expected_note_id=NOTE_2
        )
        assert detail.has_video is False
        assert detail.is_image_note is True
        assert len(detail.images) == 4

    def test_has_video_and_is_image_note_partition_the_payload(self) -> None:
        """两个属性互斥且穷尽，所以 `download_media` 的分流不会掉进第三条缝里。"""
        for fixture, expected_id in (
            ("detail_video_note.json", NOTE_1),
            ("detail_image_note.json", NOTE_2),
        ):
            detail = parse_detail_payload(
                page_payload(fixture, as_json_string=False), expected_note_id=expected_id
            )
            assert detail.has_video != detail.is_image_note, fixture
            assert detail.has_video == bool(detail.video_url)
            assert detail.is_image_note == (bool(detail.images) and not detail.video_url)

    def test_a_hijacked_detail_page_is_discarded_whole(self) -> None:
        """**note_id 串号**：被重定向到了别的笔记，整条丢弃。

        退回页面自报的 ID 继续 = 把别人的正文与图片记成这条笔记的，而库里两条相同
        `platform_video_id` 的作品会互相覆盖。
        """
        payload = page_payload("detail_note_id_mismatch.json", as_json_string=False)
        assert payload["actual"] == NOTE_2
        with pytest.raises(PlatformError) as caught:
            parse_detail_payload(payload, expected_note_id=NOTE_1)
        assert "串号" in str(caught.value)
        assert NOTE_1 in str(caught.value) and NOTE_2 in str(caught.value)
        assert caught.value.stage == "media", "详情失败的红归 media，不归 list"

    def test_a_bypassed_page_guard_still_drops_the_row(self) -> None:
        payload = page_payload("detail_self_reported_other_id.json", as_json_string=False)
        assert payload["ok"] is True, "前提：这份 payload 长得像成功"
        with pytest.raises(PlatformError, match="串号"):
            parse_detail_payload(payload, expected_note_id=NOTE_1)

    def test_login_wall_and_empty_note_are_two_different_reds(self) -> None:
        """「去扫码」与「这条笔记本身取不到」的修法完全不同。"""
        wall = detail_error_of("detail_login_wall.json")
        empty = detail_error_of("detail_empty_note.json")
        assert str(wall) != str(empty)
        assert "扫码" in str(wall) or "风控" in str(wall)
        assert "login_wall_or_removed" in str(wall)
        assert "empty_note" in str(empty)
        assert "扫码" not in str(empty) and "风控" not in str(empty)

    def test_duration_that_is_not_a_number_is_none_not_zero(self) -> None:
        payload = page_payload("detail_image_note.json", as_json_string=False)
        assert payload["duration_seconds"] == ""
        detail = parse_detail_payload(payload, expected_note_id=NOTE_2)
        assert detail.duration_seconds is None
        for bad in ("-1", "五十", None):
            payload["duration_seconds"] = bad
            got = parse_detail_payload(payload, expected_note_id=NOTE_2).duration_seconds
            assert got is None, bad

    def test_malformed_list_shapes_degrade_to_empty_not_to_a_raise(self) -> None:
        """形状漂了让**媒体那一步**去如实失败，别让"读详情"整体红。"""
        payload = page_payload("detail_video_note.json", as_json_string=False)
        payload["images"] = "https://a/b.jpg"
        payload["tags"] = {"name": "被包了一层"}
        detail = parse_detail_payload(payload, expected_note_id=NOTE_1)
        assert detail.images == ()
        assert detail.tags == ()
        assert detail.title, "本来就有正文有标题，不该因为 images 漂了而全红"


def detail_error_of(fixture: str, *, expected_note_id: str = NOTE_1) -> PlatformError:
    with pytest.raises(PlatformError) as caught:
        parse_detail_payload(
            page_payload(fixture, as_json_string=False), expected_note_id=expected_note_id
        )
    return caught.value


STRING_TUPLE_CASES: list[tuple[object, tuple[str, ...]]] = [
    ([" a ", "", "b"], ("a", "b")),
    ("astring", ()),
    (b"bytes", ()),
    (None, ()),
    ({"k": "v"}, ()),
    ([None, 1, ""], ("1",)),
]


class TestStringTupleHelper:
    """`detail._string_tuple` 今天**没有生产调用点**（`parse_detail_payload` 里内联了一份）。

    用例照写，但它是死代码这件事记在汇报里：不要因为它绿了就以为那条判据在被使用。
    """

    @pytest.mark.parametrize(("value", "expected"), STRING_TUPLE_CASES)
    def test_shapes(self, value: object, expected: tuple[str, ...]) -> None:
        assert detail_module._string_tuple(value) == expected

    def test_the_parametrised_table_is_not_empty(self) -> None:
        assert len(STRING_TUPLE_CASES) >= 5


class TestFetchNoteDetail:
    async def test_it_navigates_before_it_evaluates(self) -> None:
        """**少了那一次 navigate，表达式就在上一次停留的页面里执行** ——

        于是上一条笔记的 `noteDetailMap` 还在，`keys[0]` 拿到别人的笔记，而页面自报的
        note_id 恰好也对得上那条。本文件最贵的一种错，因为每行数据都"看起来自洽"。
        """
        bridge = FakeBridge(script=[page_payload("detail_video_note.json")])
        detail = await fetch_note_detail(
            bridge, note_id=NOTE_1, xsec_token=TOKEN_1, budget=fast_budget()
        )
        assert len(bridge.navigated) == 1 == len(bridge.evaluated)
        assert bridge.navigated[0].startswith(f"https://www.xiaohongshu.com/explore/{NOTE_1}")
        assert NOTE_1 in bridge.evaluated[0]
        assert detail.note_id == NOTE_1

    async def test_the_ticket_is_spent_on_the_navigation_only(self) -> None:
        """ADR-0016：`xsec_token` 只在取详情那一刻用。

        断言写成关系：门票出现在导航 URL 上，而 `NoteDetail` 身上**没有一个字段**装着它。
        """
        bridge = FakeBridge(script=[page_payload("detail_video_note.json")])
        detail = await fetch_note_detail(
            bridge, note_id=NOTE_1, xsec_token=TOKEN_1, budget=fast_budget()
        )
        assert TOKEN_1.split("=", maxsplit=1)[0] in bridge.navigated[0]
        own = {k: v for k, v in vars(detail).items() if k != "raw"}
        assert all(TOKEN_1 not in str(value) for value in own.values())
        assert "xsec_token" not in set(vars(detail))
        assert detail.note_id == NOTE_1, "身份是 note_id，不是那条带门票的链接"

    async def test_a_note_without_a_ticket_still_gets_a_url(self) -> None:
        bridge = FakeBridge(script=[page_payload("detail_video_note.json")])
        await fetch_note_detail(bridge, note_id=NOTE_1, budget=fast_budget())
        assert bridge.navigated == [f"https://www.xiaohongshu.com/explore/{NOTE_1}"]

    async def test_an_empty_note_id_is_refused_before_the_bridge_is_touched(self) -> None:
        bridge = FakeBridge(script=[])
        with pytest.raises(PlatformError, match="note_id"):
            await fetch_note_detail(bridge, note_id="", budget=fast_budget())
        assert bridge.navigated == [] and bridge.evaluated == []

    async def test_a_dict_from_the_bridge_is_accepted(self) -> None:
        bridge = FakeBridge(script=[page_payload("detail_video_note.json", as_json_string=False)])
        detail = await fetch_note_detail(bridge, note_id=NOTE_1, budget=fast_budget())
        assert detail.video_url


class TestSearchPayload:
    def test_candidates_are_deduped_by_user_id_in_page_order(self) -> None:
        found = parse_search_payload(page_payload("search_candidates.json", as_json_string=False))
        assert [c.user_id for c in found] == [USER_ID, OTHER_USER_ID]
        assert found[0].follower_count == 2_175_000
        assert found[1].name == ""

    def test_a_non_list_is_no_candidates(self) -> None:
        assert parse_search_payload({"candidates": "登录"}) == ()
        assert parse_search_payload({}) == ()

    def test_junk_rows_are_skipped_not_crashing(self) -> None:
        payload = {"candidates": [{"user_id": ""}, "not-a-dict", {"user_id": USER_ID}]}
        assert [c.user_id for c in parse_search_payload(payload)] == [USER_ID]


class TestSearchCreatorsByName:
    async def test_the_stored_profile_url_is_the_canonical_one(self, tmp_path: Path) -> None:
        """入库的是 `build_profile_url(user_id)`，不是搜索页那条带门票的链接。

        搜索页链接含 `xsec_token` 与 `xsec_source=pc_search` —— 存进去会让同一位博主被
        "搜索页链接"与"主页链接"认成两个人（V1 §7.1 换了个域名的重演）。
        """
        bridge = FakeBridge(script=[page_payload("search_candidates.json")])
        found = await search_creators_by_name(bridge, keyword="影视飓风", budget=fast_budget())
        assert found, "有候选就该交回候选，而不是红"
        assert TOKEN_1.split("=", maxsplit=1)[0] in next(
            c.homepage_url
            for c in parse_search_payload(
                page_payload("search_candidates.json", as_json_string=False)
            )
        ), "前提：搜索页给的那条链接确实带门票"
        for profile in found:
            assert str(profile.ref.profile_url) == (
                f"https://www.xiaohongshu.com/user/profile/{profile.ref.platform_id}"
            )
            assert "xsec_token" not in str(profile.ref.profile_url)
            assert profile.ref.platform_id in {USER_ID, OTHER_USER_ID}
        assert found[1].name == OTHER_USER_ID, "没昵称时用 ID 占位，并标注是占位"
        assert found[1].extra["nickname_is_placeholder"] is True

    async def test_login_wall_and_no_candidate_are_two_different_reds(self, tmp_path: Path) -> None:
        """V1 :902 那条注释的原话：混了会让人去改博主名字，而要做的只有扫码。"""
        wall = await search_error("search_login_wall.json")
        gone = await search_error("search_no_candidates.json")
        assert "扫码" in str(wall)
        assert "查无此人" not in str(wall)
        assert "扫码解决不了" in str(gone)
        assert "扫码登录" not in str(gone)

    async def test_an_empty_keyword_never_hits_the_bridge(self) -> None:
        bridge = FakeBridge(script=[])
        with pytest.raises(PlatformError, match="空串"):
            await search_creators_by_name(bridge, keyword="   ", budget=fast_budget())
        assert bridge.navigated == []

    async def test_the_general_page_is_tried_when_the_user_page_blows_up(self) -> None:
        """**单栏失败不掀整趟**，但那一趟的原文要留着。"""
        bridge = FakeBridge(
            script=[
                BridgeError(PLATFORM_NAME, "bridge", "桥断了"),
                page_payload("search_candidates.json"),
            ]
        )
        found, login_wall, last_error = await fetch_search_candidates(
            bridge, keyword="影视飓风", budget=fast_budget()
        )
        assert len(found) == 2
        assert login_wall is False
        assert last_error is None, "第二栏成了就不该带第一栏的错"
        assert "&type=51" in bridge.navigated[0] and "&type=1" in bridge.navigated[1]

    async def test_both_pages_failing_reports_the_original_text(self) -> None:
        bridge = FakeBridge(
            script=[
                BridgeError(PLATFORM_NAME, "bridge", "第一栏炸"),
                BridgeError(PLATFORM_NAME, "bridge", "第二栏炸"),
            ]
        )
        found, login_wall, last_error = await fetch_search_candidates(
            bridge, keyword="x", budget=fast_budget()
        )
        assert found == () and login_wall is False
        assert last_error is not None and "第二栏炸" in last_error
        with pytest.raises(PlatformError, match="第二栏炸"):
            await search_creators_by_name(
                FakeBridge(
                    script=[
                        BridgeError(PLATFORM_NAME, "bridge", "第一栏"),
                        BridgeError(PLATFORM_NAME, "bridge", "第二栏炸了"),
                    ]
                ),
                keyword="x",
                budget=fast_budget(),
            )

    async def test_keyword_is_percent_encoded_into_the_search_url(self) -> None:
        two_pages = [page_payload("search_no_candidates.json")] * 2
        bridge = FakeBridge(script=two_pages)
        await fetch_search_candidates(bridge, keyword="影视 飓风", budget=fast_budget())
        assert "%E5%BD%B1%E8%A7%86%20%E9%A3%93%E9%A3%8E" in bridge.navigated[0]

    async def test_login_wall_survives_a_second_page_that_has_candidates(self) -> None:
        """第一栏撞墙、第二栏有结果：候选要交出去，但那句"撞过墙"不能吞。"""
        bridge = FakeBridge(
            script=[page_payload("search_login_wall.json"), page_payload("search_candidates.json")]
        )
        found, login_wall, last_error = await fetch_search_candidates(
            bridge, keyword="x", budget=fast_budget()
        )
        assert found and login_wall is True and last_error is None


async def search_error(fixture: str) -> PlatformError:
    bridge = FakeBridge(script=[page_payload(fixture), page_payload(fixture)])
    with pytest.raises(PlatformError) as caught:
        await search_creators_by_name(bridge, keyword="影视飓风", budget=fast_budget())
    return caught.value


# =========================================================================== #
# cookie 档位阶梯（V1 §7.3 / §7.15 在小红书这一侧）
# =========================================================================== #


def write_cookie_file(path: Path, *, with_cookie: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        COOKIE_FILE_HEADER + ".xiaohongshu.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc\n"
        if with_cookie
        else COOKIE_FILE_HEADER
    )
    path.write_text(body, encoding="utf-8")
    return path


def ladder_for(
    tmp_path: Path,
    *,
    config: XiaohongshuConfig | None = None,
    environ: dict[str, str] | None = None,
) -> media.CookieLadder:
    instance = make_adapter(tmp_path, config=config)
    return media.resolve_cookie_ladder(
        XiaohongshuAdapter.capabilities.cookie_variants,
        config=instance._config,
        cookies=instance._deps.cookies,
        environ={} if environ is None else environ,
    )


class TestCookieLadder:
    def test_the_declared_order_is_what_the_ladder_uses(self) -> None:
        assert XiaohongshuAdapter.capabilities.cookie_variants == (
            "exported_file",
            "browser",
            "none",
        )
        assert media.XHS_COOKIE_DOMAIN == "xiaohongshu.com"

    def test_a_healthy_ladder_says_nothing(self, tmp_path: Path) -> None:
        """没问题时 `note` 必须是 None：预检页会直接显示这一行（V1 §7.20 的反面）。"""
        cookies = FileStorage(tmp_path / "data").cookies_path(media.XHS_COOKIE_DOMAIN)
        write_cookie_file(cookies)
        ladder = ladder_for(tmp_path)
        assert ladder.cookie_file == cookies
        assert ladder.rung_kinds == ("exported_file", "none")
        assert ladder.note is None
        assert ladder.browser is None

    def test_without_a_cookie_file_the_exported_rung_is_dropped_and_said(
        self, tmp_path: Path
    ) -> None:
        ladder = ladder_for(tmp_path)
        assert ladder.cookie_file is None
        assert ladder.rung_kinds == ("none",)
        assert ladder.note is not None
        assert "没有可用的导出 cookie" in ladder.note

    def test_a_header_only_file_is_not_a_rung_at_all(self, tmp_path: Path) -> None:
        """V1 §7.15 的另一半坑：只有表头的文件传出去 yt-dlp 一声不吭按匿名跑。"""
        empty = FileStorage(tmp_path / "data").cookies_path(media.XHS_COOKIE_DOMAIN)
        write_cookie_file(empty, with_cookie=False)
        ladder = ladder_for(tmp_path)
        assert ladder.cookie_file is None
        assert ladder.rung_kinds == ("none",)
        assert ladder.note is not None and "一条 cookie 都没有" in ladder.note

    def test_the_env_var_wins_over_the_config_and_says_so(self, tmp_path: Path) -> None:
        env_file = write_cookie_file(tmp_path / "env-cookies.txt")
        ignored = write_cookie_file(tmp_path / "config-cookies.txt")
        config = fast_config(cookies_file=ignored)
        ladder = ladder_for(
            tmp_path, config=config, environ={media.ENV_COOKIES_FILE: str(env_file)}
        )
        assert ladder.cookie_file == env_file
        assert ladder.note is not None
        assert "覆盖了配置里的 cookies_file" in ladder.note
        assert media.ENV_COOKIES_FILE in ladder.note

    def test_the_managed_path_is_the_last_candidate_not_the_first(self, tmp_path: Path) -> None:
        """配置里写了显式路径的人是在说"用这个"；`data/cookies/` 只是兜底。

        没有第三项的话，`cookies_file` 写相对路径、从别的 CWD 起服务时会静默退成匿名档。
        """
        managed = write_cookie_file(
            FileStorage(tmp_path / "data").cookies_path(media.XHS_COOKIE_DOMAIN)
        )
        assert ladder_for(tmp_path).cookie_file == managed
        missing = tmp_path / "nowhere.txt"
        ladder = ladder_for(tmp_path, config=fast_config(cookies_file=missing))
        assert ladder.cookie_file == managed, "约定路径必须接住"
        assert ladder.note is not None and str(missing) in ladder.note

    def test_the_browser_rung_is_off_by_default_and_warns_when_forced(self, tmp_path: Path) -> None:
        ladder = ladder_for(tmp_path, environ={media.ENV_COOKIES_FROM_BROWSER: "chrome"})
        assert ladder.browser == "chrome"
        assert "browser" in ladder.rung_kinds
        assert ladder.note is not None
        assert "7.3" in ladder.note or "Windows" in ladder.note, "那一档在 Windows 上基本读不出来"

    def test_config_browser_is_used_without_the_env_warning(self, tmp_path: Path) -> None:
        cookies = write_cookie_file(
            FileStorage(tmp_path / "data").cookies_path(media.XHS_COOKIE_DOMAIN)
        )
        ladder = ladder_for(tmp_path, config=fast_config(ytdlp_cookies_from_browser="edge"))
        assert ladder.browser == "edge"
        assert ladder.cookie_file == cookies
        assert ladder.note is None, "没走 env 就不该喊" + str(ladder.note)


class TestImageExtension:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://a/b.jpg", ".jpg"),
            ("https://a/b.jpeg", ".jpg"),
            ("https://a/b.JPG", ".jpg"),
            ("https://a/b.webp", ".webp"),
            ("https://a/b.gif", ".gif"),
            ("https://a/b.png?sign=1", ".png"),
            ("https://a/b.webp@200y.webp", ".webp"),
            ("https://a/b.jpg@h=100", ".jpg"),
            ("https://a/no-ext", ".jpg"),
            ("", ".jpg"),
        ],
    )
    def test_the_suffix_follows_the_url(self, url: str, expected: str) -> None:
        assert media.image_extension(url) == expected

    def test_the_scale_suffix_is_not_mistaken_for_a_extensionless_url(self) -> None:
        """`…jpg@…` 那一族：只看 endswith('.jpg') 会把 webp 存成 .jpg 后缀。"""
        assert media.image_extension("https://a/x!nd.webp@200y") == ".webp"
        assert media.image_extension("https://a/x.png?v=1") == ".png"


class TestProgressPlumbing:
    def test_none_callback_is_not_an_error(self) -> None:
        media.report_progress(None, 0.5)

    def test_the_fraction_is_clamped_into_zero_one(self) -> None:
        seen: list[float] = []
        media.report_progress(seen.append, -3.0)
        media.report_progress(seen.append, 42.0)
        assert seen == [0.0, 1.0]

    def test_a_broken_callback_does_not_turn_a_download_into_a_failure(self) -> None:
        """回调冒泡会造成"文件下好了但任务红了"，那是最难查的一种假失败。"""

        def boom(_fraction: float) -> None:
            msg = "调用方挂了"
            raise RuntimeError(msg)

        media.report_progress(boom, 0.5)

    def test_an_unrecognised_ytdlp_line_produces_no_event(self) -> None:
        seen: list[float] = []
        media.report_ytdlp_line(seen.append, "[download] 50.0% of 1.00MiB")
        media.report_ytdlp_line(seen.append, "[hlsnative] Downloading m3u8 manifest")
        assert len(seen) == 1

    def test_the_shared_parser_is_re_exported_not_reimplemented(self) -> None:
        assert media.progress_from_ytdlp_line is progress_from_ytdlp_line


# =========================================================================== #
# 落盘
# =========================================================================== #

IMAGE_SIZES = {
    "note/01.jpg": 1500,
    "note/02.webp": 2200,
    "note/03.jpeg": 3100,
    "note/04.png": 4000,
}
IMAGE_URLS = [
    f"https://sns-webpic-qc.xhscdn.com/note/{n}"
    for n in ("01.jpg", "02.webp@200y", "03.jpeg", "04.png?sign=1")
]


def image_client(
    *, sizes: dict[str, int] | None = None, bad: Sequence[str] = (), tiny: Sequence[str] = ()
) -> tuple[httpx.AsyncClient, list[str]]:
    """按 URL 子串给体积的假 CDN；`bad` 回 403，`tiny` 回几百字节错误页。"""
    table = IMAGE_SIZES if sizes is None else sizes
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if any(needle in url for needle in bad):
            return httpx.Response(403, content=b"nope")
        if any(needle in url for needle in tiny):
            return httpx.Response(200, content=b"x" * 200)
        for needle, size in table.items():
            if needle in url:
                return httpx.Response(200, content=b"y" * size)
        msg = f"没有为 {url!r} 配图"
        raise AssertionError(msg)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


class TestDownloadImageSet:
    async def test_every_file_on_disk_is_one_the_artifact_counts(self, tmp_path: Path) -> None:
        """断言写成关系：数盘上有几张图，再数返回了几条路径，两个数相等。"""
        http, seen = image_client()
        dest = tmp_path / "note"
        result = await media.download_image_set(
            http, IMAGE_URLS, dest=dest, limit=18, budget_seconds=5.0
        )
        on_disk = sorted((dest / media.IMAGE_SUBDIR).iterdir())
        assert result.attempted == len(IMAGE_URLS)
        assert len(result.paths) == len(on_disk) == len(IMAGE_URLS)
        assert [str(p) for p in on_disk] == [str(p) for p in sorted(result.paths)]
        assert seen == IMAGE_URLS, "每张都真的去取过"

    async def test_size_is_the_sum_of_every_image_on_disk(self, tmp_path: Path) -> None:
        http, _seen = image_client()
        dest = tmp_path / "note"
        result = await media.download_image_set(
            http, IMAGE_URLS, dest=dest, limit=18, budget_seconds=5.0
        )
        assert result.total_bytes == sum(p.stat().st_size for p in result.paths)
        assert result.total_bytes == sum(IMAGE_SIZES.values())

    async def test_filenames_are_contiguous_even_when_a_middle_one_fails(
        self, tmp_path: Path
    ) -> None:
        """`extra_paths` 的契约是"按页面顺序"，洞会被读成"缺了文件"（ADR-0019）。"""
        http, _seen = image_client(bad=("03.jpeg",))
        result = await media.download_image_set(
            http, IMAGE_URLS, dest=tmp_path / "n", limit=18, budget_seconds=5.0
        )
        names = [p.name for p in result.paths]
        assert names == ["01.jpg", "02.webp", "03.png"], names
        assert len(result.failures) == 1
        assert "第 3/4 张" in result.failures[0], "页面原始位置在失败原文里留痕"

    async def test_the_limit_trims_the_tail_and_says_how_many(self, tmp_path: Path) -> None:
        http, seen = image_client()
        dest = tmp_path / "n"
        result = await media.download_image_set(
            http, IMAGE_URLS, dest=dest, limit=2, budget_seconds=5.0
        )
        assert result.dropped_by_limit == len(IMAGE_URLS) - 2
        assert len(result.paths) == 2
        assert len(seen) == 2, "截断发生在取之前，不该白跑后面几张"
        assert sum(p.stat().st_size for p in result.paths) == result.total_bytes

    async def test_the_limit_counts_after_blank_urls_are_dropped(self, tmp_path: Path) -> None:
        http, _seen = image_client()
        result = await media.download_image_set(
            http, ["", *IMAGE_URLS, "   "], dest=tmp_path / "n", limit=18, budget_seconds=5.0
        )
        assert result.attempted == len(IMAGE_URLS)
        assert result.dropped_by_limit == 0

    async def test_a_tiny_error_page_is_not_a_picture(self, tmp_path: Path) -> None:
        """CDN 失效回的不是 404 而是几百字节错误页，HTTP 还是 200。"""
        http, _seen = image_client(tiny=("02.webp",))
        result = await media.download_image_set(
            http, IMAGE_URLS, dest=tmp_path / "n", limit=18, budget_seconds=5.0
        )
        assert len(result.paths) == len(IMAGE_URLS) - 1
        assert any("判为无效响应" in f for f in result.failures)

    async def test_no_partial_files_are_left_behind(self, tmp_path: Path) -> None:
        http, _seen = image_client(bad=("04.png",), tiny=("03.jpeg",))
        dest = tmp_path / "n"
        result = await media.download_image_set(
            http, IMAGE_URLS, dest=dest, limit=18, budget_seconds=5.0
        )
        leftovers = [p for p in dest.rglob("*") if p.name.endswith(".part")]
        assert leftovers == [], f"留下了半成品：{leftovers}"
        assert len(result.paths) == 2

    async def test_every_failure_is_reported_but_none_of_them_is_silent(
        self, tmp_path: Path
    ) -> None:
        http, _seen = image_client(bad=("01.jpg", "02.webp", "03.jpeg", "04.png"))
        result = await media.download_image_set(
            http, IMAGE_URLS, dest=tmp_path / "n", limit=18, budget_seconds=5.0
        )
        assert result.paths == ()
        assert len(result.failures) == result.attempted == len(IMAGE_URLS)

    async def test_progress_is_monotonic_and_ends_at_one(self, tmp_path: Path) -> None:
        http, _seen = image_client()
        seen_fracs: list[float] = []
        await media.download_image_set(
            http,
            IMAGE_URLS,
            dest=tmp_path / "n",
            limit=18,
            budget_seconds=5.0,
            on_progress=seen_fracs.append,
        )
        assert seen_fracs == sorted(seen_fracs)
        assert seen_fracs[-1] == 1.0
        assert len(seen_fracs) == len(IMAGE_URLS)


class TestDownloadSingleFile:
    async def test_the_first_candidate_big_enough_wins(self, tmp_path: Path) -> None:
        http, seen = image_client(sizes={"a.mp4": 9000})
        dest = tmp_path / "v"
        result = await media.download_single_file(
            http, ["https://cdn/a.mp4", "https://cdn/b.mp4"], dest=dest, budget_seconds=5.0
        )
        assert result.path == dest / media.MEDIA_FILE_NAME
        assert result.attempted == 1
        assert seen == ["https://cdn/a.mp4"], "成了就不该再问下一个"

    async def test_a_rejected_candidate_is_counted_not_hidden(self, tmp_path: Path) -> None:
        http, seen = collecting_client(
            [
                ("a.mp4", httpx.Response(403, content=b"nope")),
                ("b.mp4", httpx.Response(200, content=b"z" * 5000)),
            ]
        )
        result = await media.download_single_file(
            http,
            ["https://cdn/a.mp4", "https://cdn/b.mp4"],
            dest=tmp_path / "v",
            budget_seconds=5.0,
        )
        assert result.path is not None and result.attempted == 2
        assert len(seen) == 2
        assert len(result.failures) == 1 and "候选 1/2" in result.failures[0]

    async def test_all_candidates_failing_returns_the_original_text_of_each(
        self, tmp_path: Path
    ) -> None:
        """这里**不抛**：调用方要把它并进 `MediaDownloadError`，还要同时带 yt-dlp 的原文。"""
        urls = [f"https://cdn/{n}.mp4" for n in ("a", "b", "c")]
        http, _seen = collecting_client([(n, httpx.Response(500, content=b"x")) for n in urls])
        result = await media.download_single_file(
            http, urls, dest=tmp_path / "v", budget_seconds=5.0
        )
        assert result.path is None
        assert result.attempted == len(urls)
        assert len(result.failures) == len(urls)
        assert [str(p) for p in (tmp_path / "v").glob("*.part")] == []

    async def test_no_bytes_are_written_for_a_rejected_response(self, tmp_path: Path) -> None:
        http, _seen = collecting_client([("a.mp4", httpx.Response(200, content=b"too"))])
        dest = tmp_path / "v"
        result = await media.download_single_file(
            http, ["https://cdn/a.mp4"], dest=dest, budget_seconds=5.0
        )
        assert result.path is None
        assert [p for p in dest.rglob("*") if p.suffix == ".part"] == []
        assert not (dest / media.MEDIA_FILE_NAME).exists()

    async def test_an_empty_candidate_list_is_an_honest_zero(self, tmp_path: Path) -> None:
        http, seen = image_client()
        result = await media.download_single_file(http, [], dest=tmp_path / "v", budget_seconds=5.0)
        assert result.path is None and result.attempted == 0 and result.failures == ()
        assert seen == []

    async def test_a_transport_error_is_a_rejection_not_a_crash(self, tmp_path: Path) -> None:
        """网络炸了要变成"这一个地址不行"，不是把整趟采集掀掉。"""

        def boom(_request: httpx.Request) -> httpx.Response:
            msg = "连接被重置"
            raise httpx.ConnectError(msg)

        http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        result = await media.download_single_file(
            http, ["https://cdn/a.mp4"], dest=tmp_path / "v", budget_seconds=5.0
        )
        assert result.path is None
        assert len(result.failures) == 1
        assert "ConnectError" in result.failures[0] and "连接被重置" in result.failures[0]


class TestDegenerateCardShapes:
    def test_a_notes_key_that_is_not_a_list_yields_no_cards(self) -> None:
        assert parse_cards_payload({"notes": "登录"}) == ()
        assert parse_cards_payload({}) == ()

    def test_non_dict_rows_are_skipped(self) -> None:
        payload = {"notes": ["a string", None, {"id": NOTE_1, "title": "一条"}]}
        assert [c.note_id for c in parse_cards_payload(payload)] == [NOTE_1]

    def test_a_url_that_is_not_one_becomes_a_red_with_context(self) -> None:
        """`require_http_url` 的红要带上下文 —— 失败意味着拼接那一侧被谁改坏了。"""
        with pytest.raises(PlatformError, match="应该是一个 http"):
            listing.require_http_url("not-a-url", context=f"笔记 {NOTE_1} 的详情页")
        assert str(listing.require_http_url("//a.com/x.jpg", context="补协议")).startswith(
            "https://"
        )


# =========================================================================== #
# 适配器：身份
# =========================================================================== #


def touch(path: Path, *, size: int = 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class TestParseCreatorUrl:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.xiaohongshu.com/user/profile/{USER_ID}",
            f"https://www.xiaohongshu.com/user/profile/{USER_ID}?xsec_token=abc",
            USER_ID,
        ],
    )
    async def test_the_three_offline_shapes_all_yield_the_user_id(
        self, tmp_path: Path, url: str
    ) -> None:
        http, seen = collecting_client([])
        ref = await make_adapter(tmp_path, http=http).parse_creator_url(url)
        assert ref.platform_id == USER_ID
        assert ref.platform == "xiaohongshu"
        assert seen == [], "这三条都不该碰网络"
        assert str(ref.profile_url) == f"https://www.xiaohongshu.com/user/profile/{USER_ID}"

    async def test_a_note_link_under_the_profile_carries_both_ids(self, tmp_path: Path) -> None:
        """V1 现网最常见的形状：`/user/profile/<uid>/<note_id>` —— uid 不能被当成笔记 ID。"""
        ref = await make_adapter(tmp_path).parse_creator_url(
            f"https://www.xiaohongshu.com/user/profile/{USER_ID}/{NOTE_1}"
        )
        assert ref.platform_id == USER_ID

    async def test_a_bare_note_link_cannot_name_a_creator(self, tmp_path: Path) -> None:
        """故意的：note_id 里不含 uid，"猜一个"会得到一条指向别人的 profile_url。"""
        with pytest.raises(PlatformError) as caught:
            await make_adapter(tmp_path).parse_creator_url(
                f"https://www.xiaohongshu.com/explore/{NOTE_1}"
            )
        assert caught.value.stage == "parse_url"

    async def test_a_share_link_follows_one_redirect(self, tmp_path: Path) -> None:
        """短链里没有任何身份信息（V1 §7.1 的同一族）。"""
        final = f"https://www.xiaohongshu.com/user/profile/{USER_ID}?xsec_source=app_share"
        http, seen = collecting_client(
            [
                (
                    "xhslink.com",
                    httpx.Response(
                        302,
                        headers={"location": final},
                        request=httpx.Request("GET", "https://xhslink.com/a/AbCdEf"),
                    ),
                ),
                ("xiaohongshu.com/user", httpx.Response(200, request=httpx.Request("GET", final))),
            ]
        )
        ref = await make_adapter(tmp_path, http=http).parse_creator_url(
            "https://xhslink.com/a/AbCdEf"
        )
        assert ref.platform_id == USER_ID
        assert not str(ref.platform_id).startswith("http")
        assert str(ref.profile_url) == f"https://www.xiaohongshu.com/user/profile/{USER_ID}"
        assert str(ref.source_url) == "https://xhslink.com/a/AbCdEf"
        assert any("xhslink" in url for url in seen)

    async def test_a_share_link_that_expands_to_nothing_says_both_urls(
        self, tmp_path: Path
    ) -> None:
        http, _seen = collecting_client(
            [
                (
                    "xhslink.com",
                    httpx.Response(
                        302,
                        headers={"location": "https://www.xiaohongshu.com/explore/abc"},
                        request=httpx.Request("GET", "https://xhslink.com/a/AbCdEf"),
                    ),
                ),
                (
                    "xiaohongshu.com/explore",
                    httpx.Response(200, request=httpx.Request("GET", "https://x/a")),
                ),
            ]
        )
        with pytest.raises(PlatformError, match="展开后仍然认不出") as caught:
            await make_adapter(tmp_path, http=http).parse_creator_url(
                "https://xhslink.com/a/AbCdEf"
            )
        assert "xhslink.com/a/AbCdEf" in str(caught.value)

    async def test_a_failing_expansion_keeps_the_http_error_text(self, tmp_path: Path) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            msg = "DNS 解析失败"
            raise httpx.ConnectError(msg)

        http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        with pytest.raises(PlatformError, match="展开分享短链失败") as caught:
            await make_adapter(tmp_path, http=http).parse_creator_url(
                "https://xhslink.com/a/AbCdEf"
            )
        assert "DNS 解析失败" in str(caught.value)

    @pytest.mark.parametrize("junk", ["", "   ", "https://example.com/user/x", "抖音"])
    async def test_unrecognisable_input_is_a_parse_url_red_with_the_original(
        self, tmp_path: Path, junk: str
    ) -> None:
        with pytest.raises(PlatformError) as caught:
            await make_adapter(tmp_path).parse_creator_url(junk)
        assert caught.value.stage == "parse_url"
        if junk.strip():
            assert junk in str(caught.value)

    async def test_the_bare_id_input_does_not_invent_a_source_url(self, tmp_path: Path) -> None:
        ref = await make_adapter(tmp_path).parse_creator_url(USER_ID)
        assert ref.source_url is None, "裸 ID 不是用户粘的链接，不该编造一个来源"
        assert USER_ID in str(ref.profile_url), "库里存的链接必须含得上 platform_id"

    async def test_a_foreign_config_is_an_assembly_error_not_an_attribute_error(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PlatformError, match="XiaohongshuConfig") as caught:
            XiaohongshuAdapter(
                BilibiliConfig(display_name="B站"),  # type: ignore[arg-type]
                make_deps(tmp_path),
            )
        assert caught.value.stage == "task"


class TestHealthcheck:
    async def test_a_missing_bridge_is_reported_as_an_assembly_error(self, tmp_path: Path) -> None:
        report = await make_adapter(tmp_path, bridge=None).healthcheck()
        assert report.components["bridge"] == "unreachable"
        assert report.status == "unreachable"
        assert report.is_healthy is False
        assert "装配错误" in (report.detail or "")

    async def test_a_bridge_that_is_not_listening(self, tmp_path: Path) -> None:
        bridge = FakeBridge(health=BridgeHealth(reachable=False, browser_ok=False, error="没起"))
        report = await make_adapter(tmp_path, bridge=bridge).healthcheck()
        assert report.components["bridge"] == "unreachable"
        assert "没起" in (report.detail or "")

    async def test_a_dead_browser_is_degraded_not_a_blocked_preflight(self, tmp_path: Path) -> None:
        """V1 §7.20：桥回 503 是"浏览器没了"，第一条真请求会自愈，预检不该拦任务。"""
        bridge = FakeBridge(
            health=BridgeHealth(reachable=True, browser_ok=False, error="browser closed")
        )
        report = await make_adapter(tmp_path, bridge=bridge).healthcheck()
        assert report.components["bridge"] == "degraded"
        assert "自愈" in (report.detail or "")

    async def test_the_components_are_scored_independently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_module.shutil, "which", lambda _name: None)
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components["cookies"] == "degraded"
        assert report.components["yt_dlp"] == "degraded"
        assert "yt-dlp" in (report.detail or "")
        assert report.status == "degraded"
        assert report.is_healthy is False, "V1 §7.20：只有显式 ok 才算健康"

    async def test_everything_present_is_green_and_says_nothing_extra(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_module.shutil, "which", lambda _name: "C:/bin/yt-dlp.exe")
        write_cookie_file(FileStorage(tmp_path / "data").cookies_path(media.XHS_COOKIE_DOMAIN))
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components == {"bridge": "ok", "cookies": "ok", "yt_dlp": "ok"}
        assert report.status == "ok"
        assert report.is_healthy is True
        assert report.detail is None, "全绿还挂流水账，读的人会以为「有情况但没说清」"


class TestListingThroughTheAdapter:
    async def test_the_first_screen_is_extended_by_scrolling_and_merged(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        out = await collect(make_adapter(tmp_path, bridge=bridge), limit=30)
        assert _ids(out) == [NOTE_1, NOTE_2, NOTE_3, NOTE_4]
        assert len(bridge.navigated) == 2, "补滚要重新导航，否则滚的是别人的主页"
        assert len(bridge.evaluated) == 2
        for meta in out:
            assert meta.platform == "xiaohongshu"
            assert meta.creator_ref.platform_id == USER_ID

    async def test_a_full_first_screen_does_not_pay_for_a_second_navigation(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(script=[page_payload("profile_page.json")])
        out = await collect(make_adapter(tmp_path, bridge=bridge), limit=3)
        assert len(out) == 3
        assert len(bridge.evaluated) == 1, "凑够了就不该再滚一轮"

    async def test_limit_is_a_ceiling_not_a_suggestion(self, tmp_path: Path) -> None:
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        assert len(await collect(make_adapter(tmp_path, bridge=bridge), limit=2)) == 2

    async def test_a_non_positive_limit_touches_nothing(self, tmp_path: Path) -> None:
        bridge = FakeBridge(script=[])
        assert await collect(make_adapter(tmp_path, bridge=bridge), limit=0) == []
        assert bridge.navigated == [] and bridge.evaluated == []

    async def test_since_is_a_real_filter_here(self, tmp_path: Path) -> None:
        """与抖音相反：note_id 里就有时间，所以 `since` 判得动就真判掉。"""
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        cutoff = datetime.fromtimestamp(1_717_509_248, tz=UTC)
        out = await collect(make_adapter(tmp_path, bridge=bridge), since=cutoff)
        assert _ids(out) == [NOTE_3, NOTE_4]

    async def test_a_scroll_that_blows_up_keeps_the_first_screen(self, tmp_path: Path) -> None:
        """滚动那一趟只降级成「沿用首屏」，不掀掉整轮 —— 首屏那几条是干净数据。"""
        bridge = FakeBridge(
            script=[
                page_payload("profile_page.json"),
                BridgeError(PLATFORM_NAME, "bridge", "桥死了"),
            ]
        )
        out = await collect(make_adapter(tmp_path, bridge=bridge), limit=30)
        assert _ids(out) == [NOTE_1, NOTE_2, NOTE_3]

    async def test_a_nonsense_scroll_payload_also_keeps_the_first_screen(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(script=[page_payload("profile_page.json"), "<html>风控</html>"])
        assert len(await collect(make_adapter(tmp_path, bridge=bridge), limit=30)) == 3

    async def test_page_redirects_are_a_list_error_so_the_scheduler_moves_on(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(script=[page_payload("profile_page_user_id_mismatch.json")])
        with pytest.raises(ListError):
            await collect(make_adapter(tmp_path, bridge=bridge))

    async def test_likes_sorting_looks_further_than_the_limit(self, tmp_path: Path) -> None:
        """`LIKES_LOOKAHEAD_CARDS` 存在的理由：只拿首屏样本排出来的不是历史爆款。

        判据写成关系：注入给页面的 `wanted` 至少是那个样本量，且比 limit 大。
        """
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        adapter = make_adapter(
            tmp_path, bridge=bridge, config=advanced_config(sort_notes_by="likes")
        )
        out = await collect(adapter, limit=4)
        assert LIKES_LOOKAHEAD_CARDS > 4
        assert f"const wanted = {LIKES_LOOKAHEAD_CARDS};" in bridge.evaluated[1]
        assert _ids(out) == [NOTE_3, NOTE_1, NOTE_2, NOTE_4]
        counts = [m.like_count for m in out]
        assert counts == sorted(counts, reverse=True)

    async def test_page_order_does_not_pay_for_the_lookahead(self, tmp_path: Path) -> None:
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        await collect(make_adapter(tmp_path, bridge=bridge), limit=4)
        assert "const wanted = 4;" in bridge.evaluated[1]

    async def test_image_notes_are_dropped_only_when_the_page_said_so(self, tmp_path: Path) -> None:
        """`include_image_notes=False`：丢明确标 `normal` 的那批，判不了类型的照旧放行。"""
        config = advanced_config(include_image_notes=False)
        # 只有首屏（DOM 那半边认不出类型）时，一条都不该丢。
        only_first = FakeBridge(script=[page_payload("profile_page.json")])
        assert (
            len(await collect(make_adapter(tmp_path, bridge=only_first, config=config), limit=3))
            == 3
        )
        both = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        assert NOTE_2 in _ids(await collect(make_adapter(tmp_path, bridge=both), limit=30)), (
            "前提：默认配置下图文笔记是要收的"
        )
        both2 = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        assert _ids(
            await collect(make_adapter(tmp_path, bridge=both2, config=config), limit=30)
        ) == ([NOTE_1, NOTE_3, NOTE_4])

    async def test_profile_fetch_carries_the_placeholder_signal(self, tmp_path: Path) -> None:
        payload = page_payload("profile_page_login_wall.json", as_json_string=False)
        payload["login_wall"] = False
        bridge = FakeBridge(script=[json.dumps(payload, ensure_ascii=False)])
        profile = await make_adapter(tmp_path, bridge=bridge).fetch_creator_profile(profile_ref())
        assert profile.name == "小红书创作者"
        assert profile.extra["nickname_is_placeholder"] is True
        assert profile.ref == profile_ref()

    async def test_find_creator_by_name_is_a_supplementary_method(self, tmp_path: Path) -> None:
        bridge = FakeBridge(script=[page_payload("search_candidates.json")] * 2)
        found = await make_adapter(tmp_path, bridge=bridge).find_creator_by_name("影视飓风")
        assert [p.ref.platform_id for p in found] == [USER_ID, OTHER_USER_ID]
        assert all("xsec_token" not in str(p.ref.profile_url) for p in found)

    async def test_find_creator_by_name_without_a_bridge_is_an_assembly_error(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PlatformError, match="装配错误"):
            await make_adapter(tmp_path, bridge=None).find_creator_by_name("x")


def _ids(metas: Sequence[VideoMeta]) -> list[str]:
    return [m.platform_video_id for m in metas]


# =========================================================================== #
# 下载：图文不是失败（ADR-0019）+ 视频的两条路（V1 §7.2）
# =========================================================================== #

VIDEO_URL_NEEDLE = "sns-video-bd.xhscdn.com/stream/1.mp4"


class _NeverCalledRunner:
    """图文这条路**根本不经过 yt-dlp**，被调到了就是分流写错。"""

    async def download(self, *args: object, **kwargs: object) -> object:
        msg = "图文笔记不该去问 yt-dlp"
        raise AssertionError(msg)


@dataclass
class MediaHarness:
    adapter: XiaohongshuAdapter
    bridge: FakeBridge
    requested: list[str]


def cdn_client(
    *,
    video_bytes: int = 4096,
    sizes: dict[str, int] | None = None,
    bad: Sequence[str] = (),
    tiny: Sequence[str] = (),
) -> tuple[httpx.AsyncClient, list[str]]:
    """假 CDN：视频直链 + 图文原图，外加"403 / 几百字节错误页"两种坏形状。"""
    table: dict[str, int] = {**IMAGE_SIZES, **(sizes or {})}
    if video_bytes:
        table[VIDEO_URL_NEEDLE] = video_bytes
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if any(needle in url for needle in bad):
            return httpx.Response(403, content=b"forbidden")
        if any(needle in url for needle in tiny):
            return httpx.Response(200, content=b"x" * 200)
        for needle, size in table.items():
            if needle in url:
                return httpx.Response(200, content=b"z" * size)
        msg = f"没有为 {url!r} 配路由"
        raise AssertionError(msg)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def media_harness(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    runner: object | None = None,
    detail: str = "detail_video_note.json",
    config: XiaohongshuConfig | None = None,
    video_bytes: int = 4096,
    bad: Sequence[str] = (),
    tiny: Sequence[str] = (),
) -> MediaHarness:
    http, requested = cdn_client(video_bytes=video_bytes, bad=bad, tiny=tiny)
    bridge = FakeBridge(script=[page_payload(detail)])
    adapter = make_adapter(tmp_path, bridge=bridge, http=http, config=config)
    monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: runner or _NeverCalledRunner())
    return MediaHarness(adapter=adapter, bridge=bridge, requested=requested)


class TestDownloadMediaImageNote:
    async def test_an_image_note_produces_the_shape_adr_0019_promised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "media"
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, detail="detail_image_note.json")
        artifact = await harness.adapter.download_media(make_video(NOTE_2), dest)
        assert artifact.kind == "single_file"
        on_disk = sorted((dest / media.IMAGE_SUBDIR).iterdir())
        assert artifact.path == on_disk[0], "主文件是**第一张**图"
        assert tuple(artifact.extra_paths) == tuple(on_disk[1:]), "其余按页面顺序"
        assert len(on_disk) == len(artifact.extra_paths) + 1
        assert artifact.has_audio is False
        assert artifact.has_video is False, "一张 JPG 不许被喂给 <video>"
        assert artifact.media_source == "page_play_url"
        assert artifact.cookie_rung is None, "没走阶梯就不许编一个档位"

    async def test_size_bytes_is_the_sum_of_every_original_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`total_size_bytes()` 直接返回它、不做二次相加 —— 生产方必须按这个口径填。"""
        dest = tmp_path / "media"
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, detail="detail_image_note.json")
        artifact = await harness.adapter.download_media(make_video(NOTE_2), dest)
        every = [artifact.path, *artifact.extra_paths]
        assert artifact.size_bytes == sum(p.stat().st_size for p in every)
        assert artifact.size_bytes == sum(IMAGE_SIZES.values())
        assert artifact.size_bytes > artifact.path.stat().st_size, (
            "只填主文件那一个才是这条用例要抓的错"
        )

    async def test_the_image_path_never_consults_yt_dlp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, detail="detail_image_note.json")
        artifact = await harness.adapter.download_media(make_video(NOTE_2), tmp_path / "m")
        assert artifact.yt_dlp_error is None

    async def test_the_per_note_limit_trims_the_tail_but_the_note_still_ships(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`max_images_per_note` 是"用户要求少存"，不是失败，但也不许静默（会记 warning）。"""
        total = len(IMAGE_SIZES)
        dest = tmp_path / "m"
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            detail="detail_image_note.json",
            config=advanced_config(max_images_per_note=2),
        )
        artifact = await harness.adapter.download_media(make_video(NOTE_2), dest)
        assert len([artifact.path, *artifact.extra_paths]) == 2
        assert len(list((dest / media.IMAGE_SUBDIR).iterdir())) == 2 < total
        assert len(harness.requested) == 2, "截断发生在取之前"

    async def test_losing_one_picture_does_not_make_the_note_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            detail="detail_image_note.json",
            bad=("note/02.webp",),
        )
        artifact = await harness.adapter.download_media(make_video(NOTE_2), tmp_path / "m")
        kept = [artifact.path, *artifact.extra_paths]
        assert len(kept) == len(IMAGE_SIZES) - 1
        assert artifact.size_bytes == sum(p.stat().st_size for p in kept)
        # 02.webp 被 403 掉了，剩下三张重排成 01/02/03；`.jpeg` 归一成 `.jpg`（V1 同一条）。
        assert [f.name for f in kept] == ["01.jpg", "02.jpg", "03.png"], "序号是连续的"

    async def test_losing_every_picture_is_an_honest_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V1 只 print 一句然后继续；V2 一张都没拿到就如实抛。"""
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            detail="detail_image_note.json",
            bad=("note/01", "note/02", "note/03", "note/04"),
        )
        with pytest.raises(MediaDownloadError, match="一张都没拿到") as caught:
            await harness.adapter.download_media(make_video(NOTE_2), tmp_path / "m")
        assert str(caught.value).count("403") >= 1, "原文要带上，不然只能重放"

    async def test_the_ticket_never_reaches_the_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0016：token 只在取详情那一刻用，**不进产物**。"""
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, detail="detail_image_note.json")
        meta = make_video(NOTE_2, token=TOKEN_1)
        artifact = await harness.adapter.download_media(meta, tmp_path / "m")
        assert TOKEN_1.split("=", maxsplit=1)[0] in harness.bridge.navigated[0], (
            "前提：门票确实给出去了"
        )
        assert TOKEN_1 not in artifact.model_dump_json()
        assert TOKEN_1 not in str(artifact.path)
        # 也不进库：入库那一层只从 meta 上取具名字段，从不读 `extra`。
        source = inspect.getsource(build_video_draft)
        assert "extra" not in source, "草稿层一旦开始读 extra，门票就会进 metadata_json"
        assert "xsec_token" not in set(VideoDraft.model_fields)

    async def test_include_image_notes_false_does_not_gate_the_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """那个开关是**枚举**侧的分流判据，不是"拒绝下载已经点开的图文"。

        记在这里是因为它容易被误读成后者：真按后者实现，调用方指定一条图文笔记时
        会得到一句"配置不允许"，而配置里那句话写的是"在枚举这一步就不交出"。
        """
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            detail="detail_image_note.json",
            config=advanced_config(include_image_notes=False),
        )
        artifact = await harness.adapter.download_media(make_video(NOTE_2), tmp_path / "m")
        assert artifact.has_video is False


class TestDownloadMediaVideoNote:
    async def test_yt_dlp_winning_marks_the_source_and_keeps_error_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "m"
        main = touch(dest / "media.mp4", size=9000)
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[main]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), dest)
        assert artifact.media_source == "yt_dlp"
        assert artifact.yt_dlp_error is None
        assert artifact.cookie_rung == "带导出的登录 cookie"
        assert artifact.size_bytes == 9000
        assert harness.requested == [], "yt-dlp 成了就不该再去问直链"
        assert (
            runner.calls[0]["file_template"]
            == adapter_module.YTDLP_FILE_TEMPLATE
            == "media.%(ext)s"
        )
        assert str(runner.calls[0]["url"]).startswith("https://www.xiaohongshu.com/explore/")

    async def test_the_fallback_keeps_the_yt_dlp_original_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """V1 §7.2 的看护本体。V1 兜底成功时会 `media["errors"] = []`，V2 反过来。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "m")
        assert artifact.media_source == "page_play_url"
        assert artifact.yt_dlp_error is not None
        assert "unable to extract video url" in artifact.yt_dlp_error, "stderr 的最后一行"
        assert "带导出的登录 cookie→exit 1" in artifact.yt_dlp_error, "每一档的退出码"
        assert artifact.cookie_rung is None, "兜底这条路没经过阶梯，不许编一档"
        assert len(harness.requested) == 1

    async def test_turning_the_fallback_off_fails_loudly_and_explains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            runner=runner,
            config=fast_config(fallback_to_page_play_url=False),
        )
        with pytest.raises(MediaDownloadError, match="fallback_to_page_play_url=False") as caught:
            await harness.adapter.download_media(make_video(), tmp_path / "m")
        assert "unable to extract video url" in str(caught.value)
        assert NOTE_1 in str(caught.value) and USER_ID in str(caught.value), "_describe 的两位"
        assert harness.requested == [], "关掉兜底就不该去碰直链"

    async def test_both_routes_failing_carries_both_original_texts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(
            tmp_path, monkeypatch=monkeypatch, runner=runner, video_bytes=0, bad=(VIDEO_URL_NEEDLE,)
        )
        with pytest.raises(MediaDownloadError) as caught:
            await harness.adapter.download_media(make_video(), tmp_path / "m")
        text = str(caught.value)
        assert "页面视频直链也没能落盘" in text
        assert "unable to extract video url" in text, "两条路都走过了，各自的错都得留一份"

    async def test_unmerged_dash_parts_go_to_the_fallback_instead_of_claiming_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§7.21 从"声明不支持分片"那一侧重新进来的形状。"""
        dest = tmp_path / "m"
        video_part = touch(dest / "media.f137.mp4", size=40_000)
        audio_part = touch(dest / "media.f140.m4a", size=2_000)
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[video_part, audio_part]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), dest)
        assert artifact.media_source == "page_play_url"
        assert artifact.yt_dlp_error is not None and "未合并" in artifact.yt_dlp_error

    async def test_a_missing_binary_falls_back_rather_than_bypassing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(raises=LookupError("yt-dlp 不在 PATH 里"))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "m")
        assert artifact.media_source == "page_play_url"
        assert "未安装 yt-dlp" in (artifact.yt_dlp_error or "")

    async def test_an_empty_cookie_ladder_is_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(
            raises=MediaDownloadError(PLATFORM_NAME, "media", "阶梯里一档都凑不出来")
        )
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "m")
        assert "阶梯里一档都凑不出来" in (artifact.yt_dlp_error or "")

    async def test_paths_yt_dlp_reports_but_that_are_unreadable_are_not_a_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "m"
        ghost = dest / "media.mp4"  # 故意不创建
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[ghost]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), dest)
        assert artifact.media_source == "page_play_url"
        assert "一个都读不到" in (artifact.yt_dlp_error or "")

    async def test_extra_files_beside_a_merged_main_are_still_reported_as_one_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "m"
        main = touch(dest / "media.mp4", size=7000)
        stray = touch(dest / "media.webp", size=300)
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[main, stray]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), dest)
        assert artifact.media_source == "yt_dlp"
        assert artifact.path == main

    async def test_progress_lines_from_yt_dlp_become_progress_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(
            result=ytdlp_blocked(),
            emit_lines=["[download] 100.0% of 1.00MiB", "[download] nonsense"],
        )
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        seen: list[float] = []
        await harness.adapter.download_media(make_video(), tmp_path / "m", on_progress=seen.append)
        assert seen[0] == 1.0, "认不出的行不该产生事件"
        assert seen == sorted(seen)

    async def test_a_detail_failure_becomes_a_media_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = media_harness(
            tmp_path, monkeypatch=monkeypatch, detail="detail_note_id_mismatch.json"
        )
        with pytest.raises(MediaDownloadError) as caught:
            await harness.adapter.download_media(make_video(), tmp_path / "m")
        assert "串号" in str(caught.value)
        assert caught.value.stage == "media", "清单按 stage 分组"

    async def test_a_note_with_neither_video_nor_images_fails_with_what_the_page_said(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = page_payload("detail_image_note.json", as_json_string=False)
        payload["images"] = []
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, detail="detail_image_note.json")
        harness.bridge._script = [json.dumps(payload, ensure_ascii=False)]  # type: ignore[attr-defined]
        with pytest.raises(MediaDownloadError, match="既没有视频直链也没有原图") as caught:
            await harness.adapter.download_media(make_video(NOTE_2), tmp_path / "m")
        assert "normal" in str(caught.value), "页面给的 note_type 要出现在那句话里"

    async def test_a_missing_bridge_is_an_assembly_error_at_the_media_stage(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PlatformError, match="装配错误") as caught:
            await make_adapter(tmp_path, bridge=None).download_media(make_video(), tmp_path / "m")
        assert caught.value.stage == "media"


class TestSubtitlesAndRegistry:
    async def test_fetch_subtitles_returns_none_rather_than_raising(self, tmp_path: Path) -> None:
        """没有字幕轨 ≠ 失败：调度器要的是"没有，去走 ASR"。"""
        assert await make_adapter(tmp_path).fetch_subtitles(make_video()) is None

    def test_the_platform_is_registered_with_its_own_schema(self) -> None:
        assert PLATFORMS["xiaohongshu"] is XiaohongshuAdapter
        assert XiaohongshuAdapter.config_schema() is XiaohongshuConfig
        assert PLATFORM_CONFIG_SCHEMAS["xiaohongshu"] is XiaohongshuConfig

    def test_the_failure_reason_line_still_works_without_any_output(self, tmp_path: Path) -> None:
        result = YtDlpResult(
            ok=False, variant=None, stdout="", stderr="   \n ", returncode=7, artifacts=()
        )
        reason = adapter_module._ytdlp_failure_reason(result)
        assert "exit 7" in reason
        assert "没有产生任何尝试记录" in reason

    def test_the_runner_is_built_with_this_platforms_arguments(self, tmp_path: Path) -> None:
        """替换点之外的部分也钉一句：argv 与预算是**小红书自己那份**，不是抄来的。"""
        runner = make_adapter(tmp_path)._ytdlp_runner()
        assert isinstance(runner, YtDlpRunner)
        assert adapter_module.YTDLP_BUDGET_SECONDS > adapter_module.DIRECT_BUDGET_SECONDS, (
            "yt-dlp 那一趟比直链长是有意的：失败还要走阶梯"
        )
        assert "mp4" in adapter_module.YTDLP_EXTRA_ARGS
        assert Path(adapter_module.YTDLP_FILE_TEMPLATE).stem == Path(media.MEDIA_FILE_NAME).stem, (
            "两条路必须写同一个落点名，否则后处理要按来源分支找文件"
        )


# =========================================================================== #
# 补一次读数（fetch_metrics，ADR-0020 决定二）
#
# 这一家是四家里唯一"能力位为假、但读数能做"的：评论区是另一条签名接口（没实现），
# 而点赞数就在我们本来就要访问的详情页上。两条边界分别为真不等于两条同为真，
# 所以下面这几条钉的是"只有一项也算读数"与"读不到时该报在哪一格"。
# =========================================================================== #


class TestReadings:
    async def test_the_like_count_comes_from_the_detail_page(self, tmp_path: Path) -> None:
        """页面上那句 `"1.2万"` 变成一个 12000，且**先导航再取值**。

        导航那一步不是顺手：`evaluate` 只发表达式，少了 navigate 会在
        **上一条笔记还停着的页面**里取 `noteDetailMap`，于是拿到别人的数且自洽
        （`detail.fetch_note_detail` 的 docstring 记的就是这个最贵的错）。
        """
        bridge = FakeBridge(script=[page_payload("detail_video_note.json")])
        video = make_video(NOTE_1)

        readings = await make_adapter(tmp_path, bridge=bridge).fetch_metrics(video)

        assert readings.like_count == 12_000
        assert bridge.navigated == [str(video.webpage_url)]

    async def test_the_three_fields_this_page_does_not_give_stay_null(self, tmp_path: Path) -> None:
        """只有赞数一项 —— 其余三项必须是 NULL，**且 metadata 要说出这件事**。

        写成"三项同为 None + only 那一栏点名 like"两半：只测前半的话，
        一个"把缺的补 0"的实现会红在别处（增长率除以 0），而这里要防的是"缺的没被说明"
        —— 下一次读这条快照的人分不出"评论数为 0"与"这一家没给评论数"。
        """
        bridge = FakeBridge(script=[page_payload("detail_video_note.json")])
        readings = await make_adapter(tmp_path, bridge=bridge).fetch_metrics(make_video(NOTE_1))

        assert (readings.view_count, readings.comment_count, readings.share_count) == (
            None,
            None,
            None,
        )
        assert readings.has_any_reading, "一项非空就是有效读数"
        assert json.loads(readings.metadata_json)["only"] == ["like_count"]

    @pytest.mark.parametrize(
        ("text", "phrase"),
        [
            ("点赞人数未知", "不是个数"),
            ("", "读不出来"),
            ("-", "读不出来"),
        ],
    )
    async def test_a_like_count_that_is_not_a_number_is_a_failure_not_a_zero(
        self, tmp_path: Path, text: str, phrase: str
    ) -> None:
        """页面形状变了要红，且**不能把"读不出来"消化成 0**。

        0 是一个会被当成事实的数：下一轮算增长时它与"真的没人点赞"同形。
        两种坏法分开报（"有字但不是数" vs "这一栏压根没字"），因为一个要改选择器、
        一个要改解析器 —— `compact_number_to_int` 对前者抛 ValueError，这里换成
        带平台与 stage 的 `PlatformError` 并留原文。
        """
        payload = page_payload("detail_video_note.json", as_json_string=False)
        payload["likes"] = text
        bridge = FakeBridge(script=[json.dumps(payload)])
        with pytest.raises(PlatformError, match=phrase) as caught:
            await make_adapter(tmp_path, bridge=bridge).fetch_metrics(make_video(NOTE_1))
        assert caught.value.stage == "metrics"
        assert text in str(caught.value), "原文里要看得见那一句脏值本身"

    async def test_a_detail_page_that_cannot_be_opened_is_reported_under_metrics(
        self, tmp_path: Path
    ) -> None:
        """复用详情页就有这个副作用：它自己把失败报成 `media`。

        对下载那一步是对的，对补读数不是 —— 清单会把它排到"去查 yt-dlp / 直链"那一格，
        而真实原因只是登录墙。所以这一条看护的是**换了 stage 但原文一字未动**。
        """
        bridge = FakeBridge(script=[page_payload("detail_login_wall.json")])
        with pytest.raises(PlatformError) as caught:
            await make_adapter(tmp_path, bridge=bridge).fetch_metrics(make_video(NOTE_1))
        assert caught.value.stage == "metrics"
        assert "media" in str(caught.value), "原来那句 media 的原文要还在（不许吞掉重说）"

    async def test_a_broken_bridge_raises_instead_of_returning_nothing(
        self, tmp_path: Path
    ) -> None:
        """桥本身挂了（V1 §7.20）：抛，不消化成空读数。

        与上一条的区别是排查方向：这条要去看桥进程，那条要去看页面选择器。
        """
        bridge = FakeBridge(script=[BridgeError("xiaohongshu", "browser", "浏览器已关闭")])
        with pytest.raises(PlatformError) as caught:
            await make_adapter(tmp_path, bridge=bridge).fetch_metrics(make_video(NOTE_1))
        assert caught.value.stage == "metrics"
        assert "浏览器已关闭" in str(caught.value)

    async def test_no_bridge_at_all_is_an_assembly_error_said_plainly(self, tmp_path: Path) -> None:
        """声明了 `needs_browser=True` 却拿到 `bridge=None`：要说清是装配错了。

        这一条不只是补 `_require_bridge` 的分支：读数与下载共用那条路，而" AttributeError:
        'NoneType' object has no attribute 'evaluate'" 会让人以为是这一家的接口坏了。
        """
        adapter = make_adapter(tmp_path, bridge=None)
        with pytest.raises(PlatformError, match="桥"):
            await adapter.fetch_metrics(make_video(NOTE_1))
