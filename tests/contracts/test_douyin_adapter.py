"""抖音 Adapter 契约测试。V1 §7.1 / §7.2 / §7.3 三条陷阱的看护，外加 §7.20 的红灯形状。

**全 mock，不碰网络、不碰浏览器、不碰真二进制**：

- HTTP 用 `httpx.MockTransport` —— 跟 302 与直链字节流都在这里，
  所以"分享短链必须跟一次 302"是**真跑了重定向逻辑**的，只是没有真域名。
- CDP 桥用 `tests/contracts/_doubles.py:FakeBridge`，回的是
  `tests/fixtures/douyin/*.json` 里那份**页面 JS 会给出的东西**
  （含 V1 踩过的脏广告卡片、没有协议头的头像、占位昵称）。
- yt-dlp 用假 Runner 顶掉 `adapter._ytdlp_runner()` 这个替换点。

Task 14 会把通用那部分（每个平台都要过的若干条）抽成 `PlatformAdapterContractTests`
抽象基类；本文件里的抖音特有用例届时原样保留。
"""

from __future__ import annotations

import asyncio
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

from intelligence_hub_v2.errors import BridgeError, ListError, MediaDownloadError, PlatformError
from intelligence_hub_v2.infra.cdp_bridge import BridgeHealth
from intelligence_hub_v2.infra.cookies import COOKIE_FILE_HEADER, CookieManager
from intelligence_hub_v2.infra.pacing import RatePacer
from intelligence_hub_v2.infra.ytdlp import YtDlpCookieVariant, YtDlpResult
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.base import AdapterDeps, Capabilities, PlatformAdapter
from intelligence_hub_v2.platforms.bilibili.config import BilibiliConfig
from intelligence_hub_v2.platforms.douyin import adapter as adapter_module
from intelligence_hub_v2.platforms.douyin import media as media_module
from intelligence_hub_v2.platforms.douyin.adapter import DouyinAdapter
from intelligence_hub_v2.platforms.douyin.config import DouyinConfig
from intelligence_hub_v2.platforms.douyin.listing import (
    COLLECT_AFTER_SCROLL_FUNCTION,
    PROFILE_PAGE_FUNCTION,
    PageBudget,
    VideoCard,
    decode_page_result,
    render_page_js,
)
from intelligence_hub_v2.platforms.douyin.media import CookieLadder, resolve_cookie_ladder
from intelligence_hub_v2.platforms.douyin.urls import parse_cn_count
from intelligence_hub_v2.platforms.registry import PLATFORMS
from intelligence_hub_v2.storage.files import FileStorage

SEC_UID = "MS4wLjABAAAAabc123def456"
AWEME_ID = "7412345678901234567"

_QUOTED_PLACEHOLDER = re.compile(r"""(["'`])__\w+__""")
"""引号紧跟占位符 = 渲染后变成 `""MS4w…""`，页面 SyntaxError。"""
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "douyin"


def page_payload(name: str, *, as_json_string: bool = True) -> Any:
    """读一份页面 JS 会返回的模拟结果。

    `as_json_string=True` 是默认，因为页函数真的回 `JSON.stringify(...)`，
    桥给回来的就是字符串；想验 `decode_page_result()` 也吃 dict 时显式传 False。
    """
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return json.dumps(payload, ensure_ascii=False) if as_json_string else payload


def fast_config(**overrides: object) -> DouyinConfig:
    """默认配置 + 把限速闸拧到" practically 不等"。

    出厂的 `per_minute=30` 意味着两次桥调用之间真等 2 秒，用例里等不起。
    但**不能因此就以为闸门不存在**：`test_pacer_waits_the_configured_interval`
    与 `test_adapter_paces_at_the_shipped_per_minute_budget` 用出厂值专门验它还在。
    """
    payload: dict[str, object] = {
        "display_name": "抖音",
        "rate_limit": {"per_minute": 100_000, "per_creator_seconds": 0.0},
        **overrides,
    }
    return DouyinConfig.model_validate(payload)


