"""`XiaohongshuAdapter` —— V1 `download_xiaohongshu_latest.py` 站在 `PlatformAdapter` 契约后面。

V1 §7 的看护各自落在哪个方法里（全表见 `docs/specs/contract-tests.md`）：

| 陷阱 / 契约 | 落点 |
|---|---|
| §7.1 身份是 `user_id`，短链必须跟一次 302 | `parse_creator_url` + `xiaohongshu/urls.py` |
| ADR-0016 `xsec_token` 是门票不是身份 | `listing.NoteCard.xsec_token` 只进 `VideoMeta.extra` |
| 主页串页（打开 A 被重定向到 B）整页丢弃 | `listing.parse_profile_payload` |
| 笔记串号（跳到别的笔记）整条丢弃 | `detail.parse_detail_payload`（V1 :971 那条） |
| 登录墙 ≠ 查无此人 | 两处：`listing`（主页）与 `detail`（搜索页）各一套字段 |
| **图文笔记不是失败**（ADR-0019） | `download_media` 的分流 + `media.download_image_set` |
| §7.3 cookie 阶梯（Windows 读不出 Chrome 的库） | `media.resolve_cookie_ladder` |
| §7.20 桥的 503 是"浏览器没了"不是"桥没起" | `healthcheck` |

与 V1 的四处**有意**不同，别当疏漏：

1. V1 把清单、入库、转写、资料回写都写在这一个 1572 行脚本里；这里只做"采集"。
   入库是 `tasks/collect.py`，清单是 `core/manifest.py`，转写是 `tasks/postprocess.py`。
   判据：本模块**不 import Repository，也不 publish 事件**。
2. V1 兜底成功时会把 yt-dlp 那一轮的错**清零**（`download_xiaohongshu_latest.py:1051`
   那句 `media["errors"] = []`）。V2 反过来：`yt_dlp_error` 必须留着原文。
   丢了它就判断不出该修什么，而"看起来在跑"就是这个仓库的历史问题（V1 §7.2 同一条）。
3. V1 的 `download_note_images` 拉不到图只 `print` 一句然后继续，最后交回一个
   "这条笔记有媒体"的记录。V2 一张都没拿到时**如实抛** `MediaDownloadError`。
4. V1 的 `fetch_note_detail` 把页面的 `author` / `author_id` 原样带上，
   最后写回本地库时用的是**命令行给的那位博主**。V2 里 `VideoMeta.creator_ref`
   只有一个来源（请求的那位），页面自报的作者只在 `extra` 里留痕 ——
   不然一条被转发的笔记就会把作品挂到原作者名下，而库里对不上号。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import httpx
from pydantic import HttpUrl, TypeAdapter, ValidationError

from intelligence_hub_v2.errors import MediaDownloadError, PlatformError
from intelligence_hub_v2.infra.cdp_bridge import BridgeClient
from intelligence_hub_v2.infra.ffmpeg import has_audio_stream
from intelligence_hub_v2.infra.pacing import RatePacer
from intelligence_hub_v2.infra.ytdlp import YtDlpResult, YtDlpRunner, classify_artifacts
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.media import MediaArtifact, SingleFileArtifact
from intelligence_hub_v2.models.task import ProgressCallback
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.base import (
    AdapterDeps,
    Capabilities,
    ComponentStatus,
    HealthReport,
    PlatformConfig,
)
from intelligence_hub_v2.platforms.registry import register
from intelligence_hub_v2.platforms.xiaohongshu import listing, media
from intelligence_hub_v2.platforms.xiaohongshu.config import XiaohongshuConfig
from intelligence_hub_v2.platforms.xiaohongshu.detail import (
    NoteDetail,
    fetch_note_detail,
    search_creators_by_name,
)
from intelligence_hub_v2.platforms.xiaohongshu.listing import NoteCard, PageBudget
from intelligence_hub_v2.platforms.xiaohongshu.urls import (
    build_profile_url,
    extract_user_id,
    is_share_link,
)

PLATFORM = "xiaohongshu"

_HTTP_URL_ADAPTER: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)
"""`source_url` 那一列的校验器。`HttpUrl` 不是能直接调的类型，要靠 TypeAdapter。"""

_NO_BRIDGE_MESSAGE = (
    "装配错误：小红书声明了 capabilities.needs_browser=True，但 deps.bridge 是 None。"
    "这不是「没登录」，是 CDP 桥客户端没被注进来"
    "（看 config/app.yaml 的 cdp_bridge.url，以及 make dev 有没有把桥起起来）。"
    "小红书对无登录请求直接拦，Python 侧连页面都拿不到，所以这一条比抖音更致命。"
)

DIRECT_BUDGET_SECONDS = 300.0
"""一条页面视频直链的下载预算。V1 的 `NOTE_TIMEOUT=30` 是**单个 HTTP 请求**的超时，
而这里管的是整段落盘；照搬会把长视频掐死，所以与抖音那条兜底同一取值。"""

YTDLP_BUDGET_SECONDS = 600.0
"""yt-dlp 一趟的预算，V1 `download_video_note(seconds=600)`。
比直链长是有意的：失败时还要走 cookie 阶梯（最多三档 × 每档一次超时）。"""

LIKES_LOOKAHEAD_CARDS = 60
"""`sort_notes_by="likes"` 时先滚到多少条再排（V1 的 `BACKFILL_LOOKAHEAD=60`）。

