"""`BilibiliAdapter` —— V1 `download_bili_following_latest.py` 站在 `PlatformAdapter` 后面。

V1 §7 的看护落点：

| 陷阱 | 落点 |
| --- | --- |
| §7.15 三档；枚举与下载都要带导出 cookie | `_ladder()` / `_enumerate_with_ytdlp()` |
| §7.21 未合并 DASH 分片要成对交出 | `infra.ytdlp.classify_artifacts()` / `_pair_artifact()` |
| §7.14 枚举不依赖外部脚本，找不到也不能静默 skip | `listing.load_external_manifest()` 抛 |
| §7.13 技能脚本两份会漂，只认仓库内那份产出 | 同上，报错文案点名生产者 |
| §7.16 搜索兜底要 Node 版 playwright，默认关 | `_enumerate()` 里开启后**如实红**，不静默跳过 |

与抖音那条链路最关键的区别：**B站 没有"页面播放直链"这种兜底**。
yt-dlp 不在 PATH 就是真的下不了，所以 `healthcheck()` 里同一个组件在两处颜色不同 ——
抖音 `yt_dlp: degraded`（还有路可走），B站 `yt_dlp: unreachable`（没有）。
看护见 `test_missing_yt_dlp_is_unreachable_here_not_degraded`。

字幕（`supports_subtitles=True`）走 `subtitles.py`：**确认没有**才返回 None，
接口挂了要抛，别让一次风控静默变成一整轮 ASR。
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx

from intelligence_hub_v2.errors import ListError, MediaDownloadError, PlatformError
from intelligence_hub_v2.infra.ffmpeg import has_audio_stream
from intelligence_hub_v2.infra.pacing import RatePacer
from intelligence_hub_v2.infra.ytdlp import (
    MediaParts,
    YtDlpRunner,
    classify_artifacts,
    progress_from_ytdlp_line,
)
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.engagement import MetricReadings, VideoCommentDraft
from intelligence_hub_v2.models.media import (
    MediaArtifact,
    SingleFileArtifact,
    VideoAudioPairArtifact,
)
from intelligence_hub_v2.models.task import ProgressCallback
from intelligence_hub_v2.models.transcript import Transcript
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.base import (
    AdapterDeps,
    Capabilities,
    ComponentStatus,
    HealthReport,
    PlatformConfig,
)
from intelligence_hub_v2.platforms.bilibili import comments, listing, media, subtitles
from intelligence_hub_v2.platforms.bilibili.config import BilibiliConfig
from intelligence_hub_v2.platforms.bilibili.listing import BiliCard
from intelligence_hub_v2.platforms.bilibili.media import CookieLadder, resolve_cookie_ladder
from intelligence_hub_v2.platforms.bilibili.urls import (
    as_http_url,
    card_api_url,
    extract_bvid,
    extract_mid,
    is_short_link,
    require_model_url,
    space_url,
    space_video_url,
)
from intelligence_hub_v2.platforms.registry import register

PLATFORM = "bilibili"

YTDLP_BUDGET_SECONDS = 7200.0
"""V1 那条 `timeout=60*60*2` 原样搬过来。4K/长视频 + 三档 cookie 阶梯，
预算给小了会把"其实正在下"判成失败，然后退到匿名档拿到糊的版本。"""

NETWORK_PROBE_URL = "https://api.bilibili.com/"
"""健康检查用的探活地址。**只要有任何 HTTP 响应就算可达**（哪怕是 404）：
这一格回答的是"这台机器到得到 B站的接口"，不是"某个视频存在吗"。
不探具体作品：那会把"作品被删"混进网络红灯里。"""

_CDN_PLAY_NOTE = "（带登录 cookie 可能有 AI 字幕，当前请求没取到）"


@register(PLATFORM)
class BilibiliAdapter:
    """B站采集器。看护落点见模块 docstring 那张表。"""

    name: ClassVar[str] = PLATFORM
    display_name: ClassVar[str] = "B站"

    capabilities: ClassVar[Capabilities] = Capabilities(
        needs_browser=False,
        needs_cookies=True,
        # 顺序的唯一真源（ADR-0011）。V1 §7.15：导出文件必须排第一。
        cookie_variants=("exported_file", "browser", "anonymous"),
        supports_subtitles=True,
        supports_dash_split=True,
        list_strategy="yt_dlp_flat",
        media_strategy="yt_dlp",
        # 评论有公开的 web 接口（`x/v2/reply`），所以这一家是真能问的 —— 不是"装的"：
        # `fetch_comments` 里没有一条代码路径需要桥。
        supports_comments=True,
    )

    def __init__(self, config: PlatformConfig, deps: AdapterDeps) -> None:
        if not isinstance(config, BilibiliConfig):
            msg = (
                f"BilibiliAdapter 需要 BilibiliConfig，拿到的是 {type(config).__name__} —— "
                f"PLATFORM_CONFIG_SCHEMAS['bilibili'] 与适配器不匹配（装配错误）"
            )
            raise PlatformError(PLATFORM, "task", msg)
        self._config: BilibiliConfig = config
        self._deps = deps
        self._log = deps.logger
        self._pacer = RatePacer(per_minute=config.rate_limit.per_minute)
        # 测试替换点（同抖音那一套）
        self.request_timeout_seconds = float(config.advanced.request_timeout_seconds)

    @classmethod
    def config_schema(cls) -> type[PlatformConfig]:
        return BilibiliConfig

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #

    async def healthcheck(self) -> HealthReport:
        """探网络 / cookie / yt-dlp 三件事。

        两条与抖音不同的判红理由：

        - **没装 yt-dlp 在这里是 `unreachable`**（抖音那边是 `degraded`）。
          B站 没有第二条媒体路径，yt-dlp 不在就是采不了 —— 报 degraded
          会让人以为"跟抖音一样能跑，只是画质差一点"，那是把人往错的方向支。
        - **没有导出 cookie 也是 `degraded` 而不是 `unreachable`**：
          匿名档确实能下（只是 4K/高帧率不可用，V1 §7.15），
          而且浏览器档在非 Windows 上还能用。

        桥不在检查范围里（`needs_browser=False`）；如果配置把 `use_cdp_bridge`
        勾上了，那是配置与声明不一致，由 `test_capabilities_and_config_agree` 那类
        结构看护抓，不是运行期来兜。
        """
        components: dict[str, ComponentStatus] = {}
        details: list[str] = []

        if shutil.which("yt-dlp") is None:
            components["yt_dlp"] = "unreachable"
            details.append("PATH 里没有 yt-dlp —— B站 的枚举与媒体都走它，没有第二条路")
        else:
            components["yt_dlp"] = "ok"

        ladder = self._ladder()
        components["cookies"] = "ok" if ladder.cookie_file is not None else "degraded"
        if ladder.note:
            details.append(ladder.note)

        try:
            response = await self._deps.http.get(
                NETWORK_PROBE_URL,
                timeout=min(10.0, self.request_timeout_seconds),
                headers=listing.api_headers(referer="https://www.bilibili.com/"),
            )
            components["network"] = "ok"
            # 状态码只用来判"答没答话"；404/403 都算接口在。真被风控时是连不上或超时。
            self._log.debug("bilibili.network_probe", status=response.status_code)
        except httpx.HTTPError as exc:
            components["network"] = "unreachable"
            details.append(f"到 api.bilibili.com 不通：{type(exc).__name__}: {exc}")

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
        """把链接收成 `CreatorRef`（身份是 `mid`）。

        四条路径，前三条**不联网**：

        1. `space.bilibili.com/<mid>` / `m.bilibili.com/space/<mid>` / 裸数字
        2. 一条作品链接（`bilibili.com/video/BV…`）→ 取一次 `view` 接口反查 `owner.mid`
           （V1 没有这条：用户从"这个 UP 主发过一条这样的"进来时粘的就是作品链接。
           抖音那边做不了同样的事，因为它的详情接口要页面上下文）
        3. 都不认 → `PlatformError(stage="parse_url")` 带原文
        4. `b23.tv` 短链 → 跟一次 302 再走上面几条（**短链里没有任何身份信息**，
           同 V1 §7.1 那一类，只是抖音更常见）
        """
        text = str(url or "").strip()
        if not text:
            msg = "链接是空的（用户粘贴时被截断了？前端传参漏了？）"
            raise PlatformError(PLATFORM, "parse_url", msg)

        mid = extract_mid(text)
        if not mid and is_short_link(text):
            expanded = await self._resolve_short_link(text)
            mid = extract_mid(expanded)
            if not mid:
                # 展开后是一条作品链接也认，下一步统一处理
                text = expanded
        if not mid:
            bvid = extract_bvid(text)
            if not bvid and is_short_link(text):
                bvid = extract_bvid(await self._resolve_short_link(text))
            if bvid:
                data = await self._view(bvid)
                mid = listing.view_to_profile_fields(data)["mid"]
                if not mid:
                    msg = f"{bvid} 的 view 接口没给出 owner.mid（接口改形状了？）：原始输入 {text}"
                    raise PlatformError(PLATFORM, "parse_url", msg)
        if not mid:
            msg = f"链接里认不出 B站 博主身份（mid）：{text}"
            raise PlatformError(PLATFORM, "parse_url", msg)
        if not mid.isdigit():
            msg = f"认出来的 mid 不是纯数字（{mid!r}）：原始输入 {text}"
            raise PlatformError(PLATFORM, "parse_url", msg)

        source = text if text.lower().startswith(("http://", "https://")) else None
        return CreatorRef(
            platform=PLATFORM,
            platform_id=mid,
            profile_url=require_model_url(space_url(mid), context=f"博主 {mid} 的空间页"),
            # 来源链接认不出就当没有：它是"用户粘的那一条"，形状不受我们控制，
            # 为它把一次成功的解析判红不值得。
            source_url=as_http_url(source) if source else None,
        )

    async def _resolve_short_link(self, url: str) -> str:
        try:
            response = await self._deps.http.get(
                url,
                follow_redirects=True,
                timeout=self.request_timeout_seconds,
                headers=listing.api_headers(referer="https://www.bilibili.com/"),
            )
        except httpx.HTTPError as exc:
            msg = f"展开 b23.tv 短链失败（{url}）：{type(exc).__name__}: {exc}"
            raise PlatformError(PLATFORM, "parse_url", msg) from exc
        return str(response.url or url)

    async def fetch_creator_profile(self, ref: CreatorRef) -> CreatorProfile:
        """昵称 / 头像 / 粉丝数 / 签名。走 `x/web-interface/card`（实测匿名可访问）。"""
        payload = await self._get_json(card_api_url(ref.platform_id), stage="profile")
        # B站 的业务负载一律嵌在 `data` 里（view / player / card 三家都这样）。
        # 直接读 payload["card"] 会永远拿到 None，而症状是"每位博主都没昵称"。
        data = payload.get("data")
        card = data.get("card") if isinstance(data, Mapping) else None
        if not isinstance(card, Mapping):
            msg = f"card 接口没有 card 字段（{ref.platform_id}）：{str(payload)[:200]}"
            raise PlatformError(PLATFORM, "profile", msg)
        name = str(card.get("name") or "").strip()
        fans = _optional_int(card.get("fans"))
        return CreatorProfile(
            ref=ref,
            name=name or ref.platform_id,
            avatar_url=as_http_url(card.get("face")),
            follower_count=fans,
            bio=str(card.get("sign") or "").strip() or None,
            extra={
                "level": _optional_int((card.get("level_info") or {}).get("current_level"))
                if isinstance(card.get("level_info"), Mapping)
                else None,
                "official": str((card.get("Official") or {}).get("title") or "")
                if isinstance(card.get("Official"), Mapping)
                else "",
            },
        )

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

        **`since` 传了才会逐条补元数据**（`x/web-interface/view`，一条一个请求）。
        理由：flat-playlist 只保证 `bvid`/`url`/`title`，**发布时间要另问**；
        而没要求按时间过滤时，为填字段多发 N 个请求不划算 ——
        一位博主 30 条 × 20 位 = 600 个请求，按 `per_minute=60` 要跑 10 分钟。
        后处理阶段本来会逐条补（Task 8），所以那些字段晚一点到手不影响正确性。
        这条与抖音正相反：那边是**问了也问不出来**（详情接口要页面上下文），
        所以 `since` 在抖音是弱过滤、在这里是实过滤。

        枚举失败抛 `ListError`，**不外落到搜索兜底**除非配置显式开启
        （V1 §7.16：Node 版 playwright 那条路依赖链很脏，默认关；
        开启后检测不到要如实失败，不许静默跳过）。
        """
        if limit <= 0:
            return
        cards = await self._enumerate(ref, limit=limit)
        emitted = 0
        for card in cards:
            if emitted >= limit:
                break
            meta = listing.card_to_video_meta(card, ref=ref)
            if since is not None:
                meta = await self._enrich(meta, card)
                if meta.published_at is not None and meta.published_at < since:
                    continue
            yield meta
            emitted += 1

    async def _enumerate(self, ref: CreatorRef, *, limit: int) -> list[BiliCard]:
        """按配置选枚举路径：外部清单命中 → 用它；否则 yt-dlp flat-playlist。"""
        manifest_path = self._config.external_browser_manifest_path
        if manifest_path is not None:
            entries = listing.load_external_manifest(Path(manifest_path))
            cards = listing.manifest_cards_for(entries, mid=ref.platform_id, limit=limit)
            if cards:
                self._log.info(
                    "bilibili.list.from_manifest",
                    mid=ref.platform_id,
                    count=len(cards),
                    path=str(manifest_path),
                )
                return cards
            self._log.info("bilibili.list.manifest_miss", mid=ref.platform_id)

        # 硬失败（412 / 没装 yt-dlp / 退出码非 0）**直接往外抛，原文不动**。
        # 只有"跑通了但一条都没抽出来"才走到下面那一步 ——
        # 曾经写成 `except ListError: cards = []`，于是勾上兜底时
        # 那句真 412 被换成了"未实现"，排查的人就看不到风控原文了。
        cards = await self._enumerate_with_ytdlp(ref, limit=limit)
        if not cards:
            if self._config.advanced.search_fallback_node_playwright:
                raise ListError(
                    PLATFORM,
                    "list",
                    "yt-dlp 空间枚举没出结果，且 V2 尚未实现 Node-playwright 搜索兜底"
                    "（V1 §7.16 那条路依赖链脏：npm 全局包不在 require 搜索路径里，"
                    "要 NODE_PATH；pip 那个 playwright 不算数）。"
                    "要么把 search_fallback_node_playwright 关掉，要么等这条被真正实现 —— "
                    "**静默跳过就是臆造成功**。",
                )
            msg = (
                f"空间枚举退出码 0 但一条作品都没抽出来（mid={ref.platform_id}）："
                f"页面结构或 yt-dlp 的抽取器对不上了"
            )
            raise ListError(PLATFORM, "list", msg)
        return cards

    async def _enumerate_with_ytdlp(self, ref: CreatorRef, *, limit: int) -> list[BiliCard]:
        """V1 §7.15 的另一半：**枚举这一路也要带导出 cookie**。

        2026-09-22 本机匿名实测 `yt-dlp --flat-playlist <space>/video` 回
        `Request is blocked by server (412)` —— 那句原文正好在
        `should_escalate_cookie_rung()` 的表里，所以带 cookie 的第一档会先跑、
        风控时才会退档。只写"下载带 cookie"而枚举裸奔，是这条坑的原始形状。
        """
        ladder = self._ladder()
        await self._pace()
        try:
            result = await self._ytdlp_runner().flat_playlist(
                space_video_url(ref.platform_id),
                variants=ladder.variants,
                playlist_items=limit,
            )
        except LookupError as exc:
            raise ListError(
                PLATFORM, "list", f"未安装 yt-dlp，B站 的列表枚举没有第二条路：{exc}"
            ) from exc
        except MediaDownloadError as exc:
            raise ListError(PLATFORM, "list", str(exc)) from exc

        if not result.ok:
            msg = (
                f"空间枚举失败（mid={ref.platform_id}）：{media.ytdlp_failure_reason(result)}"
                f"｜阶梯：{ladder.note or '导出 cookie 正常'}"
            )
            raise ListError(PLATFORM, "list", msg)

        return listing.entries_to_cards(listing.parse_dump_json_lines(result.stdout), limit=limit)

    async def _enrich(self, meta: VideoMeta, card: BiliCard) -> VideoMeta:
        """逐条补 `published_at` / `cid` / 指标。单条失败**只缺字段，不丢作品**。"""
        try:
            data = await self._view(card.bvid)
        except PlatformError as exc:
            self._log.warning("bilibili.view_enrich_failed", bvid=card.bvid, error=str(exc))
            return meta
        enriched = listing.card_to_video_meta(
            listing.view_to_card(card.bvid, data), ref=ref_of(meta)
        )
        # 列表那边拿到的 title 有时比 view 的更贴（分 P 情形），保留下来
        extra = {**dict(meta.extra), **{k: v for k, v in enriched.extra.items() if v is not None}}
        return enriched.model_copy(update={"extra": extra, "title": enriched.title or meta.title})

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
        """yt-dlp 单路下载（三档 cookie 阶梯）。

        产物形状有两种，**这个区分就是 V1 §7.21**：

        - `SingleFileArtifact(media_source="yt_dlp")` —— 合并成功。
        - `VideoAudioPairArtifact(media_source="dash_split")` —— yt-dlp 留下了
          `media.f30064.mp4`（纯视频）+ `media.f30280.m4a`（纯音频）。
          这时 `yt_dlp_error` 装的不是"失败"而是"为什么是两条"
          （多半是 PATH 上没有 ffmpeg）—— 下游转写必须走 `audio_path_of()`，
          把纯视频轨喂给 `ffmpeg -vn` 会得到那句
          `Output file does not contain any stream`，长得像"ffmpeg 没装"而方向完全不同。

        `advanced.dash_split_handling == "merge"` 是严格档：拿到分片直接判失败，
        因为用户说了"我只要一个文件"，那就别给他两个文件还报成功。
        """
        ladder = self._ladder()
        await self._pace()
        try:
            result = await self._ytdlp_runner().download(
                str(video.webpage_url),
                dest,
                variants=ladder.variants,
                file_template=media.MEDIA_FILE_TEMPLATE,
                on_line=_line_reporter(on_progress),
            )
        except LookupError as exc:
            msg = f"未安装 yt-dlp，B站 的媒体下载没有第二条路（{video.platform_video_id}）：{exc}"
            raise MediaDownloadError(PLATFORM, "media", msg) from exc

        rung = str(result.variant) if result.variant else "未知档"
        if not result.ok:
            msg = (
                f"下载失败（{video.platform_video_id}）：{media.ytdlp_failure_reason(result)}"
                f"｜阶梯：{ladder.note or rung}"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        parts = classify_artifacts(result.artifacts)
        if parts.kind == "empty":
            msg = (
                f"yt-dlp 退出码 0 但没有产出可读的文件"
                f"（{video.platform_video_id}）：报出的路径 {[str(p) for p in result.artifacts]}"
                f"｜阶梯：{rung}"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        if parts.kind == "pair":
            return await self._pair_artifact(video, parts, ladder=ladder, rung=rung)

        main = parts.main
        if main is None:  # pragma: no cover - classify 保证了 kind=="single" ⇒ main 有值
            msg = f"分片分类自相矛盾（{video.platform_video_id}）：{parts.description}"
            raise MediaDownloadError(PLATFORM, "media", msg)
        if parts.extras:
            self._log.warning(
                "bilibili.extra_artifacts_ignored",
                platform_video_id=video.platform_video_id,
                chosen=str(main),
                others=[str(p) for p in parts.extras],
            )
        return SingleFileArtifact(
            path=main,
            size_bytes=_size(main),
            media_source="yt_dlp",
            # 成功就是成功：`yt_dlp_error` 只装失败原文，阶梯情况说明走 `cookie_rung`
            # 与那条 `bilibili.cookie_ladder` 日志。混着用会让一次正常的匿名下载
            # 在看板上长得像"哪里错了但没说清"。
            yt_dlp_error=None,
            cookie_rung=rung,
            has_audio=await has_audio_stream(main),
            duration_seconds=video.duration_seconds,
        )

    async def _pair_artifact(
        self, video: VideoMeta, parts: MediaParts, *, ladder: CookieLadder, rung: str
    ) -> MediaArtifact:
        why = (
            "yt-dlp 没能把视频轨与音频轨合并（多半是 PATH 里没有 ffmpeg），"
            "已按未合并 DASH 分片交出 —— 转写必须吃 audio_path()，"
            "不要 rglob('*.mp4')（V1 §7.21）"
        )
        if self._config.advanced.dash_split_handling == "merge":
            msg = (
                f"dash_split_handling='merge' 要求单文件，但拿到了一对分片"
                f"（{video.platform_video_id}）：{why}｜阶梯：{rung}"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)
        video_track, audio_track = parts.video, parts.audio
        if video_track is None or audio_track is None:  # pragma: no cover
            msg = f"分片对不完整（{video.platform_video_id}）：{parts.description}"
            raise MediaDownloadError(PLATFORM, "media", msg)
        note = f"{why}｜阶梯：{ladder.note or rung}"  # 分片**是**一种降级，所以留在 error 里
        if parts.extras:
            self._log.warning(
                "bilibili.dash_pair_extra_files",
                platform_video_id=video.platform_video_id,
                others=[str(p) for p in parts.extras],
            )
        self._log.info("bilibili.media.dash_split", platform_video_id=video.platform_video_id)
        return VideoAudioPairArtifact(
            video_path=video_track,
            audio_path=audio_track,
            video_size_bytes=_size(video_track),
            audio_size_bytes=_size(audio_track),
            yt_dlp_error=note,
            cookie_rung=rung,
            duration_seconds=video.duration_seconds,
        )

    # ------------------------------------------------------------------ #
    # 字幕
    # ------------------------------------------------------------------ #

    async def fetch_subtitles(self, video: VideoMeta) -> Transcript | None:
        """有字幕优先字幕（省一整轮 ASR）。

        返回 None 的**唯一**条件是"B站 确认这条作品没有可用字幕轨"。
        接口挂了 / 被风控 → 抛 `PlatformError`：吞成 None 会让一次网络故障
        静默变成"这一条去跑 ASR"，症状是"转写特别慢"而日志干净（最难归因的一类）。

        `cid` 多 P 视频是分 P 的：列表那边只有 bvid 时这里补一次 `view`。
        """
        cid = video.extra.get("cid")
        bvid = video.platform_video_id
        if not cid:
            try:
                data = await self._view(bvid)
            except PlatformError as exc:
                self._log.warning("bilibili.subtitle_cid_lookup_failed", bvid=bvid, error=str(exc))
                raise
            cid = data.get("cid") or (
                (data.get("pages") or [{}])[0].get("cid")
                if isinstance(data.get("pages"), list) and data.get("pages")
                else None
            )
        if not cid:
            self._log.info("bilibili.subtitle_no_cid", bvid=bvid, note=_CDN_PLAY_NOTE)
            return None
        transcript = await subtitles.fetch_transcript(
            self._deps.http,
            bvid,
            str(cid),
            budget_seconds=self.request_timeout_seconds,
        )
        if transcript is None:
            self._log.info("bilibili.subtitle_absent", bvid=bvid, note=_CDN_PLAY_NOTE)
        return transcript

    # ------------------------------------------------------------------ #
    # 评论与读数（ADR-0020 决定二）
    # ------------------------------------------------------------------ #

    async def fetch_comments(
        self, video: VideoMeta, *, limit: int = 50, sort: str = "hot"
    ) -> list[VideoCommentDraft] | None:
        """`x/v2/reply` 的顶层评论。这家**有**这项能力，所以正常路径不返回 None。

        `aid` 要先问一次 `view`：评论接口的 `oid` 要的是数字 aid，给 bvid 会回
        `code=-404 啥都木有`，**HTTP 200**（V1 同一条坑，见 `urls.reply_api_url` 的注释）。
        所以"取评论之前先补一次 view"不是多此一举，是这一家接口的形状决定的。
        """
        if sort not in comments.SORT_BY_NAME:
            # 认不出的排序词必须响，不能"当作 hot"：一个拼错的 `"newst"` 静默按赞排，
            # 界面上会长得完全像"热评就这些"（同一口径见 `media.py` 那个 sort 参数）。
            msg = f"B站 评论只认 {sorted(comments.SORT_BY_NAME)}，收到 {sort!r}"
            raise PlatformError(PLATFORM, "comments", msg)
        data = await self._view(video.platform_video_id)
        aid = data.get("aid")
        if aid is None:
            msg = (
                f"view 接口没给 aid（bvid={video.platform_video_id}），"
                "评论接口的 oid 问不了 —— 不去发一个注定 -404 的请求"
            )
            raise PlatformError(PLATFORM, "comments", msg)
        drafts, pages = await comments.fetch_top_level_comments(
            self._deps.http,
            aid=aid,
            bvid=video.platform_video_id,
            limit=limit,
            sort=comments.SORT_BY_NAME[sort],
            budget_seconds=self.request_timeout_seconds,
        )
        self._log.info(
            "bilibili.comments_fetched",
            bvid=video.platform_video_id,
            count=len(drafts),
            pages=len(pages),
        )
        return drafts

    async def fetch_metrics(self, video: VideoMeta) -> MetricReadings:
        """再问一次 `view`，取 `stat` 那一段。窗口由调用方算（`base.py` 的契约注释）。

        四项全空要抛而不是交回一个空读数：空读数会被 `MetricSnapshotRepository.put`
        拒收，但**拒收之前**这一家的这一轮已经被当成"抓过了"记进清单了 ——
        把它在这里就顶回去，失败原因才是真的那一句（"接口没给数"）。
        """
        data = await self._view(video.platform_video_id)
        readings = listing.stat_to_readings(data)
        if readings.is_empty():
            msg = (
                f"view 接口的 stat 一个数都没给（bvid={video.platform_video_id}）："
                "这是接口形状变了或被风控，不是'这条作品零互动'"
            )
            raise PlatformError(PLATFORM, "metrics", msg)
        return readings

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _view(self, bvid: str) -> Mapping[str, Any]:
        return await listing.fetch_view(
            self._deps.http, bvid, budget_seconds=self.request_timeout_seconds
        )

    async def _get_json(self, url: str, *, stage: str) -> Mapping[str, Any]:
        """任何 `api.bilibili.com` 接口的公共一层：判 HTTP、判 JSON、**判业务 code**。

        B站 把业务失败编在 200 里（`{"code": -404, "message": "啥都木有"}`），
        只看状态码会把"不存在"当成成功，然后拿着空 dict 往下走。
        """
        try:
            response = await self._deps.http.get(
                url,
                timeout=self.request_timeout_seconds,
                headers=listing.api_headers(referer="https://www.bilibili.com/"),
            )
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            msg = f"接口请求失败（{url}）：{type(exc).__name__}: {exc}"
            raise PlatformError(PLATFORM, stage, msg) from exc
        if not isinstance(payload, dict):
            msg = f"接口返回形状不对（{url}）：{type(payload).__name__}"
            raise PlatformError(PLATFORM, stage, msg)
        if payload.get("code") != 0:
            msg = (
                f"B站 接口回 code={payload.get('code')} message={payload.get('message')!r}（{url}）"
            )
            raise PlatformError(PLATFORM, stage, msg)
        return payload

    def _ladder(self, environ: Mapping[str, str] | None = None) -> CookieLadder:
        return resolve_cookie_ladder(
            self.capabilities.cookie_variants,
            config=self._config,
            cookies=self._deps.cookies,
            environ=environ,
        )

    def _ytdlp_runner(self) -> YtDlpRunner:
        """测试替换点（同抖音：`AdapterDeps` 里没有 runner 字段）。"""
        return YtDlpRunner(
            default_variants=(),
            timeout_seconds=YTDLP_BUDGET_SECONDS,
            extra_args=media.YTDLP_EXTRA_ARGS,
        )

    async def _pace(self) -> None:
        await self._pacer.wait()


def _worst_status(components: Mapping[str, ComponentStatus]) -> ComponentStatus:
    order: dict[ComponentStatus, int] = {"ok": 0, "degraded": 1, "unreachable": 2}
    ranks: tuple[ComponentStatus, ...] = ("ok", "degraded", "unreachable")
    worst = max((order[status] for status in components.values()), default=0)
    return ranks[worst]


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        msg = f"刚落盘的媒体读不到（{path}）：{exc}"
        raise MediaDownloadError(PLATFORM, "media", msg) from exc


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = str(value).strip().replace(",", "")
    return int(text) if text.isdigit() else None


def ref_of(meta: VideoMeta) -> CreatorRef:
    """从 `VideoMeta` 拿回那位博主的引用（`_enrich` 重建 meta 时要用）。"""
    return meta.creator_ref


def _line_reporter(on_progress: ProgressCallback | None) -> Callable[[str], None] | None:
    if on_progress is None:
        return None

    def on_line(line: str) -> None:
        fraction = progress_from_ytdlp_line(line)
        if fraction is not None:
            on_progress(min(1.0, max(0.0, fraction)))

    return on_line
