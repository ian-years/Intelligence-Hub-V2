"""`YouTubeAdapter` —— V1 `download_youtube_latest.py` 站在 `PlatformAdapter` 后面。

形状与 B站 同族（`needs_browser=False`、`list_strategy="yt_dlp_flat"`、
`media_strategy="yt_dlp"`），差别只有两处，但两处都是从平台上读出来的：

- **不带任何 cookie**：`cookie_variants=("none",)`，阶梯只有一档、argv 里没有
  `--cookies`。所以 V1 §7.15 那族坑（"枚举与下载两条路都要带导出 cookie"）
  在这一族不存在，`tools/refresh_bridge_cookies.py` 的默认域名清单里也就没有它。
- **网络到不了就是采不了**：AGENTS.md §1.3 要求"缺依赖时如实失败"，
  本机到 YouTube 大概率直连不通，于是 `healthcheck()` 的 `network` 一格必须是
  `unreachable` **并带上 httpx 的原文**，preflight 那一页才分得出
  "网络不通"与"没装东西"（后者由 `yt_dlp` / `ffmpeg` / `node` 三格各自回答）。

V1 里**没有搬**的东西，逐条理由（V1 的东西 → 为什么）：

- `SHOW_YOUTUBE` 环境变量 —— V2 的平台开关（yaml 的 `enabled`）天然覆盖那一格。
- 三个 `YOUTUBE_*_TIMEOUT` env 键 —— 预算归任务超时 +
  `advanced.request_timeout_seconds`；再加三个 env 键是第四处真相。**数值照搬**成模块常量。
- `list_tracked_channels()` 读 `creators.json` —— V2 的博主清单在 SQLite
  （`creators` 表 + 跟踪开关），V1 那份 JSON 由 `tools/migrate_from_v1.py` 搬进来。
- `LocalStore.upsert_video()` / `write_local_record()` —— 入库是 `tasks/collect.py`
  的活，适配器不碰库。
- `write_artifacts()` 落 `metadata.json` 与 `video-description.txt` ——
  `data-model.md §1` 的产物清单里没有这两份；描述与指标进 `videos` 表。
- `quote_for_cmd()` / `spawn_command()` —— `infra/subprocess.py` 全程 `shell=False`，
  那两处的存在理由（`.bat` 入口、`<` 被 cmd 当重定向）在 V2 不成立。
- `ytdlp_command()` 退到 `python -m yt_dlp` —— 只认 PATH 上那一份
  （§7.19 那条"进程 PATH 要与注册表一致"由 `core/runtime_env.py` 统一补）。
- 下载完 `glob("*.mp4")` 认产物 —— 那是 §7.21 的原始形状：分片情形下会把
  **纯视频轨**当成品。改成 `classify_artifacts` + "分片即失败"。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import httpx

from intelligence_hub_v2.errors import ListError, MediaDownloadError, PlatformError
from intelligence_hub_v2.infra.ffmpeg import has_audio_stream
from intelligence_hub_v2.infra.pacing import RatePacer
from intelligence_hub_v2.infra.ytdlp import (
    YtDlpCookieVariant,
    YtDlpResult,
    YtDlpRunner,
    classify_artifacts,
    plan_cookie_variants,
    progress_from_ytdlp_line,
)
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.media import MediaArtifact, SingleFileArtifact
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
from intelligence_hub_v2.platforms.registry import register
from intelligence_hub_v2.platforms.youtube import listing, media
from intelligence_hub_v2.platforms.youtube.config import YouTubeConfig
from intelligence_hub_v2.platforms.youtube.urls import (
    as_http_url,
    canonical_video_url,
    channel_url,
    enumeration_url,
    extract_channel_id,
    require_model_url,
)

PLATFORM = "youtube"

logger = get_logger(__name__)

LIST_BUDGET_SECONDS = 180.0
DOWNLOAD_BUDGET_SECONDS = 1800.0
SUBTITLE_BUDGET_SECONDS = 120.0
"""三趟 yt-dlp 的预算，数值来自 V1 的 `YOUTUBE_LIST_TIMEOUT` /
`DOWNLOAD_TIMEOUT` / `INFO_TIMEOUT` 默认值（那三个环境变量键本身没搬，见模块 docstring）。
给小了会把"其实正在下"判成失败，给大了会在网络不通时把一整轮钉在超时上。"""

PROFILE_ENTRY_PROBE = 5
"""取博主资料时最多看几条作品。多要一条都不会更准 —— 频道名在**每一条**里都一样，
但一条都没有（新频道 / 全隐藏）时这一趟必须能区分"没作品"与"没连通"。"""

NETWORK_PROBE_URL = "https://www.youtube.com/"
"""探活只问"这台机器到得到 YouTube"，不问"某个视频存在吗"：
状态码不参与判定（403/404 都算"答话了"），真被挡时是连不上或超时。"""

YT_DLP_MISSING_HINT = (
    "PATH 里没有 yt-dlp —— YouTube 的枚举、下载、字幕三条路全走它，没有第二条路。"
    "修复：装 yt-dlp（`pip install -U yt-dlp` 或包管理器）并确认它在进程 PATH 里"
    "（V1 §7.19：装了但 PATH 里没有是这台机器的常态，`core/runtime_env.py` 会补一次）"
)

_SUBTITLE_SCRATCH_PREFIX = "intelligence-hub-youtube-subs-"


@register(PLATFORM)
class YouTubeAdapter:
    """YouTube 采集器。看护落点见模块 docstring 那张表。"""

    name: ClassVar[str] = PLATFORM
    display_name: ClassVar[str] = "YouTube"

    capabilities: ClassVar[Capabilities] = Capabilities(
        needs_browser=False,
        # YouTube 的公开作品页与 yt-dlp 的抽取器都不要求会话凭证：V1 那 630 行里
        # 一个 cookie 参数都没有，2026-09-24 的迁移计划也是按"不依赖桥、能自己验完"
        # 排它的（T2.2）。会员/年龄限定内容确实要登录态，但那是 V2 未实现的功能，
        # 不是一条"今天能跑但没接"的路 —— 声明 True 反而会让调度器去等一个不需要的前置。
        needs_cookies=False,
        # 只有一个真实档位：不带 cookie。声明成阶梯而不是空元组，是为了让
        # `download_media`/`flat_playlist` 都走 `plan_cookie_variants` 那一份实现
        # （空阶梯会被 `YtDlpRunner` 判成"没有可用的 cookie 档位"而抛）。
        cookie_variants=("none",),
        # yt-dlp 能取人工字幕与自动字幕，`fetch_subtitles` 真的去取（media.py 那一节）。
        # 声明了不实现等于对调度器撒谎（`Capabilities.supports_subtitles` 的 docstring）。
        supports_subtitles=True,
        # 分片**可能**发生（没装 ffmpeg 时），但本适配器的处置是判失败而不是交出一对文件，
        # 所以"我绝不交分片"这条声明是真的。改判成 True 要连着改清单与转写侧。
        supports_dash_split=False,
        list_strategy="yt_dlp_flat",
        media_strategy="yt_dlp",
    )

    def __init__(self, config: PlatformConfig, deps: AdapterDeps) -> None:
        if not isinstance(config, YouTubeConfig):
            msg = (
                f"YouTubeAdapter 需要 YouTubeConfig，拿到的是 {type(config).__name__} —— "
                f"PLATFORM_CONFIG_SCHEMAS['youtube'] 与适配器不匹配（装配错误）"
            )
            raise PlatformError(PLATFORM, "task", msg)
        self._config: YouTubeConfig = config
        self._deps = deps
        self._log = deps.logger
        self._pacer = RatePacer(per_minute=config.rate_limit.per_minute)
        # 测试替换点（同抖音/B站 那一套）
        self.request_timeout_seconds = float(config.advanced.request_timeout_seconds)
        # 字幕中间产物的落脚根目录。**注入点**：测试给 tmp_path，
        # 运行期默认系统临时目录（一次几十 KB 的 vtt，读完就在 finally 里删）。
        # 为什么不在 `data/tmp/` 下：`AdapterDeps` 里没有 `FileStorage`，
        # 而从 `cookies.directory` 反推 data 根是脏的 —— 加字段要动 Locked 契约，走 ADR。
        self.subtitle_scratch_root: Path = Path(tempfile.gettempdir())

    @classmethod
    def config_schema(cls) -> type[PlatformConfig]:
        return YouTubeConfig

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #

    async def healthcheck(self) -> HealthReport:
        """探四件事：yt-dlp / ffmpeg / node / 网络。

        每一格答的是**不同的动作**，所以合并成一格就是丢信息：

        - `yt_dlp` 缺 → **`unreachable`**（同 B站，不像抖音那样是 `degraded`）：
          这个平台没有第二条媒体路径。
        - `ffmpeg` 缺 → `degraded`：合并拿不到，`download_media` 会判失败，
          但枚举与字幕都还能跑。
        - `node` 缺 → 只在 `advanced.require_node` 为真时报 `degraded`。
          探到但没配要求时**不进 `components`** —— `HealthReport` 的字典里没有
          "unknown"，"没探"就不该占一格（`platforms/base.py::ComponentStatus`）。
        - `network` 不通 → `unreachable` 并带 httpx 原文。
          配了 `proxy` 时探活也走那个代理，否则会红成一个**假**的"网络不通"。

        `cookies` 一格**故意没有**：`needs_cookies=False`，问它等于让用户去修一个
        不存在的前置。
        """
        components: dict[str, ComponentStatus] = {}
        details: list[str] = []

        components["yt_dlp"] = "ok" if shutil.which("yt-dlp") else "unreachable"
        if components["yt_dlp"] != "ok":
            details.append(YT_DLP_MISSING_HINT)

        if shutil.which("ffmpeg"):
            components["ffmpeg"] = "ok"
        else:
            components["ffmpeg"] = "degraded"
            details.append(
                "PATH 里没有 ffmpeg：媒体下载会因为拿不到合并产物而判失败"
                "（本适配器不交未合并的 DASH 分片，V1 §7.21）"
            )

        if self._config.advanced.require_node:
            if media.node_available():
                components["node"] = "ok"
            else:
                components["node"] = "degraded"
                details.append(
                    "PATH 里没有 node：新版 yt-dlp 解析 YouTube 签名会失败"
                    "（V1 §4.1，装 Node.js 22+；不想要这条提醒就把 advanced.require_node 关掉）"
                )

        try:
            status = await self._probe_network()
            components["network"] = "ok"
            self._log.debug(
                "youtube.network_probe", status=status, proxied=bool(self._config.proxy)
            )
        except httpx.HTTPError as exc:
            components["network"] = "unreachable"
            via = f"（配了 proxy {self._config.proxy}）" if self._config.proxy else "（未配 proxy）"
            details.append(
                f"到 www.youtube.com 不通{via}：{type(exc).__name__}: {exc}。"
                "本机直连 YouTube 通常不通 —— 要么在 config/platforms.yaml 的 youtube.proxy "
                "填一个可用代理，要么接受这一格红"
            )

        return HealthReport(
            platform=PLATFORM,
            status=_worst_status(components),
            detail="；".join(details) or None,
            checked_at=datetime.now(UTC),
            components=components,
        )

    async def _probe_network(self) -> int:
        """问一次 YouTube。配了代理就换一条经代理的短连接。"""
        budget = min(10.0, self.request_timeout_seconds)
        if not self._config.proxy:
            response = await self._deps.http.get(NETWORK_PROBE_URL, timeout=budget)
            return response.status_code
        async with httpx.AsyncClient(proxy=self._config.proxy, timeout=budget) as client:
            response = await client.get(NETWORK_PROBE_URL)
            return response.status_code

    # ------------------------------------------------------------------ #
    # 博主
    # ------------------------------------------------------------------ #

    async def parse_creator_url(self, url: str) -> CreatorRef:
        """把链接收成 `CreatorRef`（身份是 `UC…` 频道 ID）。

        两条路，差别是**要不要联网**：

        1. 链接里就有 `UC…`（或用户直接粘了裸 ID）→ 不联网。
           这就是契约测试钩子 `resolvable_profile_url()` 用的那一条。
        2. 只有 `@手柄` / `/c/名字` / 一条**作品**链接 → 身份不在这串文本里，
           必须问一次 yt-dlp 才拿得到（V1 没有这一步：它把主页链接原样交给
           yt-dlp 就完事，于是同一个频道可以以 `@手柄` 与 `UC…` 两种身份进库 ——
           正是 V1 §7.1 那一族"拿一个会变的东西当主键"）。

        认不出/解析不出身份一律 `PlatformError(stage="parse_url")` 带原文，
        **不**退化成"把 URL 本身当 platform_id"。
        """
        text = str(url or "").strip()
        if not text:
            msg = "链接是空的（用户粘贴时被截断了？前端传参漏了？）"
            raise PlatformError(PLATFORM, "parse_url", msg)

        channel_id = extract_channel_id(text)
        source = as_http_url(text) if text.lower().startswith(("http://", "https://")) else None
        if not channel_id:
            # `enumeration_url` 自己会分"作品链接 / 手柄页 / 频道页"三种来路，
            # 这里不再判一遍：两处各判一遍迟早漂成"能枚举但不能解析身份"。
            target = enumeration_url(text, homepage_url=str(source or ""))
            channel_id = await self._resolve_channel_id(target, original=text)
        if not channel_id:
            msg = f"链接里认不出 YouTube 频道身份（UC… 频道 ID）：{text}"
            raise PlatformError(PLATFORM, "parse_url", msg)

        return CreatorRef(
            platform=PLATFORM,
            platform_id=channel_id,
            profile_url=require_model_url(channel_url(channel_id), context="频道主页"),
            # 来源链接认不出就当没有：它是"用户粘的那一条"，形状不受我们控制。
            source_url=source,
        )

    async def _resolve_channel_id(self, target: str, *, original: str) -> str:
        """问一次 yt-dlp"这个地址属于哪个频道"。答不出就返回空串（调用方判红）。"""
        await self._pace()
        result = await self._flat_playlist(target, playlist_items=1, stage="parse_url")
        entries = listing.parse_dump_json_lines(result.stdout)
        channel_id = listing.channel_id_from_entries(entries)
        if not channel_id:
            note = media.ytdlp_failure_reason(result) if not result.ok else "输出里没有 channel_id"
            msg = (
                f"从 {original!r} 认不出频道身份（枚举了 {target}）：{note}。"
                "要么这条链接不含频道信息，要么 YouTube 侧改字段了"
            )
            raise PlatformError(PLATFORM, "parse_url", msg)
        return channel_id

    async def fetch_creator_profile(self, ref: CreatorRef) -> CreatorProfile:
        """昵称 + 一条"我们能看到什么"的说明。

        拿不到的字段**留 None**而不是猜：

        - `avatar_url`：flat 条目里的 `thumbnail` 是**某条作品的封面**，
          不是频道头像。把作品封面当头像存进 `creators.avatar_url`
          是那种"看起来有数据、越看越不对"的脏。
        - `follower_count`：`--flat-playlist` 不带订阅数（完整 info 里才有，
          而 V1 也从来没读过那个字段）。0 与 None 的区别在这里是实质的：
          0 粉是一个合法值（V1 §1.3）。

        顺手做一次**身份对账**：条目自报的 `channel_id` 与请求的 ref 不一致时抛，
        因为那意味着我们拿 A 频道的名字去登记 B 频道（`urls._CHANNEL_ID_RE` 判松的
        代价就在这里兑付）。
        """
        await self._pace()
        target = enumeration_url(ref.platform_id, homepage_url=str(ref.profile_url))
        result = await self._flat_playlist(
            target, playlist_items=PROFILE_ENTRY_PROBE, stage="profile"
        )
        entries = listing.parse_dump_json_lines(result.stdout)
        videos, _rejected = listing.to_videos(entries)
        reported = listing.channel_id_from_entries(entries)
        if reported and reported != ref.platform_id:
            msg = (
                f"{ref.platform_id} 的枚举结果自报频道是 {reported} —— 身份不一致，"
                "不把别人的名字登成这位博主（多半是链接被重定向或 ID 抄错）"
            )
            raise PlatformError(PLATFORM, "profile", msg)
        named = next((video.channel for video in videos if video.channel), "")
        if not named and not result.ok:
            msg = f"取 {ref.platform_id} 的频道资料失败：{media.ytdlp_failure_reason(result)}"
            raise PlatformError(PLATFORM, "profile", msg)
        return CreatorProfile(
            ref=ref,
            name=named or ref.platform_id,
            avatar_url=None,
            follower_count=None,
            bio=None,
            extra={
                "source": "ytdlp_flat_playlist",
                "entries_probed": len(videos),
                "channel_id_reported": reported,
                # 说清"为什么这两格是空的"，否则看板上像数据没同步（V1 §1.3）
                "note": "flat-playlist 不含头像与订阅数，留 None 不猜",
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
        """流式产出作品元数据，**时间窗按 V1 的语义**。

        `since` 在 YouTube 这一族是**实过滤**（flat 条目常带 `timestamp`，
        与抖音"问了也问不出来"的弱过滤相反）：窗外直接不进结果。
        但**日期缺失的条目照样收**并计入 `undated`（V1 那句"不编造时间，按原顺序取"），
        理由与代价写在 `listing.within_window` 的 docstring 里。

        开了时间窗时向 yt-dlp 多要几倍候选（`listing.scan_budget`，V1 同一个乘数）：
        窗口边界的私密视频/社区帖会把名额挤掉。
        """
        if limit <= 0:
            return
        videos, notes = await self._enumerate(ref, limit=limit, filtered=since is not None)
        outcome = listing.within_window(videos, since=since, limit=limit)
        if outcome.undated:
            self._log.warning(
                "youtube.list.undated_entries",
                channel_id=ref.platform_id,
                count=outcome.undated,
                note=f"{outcome.undated} 条没有发布时间，按列表顺序取用（未编造时间）",
            )
        emitted = 0
        for video in outcome.selected:
            yield listing.video_meta_for(video, ref=ref)
            emitted += 1
        if emitted == 0 and notes:
            msg = f"频道枚举没抽出一条作品（channel_id={ref.platform_id}）：{'；'.join(notes[:5])}"
            raise ListError(PLATFORM, "list", msg)

    async def _enumerate(
        self, ref: CreatorRef, *, limit: int, filtered: bool
    ) -> tuple[list[listing.FlatVideo], list[str]]:
        target = enumeration_url(ref.platform_id, homepage_url=str(ref.profile_url))
        needed = listing.scan_budget(limit, filtered=filtered)
        result = await self._flat_playlist(target, playlist_items=needed, stage="list")
        entries = listing.parse_dump_json_lines(result.stdout)
        videos, notes = listing.to_videos(entries)
        if not videos:
            detail = "；".join(notes[:5]) or media.ytdlp_failure_reason(result)
            msg = (
                f"频道枚举一条作品都没有（channel_id={ref.platform_id}，"
                f"枚举了 {needed} 条候选）：{detail}"
            )
            raise ListError(PLATFORM, "list", msg)
        return videos, notes

    async def _flat_playlist(
        self, url: str, *, playlist_items: int | None, stage: str
    ) -> YtDlpResult:
        """枚举那一趟。`LookupError`（没装 yt-dlp）按 `stage` 翻成对应的错型。

        为什么不原样往上抛：`ListError` 会被 `tasks/collect.py` 记成"这一位博主的
        列表失败"，而 `list_creator_videos` 之外的两处（身份反查、博主资料）
        要的是 `PlatformError` + 各自的 stage，清单里才指得清是哪一步。
        """
        try:
            return await self._ytdlp_runner().flat_playlist(
                url, variants=self._ladder(), playlist_items=playlist_items
            )
        except LookupError as exc:
            message = (
                f"未安装 yt-dlp，YouTube 的{'列表枚举' if stage == 'list' else '身份反查'}"
                f"没有第二条路：{exc}"
            )
            if stage == "list":
                raise ListError(PLATFORM, "list", message) from exc
            raise PlatformError(PLATFORM, stage, message) from exc
        except MediaDownloadError as exc:
            # 阶梯走完还是失败：换成调用方那一层的错型，原文一个字不改
            raise ListError(PLATFORM, stage, str(exc)) from exc

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
        """yt-dlp 单路下载，产物只会是**一个合并好的文件**。

        `supports_dash_split=False` 的兑现方式：`classify_artifacts` 判出 `pair`
        时**整趟判失败**并点名 ffmpeg，而不是挑一条"看起来像视频"的交出去 ——
        后者正是 V1 的 `glob("*.mp4")` 在分片情形下做的事（§7.21 的原始形状）。
        """
        await self._pace()
        rung = self._rung_label()
        try:
            result = await self._ytdlp_runner().download(
                str(video.webpage_url or canonical_video_url(video.platform_video_id)),
                dest,
                variants=self._ladder(),
                file_template=media.MEDIA_FILE_TEMPLATE,
                on_line=_line_reporter(on_progress),
                timeout=DOWNLOAD_BUDGET_SECONDS,
            )
        except LookupError as exc:
            msg = (
                f"未安装 yt-dlp，YouTube 的媒体下载没有第二条路（{video.platform_video_id}）：{exc}"
            )
            raise MediaDownloadError(PLATFORM, "media", msg) from exc

        if not result.ok:
            msg = (
                f"下载失败（{video.platform_video_id}）：{media.ytdlp_failure_reason(result)}"
                f"｜档位：{rung}"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)

        parts = classify_artifacts(result.artifacts)
        if parts.kind == "empty":
            msg = (
                f"yt-dlp 退出码 0 但没有产出可读的文件"
                f"（{video.platform_video_id}）：报出的路径 {[str(p) for p in result.artifacts]}"
                f"｜档位：{rung}"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)
        if parts.kind == "pair":
            video_track, audio_track = parts.video, parts.audio
            pair = f"{video_track.name if video_track else '?'} + "
            pair += audio_track.name if audio_track else "（缺音频轨）"
            msg = (
                f"yt-dlp 没能把视频轨与音频轨合并（{video.platform_video_id}）：{pair}。"
                "本适配器声明 supports_dash_split=False，不交分片对 —— "
                "修复：装 ffmpeg 并进 PATH（V1 §7.19），或降低 format_preference 到"
                "有渐进式（progressive）整片的档"
            )
            raise MediaDownloadError(PLATFORM, "media", msg)
        main = parts.main
        if main is None:  # pragma: no cover - classify 保证 single ⇒ main 有值
            msg = f"产物分类自相矛盾（{video.platform_video_id}）：{parts.description}"
            raise MediaDownloadError(PLATFORM, "media", msg)
        if parts.extras:
            self._log.warning(
                "youtube.extra_artifacts_ignored",
                platform_video_id=video.platform_video_id,
                chosen=str(main),
                others=[str(p) for p in parts.extras],
            )
        return SingleFileArtifact(
            path=main,
            size_bytes=_size(main),
            media_source="yt_dlp",
            yt_dlp_error=None,
            cookie_rung=rung,
            has_audio=await has_audio_stream(main),
            duration_seconds=video.duration_seconds,
        )

    # ------------------------------------------------------------------ #
    # 字幕（`supports_subtitles=True` 的兑现）
    # ------------------------------------------------------------------ #

    async def fetch_subtitles(self, video: VideoMeta) -> Transcript | None:
        """让 yt-dlp 只取字幕，回来解析成 `Transcript`。

        返回 None 的**唯一**条件是"这一条作品确实没有我们要的那种字幕轨"
        （目录里没有 `.vtt`，或有一轨但一条 cue 都没解析出来 —— 空轨与没轨
        在下游是同一件事：去跑 ASR）。跑失败**抛**，不静默成 None：
        一次网络故障被吞成"没字幕"的代价是一整轮白烧 CPU，而日志干净
        （B站 那边同一条判断，见 `bilibili/subtitles.py` 的表）。
        """
        url = str(video.webpage_url or canonical_video_url(video.platform_video_id))
        scratch = (
            self.subtitle_scratch_root / f"{_SUBTITLE_SCRATCH_PREFIX}{video.platform_video_id}"
        )
        # 专用目录：建在这一格而不是 `download_media` 的产物目录里，
        # 因为 `find_subtitle_files` 的"扫目录"只有在**目录里只有字幕**时才成立。
        await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)
        try:
            await self._pace()
            try:
                result = await self._subtitle_runner().download(
                    url,
                    scratch,
                    variants=self._ladder(),
                    file_template=media.SUBTITLE_FILE_TEMPLATE,
                    timeout=SUBTITLE_BUDGET_SECONDS,
                )
            except LookupError as exc:
                msg = f"未安装 yt-dlp，取不到 {video.platform_video_id} 的字幕轨：{exc}"
                raise PlatformError(PLATFORM, "subtitle", msg) from exc
            if not result.ok:
                msg = (
                    f"取字幕失败（{video.platform_video_id}）：{media.ytdlp_failure_reason(result)}"
                )
                raise PlatformError(PLATFORM, "subtitle", msg)
            found = media.find_subtitle_files(scratch)
            if not found:
                self._log.info("youtube.subtitle_absent", platform_video_id=video.platform_video_id)
                return None
            chosen = media.choose_subtitle_file(found, self._config.advanced.subtitle_languages)
            if chosen is None:  # pragma: no cover - find 非空时不会挑不出
                return None
            segments = media.parse_vtt(chosen.read_text(encoding="utf-8", errors="replace"))
            if not segments:
                self._log.info(
                    "youtube.subtitle_empty_track",
                    platform_video_id=video.platform_video_id,
                    track=chosen.name,
                )
                return None
            return media.build_transcript(segments, language=media.subtitle_language(chosen))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _ladder(self) -> tuple[YtDlpCookieVariant, ...]:
        """一档：不带任何 cookie。**顺序不在这里**，真源是 `capabilities.cookie_variants`。"""
        return tuple(plan_cookie_variants(self.capabilities.cookie_variants))

    def _rung_label(self) -> str:
        """清单与产物上写的档位名。

        `plan_cookie_variants` 给 `none` 档的标签是"匿名（登录档画质不可用）"——
        那句是给 B站 那种"有登录档但没带上"的场景写的。YouTube 压根没有登录档，
        照抄会让预检页读起来像"这里降级了，去查为什么"。
        """
        return "none（本平台不需要登录态）"

    def _ytdlp_runner(self) -> YtDlpRunner:
        """测试替换点（同抖音/B站：`AdapterDeps` 里没有 runner 字段）。"""
        return YtDlpRunner(
            default_variants=self._ladder(),
            timeout_seconds=LIST_BUDGET_SECONDS,
            extra_args=media.ytdlp_extra_args(self._config),
        )

    def _subtitle_runner(self) -> YtDlpRunner:
        """字幕那一趟的 argv 与媒体那一趟**不同**（`--skip-download` + 只写字幕）。"""
        return YtDlpRunner(
            default_variants=self._ladder(),
            timeout_seconds=SUBTITLE_BUDGET_SECONDS,
            extra_args=media.subtitle_extra_args(self._config),
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


def _line_reporter(on_progress: ProgressCallback | None) -> Callable[[str], None] | None:
    if on_progress is None:
        return None

    def on_line(line: str) -> None:
        fraction = progress_from_ytdlp_line(line)
        if fraction is not None:
            on_progress(min(1.0, max(0.0, fraction)))

    return on_line
