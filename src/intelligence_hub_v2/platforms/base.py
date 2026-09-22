"""平台适配器的公共契约。

本模块是**契约**而不是实现：`PlatformAdapter` Protocol、`Capabilities`、`AdapterDeps`、
`PlatformConfig` 基类，以及从 `models/` 转出的数据类型 re-export。
V3 重写时这一份就是规范 —— 换语言、换框架，只要还满足这个 Protocol 就能接上调度器。

状态：Locked。改动需走 ADR（docs/specs/platform-adapter.md）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.media import (
    MediaArtifact,
    MediaSource,
    SingleFileArtifact,
    VideoAudioPairArtifact,
    audio_path_of,
    total_size_bytes,
)
from intelligence_hub_v2.models.task import ProgressCallback
from intelligence_hub_v2.models.transcript import Transcript
from intelligence_hub_v2.models.video import VideoMeta

if TYPE_CHECKING:
    # 依赖方向图上 infra 在 platforms 下面，本该可以运行期往下引 —— 但 Task 5/6 之后
    # 这一头**也必须保持 TYPE_CHECKING**，因为 `platforms/__init__.py` 末尾要在导入期
    # 把适配器装上（注册表要"实现了哪些平台"在导入时就固定）。于是链条变成：
    #   platforms/__init__ → douyin.adapter → infra.cookies → infra/__init__
    #     → infra.ytdlp → platforms.base（本模块，正在执行中）→ 循环
    # Task 6 的实际解法是把 `infra/ytdlp.py` 那边对 `CookieVariant` 的运行期导入
    # 也挪进 TYPE_CHECKING（它只出现在注解里），这一侧才不必破例。
    # 两边各自的理由都写在那两个文件的 TYPE_CHECKING 块里，别只删一处。
    from intelligence_hub_v2.core.event_bus import EventBus
    from intelligence_hub_v2.infra.cdp_bridge import BridgeClient
    from intelligence_hub_v2.infra.cookies import CookieManager
    from intelligence_hub_v2.storage.db import SqliteStorage

__all__ = [
    "AdapterDeps",
    "Capabilities",
    "CreatorProfile",
    "CreatorRef",
    "HealthReport",
    "ListStrategy",
    "MediaArtifact",
    "MediaSource",
    "PlatformAdapter",
    "PlatformConfig",
    "RateLimitConfig",
    "SingleFileArtifact",
    "Transcript",
    "VideoAudioPairArtifact",
    "VideoMeta",
    "audio_path_of",
    "total_size_bytes",
]

ListStrategy = Literal["api", "browser_scroll", "yt_dlp_flat", "external_manifest"]
"""列表枚举策略。

- `api`: 公开 web-interface（B站）
- `browser_scroll`: 桥内页面 JS 滚动加载（抖音/小红书）
- `yt_dlp_flat`: `yt-dlp --flat-playlist`（YouTube）
- `external_manifest`: 吃外部浏览器清单（V1 `bilibili-download` 技能那条路）
"""

MediaStrategy = Literal["yt_dlp", "page_play_url", "yt_dlp_with_fallback"]
"""媒体下载策略。

- `yt_dlp`: 纯 yt-dlp
- `page_play_url`: 纯页面播放直链（抖音兜底）
- `yt_dlp_with_fallback`: yt-dlp 失败 → 页面播放直链。**抖音的常态**，
  不是故障：V1 §7.2 实测抖音对非浏览器客户端做风控，yt-dlp 从来没产出一片媒体。