def refusing_client() -> httpx.AsyncClient:
    """一个**任何请求都会炸**的客户端。

    默认给这个而不是 `httpx.AsyncClient()`，两个理由各占一半：

    1. 契约测试不许有可能偷偷摸到真网络 —— 忘了传 `http=` 的用例必须当场红，
       而不是"在这台机器上碰巧过了"。
    2. 在这台 Windows 机器上建一个**默认真传输**的 httpx 客户端要 2.1 秒
       （每个客户端都重新枚举一次系统证书库，实测见 `docs/lessons.md`）。
       85 条用例各建一个，就是 3 分钟纯等待。
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(_refuse))


def _refuse(request: httpx.Request) -> httpx.Response:
    msg = f"契约测试发出了真请求：{request.method} {request.url}"
    raise AssertionError(msg)


def make_deps(
    tmp_path: Path,
    *,
    config: DouyinConfig | None = None,
    bridge: Any = ...,  # 三态：省略=给一个活桥 / None=装配漏了 / 对象=自定义
    http: httpx.AsyncClient | None = None,
) -> AdapterDeps:
    return AdapterDeps(
        config=config or fast_config(),
        # 适配器不碰库、不发事件（模块 docstring 第 1 条）。这两个 None 是**故意的**：
        # 真去接一个假 storage，就等于允许适配器偷偷写库 —— 那是 Task 8 的职责边界。
        storage=None,  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        http=http or refusing_client(),
        logger=get_logger("test.douyin"),
        cookies=CookieManager(FileStorage(tmp_path / "data")),
        bridge=None if bridge is None else (FakeBridge() if bridge is ... else bridge),
    )


def make_adapter(
    tmp_path: Path,
    *,
    config: DouyinConfig | None = None,
    bridge: Any = ...,
    http: httpx.AsyncClient | None = None,
) -> DouyinAdapter:
    adapter = DouyinAdapter(
        config or fast_config(), make_deps(tmp_path, config=config, bridge=bridge, http=http)
    )
    adapter.page_budget = PageBudget(
        grid_poll_rounds=1, grid_poll_ms=0, max_scroll_rounds=1, settle_ms=0
    )
    adapter.direct_budget_seconds = 5.0
    return adapter


def profile_ref() -> CreatorRef:
    return CreatorRef(
        platform="douyin",
        platform_id=SEC_UID,
        profile_url=f"https://www.douyin.com/user/{SEC_UID}",
    )


def make_video(aweme_id: str = AWEME_ID) -> VideoMeta:
    return VideoMeta(
        platform="douyin",
        platform_video_id=aweme_id,
        creator_ref=profile_ref(),
        title="口播稿怎么写前三秒",
        webpage_url=f"https://www.douyin.com/video/{aweme_id}",
    )


def write_cookie_file(path: Path, *, with_cookie: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        COOKIE_FILE_HEADER + ".douyin.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc\n"
        if with_cookie
        else COOKIE_FILE_HEADER
    )
    path.write_text(body, encoding="utf-8")
    return path


def collecting_client(
    routes: Sequence[tuple[str, httpx.Response]],
) -> tuple[httpx.AsyncClient, list[str]]:
    """按 URL 子串匹配的假 HTTP 客户端，外加它收到的请求列表。"""
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


async def collect(
    adapter: DouyinAdapter, *, limit: int = 30, since: datetime | None = None
) -> list[VideoMeta]:
    out: list[VideoMeta] = []
    async for meta in adapter.list_creator_videos(profile_ref(), since=since, limit=limit):
        out.append(meta)
    return out


# =========================================================================== #
# §7.1 —— 身份是 sec_uid，不是 URL 里的东西
# =========================================================================== #


class TestParseCreatorUrl:
    async def test_share_link_follows_302_to_sec_uid(self, tmp_path: Path) -> None:
        """V1 §7.1 的正身：短链不含身份，必须跟一次 302 才拿得到 sec_uid。"""
        final = f"https://www.douyin.com/user/{SEC_UID}?from_tab_name=main"
        http, seen = collecting_client(
            [
                (
                    "v.douyin.com",
                    httpx.Response(
                        302,
                        headers={"location": final},
                        request=httpx.Request("GET", "https://v.douyin.com/iAbc/"),
                    ),
                ),
                ("douyin.com/user", httpx.Response(200, request=httpx.Request("GET", final))),
            ]
        )
        ref = await make_adapter(tmp_path, http=http).parse_creator_url(
            "https://v.douyin.com/iAbc/"
        )
        assert ref.platform_id == SEC_UID
        assert not ref.platform_id.startswith("http")  # §7.1 的判据本身
        assert str(ref.profile_url) == f"https://www.douyin.com/user/{SEC_UID}"
        assert str(ref.source_url) == "https://v.douyin.com/iAbc/"
        reached = ["/iAbc/" in url for url in seen]
        assert reached == [True, False], "先短链、再落地页，两跳为止（绝不能回头再问一遍短链）"

    async def test_canonical_profile_url_does_not_touch_the_network(self, tmp_path: Path) -> None:
        """主页链接本来就读得到 ID。再跟一次 302 是白等一个来回，
        而且让"离线跑契约测试"变成"要看网络运气"（V1 的同一取舍）。"""

        def refuse(_request: httpx.Request) -> httpx.Response:
            msg = "不该发任何请求"
            raise AssertionError(msg)

        http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        ref = await make_adapter(tmp_path, http=http).parse_creator_url(
            f"https://www.douyin.com/user/{SEC_UID}?is_sub=1"
        )
        assert ref.platform_id == SEC_UID

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (f"https://www.douyin.com/user/{SEC_UID}", SEC_UID),
            (f"https://www.douyin.com/share/user/{SEC_UID}", SEC_UID),
            (f"https://m.douyin.com/user/{SEC_UID}", SEC_UID),
            (SEC_UID, SEC_UID),
            (f"https://www.iesdouyin.com/share/?sec_uid={SEC_UID}&u_code=0", SEC_UID),
        ],
    )
    async def test_sec_uid_recognised_from_each_shape(
        self, tmp_path: Path, url: str, expected: str
    ) -> None:
        assert (await make_adapter(tmp_path).parse_creator_url(url)).platform_id == expected

    async def test_the_path_wins_over_a_conflicting_query_sec_uid(self, tmp_path: Path) -> None:
        """一条同时带两者的链接：取路径段（页面主角），不取 query（往往是当前登录者）。

        V1 的分支顺序就是它，钉住是因为反过来的结果**看起来完全合理** ——
        会把资料写到一个不相干的号上。
        """
        url = f"https://www.douyin.com/user/{SEC_UID}?sec_uid=MS4wLjABAAAAsomeoneelse"
        assert (await make_adapter(tmp_path).parse_creator_url(url)).platform_id == SEC_UID

    async def test_bare_id_still_yields_a_usable_profile_url(self, tmp_path: Path) -> None:
        """库里存的 profile_url 必须含得上 platform_id，否则下一轮对不上号。"""
        ref = await make_adapter(tmp_path).parse_creator_url(SEC_UID)
        assert SEC_UID in str(ref.profile_url)
        assert ref.source_url is None, "裸 ID 不是用户粘的链接，不该编造一个来源"

    async def test_url_encoded_in_the_sec_uid_slot_is_refused(self, tmp_path: Path) -> None:
        """§7.1 的脏行形状：`?sec_uid=` 的值本身被 URL 编码成一整条链接。

        `unquote` 之后它就是一个 URL。放它进库等于历史脏数据重生，
        所以宁可在门口红一次 —— 这一条就是 `is_http_url(sec_uid)` 存在的理由。
        """
        dirty = "https://www.iesdouyin.com/share/?sec_uid=https%3A%2F%2Fv.douyin.com%2FiAbc"
        with pytest.raises(PlatformError, match="而是一条 URL") as caught:
            await make_adapter(tmp_path).parse_creator_url(dirty)
        assert dirty in str(caught.value), "原文要带上，否则排查的人得重放一遍才知道输入是什么"

    @pytest.mark.parametrize("junk", ["", "   ", "not-a-url", "抖音", "https://example.com/user/x"])
    async def test_unrecognisable_input_raises_parse_url_with_the_original_text(
        self, tmp_path: Path, junk: str
    ) -> None:
        with pytest.raises(PlatformError) as caught:
            await make_adapter(tmp_path).parse_creator_url(junk)
        assert caught.value.stage == "parse_url"
        assert caught.value.platform == "douyin"
        if junk.strip():
            assert junk in str(caught.value)

    async def test_share_link_expanding_nowhere_reports_both_urls(self, tmp_path: Path) -> None:
        """短链过期 / 抖音换了跳转目标：两句 URL 都要在，不然没人知道断在哪一环。"""
        http, _ = collecting_client(
            [
                (
                    "v.douyin.com",
                    httpx.Response(
                        200, text="作品已失效", request=httpx.Request("GET", "https://x")
                    ),
                )
            ]
        )
        adapter = make_adapter(tmp_path, http=http)
        with pytest.raises(PlatformError, match="仍然认不出 sec_uid") as caught:
            await adapter.parse_creator_url("https://v.douyin.com/iAbc/")
        message = str(caught.value)
        assert "v.douyin.com/iAbc/" in message
        assert "作品已失效" not in message, "带 URL 就够，不必把整页文案塞进清单"

    async def test_network_failure_becomes_parse_url_not_a_traceback(self, tmp_path: Path) -> None:
        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("name resolution failed")

        http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        with pytest.raises(PlatformError, match="ConnectError") as caught:
            await make_adapter(tmp_path, http=http).parse_creator_url("https://v.douyin.com/iAbc/")
        assert caught.value.stage == "parse_url"
        assert isinstance(caught.value.__cause__, httpx.ConnectError), "原异常要挂在 __cause__ 上"


# =========================================================================== #
# 列表枚举（含 V1 的"宁可空手，不能脏"）
# =========================================================================== #


class TestListCreatorVideos:
    async def test_cards_become_video_meta_with_the_required_fields(self, tmp_path: Path) -> None:
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        videos = await collect(make_adapter(tmp_path, bridge=bridge), limit=3)
        assert [v.platform_video_id for v in videos] == [
            AWEME_ID,
            "7412345678901234568",
            "7412345678901234590",
        ]
        first = videos[0]
        assert first.platform == "douyin"
        assert first.title == "口播稿怎么写前三秒"
        assert str(first.webpage_url) == f"https://www.douyin.com/video/{AWEME_ID}"
        assert first.creator_ref.platform_id == SEC_UID
        assert first.like_count == 12000, "页面上写的是 1.2万，库里要能排序"
        assert first.published_at is None and first.duration_seconds is None

    async def test_grid_dirty_rows_are_dropped_not_registered(self, tmp_path: Path) -> None:
        """fixture 里那条 `id=https://v.douyin.com/ad/`（练字帖广告）必须被丢掉。

        V1 实测踩过：整页扫 `a[href*="/video/"]` 会把别人家的广告与推荐位算到
        本博主头上，**而且报成功**。采集口播稿的库，宁可这一轮空手。
        """
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        videos = await collect(make_adapter(tmp_path, bridge=bridge), limit=3)
        assert all(v.platform_video_id.isdigit() for v in videos)
        assert all("广告" not in v.title for v in videos)

    async def test_duplicate_cards_are_collapsed(self, tmp_path: Path) -> None:
        """异步水合会把同一张卡片扫到两次（fixture 里就摆了这么一条）。"""
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        ids = [
            v.platform_video_id
            for v in await collect(make_adapter(tmp_path, bridge=bridge), limit=99)
        ]
        assert len(ids) == len(set(ids)) == 3

    async def test_grid_not_rendered_raises_list_error_with_the_pages_own_words(
        self, tmp_path: Path
    ) -> None:
        """`ok:false` 那句中文要整段进异常：它区分"卡在验证页"与"首页被降级成推荐流"。"""
        bridge = FakeBridge(script=[page_payload("profile_page_grid_missing.json")])
        with pytest.raises(ListError) as caught:
            await collect(make_adapter(tmp_path, bridge=bridge))
        message = str(caught.value)
        assert "作品网格没有渲染" in message
        assert "停在 https://www.douyin.com/user/" in message
        assert caught.value.stage == "list"

    async def test_sec_uid_mismatch_is_refused_rather_than_attributed(self, tmp_path: Path) -> None:
        """打开 A 主页被重定向到 B（登录态串页）。

        这里必须红。用页面自报的 sec_uid 继续跑，等于把 B 的作品登记给 A，
        而库里已有的 A 那一行会被刷成 B 的昵称 —— 数据坏了还不报错。
        """
        bridge = FakeBridge(script=[page_payload("profile_page_sec_uid_mismatch.json")])
        with pytest.raises(PlatformError, match="串号") as caught:
            await collect(make_adapter(tmp_path, bridge=bridge))
        assert "MS4wLjABAAAAOTHERperson" in str(caught.value)

    async def test_scrolling_only_happens_when_the_first_screen_is_short(
        self, tmp_path: Path
    ) -> None:
        """首屏已经够 limit 条 → 不该再导航一次、滚一次。"""
        bridge = FakeBridge(script=[page_payload("profile_page.json")])
        videos = await collect(make_adapter(tmp_path, bridge=bridge), limit=2)
        assert len(videos) == 2
        assert bridge.navigated == [f"https://www.douyin.com/user/{SEC_UID}"]

    async def test_scroll_failure_keeps_the_first_screen(self, tmp_path: Path) -> None:
        """滚动那一趟炸了只降级成"沿用首屏"并记一条 warning，不掀掉整轮。"""
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), BridgeError("douyin", "list", "桥没响应")]
        )
        videos = await collect(make_adapter(tmp_path, bridge=bridge), limit=30)
        assert [v.platform_video_id for v in videos] == [AWEME_ID, "7412345678901234568"]

    async def test_a_dead_browser_propagates_with_its_own_text(self, tmp_path: Path) -> None:
        """浏览器死了（`BridgeError`）**必须往外抛**，不能消化成空列表。

        V1 §7.20：消化成"没有作品"的话自愈分支永远走不到，
        症状变成"这个博主好像一条都没发"。
        """
        bridge = FakeBridge(
            script=[BridgeError("douyin", "list", "桥的浏览器已关闭：Context has been closed")]
        )
        with pytest.raises(BridgeError, match="Context has been closed"):
            await collect(make_adapter(tmp_path, bridge=bridge))

    async def test_since_cannot_drop_undated_cards(self, tmp_path: Path) -> None:
        """**抖音的 since 是弱过滤**：网格卡片上没有发布时间，判不了就放行。

        这条守的是"别把未知当结论"：按 0/None 判旧会把一整轮作品丢掉，
        而看板上只表现为"今天怎么一条都没采到"。真正的增量靠 Task 8 按 ID 查重。
        """
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        videos = await collect(
            make_adapter(tmp_path, bridge=bridge),
            limit=3,
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert len(videos) == 3

    async def test_a_real_publish_date_is_filtered_by_the_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`since` 判得动的时候**要真的判掉**。

        旧写法从头到尾没进过 `list_creator_videos`：它自己 `model_copy` 一个带日期的
        meta，再断言两个手搓 datetime 谁大谁小 —— 把适配器里那行
        `if since is not None and … < since: continue` 删掉，它照样绿
        （`tests/contracts` 的覆盖率也显示那一行从未被执行）。

        这里只替"卡片→meta"这一层映射：抖音列表页本来就不给发布日期，
        `card_to_video_meta` 因此把 `published_at` 写死 None（§7 记过的弱过滤，
        不是 bug），所以要人造一个日期才走得到比较那一行。**过滤器本身是真代码。**
        """
        old = datetime(2020, 1, 1, tzinfo=UTC)
        fresh = datetime(2030, 1, 1, tzinfo=UTC)
        real = adapter_module.card_to_video_meta

        def dated(card: VideoCard, *, ref: CreatorRef) -> VideoMeta:
            stamp = old if card.aweme_id == AWEME_ID else fresh
            return real(card, ref=ref).model_copy(update={"published_at": stamp})

        monkeypatch.setattr(adapter_module, "card_to_video_meta", dated)

        async def collect(since: datetime | None) -> set[str]:
            bridge = FakeBridge(
                script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
            )
            adapter = make_adapter(tmp_path / str(since), bridge=bridge)
            out: set[str] = set()
            async for meta in adapter.list_creator_videos(profile_ref(), limit=3, since=since):
                out.add(meta.platform_video_id)
            return out

        unfiltered = await collect(None)
        assert AWEME_ID in unfiltered, "先确认那条卡片真的会被枚举出来"

        kept = await collect(fresh)

        assert AWEME_ID not in kept
        assert unfiltered - kept == {AWEME_ID}, "只该掉那一条过期的，别的都不许受影响"

    async def test_limit_caps_output_and_partial_iteration_closes_cleanly(
        self, tmp_path: Path
    ) -> None:
        """调度器中途取消走的是 `aclose()`：只取 1 条就关掉，不许抛。"""
        bridge = FakeBridge(
            script=[page_payload("profile_page.json"), page_payload("scroll_page.json")]
        )
        adapter = make_adapter(tmp_path, bridge=bridge)
        generator = adapter.list_creator_videos(profile_ref(), limit=99)
        first = await generator.__anext__()
        await generator.aclose()
        assert first.platform_video_id == AWEME_ID

    async def test_limit_zero_yields_nothing_and_opens_no_page(self, tmp_path: Path) -> None:
        bridge = FakeBridge(script=[page_payload("profile_page.json")])
        assert await collect(make_adapter(tmp_path, bridge=bridge), limit=0) == []
        assert bridge.navigated == [], "limit=0 连页都不该进"

    def test_the_shipped_page_budget_would_make_tests_crawl(self) -> None:
        """出厂预算是真等 12 秒量级（用例靠改小它才快）。
        但默认值不能被顺手改成 0 —— 那是给真实采集用的。"""
        default = PageBudget()
        assert default.grid_poll_rounds * default.grid_poll_ms >= 5_000
        assert default.max_scroll_rounds >= 5

    def test_page_js_placeholders_are_injected_unquoted(self) -> None:
        """V1 §2 契约三：占位符**不加引号**，注入的是 `json.dumps(value)`。

        模板里写成 `"__EXPECTED_SEC_UID__"` 会拼成 `""MS4w…""` → 页面 SyntaxError，
        而 Python 侧只看到一个 evaluate 失败，排查方向会被带去"网站风控"。
        """
        js = render_page_js(
            "const expected = __EXPECTED_SEC_UID__; const rounds = __GRID_POLL_ROUNDS__;",
            sec_uid=SEC_UID,
            budget=PageBudget(grid_poll_rounds=3, grid_poll_ms=120),
        )
        assert f'const expected = "{SEC_UID}";' in js
        assert "const rounds = 3;" in js
        assert '""MS4w' not in js

    def test_no_shipped_template_wraps_a_placeholder_in_quotes(self) -> None:
        """上一条只渲染**手搓的一行模板**，所以它钉的是渲染器，不是出厂模板。

        实测把 `PROFILE_PAGE_FUNCTION` 里的 `const expected = __EXPECTED_SEC_UID__;`
        加上引号，整套离线用例仍然全绿（`"__" not in rendered` 那种断言照样成立），
        而真机上每一次抖音采集都会 SyntaxError —— Python 侧只看到一个 evaluate 失败，
        排查方向被带去"网站风控"。这条改钉**出厂的三个模板本身**。
        """
        shipped = {
            "PROFILE_PAGE_FUNCTION": PROFILE_PAGE_FUNCTION,
            "COLLECT_AFTER_SCROLL_FUNCTION": COLLECT_AFTER_SCROLL_FUNCTION,
            "VIDEO_DETAIL_FUNCTION": media_module.VIDEO_DETAIL_FUNCTION,
        }
        assert all(shipped.values()), "模板常量不该被清空"
        for name, template in shipped.items():
            quoted = _QUOTED_PLACEHOLDER.findall(template)
            assert not quoted, f"{name} 里这些占位符被引号包住了：{quoted}"

    def test_the_rendered_shipped_template_holds_exactly_one_quote_pair(self) -> None:
        """正向那半：渲染出来的确实是一个字符串字面量，不是 `""MS4w…""`。"""
        js = render_page_js(PROFILE_PAGE_FUNCTION, sec_uid=SEC_UID, budget=PageBudget())
        assert f'const expected = "{SEC_UID}";' in js
        assert f'""{SEC_UID}' not in js

    def test_a_leftover_placeholder_is_refused_at_render_time(self) -> None:
        """漏替换的占位符会以 `ReferenceError` 出现在桥那一头，
        Python 侧只看到"evaluate 失败" —— 所以要在渲染这一步就拦。"""
        with pytest.raises(ValueError, match="__BRAND_NEW__"):
            render_page_js("const x = __BRAND_NEW__;", sec_uid=SEC_UID, budget=PageBudget())

    def test_decode_page_result_accepts_dict_and_reports_unparsable_text(self) -> None:
        assert decode_page_result({"ok": True}, stage="list") == {"ok": True}
        with pytest.raises(ListError, match="不是合法 JSON"):
            decode_page_result("<html>验证码</html>", stage="list")
        with pytest.raises(ListError, match="可解析结果"):
            decode_page_result(["not", "a", "dict"], stage="list")

    def test_profile_page_function_ships_the_expected_placeholders(self) -> None:
        """模板与渲染器之间的契约：模板加占位符，渲染器必须知道怎么填。

        这一条防的是"改了 JS 忘了改 `placeholder_values()`" ——
        那个错在真机上表现为页面上一个 ReferenceError，而在测试里
        只有这一条能抓到。
        """
        rendered = render_page_js(PROFILE_PAGE_FUNCTION, sec_uid=SEC_UID, budget=PageBudget())
        assert "__" not in rendered
        scrolled = render_page_js(
            COLLECT_AFTER_SCROLL_FUNCTION, sec_uid=SEC_UID, budget=PageBudget(), wanted=9
        )
        assert "const wanted = 9;" in scrolled
        assert "__" not in scrolled


