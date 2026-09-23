"""`DouyinAdapter` —— V1 `download_douyin_latest.py` 站在 `PlatformAdapter` 契约后面。

V1 §7 的看护各自落在哪个方法里（全表见 `docs/specs/contract-tests.md`）：

| 陷阱 | 落点 |
|---|---|
| §7.1 sec_uid 不是 URL 里的东西，短链必须跟 302 | `parse_creator_url` + `douyin/urls.py` |
| §7.2 yt-dlp 必失败 → 兜底是常态，`yt_dlp_error` 要留原文 | `download_media` + `douyin/media.py` |
| §7.3 cookie 优先级阶梯（Windows 读不了 Chrome 的库） | `media.resolve_cookie_ladder` |
| §7.20 桥的 503 是"浏览器没了"不是"桥没起" | `healthcheck` |

与 V1 的三处**有意**不同，别当疏漏：

1. V1 把清单、入库、转写、资料回写都写在这一个脚本里；这里只做"采集"。
   入库是 Task 8 的 handler，清单是 `core/manifest.py`，转写是 `asr/`。
   判据：本模块**不 import Repository，也不 publish 事件**。
2. V1 在下载旁边写 `metadata.json` / `video-description.txt` 当"作品的档案"，
   因为没有别的地方能放。V2 有 `videos` 表，`media_source` 与 `yt_dlp_error`
   走 `MediaArtifact` 交给 handler 落库 —— 再写一份 sidecar 就是第二处真相。
3. V1 的 `creator_sec_uid` 带 `lru_cache`；这里不带。V1 缓存是因为整库每行都可能
   引用同一条短链，而 V2 的 `parse_creator_url` 一位博主一次调用 ——
   换来"常驻服务里一个按用户输入增长的无界缓存"不值得。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import httpx

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
    MediaSource,
    PlatformConfig,
)
from intelligence_hub_v2.platforms.douyin import listing, media
from intelligence_hub_v2.platforms.douyin.config import DouyinConfig
from intelligence_hub_v2.platforms.douyin.listing import PageBudget, VideoCard, card_to_video_meta
from intelligence_hub_v2.platforms.douyin.media import CookieLadder, resolve_cookie_ladder
from intelligence_hub_v2.platforms.douyin.urls import (
    as_http_url,
    canonical_profile_url,
    extract_sec_uid,
    is_http_url,
    is_share_link,
    require_http_url,
)
from intelligence_hub_v2.platforms.registry import register

PLATFORM = "douyin"

_NO_BRIDGE_MESSAGE = (
    "装配错误：抖音声明了 capabilities.needs_browser=True，但 deps.bridge 是 None。"
    "这不是“没登录”，是 CDP 桥客户端没被注进来"
    "（看 config/app.yaml 的 cdp_bridge.url，以及 make dev 有没有把桥起起来）。"
)

DIRECT_BUDGET_SECONDS = 300.0
"""一条兜底直链的下载预算。CDN 直链可达几百 MB，慢网络下 5 分钟比"整轮卡住"好。
V1 同名参数是 `DOUYIN_MEDIA_TIMEOUT`；收成常量 + 实例属性，因为"从环境里偷偷改一个
数值型行为"在预检里看不见。"""

YTDLP_BUDGET_SECONDS = 600.0
"""yt-dlp 一趟的预算，V1 的 `download_media(seconds=600)`。比直链长是有意的：
yt-dlp 失败时还要走 cookie 阶梯（最多三档 × 每档一次超时），而退档本身就是要花的钱。"""

YTDLP_EXTRA_ARGS: tuple[str, ...] = (
    "--no-update",
    "--no-write-comments",
    # V1 实测的档位：1080p 以下视频轨 + 音频轨，合不出来就退整片。
    # 不追 4K 是有意的 —— 更强的签名正是 §7.2 那一档失败的主因。
    "-f",
    "bv*[height<=1080]+ba/b[height<=1080]",
    "--merge-output-format",
    "mp4",
)
YTDLP_FILE_TEMPLATE = "media.%(ext)s"
"""与 `media.download_first_play_url()` 同一个落点名（`data-model.md §1` 的 Locked 布局）。

V1 用 `%(id)s.%(ext)s`，产物叫 `<aweme_id>.mp4`。V2 里"主媒体叫什么"只有一个答案，
两条路（yt-dlp / 页面直链）必须写同一个名字，否则后处理要按来源分支找文件。
"""