"""

CookieVariant = Literal["exported_file", "browser", "anonymous", "none"]
"""cookie 档位。V1 §7.3 / §7.15 那条阶梯的契约化。"""


@dataclass(frozen=True, slots=True)
class Capabilities:
    """平台能力声明。调度器据此决定前置条件。

    frozen + slots：能力是**声明式**的，跑起来之后不许被谁改掉。
    """

    needs_browser: bool
    """True → 调度器必须先确认 CDP 桥活（`/health` 通）。抖音/小红书 True，B站/YouTube False。

    注意 V1 §7.20 的教训：桥的 503 语义是"桥在跑、浏览器没了"，
    **不是**"桥没起"。自愈只发生在第一条真请求上，所以前置检查必须把
    `HTTPError` 当成"桥可用"，否则会被自己的健康检查堵死。
    """

    needs_cookies: bool
    """True → 必须先有 cookies 文件。"""

    cookie_variants: tuple[CookieVariant, ...]
    """cookie 阶梯顺序，如 `('exported_file', 'browser', 'anonymous')`。

    V1 §7.15 那条「B站 cookie 三档」的契约化。每个 variant 必须对应
    `download_media` 里一条真实实现路径 —— 声明了但没实现等于撒谎。
    档位差别往往不是"能不能下"，是**画质**（登录档 1772p vs 匿名 886p）。
    """

    supports_subtitles: bool
    """B站/YouTube True，抖音/小红书 False。False 时 `fetch_subtitles` 直接返回 None。"""

    supports_dash_split: bool
    """True → `download_media` 可能返回未合并的 DASH 分片。

    B站 True。V1 §7.21：转写必须认音频轨，只 `rglob('*.mp4')` 会把纯视频流
    喂给 ffmpeg，报出来的 `Output file does not contain any stream`
    **长得和"ffmpeg 没装"一模一样**，但方向完全不同。
    """

    list_strategy: ListStrategy
    """列表枚举策略。"""

    media_strategy: MediaStrategy
    """媒体下载策略。"""


class RateLimitConfig(BaseModel):
    """限速配置。平台侧风控是第一约束，默认值取的是 V1 实测能跑通的档。"""

    model_config = ConfigDict(extra="forbid")

    per_minute: int = Field(default=30, ge=1)
    """每分钟最多多少个请求。"""

    per_creator_seconds: float = Field(default=1.0, ge=0.0)
    """两个博主之间的间隔秒数。"""


class PlatformConfig(BaseModel):
    """所有平台配置的基类。每个平台继承它，加平台特有字段。

    这是 spec §3 锁定的契约：`/api/platforms/{name}/schema` 直接吐
    `config_schema().model_json_schema()`，前端据此自动渲染表单。
    **字段名就是 API 契约**，改名等于破坏前端。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    """前端配置面板的开关。关掉 = 该平台所有任务自动从 `/api/tasks` 消失。"""

    display_name: str
    """人类可读名称，如 '抖音' / 'B站'。前端展示用，必填。"""

    cookies_file: Path | None = None
    """Netscape 格式 cookie 文件路径。`capabilities.needs_cookies=True` 时必填。

    V1 §7.3：Windows 上 yt-dlp 读不了 Chrome 的 cookie 库
    （Chrome 开着 → `Could not copy Chrome cookie database`；
    关着 → `Failed to decrypt with DPAPI`）。唯一稳定路径是 `--cookies <文件>`。
    """

    use_cdp_bridge: bool = False
    """是否走 CDP 桥。`capabilities.needs_browser=True` 时必须为 True。"""

    media_strategy: MediaStrategy
    """媒体下载策略。子类通常收窄成单个 Literal 并给默认值。"""

    list_strategy: ListStrategy
    """列表枚举策略。子类通常收窄成单个 Literal 并给默认值。"""

    videos_per_creator: int = Field(default=30, ge=1, le=200)
    """每个博主最多收多少条。"""

    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    advanced: Any = Field(default_factory=dict)
    """平台特有字段，前端默认折叠（`ui:advanced`）。

    基类故意声明成 `Any` 而不是 spec 里写的 `dict[str, Any]`：
    子类要把它覆盖成自己的强类型模型（`DouyinAdvanced` 等），
    而 `DouyinAdvanced` 不是 `dict[str, Any]` 的子类型，
    那样写 mypy 会按 Liskov 判覆盖不兼容。见 docs/lessons.md 实施期教训。
    """