# =========================================================================== #
# 资料解析
# =========================================================================== #


class TestFetchCreatorProfile:
    async def test_profile_fields_are_parsed(self, tmp_path: Path) -> None:
        bridge = FakeBridge(script=[page_payload("profile_page.json")])
        profile = await make_adapter(tmp_path, bridge=bridge).fetch_creator_profile(profile_ref())
        assert profile.name == "姜胡说"
        assert profile.follower_count == 124_000, (
            "12.4万 → int。V1 那个 format 方向是反的（给飞书看）"
        )
        assert str(profile.avatar_url).startswith("https://p3.douyinpic.com")
        assert profile.ref.platform_id == SEC_UID

    async def test_scheme_relative_avatar_becomes_none_instead_of_crashing(
        self, tmp_path: Path
    ) -> None:
        """页面给的头像经常没有协议头。收成 None，而不是让 Pydantic 在采集链路里抛。"""
        bridge = FakeBridge(script=[page_payload("profile_page_placeholder.json")])
        profile = await make_adapter(tmp_path, bridge=bridge).fetch_creator_profile(profile_ref())
        assert profile.avatar_url is None
        assert profile.follower_count == 1285, "'粉丝 1,285' 这种带前缀的也要能读"
        assert profile.extra["nickname_is_placeholder"] is True, "占位昵称不能覆盖库里已有的真名字"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1234", 1234),
            ("1,234", 1234),
            ("1.2万", 12000),
            ("3.5亿", 350_000_000),
            ("10w+", 100_000),
            ("粉丝 1.2万", 12000),
            ("", None),
            ("未登录", None),
            (None, None),
            (True, None),
            (-5, None),
            (7, 7),
        ],
    )
    def test_count_parsing(self, text: object, expected: int | None) -> None:
        assert parse_cn_count(text) == expected