@register(PLATFORM)
class DouyinAdapter:
    """抖音采集器。看护落点见模块 docstring 那张表。"""

    name: ClassVar[str] = PLATFORM
    display_name: ClassVar[str] = "抖音"

    capabilities: ClassVar[Capabilities] = Capabilities(
        needs_browser=True,
        needs_cookies=True,
        # 阶梯顺序的**唯一真源**（`docs/adr/0011`）。`browser` 排第二，而且它默认不存在：
        # V1 §7.3 的实测是 Windows 上那一档永远读不出来，只有显式设过
        # `DOUYIN_YTDLP_COOKIES_FROM_BROWSER` 或在配置里填了浏览器名才会出现。
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
        if not isinstance(config, DouyinConfig):
            msg = (
                f"DouyinAdapter 需要 DouyinConfig，拿到的是 {type(config).__name__} —— "
                f"PLATFORM_CONFIG_SCHEMAS['douyin'] 与适配器不匹配（装配错误）"
            )
            raise PlatformError(PLATFORM, "task", msg)
        self._config: DouyinConfig = config
        self._deps = deps
        self._log = deps.logger
        self._pacer = RatePacer(per_minute=config.rate_limit.per_minute)
        # 这两个是测试的替换点：默认值合计约 12 秒，用例里等 12 秒
        # 就把契约测试变成了集成测试。
        self.page_budget = PageBudget()
        self.direct_budget_seconds = DIRECT_BUDGET_SECONDS

    @classmethod
    def config_schema(cls) -> type[PlatformConfig]:
        return DouyinConfig

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #

    async def healthcheck(self) -> HealthReport:
        """探桥 / cookie / yt-dlp 三件事，各给一个组件灯。

        三条判红与判绿的理由，全部来自 V1：

        - **桥回 503 算 `degraded` 不算 `unreachable`**（§7.20）。那是"桥在跑、
          浏览器被关了"，而桥靠**第一条真请求**就地重建浏览器 —— 预检如果因此拦下任务，
          等于被自己的健康检查堵死。V1 在 2026-09-21 真栽过一次。
        - **没有 cookie 文件算 `degraded`**：抖音的登录态在桥那份 profile 里，
          cookie 文件只影响 yt-dlp 那一档的画质，而 yt-dlp 对抖音基本产不出媒体（§7.2）。
        - **没装 yt-dlp 也算 `degraded`**：页面直链那条路不需要它。
          判成 `unreachable` 会让人去装包，而这条链路本来就能跑通。
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
            details.append("PATH 里没有 yt-dlp；页面播放直链那条兜底路不需要它（V1 §7.2）")
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

        1. 已经是主页链接（`douyin.com/user/<sec_uid>`）、落地页（`?sec_uid=…`）
           或裸 sec_uid → **不碰网络**。
        2. `v.douyin.com/<码>` 短链 → 跟一次 302 再认。**这种链接里没有任何身份信息**，
           拿它当 platform_id 入库的症状是"回写命中 1 条但只刷了 updated_at"，
           博主资料永远落不上去，而且不报错。
        3. 都不认 → `PlatformError(stage="parse_url")`，消息里带原始输入。

        最后一道 `is_http_url(sec_uid)` 是脏行识别器：`?sec_uid=` 的值本身可以被
        URL-编码成一整条链接（页面里真的这么拼过），`unquote` 之后就是一个 URL。
        那种值一旦入库就是 §7.1 的历史脏行，所以在门口拦掉，而不是交给下游去猜。
        """
        text = str(url or "").strip()
        if not text:
            msg = "链接是空的（用户粘贴时被截断了？前端传参漏了？）"
            raise PlatformError(PLATFORM, "parse_url", msg)

        sec_uid = extract_sec_uid(text)
        if not sec_uid and is_share_link(text):
            expanded = await self._resolve_share_url(text)
            sec_uid = extract_sec_uid(expanded)
            if not sec_uid:
                msg = (
                    f"分享短链展开后仍然认不出 sec_uid：{text} → {expanded}"
                    f"（抖音换了跳转目标，或者这条码已经过期）"
                )
                raise PlatformError(PLATFORM, "parse_url", msg)
        if not sec_uid:
            msg = f"链接里没有 sec_uid，也不是能展开的抖音分享短链：{text}"
            raise PlatformError(PLATFORM, "parse_url", msg)
        if is_http_url(sec_uid):
            msg = f"从链接里认出来的不是 ID 而是一条 URL（{sec_uid}）：原始输入 {text}"
            raise PlatformError(PLATFORM, "parse_url", msg)

        return CreatorRef(
            platform=PLATFORM,
            platform_id=sec_uid,
            # 一律给规范主页：存进去的 profile_url 必须含得上 platform_id。
            # 存一条不含 sec_uid 的链接（短链、带 from 参数的落地页），
            # 下一轮采集就会对不上号，同一个人被收录两次。
            profile_url=require_http_url(canonical_profile_url(sec_uid), context="抖音主页"),
            # source_url 是"用户粘的那一条"，形状不受我们控制 —— 认不出就当没有，
            # 不为了它把整次解析判红（判红会让"手工粘错一个字符"变成异常而不是重填）。
            source_url=as_http_url(text) if is_http_url(text) else None,
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
        """昵称 / 头像 / 粉丝数。一次进页同时把首屏作品带回来（V1 同款省一次导航）。

        `extra["nickname_is_placeholder"]` 是给入库层的信号：页面被降级时会回
        "抖音创作者"这种占位名，让它覆盖库里已有的真昵称就是 V1 §7.24 那一类
        "把数据刷坏而且刷完看不出来"。
        """
        parsed = await self._open_profile_page(ref)
        return parsed.to_profile(ref)

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
        """流式产出作品元数据。

        **`since=` 在抖音这一侧是弱过滤，别把它当增量游标用**：
        主页网格的卡片 DOM 上只有 id / 标题 / 点赞数，**没有发布时间**
        （`urls`/`listing` 不去认一个页面上不存在的东西）。所以这里的口径是
        "判得了才跳过，判不了就放行"，绝不会因为判不了就整轮丢弃。
        真正的增量靠 `videos` 表按 `platform_video_id` 查重（Task 8 的 handler）。
        V1 同样是"靠查重不靠 since"，这条不是 V2 的退化，是平台侧的限制 ——
        已记进 `docs/lessons.md`。

        首屏不够 `limit` 条时会**重新导航一次**再滚（V1 的行为，不是疏漏）：
        第二次进页保证网格已经存在，滚动只是把视口外的卡片水合出来。
        滚动那一趟失败只降级成"沿用首屏"并记一条 warning，不掀掉整轮 ——
        首屏那几条是干净数据。
        """
        if limit <= 0:
            return
        parsed = await self._open_profile_page(ref, stage="list")
        cards = list(parsed.cards)
        if len(cards) < limit:
            cards = await self._extend_by_scrolling(ref, known=cards, wanted=limit)

        seen: set[str] = set()
        emitted = 0
        for card in cards:
            if emitted >= limit:
                break
            if card.aweme_id in seen:
                continue
            seen.add(card.aweme_id)
            meta = card_to_video_meta(card, ref=ref)
            if since is not None and meta.published_at is not None and meta.published_at < since:
                continue
            yield meta
            emitted += 1

    async def _extend_by_scrolling(
        self, ref: CreatorRef, *, known: list[VideoCard], wanted: int
    ) -> list[VideoCard]:
        js = listing.render_page_js(
            listing.COLLECT_AFTER_SCROLL_FUNCTION,
            sec_uid=ref.platform_id,
            budget=self.page_budget,
            wanted=wanted,
        )
        bridge = self._require_bridge()
        out = list(known)
        try:
            await self._pace()
            await bridge.navigate(str(ref.profile_url))
            payload = listing.decode_page_result(await bridge.evaluate(js), stage="list")
        except (PlatformError, ValueError, OSError) as exc:
            self._log.warning(
                "douyin.scroll_failed_keep_first_screen",
                platform_id=ref.platform_id,
                kept=len(known),
                wanted=wanted,
                error=f"{type(exc).__name__}: {exc}",
            )
            return out
        known_ids = {card.aweme_id for card in known}
        extra = [
            card for card in listing.parse_cards_payload(payload) if card.aweme_id not in known_ids
        ]
        out.extend(extra)
        return out

    async def _open_profile_page(
        self, ref: CreatorRef, *, stage: str = "profile"
    ) -> listing.ParsedProfile:
        """进一次主页页：导航 + 注入 + 解析。`stage` 只影响异常类型（见 `_profile_error`）。"""
        bridge = self._require_bridge()
        js = listing.render_page_js(
            listing.PROFILE_PAGE_FUNCTION,
            sec_uid=ref.platform_id,
            budget=self.page_budget,
        )
        await self._pace()
        await bridge.navigate(str(ref.profile_url))
        payload = listing.decode_page_result(await bridge.evaluate(js), stage=stage)
        return listing.parse_profile_payload(payload, expected_sec_uid=ref.platform_id, stage=stage)

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
        """yt-dlp 优先，失败退到页面播放直链（V1 §7.2 的常态）。

        返回值的两个字段是这个方法的存在理由：

        - `media_source`：这条片**实际**走了哪条路。抖音这边几乎总是 `page_play_url`，
          把它记成 `yt_dlp` 会让后来人以为该修的是 cookie。
        - `yt_dlp_error`：兜底成功时 yt-dlp 的**失败原文**（每一档的退出码 + stderr 尾行）。
          V1 早期一兜底成功就把原文丢掉，日志只剩"未拿到媒体"，
          于是永远判断不出该修什么 —— 那句"看起来在跑"就是这么来的。

        两条路都失败才抛 `MediaDownloadError`，消息里带**两轮各自的原文**。
        """
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

        self._log.info("douyin.media.fallback", reason=reason, ladder=ladder.note)
        return await self._via_page_play_url(
            video, dest, yt_dlp_error=reason, on_progress=on_progress
        )

    async def _via_ytdlp(
        self,
        video: VideoMeta,
        dest: Path,
        ladder: CookieLadder,
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
            # 二进制没装：V1 的行为是"改用页面播放直链"并说一句。
            # 原样往上抛会绕过兜底，而兜底恰恰是抖音这条路唯一走得通的那条。
            return None, f"未安装 yt-dlp：{exc}"
        except MediaDownloadError as exc:  # 阶梯空到一档都跑不了
            return None, str(exc)

        if result.ok and result.artifacts:
            return await self._artifact_from_ytdlp(result, video)
        return None, _ytdlp_failure_reason(result)

    async def _artifact_from_ytdlp(
        self, result: YtDlpResult, video: VideoMeta
    ) -> tuple[MediaArtifact | None, str]:
        """yt-dlp 报回来的那批路径 → 产物，或者"这一趟不算成"。

        只认它自己报出来的路径、**不扫目录**（V1 §7.21），并且用的是与 B站 同一份
        `infra.ytdlp.classify_artifacts` —— 分片命名是 **yt-dlp 的知识**，不是 B站的平台
        知识，两处各写一份就会漂（本方法以前就是"按体积取主文件"）。

        抖音声明 `supports_dash_split=False`，没有字段能诚实表达"这是一对未合并的轨"，
        所以遇到 pair 必须把这一趟判为**失败**、交给页面播放直链那条走得通的路。
        按体积挑一条的旧做法，产物是一条**无声视频轨**却被记成 `media_source="yt_dlp"`
        的成品，而本机没有 ffprobe 时 `has_audio_stream()` 问不出来就返回 True ——
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
                f"而抖音这一侧不做合并（supports_dash_split=False）→ 改走页面播放直链"
            )
            self._log.warning(
                "douyin.ytdlp.unmerged_dash_parts",
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
                "douyin.ytdlp.extra_artifacts_ignored",
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

    async def _via_page_play_url(
        self,
        video: VideoMeta,
        dest: Path,
        *,
        yt_dlp_error: str,
        on_progress: ProgressCallback | None,
    ) -> MediaArtifact:
        bridge = self._require_bridge()
        try:
            urls = await media.fetch_play_urls(bridge, video.platform_video_id)
        except PlatformError as exc:
            msg = f"{exc}（yt-dlp 那一轮：{yt_dlp_error}）"
            raise MediaDownloadError(PLATFORM, "media", msg) from exc
        if not urls:
            msg = (
                f"页面没给出任何播放直链（{self._describe(video)}）"
                f"（yt-dlp 那一轮：{yt_dlp_error}）"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        download = await media.download_first_play_url(
            self._deps.http,
            urls,
            dest=dest,
            budget_seconds=self.direct_budget_seconds,
            on_progress=on_progress,
        )
        if download.path is None:
            detail = "；".join(download.failures) or "一个候选地址都没有"
            msg = (
                f"播放直链全部下载失败（试过 {download.attempted} 个：{detail}）"
                f"（yt-dlp 那一轮：{yt_dlp_error}）"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        size_bytes = await asyncio.to_thread(_stat_size, download.path)
        # 兜底这条路**不经过 yt-dlp**，所以没有"哪一档 cookie"可记 —— 桥的页面上下文
        # 就是登录态本身。留 None 而不是编一个 "page_context" 假装它是阶梯上的一档。
        page_rung = None
        # 签名直链不进返回值：CDN 直链几小时后失效（V1 §7.2），
        # 库里躺一堆死链比库里少一条链更难查。看护见
        # `test_signed_play_url_never_reaches_the_artifact`。
        self._log.info(
            "douyin.media.play_url_ok",
            platform_video_id=video.platform_video_id,
            candidates=len(urls),
            bytes=size_bytes,
            yt_dlp_error=yt_dlp_error,
        )
        return await self._single_file_artifact(
            download.path,
            size_bytes,
            media_source="page_play_url",
            yt_dlp_error=yt_dlp_error,
            cookie_rung=page_rung,
        )

    async def _single_file_artifact(
        self,
        path: Path,
        size_bytes: int,
        *,
        media_source: MediaSource,
        yt_dlp_error: str | None,
        cookie_rung: str | None = None,
    ) -> SingleFileArtifact:
        """收成 `SingleFileArtifact`。两处不是走形式的细节：

        - `has_audio` **问 ffprobe，而不是照默认值填 True**。这里是抖音这一侧
          V1 §7.21 的落点：平台声明 `supports_dash_split=False`，意思是
          "拿到纯视频轨时我没有 `VideoAudioPairArtifact` 这个表达方式可用"——
          那更不能靠默认值假装它有音频。问不出来时 `has_audio_stream()` 返回 True
          （判成"没有"的后果是白白丢一段口播稿，而丢稿子在看板上完全不可见）。
        - `path` 是 `dest` 下的路径，**相对 `data/` 的归一化是入库那一层的事** ——
          `AdapterDeps` 里没有 `FileStorage`，而只有 handler 知道这条作品最终归在谁名下。
          见 `docs/specs/platform-adapter.md §2.4` 的修订说明。
        """
        return SingleFileArtifact(
            path=path,
            size_bytes=size_bytes,
            media_source=media_source,
            yt_dlp_error=yt_dlp_error,
            cookie_rung=cookie_rung,
            has_audio=await has_audio_stream(path),
        )

    async def fetch_subtitles(self, video: VideoMeta) -> None:
        """抖音没有公开字幕轨 —— **返回 None，不抛**。

        抛会让"每条作品都先失败一次"变成常态，而调度器要的是"没有字幕，去走 ASR"
        （`platforms/base.py` 的 Protocol docstring 同一口径）。
        """
        self._log.debug("douyin.subtitles.unsupported", platform_video_id=video.platform_video_id)
        return None  # noqa: RET501,PLR1711 - 这个 None **就是**契约（"没有字幕"而不是"失败"），
        # 省掉它可读性更差：Protocol 那边写的是 `-> Transcript | None`。

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _describe(self, video: VideoMeta) -> str:
        return f"{video.platform_video_id} @ {video.creator_ref.platform_id}"

    def _require_bridge(self) -> BridgeClient:
        """拿桥。为 None 时给一条**说得清是装配错误**的红，而不是 AttributeError。"""
        if self._deps.bridge is None:
            raise PlatformError(PLATFORM, "list", _NO_BRIDGE_MESSAGE)
        return self._deps.bridge

    def _ladder(self, environ: Mapping[str, str] | None = None) -> CookieLadder:
        return resolve_cookie_ladder(
            self.capabilities.cookie_variants,
            config=self._config,
            cookies=self._deps.cookies,
            environ=environ,
        )

    def _ytdlp_runner(self) -> YtDlpRunner:
        """构造 yt-dlp Runner。**这是测试的替换点**：

        `AdapterDeps` 里没有 `YtDlpRunner` 这个字段 —— deps 是"平台共用的外部世界"，
        而 argv 与超时是每个平台自己的事。所以换假对象用
        `monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: fake_runner)`，
        而不是往 `AdapterDeps` 上再加一个只有两个平台会读的洞。
        """
        return YtDlpRunner(
            default_variants=(),
            timeout_seconds=YTDLP_BUDGET_SECONDS,
            extra_args=YTDLP_EXTRA_ARGS,
        )

    async def _pace(self) -> None:
        """按 `rate_limit.per_minute` 给桥请求上闸（抖音侧风控是第一约束，§7.2 的来源）。

        只管"同一个平台内部别把页面刷爆"。`per_creator_seconds`（跨博主的间隔）
        **不在这里实现**：适配器一次只看到一位博主，跨博主的序列只有调度器有全局视图
        （Task 8 的 per-platform Semaphore 那一层）。
        """
        await self._pacer.wait()


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