ComponentStatus = Literal["ok", "degraded", "unreachable"]
"""子系统的红绿灯。**没有 `"unknown"`**：组件级要么探到了要么没探到，
"没探到"就不该出现在 `components` 这个字典里（而不是填一个 unknown 值）。
平台整体状态才有 `unknown`（探了但判不出来，见 V1 §7.20）。"""


class HealthReport(BaseModel):
    """`healthcheck()` 的输出。替代 V1 launcher 的 `health_checks()`。

    **`status` 与 `detail` 是一对**：红了却没 `detail` 就等于把 V1 §7.20 那句
    "红了但不知道为什么红"重演一遍。所以这里不强制（合法场景是 `ok` 无 detail），
    但适配器实现里 `unreachable` / `degraded` 必须带原文 —— 看护在契约测试基类（Task 14）。
    """

    platform: str
    status: Literal["ok", "degraded", "unreachable", "unknown"]
    detail: str | None = None
    """失败/降级的**原文**。桥的报错、yt-dlp 的 stderr、cookie 缺失的具体路径都算。"""

    checked_at: datetime
    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    """子组件状态，如 `{"cookies": "ok", "bridge": "unreachable", "network": "ok"}`。

    为什么平台整体还是一位、又要单独列子组件：Settings 页的预检子页要能指出
    "B站 红了是因为 cookie 文件不在，不是网络不通" —— 只有一位状态就得靠人猜。
    """

    @property
    def is_healthy(self) -> bool:
        """只有显式 `ok` 才算健康。与 `models.platform.PlatformRecord.is_healthy` 同一口径。

        `unknown`（探了但判不出来）不是绿灯 —— V1 §7.20 那次就是把"测不到"
        显示成"正常"，于是三连"未登录"其实是桥的浏览器早死了。
        """
        return self.status == "ok"


@dataclass
class AdapterDeps:
    """注入给适配器的依赖袋。测试时整袋换 mock，不碰真网络与真浏览器。

    为什么是 dataclass 而不是 Protocol：**它是纯被动的容器**，没有任何行为可契约化；
    而构造点（Task 8 的运行器）需要一眼看清"给了哪六样、哪样可空"。

    `bridge` 与 `cookies` 可空的理由不同，别混：
    - `bridge=None`：这个平台不走 CDP 桥（B站/YouTube）。
      声明了 `capabilities.needs_browser=True` 却拿到 `None` 是**装配错误**，
      适配器应在 `healthcheck()` 里报 `unreachable`，而不是在第一个请求上 `AttributeError`。
    - `cookies` 永远有对象（可能指向一个不存在的文件）：
      "没有 cookie 文件"是常态（第一次跑、刚过期），需要一个能问的路径而不是 None。
    """

    config: PlatformConfig
    """已校验的平台配置。与 `capabilities` 双份存在是有意的：
    适配器既要知道"能做什么"（类级、静态）也要知道"用户配了什么"（实例级、可热重载）。
    """

    storage: SqliteStorage
    events: EventBus
    http: httpx.AsyncClient
    logger: structlog.BoundLogger
    cookies: CookieManager
    bridge: BridgeClient | None = None