# =========================================================================== #
# §7.3 —— cookie 档位阶梯
# =========================================================================== #


class TestCookieLadder:
    def ladder(
        self, tmp_path: Path, config: DouyinConfig, environ: dict[str, str] | None = None
    ) -> CookieLadder:
        return resolve_cookie_ladder(
            DouyinAdapter.capabilities.cookie_variants,
            config=config,
            cookies=CookieManager(FileStorage(tmp_path / "data")),
            environ=environ or {},
        )

    def test_order_comes_from_capabilities_and_starts_with_the_exported_file(
        self, tmp_path: Path
    ) -> None:
        """V1 §7.3 / §7.15：导出文件排第一。守的是 ADR-0011 —— 配置里没有第二个顺序。"""
        config = fast_config(
            cookies_file=write_cookie_file(tmp_path / "cfg" / "douyin.com.txt"),
            ytdlp_cookies_from_browser="chrome",
        )
        rungs = self.ladder(tmp_path, config).variants
        assert [v.kind for v in rungs] == ["exported_file", "browser", "none"]
        assert rungs[0].args[0] == "--cookies"
        assert rungs[1].args == ("--cookies-from-browser", "chrome")
        assert rungs[2].args == ()
        assert all(isinstance(v, YtDlpCookieVariant) for v in rungs)

    def test_header_only_cookie_file_is_not_offered_as_a_login_rung(self, tmp_path: Path) -> None:
        """§7.15 的另一半坑：**只有表头**的文件传出去 yt-dlp 一声不吭，只按匿名处理。

        比"文件不存在"更阴 —— 整条链路都是绿的，症状要过几天才浮出来
        （"这批视频怎么这么糊"）。所以它连"导出文件档"都不算。
        """
        empty = write_cookie_file(tmp_path / "cfg" / "douyin.com.txt", with_cookie=False)
        result = self.ladder(tmp_path, fast_config(cookies_file=empty))
        assert [v.kind for v in result.variants] == ["none"]
        assert result.cookie_file is None
        assert "一条 cookie 都没有" in (result.note or "")

    def test_missing_configured_path_falls_back_to_the_convention_path_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """配置指的路径不在（多半是相对路径 + 换了 CWD），约定那份能用 → 用约定那份，
        并把"配置那处为什么没用上"留在 note 里。静默换 = 第二处真相。"""
        files = CookieManager(FileStorage(tmp_path / "data"))
        managed = files.write_netscape(
            "douyin.com",
            [{"domain": ".douyin.com", "name": "sessionid", "value": "abc", "expires": 1893456000}],
        )
        result = self.ladder(tmp_path, fast_config(cookies_file=tmp_path / "nowhere" / "x.txt"))
        assert [v.kind for v in result.variants] == ["exported_file", "none"]
        assert result.cookie_file == managed
        assert "文件不存在" in (result.note or "")

    def test_env_var_wins_over_config_and_says_so(self, tmp_path: Path) -> None:
        """兼容 V1 的 `DOUYIN_YTDLP_COOKIES_FILE`：env 优先，note 里要说明是谁赢的。"""
        from_env = write_cookie_file(tmp_path / "from-env" / "douyin.com.txt")
        config = fast_config(cookies_file=write_cookie_file(tmp_path / "cfg" / "douyin.com.txt"))
        result = self.ladder(tmp_path, config, {media_module.ENV_COOKIES_FILE: str(from_env)})
        assert result.cookie_file == from_env
        assert "环境变量" in (result.note or "")

    def test_browser_rung_absent_unless_somewhere_names_a_browser(self, tmp_path: Path) -> None:
        """V1 §7.3：Windows 上这一档永远读不出来，所以**默认没有**这一档。"""
        config = fast_config(cookies_file=write_cookie_file(tmp_path / "cfg" / "douyin.com.txt"))
        assert [v.kind for v in self.ladder(tmp_path, config).variants] == ["exported_file", "none"]
        from_env = self.ladder(tmp_path, config, {media_module.ENV_COOKIES_FROM_BROWSER: "edge"})
        assert "browser" in [v.kind for v in from_env.variants]
        assert from_env.browser == "edge"

    def test_a_healthy_ladder_says_nothing(self, tmp_path: Path) -> None:
        """没问题时 note 是 None。

        健康检查的 `detail` 会原样显示在预检页那一行，全绿却挂一句流水账
        会让人以为"有情况但没说清"。
        """
        config = fast_config(cookies_file=write_cookie_file(tmp_path / "cfg" / "douyin.com.txt"))
        assert self.ladder(tmp_path, config).note is None

    async def test_the_ladder_actually_reaches_yt_dlp_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """阶梯不能只是"算出来对了"，要真的传进下载那一趟。"""
        config = fast_config(
            cookies_file=write_cookie_file(tmp_path / "cfg" / "douyin.com.txt"),
            ytdlp_cookies_from_browser="chrome",
        )
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[write_media(tmp_path, "media.mp4")]))
        adapter = make_adapter(tmp_path, config=config)
        monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: runner)
        await adapter.download_media(make_video(), tmp_path / "media")
        assert [v.kind for v in runner.calls[0]["variants"]] == ["exported_file", "browser", "none"]
        assert runner.calls[0]["file_template"] == "media.%(ext)s", "与页面直链那条路同名"