**这条存在的理由**：只拿首屏十来个样本按点赞排，挑出来的不是"这位博主的历史爆款"，
是"最新发布里的相对热门"，而两者在看板上一模一样。V1 同一判据，取值也同一。
`limit` 比它大时按 `limit` 走 —— 上限是"至少看这么多"，不是"最多看这么多"。
"""

YTDLP_EXTRA_ARGS: tuple[str, ...] = (
    "--no-update",
    "--no-write-comments",
    # 1080p 以下视频轨 + 音频轨，合不出来就退整片。与抖音同一档：
    # 小红书没有"4K 需要登录"这一说，追更强签名只会多换一次风控失败。
    "-f",
    "bv*[height<=1080]+ba/b[height<=1080]",
    "--merge-output-format",
    "mp4",
)
YTDLP_FILE_TEMPLATE = "media.%(ext)s"
"""与 `media.download_single_file()` 同一个落点名（`data-model.md §1` 的 Locked 布局）。

V1 用 `%(id)s.%(ext)s`，产物叫 `<note_id>.mp4`。V2 里"主媒体叫什么"只有一个答案，
两条路（yt-dlp / 页面直链）必须写同一个名字，否则后处理要按来源分支找文件。
"""

_SHORT_LINK_HOSTS = frozenset({"xhslink.com", "www.xhslink.com", "xhslink.cn"})
"""App 里「复制链接」给的短码域名。**这种链接里没有任何身份信息**（V1 §7.1 的同一族）：
`https://xhslink.com/a/AbCdEf` 只是一个跳转令牌，必须跟一次 302 才知道它指向谁。
拿它当 `platform_id` 入库的症状与抖音那条一模一样 —— "回写命中 1 条但其实只刷了
updated_at"，博主资料永远落不上去，而且不报错。
"""


@register(PLATFORM)
class XiaohongshuAdapter:
    """小红书采集器。看护落点见模块 docstring 那张表。"""

    name: ClassVar[str] = PLATFORM
    display_name: ClassVar[str] = "小红书"

    capabilities: ClassVar[Capabilities] = Capabilities(
        needs_browser=True,
        needs_cookies=True,
        # 阶梯顺序的**唯一真源**（ADR-0011）。与抖音同一档形状：
        # `browser` 排第二，而且它默认不存在（V1 §7.3 实测 Windows 上那一档永远读不出来）。
        # `none` 而不是 `anonymous`：与抖音同用"这一趟什么 cookie 都不带"那一档的字面值，
        # 两家的语义相同（B站 的 `anonymous` 是它自己那套导出后的匿名请求）。
        cookie_variants=("exported_file", "browser", "none"),
        supports_subtitles=False,
        supports_dash_split=False,
        list_strategy="browser_scroll",
        media_strategy="yt_dlp_with_fallback",
    )

    def __init__(self, config: PlatformConfig, deps: AdapterDeps) -> None:
        # Protocol 的签名收基类（注册表按 `PlatformConfig` 传），所以运行期自己判型。
        # 不判的话第一个 `config.fallback_to_page_play_url` 就是一句 AttributeError，
        # 而真正的问题在装配那一层。
        if not isinstance(config, XiaohongshuConfig):
            msg = (
                f"XiaohongshuAdapter 需要 XiaohongshuConfig，拿到的是 {type(config).__name__} —— "
                f"PLATFORM_CONFIG_SCHEMAS['xiaohongshu'] 与适配器不匹配（装配错误）"
            )
            raise PlatformError(PLATFORM, "task", msg)
        self._config: XiaohongshuConfig = config
        self._deps = deps
        self._log = deps.logger
        self._pacer = RatePacer(per_minute=config.rate_limit.per_minute)
        # 这三个是测试的替换点：默认值合计十几秒（V1 实测的滚动/轮询预算），
        # 用例里等十几秒就把契约测试变成了集成测试。
        self.page_budget = PageBudget()
        self.direct_budget_seconds = DIRECT_BUDGET_SECONDS
        self.ytdlp_budget_seconds = YTDLP_BUDGET_SECONDS

    @classmethod
    def config_schema(cls) -> type[PlatformConfig]:
        return XiaohongshuConfig

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #

    async def healthcheck(self) -> HealthReport:
        """探桥 / cookie / yt-dlp 三件事，各给一个组件灯。

        三条判红与判绿的理由：

        - **桥回 503 算 `degraded` 不算 `unreachable`**（§7.20）。那是"桥在跑、
          浏览器被关了"，而桥靠**第一条真请求**就地重建浏览器 —— 预检如果因此拦下任务，
          等于被自己的健康检查堵死。V1 在 2026-09-21 真栽过一次。
        - **没有 cookie 文件算 `degraded`**：小红书的登录态在桥那份 profile 里，
          cookie 文件只影响 yt-dlp 那一档，而 yt-dlp 对小红书只是兜底位。
        - **没装 yt-dlp 也算 `degraded`**：图文笔记完全不需要它，视频笔记有页面直链。
          判成 `unreachable` 会让人去装包，而这条链路本来就能跑通。

        **这里不探"小红书登录态"**：唯一诚实的探法是进一个主页看会不会撞登录墙，
        那是一次真导航（并被平台记成一次访问）。V1 也没做，做了就要走 ADR。
        """
        components: dict[str, ComponentStatus] = {}
        details: list[str] = []

        bridge = self._deps.bridge
        if bridge is None:
            components["bridge"] = "unreachable"
            details.append(_NO_BRIDGE_MESSAGE)
        else:
            health = await bridge.health()
            if not health.reachable:
                components["bridge"] = "unreachable"
                details.append(f"桥没在听（{bridge.base_url}）：{health.error}")
            elif not health.browser_ok:
                components["bridge"] = "degraded"
                details.append(
                    f"桥在跑但浏览器没了（{bridge.base_url}），"
                    f"第一条真请求会自愈，所以不拦任务：{health.error}"
                )
            else:
                components["bridge"] = "ok"

        ladder = self._ladder()
        components["cookies"] = "ok" if ladder.cookie_file is not None else "degraded"
        if ladder.note:
            details.append(ladder.note)

        if shutil.which("yt-dlp") is None:
            components["yt_dlp"] = "degraded"
            details.append("PATH 里没有 yt-dlp；图文笔记不需要它，视频笔记有页面视频直链那条兜底")
        else:
            components["yt_dlp"] = "ok"

        status = _worst_status(components)
        return HealthReport(
            platform=PLATFORM,
            status=status,
            detail="；".join(details) or None,
            checked_at=datetime.now(UTC),
            components=components,
        )

    # ------------------------------------------------------------------ #
    # 博主
    # ------------------------------------------------------------------ #

    async def parse_creator_url(self, url: str) -> CreatorRef:
        """把用户粘贴的链接收成 `CreatorRef`（V1 §7.1）。

        三条路径，只有中间那条要联网：

        1. 已经是主页链接（`xiaohongshu.com/user/profile/<user_id>`）、
           **带 uid 的笔记链接**（`/user/profile/<uid>/<note_id>`，V1 现网最常见形状）
           或裸 uid → **不碰网络**。
        2. `xhslink.com/<码>` 短链 → 跟一次 302 再认。这种链接里没有任何身份信息。
        3. 都不认 → `PlatformError(stage="parse_url")`，消息里带原始输入。

        **纯笔记链接（`/explore/<note_id>`）在这里是认不出博主的** —— 那是故意的：
        note_id 里不含 uid，而"猜一个"会得到一条指向别人的 `profile_url`。
        那种链接走 `single_link` 那条任务（它只需要作品身份）。
        """
        text = str(url or "").strip()
        if not text:
            msg = "链接是空的（用户粘贴时被截断了？前端传参漏了？）"
            raise PlatformError(PLATFORM, "parse_url", msg)

        user_id = extract_user_id(text)
        if not user_id and is_share_link(text):
            expanded = await self._resolve_share_url(text)
            user_id = extract_user_id(expanded)
            if not user_id:
                msg = (
                    f"分享短链展开后仍然认不出 user_id：{text} → {expanded}"
                    f"（小红书换了跳转目标，或者这条码已经过期）"
                )
                raise PlatformError(PLATFORM, "parse_url", msg)
        if not user_id:
            msg = f"链接里没有 user_id，也不是能展开的小红书分享短链：{text}"
            raise PlatformError(PLATFORM, "parse_url", msg)
        if user_id.lower().startswith(("http://", "https://")):
            msg = f"从链接里认出来的不是 ID 而是一条 URL（{user_id}）：原始输入 {text}"
            raise PlatformError(PLATFORM, "parse_url", msg)

        return CreatorRef(
            platform=PLATFORM,
            platform_id=user_id,
            # 一律给规范主页：存进去的 profile_url 必须含得上 platform_id。
            # 存一条不含 uid 的链接（短链、带 xsec_token 的搜索页链接），
            # 下一轮采集就会对不上号，同一个人被收录两次（V1 §7.1 那一族）。
            profile_url=listing.require_http_url(build_profile_url(user_id), context="小红书主页"),
            # source_url 是"用户粘的那一条"，形状不受我们控制 —— 认不出就当没有，
            # 不为了它把整次解析判红（判红会让"手工粘错一个字符"变成异常而不是重填）。
            source_url=_as_http_url_or_none(text),
        )

    async def _resolve_share_url(self, url: str) -> str:
        """跟一次 302，返回最终地址。失败抛 `PlatformError`，两条 URL 都带上。"""
        try:
            response = await self._deps.http.get(
                url, follow_redirects=True, headers={"User-Agent": media.USER_AGENT}
            )
        except httpx.HTTPError as exc:
            msg = f"展开分享短链失败（{url}）：{type(exc).__name__}: {exc}"
            raise PlatformError(PLATFORM, "parse_url", msg) from exc
        return str(response.url or url)

    async def fetch_creator_profile(self, ref: CreatorRef) -> CreatorProfile:
        """昵称 / 头像 / 粉丝数。一次进页同时把首屏笔记卡片带回来（V1 同款省一次导航）。

        `extra["nickname_is_placeholder"]` 是给入库层的信号：页面被降级时会回
        "小红书创作者"这种占位名，让它覆盖库里已有的真昵称就是 V1 §7.24 那一类
        "把数据刷坏而且刷完看不出来"。
        """
        parsed = await self._open_profile_page(ref)
        return parsed.to_profile(ref)

    async def find_creator_by_name(self, name: str) -> list[CreatorProfile]:
        """V1 `--creator-name` 那条站内搜索兜底：按昵称搜主页，交回候选列表。

        **为什么在 `PlatformAdapter` 契约之外**：Protocol 里"认博主"只有一个入口
        （`parse_creator_url`，它只吃 URL），加一档"按名搜"等于改契约，要走 ADR。
        所以它是小红书独有的补充方法：`add_creator` 那条任务今天不经过它，
        而 V1 那条能力（"我只知道昵称，桥里搜一下"）得有地方活着 ——
        判据与页面 JS 都在 `detail.search_creators_by_name`。
        """
        bridge = self._require_bridge(stage="profile")
        await self._pace()
        return await detail_search(name, bridge=bridge, budget=self.page_budget)

    # ------------------------------------------------------------------ #
    # 作品列表
    # ------------------------------------------------------------------ #

    async def list_creator_videos(
        self,
        ref: CreatorRef,
        *,
        since: datetime | None = None,
        limit: int = 30,
    ) -> AsyncIterator[VideoMeta]:
        """流式产出笔记元数据。

        **`since=` 在小红书这一侧是真过滤**（与抖音相反）：`note_id` 前 8 位十六进制
        就是发布时间戳（ADR-0016 把它定成"页面给不出时间时的第二手"），
        所以这里判得动就真判掉。抖音那一侧的卡片 DOM 上什么都没有，只能弱过滤。

        首屏不够 `limit` 条时会**再滚一轮**（V1 的行为，不是疏漏）：
        主页只渲染视口内的卡片。滚动那一趟失败只降级成"沿用首屏"并记一条 warning，
        不掀掉整轮 —— 首屏那几条是干净数据。

        按 likes 排（`advanced.sort_notes_by="likes"`，V1 `--sort-by likes` 的爆款回溯）
        时会**多滚一些样本**再排：见 `LIKES_LOOKAHEAD_CARDS` 那段理由。
        排完才截 `limit`，所以交出的顺序是"样本里最火的在前"而不是"最新在最前" ——
        调用方要的就是这个差别。
        """
        if limit <= 0:
            return
        parsed = await self._open_profile_page(ref, stage="list")
        cards = list(parsed.cards)
        wanted = self._wanted_cards(limit)
        if len(cards) < wanted:
            # 滚动那一趟交回 tuple（它是"页面上有什么"的一份快照，不是给谁再往里塞东西的
            # 累积器），而上面那行是 list —— 换回来时要显式回到同一个类型，否则
            # `sort_cards` 的入参形状会随"这一轮要不要补滚"而变。
            cards = list(await self._extend_by_scrolling(ref, known=cards, wanted=wanted))

        by_sort = listing.sort_cards(
            _merge_unique(cards), sort_by=self._config.advanced.sort_notes_by
        )
        emitted = 0
        for card in by_sort:
            if emitted >= limit:
                break
            meta = listing.card_to_video_meta(card, ref=ref)
            if since is not None and meta.published_at is not None and meta.published_at < since:
                continue
            if not self._config.advanced.include_image_notes and listing.is_image_note(
                card.note_type
            ):
                continue
            yield meta
            emitted += 1

    def _wanted_cards(self, limit: int) -> int:
        """这一次枚举要凑多少条卡片。**只有按 likes 排的时候才放大**。

        日更采集要的是"最新那几条"，多滚几屏只是多挨几次风控；
        爆款回溯要的恰恰是样本量，两者不能共用一个数。
        """
        if self._config.advanced.sort_notes_by != "likes":
            return limit
        return max(limit, LIKES_LOOKAHEAD_CARDS)

    async def _extend_by_scrolling(
        self, ref: CreatorRef, *, known: list[NoteCard], wanted: int
    ) -> tuple[NoteCard, ...]:
        bridge = self._require_bridge(stage="list")
        js = listing.render_page_js(
            listing.COLLECT_AFTER_SCROLL_FUNCTION, budget=self.page_budget, wanted=wanted
        )
        try:
            await self._pace()
            # 重新导航是必需的：`evaluate(js)` 只发表达式，不 navigate 就会在
            # **上一次停留的页面**里执行 —— 那时滚的是别人的主页，收的是别人的笔记。
            await bridge.navigate(str(ref.profile_url))
            payload = listing.decode_page_result(await bridge.evaluate(js), stage="list")
        except (PlatformError, ValueError, OSError) as exc:
            self._log.warning(
                "xiaohongshu.scroll_failed_keep_first_screen",
                platform_id=ref.platform_id,
                kept=len(known),
                wanted=wanted,
                error=f"{type(exc).__name__}: {exc}",
            )
            return tuple(known)
        # 互补合并而不是拼接：DOM 那份有标题/封面，状态树那份有 token/类型（见 listing 纪律 4）。
        return listing.merge_note_cards(known, listing.parse_cards_payload(payload))

    async def _open_profile_page(
        self, ref: CreatorRef, *, stage: str = "profile"
    ) -> listing.ParsedProfile:
        """进一次主页页：导航 + 注入 + 解析。`stage` 只影响异常类型（见 `_page_error`）。"""
        bridge = self._require_bridge(stage=stage)
        js = listing.render_page_js(
            listing.PROFILE_PAGE_FUNCTION, budget=self.page_budget, user_id=ref.platform_id
        )
        await self._pace()
        await bridge.navigate(str(ref.profile_url))
        payload = listing.decode_page_result(await bridge.evaluate(js), stage=stage)
        return listing.parse_profile_payload(payload, expected_user_id=ref.platform_id, stage=stage)

    # ------------------------------------------------------------------ #
    # 媒体
    # ------------------------------------------------------------------ #

    async def download_media(
        self,
        video: VideoMeta,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> MediaArtifact:
        """一条笔记的媒体：先问详情页，再按**页面实际给了什么**分流。

        分流表（V1 :1407 那三行的契约化）：

        | 页面给的 | 产物 | `media_source` |
        |---|---|---|
        | `video_url` | 一条 `media.mp4`（yt-dlp 优先，失败退直链） | `yt_dlp` / `page_play_url` |
        | 只有 `images` | 第一张当主文件，其余进 `extra_paths`，`has_video=False` | 原图直链 |
        | 两样都没有 | 抛 `MediaDownloadError`，原文带详情页那一步的报错 | — |

        **图文那一行是本方法的存在理由**（ADR-0019）：把它报成
        `MediaDownloadError` 会让一条什么都没失败的笔记记成采集失败，
        而 `collect._Tally.to_result()` 在 `downloaded==0 and failed>0` 时把整轮判红 ——
        一个图文占多数的博主会让看板常年红着。

        先问详情再决定走哪条，是为了**不在视频笔记上白跑 yt-dlp**：详情那条页
        无论哪一路都要问（图文要图片地址，视频要 masterUrl 当兜底），
        所以"类型"这个判据总是免费的。
        """
        note_id = video.platform_video_id
        detail = await self._fetch_detail(video)
        if detail.has_video:
            return await self._video_artifact(video, detail, dest, on_progress=on_progress)
        if detail.is_image_note:
            return await self._image_artifact(note_id, detail, dest, on_progress=on_progress)
        msg = (
            f"这条笔记既没有视频直链也没有原图（{note_id}）："
            f"页面给的 note_type={detail.note_type or '（空）'}，"
            f"可能是被删除、审核中，或小红书改了详情结构"
        )
        raise MediaDownloadError(PLATFORM, "media", msg)

    async def _fetch_detail(self, video: VideoMeta) -> NoteDetail:
        """详情页那一步。`xsec_token` 从 `video.extra` 里取（枚举那一步带过来的）。"""
        bridge = self._require_bridge(stage="media")
        token = str(video.extra.get("xsec_token") or "")
        await self._pace()
        try:
            return await fetch_note_detail(
                bridge, note_id=video.platform_video_id, xsec_token=token, budget=self.page_budget
            )
        except PlatformError as exc:
            # 详情失败就是媒体失败，但 stage 要换成 media：
            # 清单按 stage 分组，"枚举一位博主"与"下载一条笔记"的红要做的动作不同。
            msg = str(exc)
            raise MediaDownloadError(PLATFORM, "media", msg) from exc

    async def _video_artifact(
        self,
        video: VideoMeta,
        detail: NoteDetail,
        dest: Path,
        *,
        on_progress: ProgressCallback | None,
    ) -> MediaArtifact:
        """视频笔记：yt-dlp 优先，失败退到页面的 `masterUrl`。"""
        ladder = self._ladder()
        artifact, reason = await self._via_ytdlp(video, dest, ladder, on_progress=on_progress)
        if artifact is not None:
            return artifact

        if not self._config.fallback_to_page_play_url:
            msg = (
                f"yt-dlp 未拿到媒体，且 fallback_to_page_play_url=False 关掉了兜底："
                f"{reason}｜{self._describe(video)}"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        self._log.info("xiaohongshu.media.fallback", reason=reason, ladder=ladder.note)
        download = await media.download_single_file(
            self._deps.http,
            [detail.video_url],
            dest=dest,
            budget_seconds=self.direct_budget_seconds,
            on_progress=on_progress,
        )
        if download.path is None:
            detail_text = "；".join(download.failures) or "页面没给出可直连的地址"
            msg = (
                f"页面视频直链也没能落盘（试过 {download.attempted} 个：{detail_text}）"
                f"（yt-dlp 那一轮：{reason}）"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        size_bytes = await asyncio.to_thread(_stat_size, download.path)
        self._log.info(
            "xiaohongshu.media.direct_url_ok",
            platform_video_id=video.platform_video_id,
            bytes=size_bytes,
            yt_dlp_error=reason,
        )
        # 兜底这条路**不经过 yt-dlp**，所以没有"哪一档 cookie"可记 —— 桥的页面上下文
        # 就是登录态本身。留 None 而不是编一个 "page_context" 假装它是阶梯上的一档。
        return await self._single_file_artifact(
            download.path,
            size_bytes,
            media_source="page_play_url",
            yt_dlp_error=reason,
            cookie_rung=None,
            duration_seconds=detail.duration_seconds,
        )

    async def _via_ytdlp(
        self,
        video: VideoMeta,
        dest: Path,
        ladder: media.CookieLadder,
        *,
        on_progress: ProgressCallback | None,
    ) -> tuple[MediaArtifact | None, str]:
        """yt-dlp 那一趟。返回 `(产物, "")` 或 `(None, 失败原文)`。"""
        try:
            result = await self._ytdlp_runner().download(
                str(video.webpage_url),
                dest,
                variants=ladder.variants,
                file_template=YTDLP_FILE_TEMPLATE,
                on_line=_line_reporter(on_progress),
            )
        except LookupError as exc:
            # 二进制没装：与抖音同一处置 —— 原样往上抛会绕过兜底，
            # 而兜底恰恰是小红书这一侧更常走通的那条。
            return None, f"未安装 yt-dlp：{exc}"
        except MediaDownloadError as exc:  # 阶梯空到一档都跑不了
            return None, str(exc)

        if result.ok and result.artifacts:
            try:
                return await self._artifact_from_ytdlp(result, video)
            except MediaDownloadError as exc:
                # "yt-dlp 说下好了，但报出来的路径一个都读不到"是**这一趟没给出可用产物**，
                # 不是"两条路都死了" —— 原样穿透会绕过页面直链兜底（与上面 LookupError 同理）。
                return None, str(exc)
        return None, _ytdlp_failure_reason(result)

    async def _artifact_from_ytdlp(
        self, result: YtDlpResult, video: VideoMeta
    ) -> tuple[MediaArtifact | None, str]:
        """yt-dlp 报回来的那批路径 → 产物，或者"这一趟不算成"。

        只认它自己报出来的路径、**不扫目录**（V1 §7.21），并且用的是与抖音、B站
        同一份 `infra.ytdlp.classify_artifacts` —— 分片命名是 **yt-dlp 的知识**，
        不是某个平台的知识，三处各写一份就会漂。

        小红书声明 `supports_dash_split=False`，没有字段能诚实表达"这是一对未合并的轨"，
        所以遇到 pair 必须把这一趟判为**失败**、交给页面视频直链那条走得通的路。
        按体积挑一条的做法会让一条**无声视频轨**被记成 `media_source="yt_dlp"` 的成品，
        而本机没有 ffprobe 时 `has_audio_stream()` 问不出来就返回 True ——
        等于让它声称自己有音轨。
        """
        parts = classify_artifacts(result.artifacts)
        if parts.kind == "empty":
            msg = (
                f"yt-dlp 说下好了，但报出来的路径一个都读不到："
                f"{[str(p) for p in result.artifacts]}（{self._describe(video)}）"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        main = parts.main  # 排掉 empty 之后，None 就只可能是 "pair"
        if main is None:
            reason = (
                f"yt-dlp 只留下未合并的 DASH 分片（{parts.description}），"
                f"而小红书这一侧不做合并（supports_dash_split=False）→ 改走页面视频直链"
            )
            self._log.warning(
                "xiaohongshu.ytdlp.unmerged_dash_parts",
                platform_video_id=video.platform_video_id,
                parts=[str(p) for p in (parts.video, parts.audio) if p is not None],
                extras=[str(p) for p in parts.extras],
                rung=str(result.variant),
            )
            return None, reason

        try:
            size_bytes = await asyncio.to_thread(_stat_size, main)
        except OSError as exc:  # 分类与 stat 之间被清了（杀软/回收）：如实失败
            return None, f"yt-dlp 报出来的主文件读不到大小 {main}: {exc}"
        if parts.extras:
            # 合并成功却还留着中间分片/封面：报单文件是对的，但要点名而不是静默丢。
            self._log.warning(
                "xiaohongshu.ytdlp.extra_artifacts_ignored",
                platform_video_id=video.platform_video_id,
                chosen=str(main),
                others=[str(p) for p in parts.extras],
                rung=str(result.variant),
            )
        artifact = await self._single_file_artifact(
            main,
            size_bytes,
            media_source="yt_dlp",
            yt_dlp_error=None,
            cookie_rung=str(result.variant) if result.variant else None,
        )
        return artifact, ""

    async def _image_artifact(
        self,
        note_id: str,
        detail: NoteDetail,
        dest: Path,
        *,
        on_progress: ProgressCallback | None,
    ) -> SingleFileArtifact:
        """图文笔记的原图 → `SingleFileArtifact`（ADR-0019）。**这不是失败路径**。

        四个字段各有存在的理由：

        - `path` = 第一张、`extra_paths` = 其余，按页面顺序。图文笔记没有天然的
          "代表文件"，选第一张是因为 V1 的 `images/01.jpg` 就是首图（ADR-0019 后果 3）。
        - `size_bytes` = **全部原图之和**，不是主文件那一个。`total_size_bytes()`
          直接返回它、不做二次相加，所以生产方必须按这个口径填 ——
          否则"清单里 200 KB / 盘上 6 MB"这种分叉就会出现。
        - `has_audio=False` / `has_video=False`：**不许问 ffprobe**。一张 JPG 问不出
          音轨，而 `has_audio_stream()` 在问不出来时返回 True（判成"没有"会白丢口播稿），
          照那条默认值填就等于让一张图片声称自己有音频、然后被喂进转写链。
        - `media_source="page_play_url"`：来源确实是"页面给的地址"。
          不新开通路（ADR-0019 的选项 A 被否了，代价是一条 DB CHECK 与五个契约面）。
        """
        limit = self._config.advanced.max_images_per_note
        download = await media.download_image_set(
            self._deps.http,
            detail.images,
            dest=dest,
            limit=limit,
            budget_seconds=self.direct_budget_seconds,
            on_progress=on_progress,
        )
        if download.dropped_by_limit:
            self._log.warning(
                "xiaohongshu.media.images_trimmed",
                platform_video_id=note_id,
                kept=len(detail.images) - download.dropped_by_limit,
                dropped=download.dropped_by_limit,
                limit=limit,
            )
        if download.failures:
            # 少了几张是真损失，必须留原文；但它不该把剩下的那几张判成失败。
            self._log.warning(
                "xiaohongshu.media.images_partial",
                platform_video_id=note_id,
                kept=len(download.paths),
                attempted=download.attempted,
                failures=list(download.failures),
            )
        if not download.paths:
            detail_text = "；".join(download.failures) or "页面没给出任何图片地址"
            msg = f"图文笔记的原图一张都没拿到（{note_id}）：{detail_text}"
            raise MediaDownloadError(PLATFORM, "media", msg)

        first, *rest = download.paths
        return SingleFileArtifact(
            path=first,
            size_bytes=download.total_bytes,
            media_source="page_play_url",
            yt_dlp_error=None,
            # 图文这条路**根本不经过 yt-dlp**，所以没有 cookie 档位可记。
            # 留 None 而不是编一个值假装它是阶梯上的一档（与抖音那条兜底同一判据）。
            cookie_rung=None,
            has_audio=False,
            has_video=False,
            extra_paths=tuple(rest),
        )

    async def _single_file_artifact(
        self,
        path: Path,
        size_bytes: int,
        *,
        media_source: str,
        yt_dlp_error: str | None,
        cookie_rung: str | None = None,
        duration_seconds: float | None = None,
    ) -> SingleFileArtifact:
        """收成 `SingleFileArtifact`。两处不是走形式的细节：

        - `has_audio` **问 ffprobe，而不是照默认值填 True**（V1 §7.21 的落点）。
          问不出来时 `has_audio_stream()` 返回 True：判成"没有"的后果是白丢一段口播稿，
          而丢稿子在看板上完全不可见。**图文笔记不走这里**（见 `_image_artifact`）。
        - `path` 是 `dest` 下的路径，**相对 `data/` 的归一化是入库那一层的事** ——
          `AdapterDeps` 里没有 `FileStorage`，只有 handler 知道这条作品最终归在谁名下。
        """
        return SingleFileArtifact(
            path=path,
            size_bytes=size_bytes,
            media_source=media_source,  # type: ignore[arg-type]
            yt_dlp_error=yt_dlp_error,
            cookie_rung=cookie_rung,
            duration_seconds=duration_seconds,
            has_audio=await has_audio_stream(path),
        )

    async def fetch_subtitles(self, video: VideoMeta) -> None:
        """小红书没有公开字幕轨 —— **返回 None，不抛**。

        抛会让"每条作品都先失败一次"变成常态，而调度器要的是"没有字幕，去走 ASR"
        （`platforms/base.py` 的 Protocol docstring 同一口径）。
        """
        self._log.debug(
            "xiaohongshu.subtitles.unsupported", platform_video_id=video.platform_video_id
        )
        return None  # noqa: RET501,PLR1711 - 这个 None **就是**契约（"没有字幕"而不是"失败"）。

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _describe(self, video: VideoMeta) -> str:
        return f"{video.platform_video_id} @ {video.creator_ref.platform_id}"

    def _require_bridge(self, *, stage: str) -> BridgeClient:
        """拿桥。为 None 时给一条**说得清是装配错误**的红，而不是 AttributeError。"""
        if self._deps.bridge is None:
            raise PlatformError(PLATFORM, stage, _NO_BRIDGE_MESSAGE)
        return self._deps.bridge

    def _ladder(self, environ: Mapping[str, str] | None = None) -> media.CookieLadder:
        return media.resolve_cookie_ladder(
            self.capabilities.cookie_variants,
            config=self._config,
            cookies=self._deps.cookies,
            environ=environ,
        )

    def _ytdlp_runner(self) -> YtDlpRunner:
        """构造 yt-dlp Runner。**这是测试的替换点**（与抖音同一形状，理由也同一）：

        `AdapterDeps` 里没有 `YtDlpRunner` 这个字段 —— deps 是"平台共用的外部世界"，
        而 argv 与超时是每个平台自己的事。所以换假对象用
        `monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: fake_runner)`。
        """
        return YtDlpRunner(
            default_variants=(),
            timeout_seconds=self.ytdlp_budget_seconds,
            extra_args=YTDLP_EXTRA_ARGS,
        )

    async def _pace(self) -> None:
        """按 `rate_limit.per_minute` 给桥请求上闸（小红书侧风控是第一约束）。

        只管"同一个平台内部别把页面刷爆"。`per_creator_seconds`（跨博主的间隔）
        **不在这里实现**：适配器一次只看到一位博主，跨博主的序列只有调度器有全局视图。
        """
        await self._pacer.wait()


async def detail_search(
    name: str, *, bridge: BridgeClient, budget: PageBudget
) -> list[CreatorProfile]:
    """`detail.search_creators_by_name` 的一层薄壳，存在的唯一理由是**替换点**。

    用例里换 `adapter.find_creator_by_name` 的这一手比换 `detail` 模块里的函数干净：
    前者是一个实例属性，后者要 monkeypatch 模块命名空间。
    """
    return await search_creators_by_name(bridge, keyword=name, budget=budget)


def _merge_unique(cards: list[NoteCard]) -> tuple[NoteCard, ...]:
    """按 note_id 去重，保留**第一次出现**的那条的位置。

    异步水合会把同一张卡片扫到两次，而第二次往往字段更全（图片加载完了才渲染出标题）。
    所以这里不是"丢掉后来的"，是"位置留前面的、字段做互补" —— 交给 `merge_note_cards`。
    """
    return listing.merge_note_cards(cards, cards)


def _stat_size(path: Path) -> int:
    return path.stat().st_size


_BY_RANK: tuple[ComponentStatus, ...] = ("ok", "degraded", "unreachable")
"""下标即等级：`ok < degraded < unreachable`。**没有 "unknown" 这一档** ——
组件级要么探到了，要么根本不该出现在 `components` 字典里（`base.ComponentStatus` 的约定）。
整体状态才有 unknown，那是"探了但判不出来"（V1 §7.20）。"""


def _worst_status(components: Mapping[str, ComponentStatus]) -> ComponentStatus:
    """整体位取组件里最差的那一位。"""
    order: dict[ComponentStatus, int] = {"ok": 0, "degraded": 1, "unreachable": 2}
    worst = max((order[status] for status in components.values()), default=0)
    return _BY_RANK[worst]


def _ytdlp_failure_reason(result: YtDlpResult) -> str:
    """每一档的退出码 + 最后一行 stderr，合成一句能自己讲完故事的话。"""
    tail = [line for line in result.stderr.strip().splitlines() if line.strip()]
    last = tail[-1] if tail else f"exit {result.returncode}"
    return f"{result.attempts_note()}｜{last}"


def _line_reporter(on_progress: ProgressCallback | None) -> Callable[[str], None] | None:
    """把"yt-dlp 的一行输出"转成"0.0~1.0 的进度"，认不出的行不产生事件。"""
    if on_progress is None:
        return None

    def on_line(line: str) -> None:
        media.report_ytdlp_line(on_progress, line)

    return on_line


def _as_http_url_or_none(value: object) -> HttpUrl | None:
    """用户粘的那一条：像 URL 就收成 `HttpUrl`，不像就当没有。

    为什么在这里判而不复用 `listing.require_http_url`：那条是"这里不给 None"的版本，
    而 `source_url` 这一列**本来就可空** —— 认不出来源链接（输入是裸 ID）不该让整次解析红掉。
    """
    text = str(value or "").strip()
    if not text or not text.lower().startswith(("http://", "https://")):
        return None
    try:
        return _HTTP_URL_ADAPTER.validate_python(text)
    except ValidationError:
        return None