@runtime_checkable
class PlatformAdapter(Protocol):
    """所有平台采集器必须实现的接口。V3 重写时，这就是规范本身。

    **`runtime_checkable` 的 isinstance 只查方法在不在**，签名与返回类型靠 mypy
    （`tests/unit/platforms/test_base.py` 里有几条把实现类赋给这个 Protocol 的静态断言）。
    两层都要：前者防"忘了实现某个方法"，后者防"实现了但形状不对"。

    类级成员写成 `ClassVar` 是必须的 —— 注册表在**不实例化**的情况下就要能读到
    `name` / `capabilities` / `config_schema()`（Settings 页渲染表单、调度器判断前置条件
    都发生在"还没有任何任务在跑"的时候）。
    """

    name: ClassVar[str]
    """平台标识，全小写下划线。必须同时是 `PLATFORMS`、`PLATFORM_CONFIG_SCHEMAS`
    与 `config/platforms.yaml` 的 key —— 三处不一致的排查成本极高，注册表会当场拒绝。"""

    display_name: ClassVar[str]
    capabilities: ClassVar[Capabilities]

    @classmethod
    def config_schema(cls) -> type[PlatformConfig]:
        """返回 Pydantic 模型类。`/api/platforms/{name}/schema` 直接吐它的
        `model_json_schema()`，前端据此自动渲染表单。"""
        ...

    def __init__(self, config: PlatformConfig, deps: AdapterDeps) -> None: ...

    # ---- 健康检查 ----

    async def healthcheck(self) -> HealthReport:
        """探测可达性、cookie 有效性、桥连通性。

        **必须如实报告，不许臆造成功**（V1 §1.3）：探测手段自己挂了（桥没起、
        ffprobe 不在 PATH）要报 `unreachable` / `degraded` 并带上原文，
        而不是"没发现问题"所以返回 `ok`。
        """
        ...

    # ---- 博主 ----

    async def parse_creator_url(self, url: str) -> CreatorRef:
        """把用户粘贴的 URL 规范化成 `CreatorRef`。

        V1 §7.1 那条陷阱统一进这里：抖音的 `v.douyin.com/<code>/` 短链**不含身份**，
        必须跟一次 302 才拿到 `sec_uid`。拿短链当 `platform_id` 入库会造成
        "回写命中 1 条但其实只刷了 updated_at" —— 博主资料永远落不上去。
        失败抛 `PlatformError(stage="parse_url")`，带原文。
        """
        ...

    async def fetch_creator_profile(self, ref: CreatorRef) -> CreatorProfile:
        """拉昵称/头像/粉丝数。V1 `download_creator_profiles.py` 的对应物。"""
        ...

    # ---- 视频列表 ----

    def list_creator_videos(
        self,
        ref: CreatorRef,
        *,
        since: datetime | None = None,
        limit: int = 30,
    ) -> AsyncIterator[VideoMeta]:
        """**流式**产出视频元数据。

        为什么是 `AsyncIterator` 而不是 `list[VideoMeta]`：
        ① 小红书/B站 滚动加载几百条时不该一次全留在内存里；
        ② 调度器可以中途取消 —— 用户点停止时正在滚第 30 屏，
           用 `aclose()` 关迭代器就够，不需要"跑完再丢弃"。
        `limit` 是上限不是保证（平台侧只给了这么多，或 `since` 提前截断）。
        失败抛 `ListError(stage="list")`。
        """
        ...

    # ---- 媒体下载 ----

    async def download_media(
        self,
        video: VideoMeta,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> MediaArtifact:
        """下载媒体到 `dest` 目录，返回带来源与失败原文的产物。

        两个字段不许省：
        - `media_source`：这条片实际走了哪条路（V1 §7.2 的 `yt_dlp` vs `page_play_url`）。
        - `yt_dlp_error`：兜底成功时 yt-dlp 的**失败原文**。
          丢了它就判断不出该修什么 —— 而"看起来在跑"就是这个仓库的历史问题。

        实现**必须遵守 `capabilities.cookie_variants` 的阶梯顺序**（V1 §7.15）。
        档位差别往往不是"能不能下"而是**画质**（B站 登录档 1772p vs 匿名 886p），
        所以阶梯走完才允许报 `MediaDownloadError`。
        """
        ...

    # ---- 字幕（可选能力） ----

    async def fetch_subtitles(self, video: VideoMeta) -> Transcript | None:
        """有字幕优先字幕（B站/YouTube），拿不到返回 `None` 让调度器走本地 ASR。

        `capabilities.supports_subtitles=False` 的平台**直接返回 None**，
        不要抛 —— 抖音压根没有公开字幕轨，抛异常会让"每条作品都先失败一次"变成常态。
        """
        ...