# =========================================================================== #
# §7.2 —— yt-dlp 必失败，兜底是常态，原文要留着
# =========================================================================== #


def write_media(tmp_path: Path, name: str, *, size: int = 2048) -> Path:
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    path = media_dir / name
    path.write_bytes(b"x" * size)
    return path


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
    return YtDlpResult(
        ok=False,
        variant=YtDlpCookieVariant("none", (), "匿名（登录档画质不可用）"),
        stdout="",
        stderr=(
            "ERROR: [Douyin] 7412345678901234567: "
            "Fresh cookies (not necessarily logged in) are needed\n"
        ),
        returncode=1,
        attempts=(
            ("带导出的登录 cookie", 1, "Fresh cookies needed"),
            ("匿名（登录档画质不可用）", 1, "Fresh cookies needed"),
        ),
    )


@dataclass
class MediaHarness:
    adapter: DouyinAdapter
    bridge: FakeBridge
    requested: list[str]


def media_harness(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeYtDlpRunner,
    detail: Any = None,
    play_bytes: int = 4096,
    config: DouyinConfig | None = None,
) -> MediaHarness:
    """一条走到底的抖音媒体链路：假 runner + 假桥（给播放直链）+ 假 CDN。"""
    script: list[Any] = [detail if detail is not None else page_payload("video_detail.json")]
    bridge = FakeBridge(script=script)
    play = httpx.Response(200, content=b"video-" + b"x" * play_bytes)
    http, requested = collecting_client([("douyinvod.com", play)])
    adapter = make_adapter(tmp_path, bridge=bridge, http=http, config=config)
    monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: runner)
    return MediaHarness(adapter=adapter, bridge=bridge, requested=requested)


