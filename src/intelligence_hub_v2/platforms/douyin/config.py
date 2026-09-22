"""抖音平台配置。

契约来源：docs/specs/config-schema.md §3.1。
V1 实测经验全部写在字段 docstring 里 —— 那些是踩出来的，不是设计出来的。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from intelligence_hub_v2.platforms.base import PlatformConfig


class DouyinAdvanced(BaseModel):
    """抖音高级参数。前端默认折叠（`ui:advanced`）。"""

    model_config = ConfigDict(extra="forbid")

    retry_max: int = Field(default=3, ge=0)
    """单条视频的重试次数。"""

    retry_backoff_seconds: float = Field(default=2.0, ge=0.0)
    """重试退避基数（指数退避）。"""

    request_timeout_seconds: int = Field(default=30, ge=1)

    max_video_duration_seconds: int | None = Field(default=None, ge=1)
    """超过这个时长的视频跳过（省 ASR 时间）。None = 不限。"""


class DouyinConfig(PlatformConfig):
    """抖音配置。

    两条与 V1 实测强绑定的默认值，别"顺手优化"：

    1. `media_strategy = 'yt_dlp_with_fallback'` —— V1 §7.2 实测抖音对非浏览器
       客户端做风控，直连 `/aweme/v1/web/aweme/detail/` 恒 403
       `Fresh cookies (not necessarily logged in) are needed`，
       **与 cookie 新不新鲜无关**（带整套导出 cookie 和不带都是 403）。
       缺的是页面里那层 `a_bogus` 签名，yt-dlp 造不出来。
       所以"yt-dlp 未拿到媒体，已改用页面播放直链"是**常态而不是故障**。
    2. `list_strategy = 'browser_scroll'` + `use_cdp_bridge = True` ——
       列表枚举只能在已登录浏览器的页面上下文里做。
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui:order": ["enabled"]})

    display_name: str = "抖音"

    media_strategy: Literal["yt_dlp_with_fallback"] = "yt_dlp_with_fallback"
    list_strategy: Literal["browser_scroll"] = "browser_scroll"
    use_cdp_bridge: bool = True

    ytdlp_cookie_priority: tuple[str, ...] = (
        "env:DOUYIN_YTDLP_COOKIES_FROM_BROWSER",
        "env:DOUYIN_YTDLP_COOKIES_FILE",
        "file:cookies_file",
        "none",
    )
    """yt-dlp cookie 优先级阶梯（V1 §7.3）。

    每一项的语义：
    - `env:<NAME>`: 读环境变量 `<NAME>`，值直接当 yt-dlp 参数
      （`DOUYIN_YTDLP_COOKIES_FROM_BROWSER` → `--cookies-from-browser`，
      `DOUYIN_YTDLP_COOKIES_FILE` → `--cookies`）
    - `file:cookies_file`: 用本配置的 `cookies_file` 字段，文件存在才传
    - `none`: 什么都不传

    顺序即优先级，**从左到右第一个可用的胜出**。
    """

    fallback_to_page_play_url: bool = True
    """yt-dlp 失败时是否兜底到页面播放直链（V1 §7.2，常态）。

    兜底时必须保留 yt-dlp 的失败原文（`MediaArtifact.yt_dlp_error`）——
    V1 早期一兜底成功就把原文丢掉，日志只剩"未拿到媒体"，
    等于没法判断该修什么。
    """

    persist_play_url: bool = False
    """是否把播放直链写进库。**默认 False，且不该改成 True。**

    CDN 播放直链是签名的、不带 cookie，几小时后失效（V1 §7.2）。
    把 URL 当持久数据 = 库里躺一堆死链。
    """

    advanced: DouyinAdvanced = Field(default_factory=DouyinAdvanced)
