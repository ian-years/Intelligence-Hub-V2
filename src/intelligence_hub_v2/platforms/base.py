"""平台适配器的公共契约类型。

本模块只放**契约**：`PlatformConfig` 基类、`RateLimitConfig`、`Capabilities`。
`PlatformAdapter` Protocol 与 `AdapterDeps` 在 Task 5 补进来（同一个文件，spec §2）。

状态：Locked。改动需走 ADR（docs/specs/platform-adapter.md）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