class TestDownloadMedia:
    SIGNED = "v3-web.douyinvod.com"

    async def test_an_unmerged_dash_pair_falls_back_instead_of_claiming_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§7.21 会从"声明不支持分片"的那一侧重新进来。

        抖音路上 yt-dlp **通常**直接失败（§7.2），兜底才是常态。但它偶尔真下动了却没合并，
        报回来的是 `media.f137.mp4`（纯视频轨，大）+ `media.f140.m4a`（音频轨，小）。
        旧实现"按体积取主文件、其余只记日志不入库" → 那条**无声视频轨**被记成
        `media_source="yt_dlp"` 的成品；而本机没有 ffprobe 时 `has_audio_stream()`
        返回 True（问不出来就当有），于是这条媒体**声称自己有音轨**。
        下游 `extract_audio(-vn)` 得到的正是 §7.21 描述的那句"长得像 ffmpeg 没装"的错。

        抖音声明 `supports_dash_split=False`，没有诚实表达"这是一对"的字段，
        所以正确行为是把这一趟判为失败、交给页面播放直链那条真正走得通的路。
        """
        video_part = write_media(tmp_path, "media.f137.mp4", size=40_000)
        audio_part = write_media(tmp_path, "media.f140.m4a", size=2_000)
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[video_part, audio_part]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)

        artifact = await harness.adapter.download_media(make_video(), tmp_path / "media")

        assert artifact.media_source == "page_play_url"  # 走了兜底，没把无声轨当成品
        assert artifact.yt_dlp_error is not None
        assert "未合并" in artifact.yt_dlp_error

    async def test_yt_dlp_success_marks_the_source_and_keeps_error_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[write_media(tmp_path, "media.mp4")]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert artifact.media_source == "yt_dlp"
        assert artifact.yt_dlp_error is None
        assert isinstance(artifact, SingleFileArtifact)
        assert artifact.size_bytes > 0
        assert harness.requested == [], "yt-dlp 成了就不该再去问直链"

    async def test_fallback_marks_the_source_and_keeps_the_yt_dlp_original_text(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """V1 §7.2 的看护本体：兜底成功时 **yt-dlp 的失败原文必须留着**。

        V1 早期一兜底成功就把原文丢掉，日志只剩"未拿到媒体"，
        于是永远判断不出该修什么 —— 那句"看起来在跑"就是这么来的。
        """
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert artifact.media_source == "page_play_url"
        assert artifact.yt_dlp_error is not None
        assert "Fresh cookies" in artifact.yt_dlp_error
        assert "匿名（登录档画质不可用）→exit 1" in artifact.yt_dlp_error, "每一档的退出码都要在"
        assert harness.requested == [
            f"https://{self.SIGNED}/signed/abc?signature=XYZ&expires=1735689600"
        ]
        assert (tmp_path / "media" / "media.mp4").is_file()

    async def test_signed_play_url_never_reaches_the_artifact(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """CDN 直链是签名的、几小时后失效（V1 §7.2）。当持久数据用 = 库里躺一堆死链。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "media")
        dumped = json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False)
        assert "douyinvod" not in dumped and "signature" not in dumped
        assert "http" not in str(artifact.path)

    async def test_the_address_is_asked_from_the_page_context_not_from_python(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """ "问地址"必须在页面上下文里问（Python 直接发是 403）。
        所以它是桥的 navigate + evaluate，而**字节流**才走 httpx。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert harness.bridge.navigated == [f"https://www.douyin.com/video/{AWEME_ID}"]
        assert "__AWEME_ID__" not in harness.bridge.evaluated[0]
        assert harness.requested, "落盘这一半确实在 Python 这边"

    async def test_missing_yt_dlp_binary_still_falls_back_and_says_so(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """V1 §7.2 + §1.3：没装 yt-dlp 不是失败，兜底本来就是这条路。"""
        runner = FakeYtDlpRunner(
            raises=LookupError("找不到可执行文件 'yt-dlp'（命令：yt-dlp ...）")
        )
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert artifact.media_source == "page_play_url"
        assert "未安装 yt-dlp" in (artifact.yt_dlp_error or "")

    async def test_tiny_error_page_is_not_accepted_as_media(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """CDN 失效时回的是几百字节的 JSON 错误页，HTTP 状态还是 200。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner, play_bytes=10)
        with pytest.raises(MediaDownloadError, match="判为无效响应"):
            await harness.adapter.download_media(make_video(), tmp_path / "media")

    async def test_no_partial_file_survives_a_failed_download(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """半途失败不许留下 `.part`，也不许留下一个看起来完整的 `media.mp4` ——
        后处理扫到它就会跳过重下，库里从此躺一段坏文件。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner, play_bytes=10)
        with pytest.raises(MediaDownloadError):
            await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert not list((tmp_path / "media").rglob("*"))

    async def test_both_rounds_failures_are_reported_together(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """两条路都走过了，各自的错都得留一份 —— 只留一句"未拿到媒体"就是重演 V1。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            runner=runner,
            detail=page_payload("video_detail_http_403.json"),
        )
        with pytest.raises(MediaDownloadError) as caught:
            await harness.adapter.download_media(make_video(), tmp_path / "media")
        message = str(caught.value)
        assert "http_403" in message, "页面那一轮的原文"
        assert "Fresh cookies" in message, "yt-dlp 那一轮的原文"

    async def test_fallback_can_be_switched_off_by_config(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`fallback_to_page_play_url=False` 时不许偷偷兜底 —— 那是用户明确的选择。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            runner=runner,
            config=fast_config(fallback_to_page_play_url=False),
        )
        with pytest.raises(MediaDownloadError, match="关掉了兜底"):
            await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert runner.calls, "yt-dlp 那一趟还是要跑的"

    async def test_progress_callback_sees_yt_dlp_percentages(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        seen: list[float] = []
        runner = FakeYtDlpRunner(
            result=ytdlp_ok(artifacts=[write_media(tmp_path, "media.mp4")]),
            emit_lines=[
                "[download]  40.0% of 1.00MiB",
                "[info] 不是一行进度",
                "[download] 100.0% of 1.00MiB",
            ],
        )
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        await harness.adapter.download_media(
            make_video(), tmp_path / "media", on_progress=seen.append
        )
        assert seen == [0.4, 1.0], "认不出的行不该报 0（那会让进度条反复跳回起点）"

    async def test_a_broken_progress_callback_does_not_fail_the_download(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def explode(_fraction: float) -> None:
            raise RuntimeError("SSE 那边炸了")

        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(
            make_video(), tmp_path / "media", on_progress=explode
        )
        assert artifact.path.is_file(), "文件已经落到磁盘了，回调炸不该把它判死"

    async def test_an_unprobeable_file_is_still_reported_as_having_audio(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """V1 §7.21 的形状：问不出来算有音频。判成"没有"的后果是白丢一段口播稿。

        这台机器的 PATH 里**真的没有 ffprobe**（V1 §7.19），所以这条顺带验了
        "缺二进制不会把一次已经成功的下载变成采集失败"。
        """
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[write_media(tmp_path, "media.mp4")]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert artifact.has_audio is True

    async def test_extra_yt_dlp_artifacts_are_ignored_and_logged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """抖音声明 `supports_dash_split=False`：多出来的分片不能被当成第二条作品。"""
        small = write_media(tmp_path, "media.f140.m4a", size=64)
        big = write_media(tmp_path, "media.mp4", size=4096)
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[small, big]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        artifact = await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert artifact.path == big
        assert artifact.media_source == "yt_dlp"


class TestTheOutsideWorldMisbehaves:
    """桥 / CDN 回了**形状不对**的东西时该红成什么样。

    这一组存在的理由：正常 fixture 只能证明"页面配合时我们对"，而采集链路每天面对的是
    页面改版、风控中间页、CDN 抽风。这些分支没有用例，就等于"实现里写了但没人确认它
    真的会走到" —— 那是最舒服的一种死代码。
    """

    async def test_play_urls_that_is_not_a_list_is_reported(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            runner=runner,
            detail=json.dumps({"ok": True, "play_urls": "https://v3-web.douyinvod.com/one"}),
        )
        with pytest.raises(MediaDownloadError, match="形状不对"):
            await harness.adapter.download_media(make_video(), tmp_path / "media")

    async def test_only_non_http_candidates_counts_as_no_link_at_all(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`ok:true` 但地址全是相对串 / `javascript:` —— 一个都不能当成可下的候选。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            runner=runner,
            detail=json.dumps(
                {"ok": True, "play_urls": ["//v3-web.douyinvod.com/x.mp4", "javascript:alert(1)"]}
            ),
        )
        with pytest.raises(MediaDownloadError, match="没给出任何播放直链") as caught:
            await harness.adapter.download_media(make_video(), tmp_path / "media")
        assert "Fresh cookies" in str(caught.value), "yt-dlp 那一轮的原文还是要带上"
        assert harness.requested == [], "没有合法地址就不该发出一个请求"

    async def test_a_403_from_the_cdn_is_reported_candidate_by_candidate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        bridge = FakeBridge(script=[page_payload("video_detail.json")])
        http, _ = collecting_client([("douyinvod.com", httpx.Response(403, text="forbidden"))])
        adapter = make_adapter(tmp_path, bridge=bridge, http=http)
        monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: runner)
        with pytest.raises(MediaDownloadError) as caught:
            await adapter.download_media(make_video(), tmp_path / "media")
        assert "候选 1/2: HTTP 403" in str(caught.value), "两个候选、两次错，都要说得清"

    async def test_a_connection_drop_mid_stream_is_reported_as_that_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        bridge = FakeBridge(script=[page_payload("video_detail.json")])

        def drop(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection reset")

        http = httpx.AsyncClient(transport=httpx.MockTransport(drop))
        adapter = make_adapter(tmp_path, bridge=bridge, http=http)
        monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: runner)
        with pytest.raises(MediaDownloadError, match="ReadError"):
            await adapter.download_media(make_video(), tmp_path / "media")
        assert not list((tmp_path / "media").rglob("*")), "断流之后不许留 .part"

    async def test_yt_dlp_reporting_paths_that_are_not_there(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """yt-dlp 回 0 但报出来的文件一个都读不到（被杀、写错盘）。

        不许兜底成"当它成功了"：`media_path` 进库指着一个不存在的文件，
        症状是过几天转写时报"文件找不到"，而没人会想到是采集那轮的退出码在骗人。
        """
        ghost = tmp_path / "media" / "media.mp4"
        runner = FakeYtDlpRunner(result=ytdlp_ok(artifacts=[ghost]))
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        with pytest.raises(MediaDownloadError, match="一个都读不到"):
            await harness.adapter.download_media(make_video(), tmp_path / "media")

    async def test_a_non_numeric_video_id_never_reaches_the_page(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """脏 `platform_video_id`（历史迁移数据里有）不该被拼进详情接口。"""
        runner = FakeYtDlpRunner(result=ytdlp_blocked())
        harness = media_harness(tmp_path, monkeypatch=monkeypatch, runner=runner)
        with pytest.raises(MediaDownloadError, match="不是合法的抖音作品 ID"):
            await harness.adapter.download_media(make_video("not-a-number"), tmp_path / "media")
        assert harness.bridge.navigated == []


# =========================================================================== #
# 字幕（能力声明的反面）
# =========================================================================== #


class TestSubtitles:
    async def test_returns_none_without_raising(self, tmp_path: Path) -> None:
        """抖音压根没有公开字幕轨。抛异常会让"每条作品都先失败一次"变成常态。"""
        assert await make_adapter(tmp_path).fetch_subtitles(make_video()) is None


# =========================================================================== #
# §7.20 —— 红灯的形状
# =========================================================================== #


class TestHealthcheck:
    async def test_bridge_503_is_degraded_not_unreachable(self, tmp_path: Path) -> None:
        """ "桥在跑、浏览器被关了"必须放行：自愈只发生在第一条真请求上。

        V1 2026-09-21 真栽过一次 —— 预检被自己的健康检查堵死，
        文案变成"请先启动 Chrome/CDP 代理"，而桥就在 3457 上跑着。
        """
        bridge = FakeBridge(
            health=BridgeHealth(reachable=True, browser_ok=False, error="Context has been closed")
        )
        report = await make_adapter(tmp_path, bridge=bridge).healthcheck()
        assert report.components["bridge"] == "degraded"
        assert report.status == "degraded"
        assert "第一条真请求会自愈" in (report.detail or "")
        assert report.is_healthy is False

    async def test_bridge_not_listening_is_unreachable(self, tmp_path: Path) -> None:
        bridge = FakeBridge(
            health=BridgeHealth(reachable=False, browser_ok=False, error="ConnectError")
        )
        report = await make_adapter(tmp_path, bridge=bridge).healthcheck()
        assert report.components["bridge"] == "unreachable"
        assert report.status == "unreachable"

    async def test_bridge_none_is_an_assembly_error_explained_in_words(
        self, tmp_path: Path
    ) -> None:
        """声明了 needs_browser=True 却拿到 None 是**装配错误**。

        要的是 healthcheck 里一条能读懂的红，而不是第一个请求上的 AttributeError。
        """
        adapter = make_adapter(tmp_path, bridge=None)
        report = await adapter.healthcheck()
        assert report.components["bridge"] == "unreachable"
        assert "装配错误" in (report.detail or "")
        with pytest.raises(PlatformError, match="装配错误"):
            await adapter._open_profile_page(profile_ref())

    async def test_everything_probeable_and_present_is_green(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "intelligence_hub_v2.platforms.douyin.adapter.shutil.which", lambda _n: "/x/yt-dlp"
        )
        CookieManager(FileStorage(tmp_path / "data")).write_netscape(
            "douyin.com",
            [{"domain": ".douyin.com", "name": "s", "value": "v", "expires": 1893456000}],
        )
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components == {"bridge": "ok", "cookies": "ok", "yt_dlp": "ok"}
        assert report.status == "ok" and report.is_healthy is True
        assert report.detail is None, "全绿时不该编一句"

    async def test_no_cookie_file_is_degraded_not_unreachable(self, tmp_path: Path) -> None:
        """抖音的登录态在桥那份 profile 里；cookie 文件只影响 yt-dlp 那一档的画质。"""
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components["cookies"] == "degraded"
        assert "匿名" in (report.detail or "")

    async def test_missing_yt_dlp_is_degraded_because_the_fallback_does_not_need_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """判成 unreachable 会让人去装包，而这条链路本来就能跑通（§7.2）。"""
        monkeypatch.setattr(
            "intelligence_hub_v2.platforms.douyin.adapter.shutil.which", lambda _n: None
        )
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components["yt_dlp"] == "degraded"
        assert "页面播放直链" in (report.detail or "")

    async def test_checked_at_is_timezone_aware(self, tmp_path: Path) -> None:
        report = await make_adapter(tmp_path).healthcheck()
        assert report.checked_at.tzinfo is not None


# =========================================================================== #
# 限速闸（出厂值下也要真的在跳）
# =========================================================================== #


class TestRateLimit:
    async def test_pacer_waits_the_configured_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []

        async def fake_sleep(delay: float, *args: object, **kwargs: object) -> None:
            slept.append(delay)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        pacer = RatePacer(per_minute=30)
        await pacer.wait()
        assert slept == [], "第一趟不该先憋住"
        await pacer.wait()
        assert slept == [pytest.approx(2.0, abs=0.05)], "per_minute=30 → 间隔 2 秒"

    async def test_adapter_paces_at_the_shipped_per_minute_budget(self, tmp_path: Path) -> None:
        """用例里全都关了限速（不然契约测试要跑两分钟），所以这一条用**出厂配置**
        验闸门本身没被写歪 —— 否则"测试通过"只证明了关掉的那部分能关掉。"""
        shipped = DouyinConfig(display_name="抖音")  # per_minute 默认 30
        adapter = make_adapter(tmp_path, config=shipped)
        assert adapter._pacer.interval_seconds == pytest.approx(2.0)


class TestContractShape:
    def test_registered_under_the_platform_name(self) -> None:
        assert PLATFORMS["douyin"] is DouyinAdapter

    def test_satisfies_the_protocol(self, tmp_path: Path) -> None:
        adapter: PlatformAdapter = make_adapter(tmp_path)
        assert isinstance(adapter, PlatformAdapter)

    def test_capabilities_match_the_shipped_spec_table(self) -> None:
        """`docs/specs/platform-adapter.md §3` 那张表的一行。改这里等于改契约，要走 ADR。"""
        assert DouyinAdapter.capabilities == Capabilities(
            needs_browser=True,
            needs_cookies=True,
            cookie_variants=("exported_file", "browser", "none"),
            supports_subtitles=False,
            supports_dash_split=False,
            list_strategy="browser_scroll",
            media_strategy="yt_dlp_with_fallback",
        )

    def test_capabilities_and_the_config_defaults_agree_on_strategies(self, tmp_path: Path) -> None:
        """能力声明与配置默认值是两处真相，**必须**一致 ——
        不一致时前端按配置渲染、调度器按能力决策，两边各说各话。"""
        defaults = DouyinConfig(display_name="抖音")
        caps = DouyinAdapter.capabilities
        assert caps.list_strategy == defaults.list_strategy
        assert caps.media_strategy == defaults.media_strategy
        assert caps.needs_browser == defaults.use_cdp_bridge
        assert set(caps.cookie_variants) >= {"exported_file", "none"}

    def test_config_schema_is_the_douyin_model(self) -> None:
        assert DouyinAdapter.config_schema() is DouyinConfig

    def test_wrong_config_type_is_an_assembly_error_not_an_attribute_error(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PlatformError, match="装配错误"):
            DouyinAdapter(BilibiliConfig(display_name="B站"), make_deps(tmp_path))

    async def test_fetch_subtitles_is_declared_unsupported_in_capabilities(
        self, tmp_path: Path
    ) -> None:
        """能力声明说没有字幕，实现就必须真的不抛 —— 两处不一致会让调度器白等一轮重试。"""
        assert DouyinAdapter.capabilities.supports_subtitles is False
        assert await make_adapter(tmp_path).fetch_subtitles(make_video()) is None
