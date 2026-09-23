"""抖音平台配置。

契约来源：docs/specs/config-schema.md §3.1。
V1 实测经验全部写在字段 docstring 里 —— 那些是踩出来的，不是设计出来的。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from intelligence_hub_v2.platforms.base import PlatformConfig


class DouyinAdvanced(BaseModel):
    """抖音高级参数。前端默认折叠（`ui:advanced`）。

    **V2.0 这一整组都没有读取路径**，四个字段因此逐个标了 `ui:hidden`（ADR-0012）：
    重试走的是 `DouyinAdapter` 里写死的预算（`DIRECT_BUDGET_SECONDS` /
    `YTDLP_BUDGET_SECONDS`），时长上限压根没有过滤器。留着字段而不隐藏，就是在
    Settings 页上放四个能存、能回显、什么都不做的框。
    """

    model_config = ConfigDict(extra="forbid")

    retry_max: int = Field(
        default=3,
        ge=0,
        description="单条视频的重试次数。**V2.0 未实现**：媒体下载失败即按原样失败，"
        "重试预算由适配器写死，不受本字段控制。",
        json_schema_extra={"ui:hidden": True},
    )

    retry_backoff_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="重试退避基数（指数退避）。**V2.0 未实现**，与 retry_max 同一批。",
        json_schema_extra={"ui:hidden": True},
    )

    request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="单次请求超时。**抖音侧未实现**：实际预算是适配器里的 "
        "`DIRECT_BUDGET_SECONDS`(300) / `YTDLP_BUDGET_SECONDS`(600) —— "
        "那是整段下载的预算不是单次请求超时，把 30 接上去会把下载掐死，所以不接。",
        json_schema_extra={"ui:hidden": True},
    )

    max_video_duration_seconds: int | None = Field(
        default=None,
        ge=1,
        description="超过这个时长的视频跳过（省 ASR 时间）。None = 不限。"
        "**V2.0 未实现**：collect 没有时长过滤器，本字段当前不产生任何效果。",
        json_schema_extra={"ui:hidden": True},
    )


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
    3. `ytdlp_cookies_from_browser = None`（B站 那边是 `"chrome"`）—— 同一个 cookie 库
       在 Windows 上对抖音是"读不出来"，对 B站 是"读得出来但没必要"，两边默认值不同是有意的。

    **这里没有任何字段决定 cookie 阶梯的顺序**：顺序是 `DouyinAdapter.capabilities`
    的声明（`("exported_file", "browser", "none")`），本模型只回答"这一档用哪个文件、
    哪个浏览器"。理由与 V1 的 env 名怎么兼容见 `docs/adr/0011`。
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui:order": ["enabled"]})

    display_name: str = "抖音"

    media_strategy: Literal["yt_dlp_with_fallback"] = Field(
        default="yt_dlp_with_fallback",
        description="媒体下载策略。只有一个合法取值，因此它是文档不是配置：真源是 "
        "`DouyinAdapter.capabilities.media_strategy`（ADR-0011）。V2.1 之前"
        "改这个值不产生任何效果。",
        json_schema_extra={"ui:hidden": True},
    )
    list_strategy: Literal["browser_scroll"] = Field(
        default="browser_scroll",
        description="列表枚举策略。同上：真源是 `capabilities.list_strategy`，"
        "适配器不按本字段分支，改它没有效果。",
        json_schema_extra={"ui:hidden": True},
    )
    use_cdp_bridge: bool = Field(
        default=True,
        description="是否走 CDP 桥。**由 capabilities.needs_browser 决定，不受本字段控制**"
        "（装配点 `core/task_registry.py`）。抖音恒为 True —— 关掉不会让桥不被使用。",
        json_schema_extra={"ui:hidden": True},
    )

    ytdlp_cookies_from_browser: str | None = None
    """`--cookies-from-browser` 的目标浏览器。**默认 None = 没有浏览器档**。

    为什么默认关（V1 §7.3，`docs/adr/0011`）：Windows 上这一档**永远**读不出来 ——
    Chrome 开着回 `Could not copy Chrome cookie database`，关着回
    `Failed to decrypt with DPAPI`。而这两句都在"触发退档"的判据表里
    （`infra.ytdlp.looks_like_cookie_failure`），所以开着它不是"白试一档"，
    是"每条视频先白扔一个子进程，再把真原因混进 cookie 报错里"。
    需要的人显式填 `"chrome"`，或者设 `DOUYIN_YTDLP_COOKIES_FROM_BROWSER`（兼容 V1，
    env 优先于本字段，因为设 env 的人明确知道自己在做什么）。

    **它只决定"浏览器档用哪个浏览器"，不决定阶梯顺序** —— 顺序的唯一真源是
    `DouyinAdapter.capabilities.cookie_variants`（ADR-0011）。
    """

    fallback_to_page_play_url: bool = True
    """yt-dlp 失败时是否兜底到页面播放直链（V1 §7.2，常态）。

    兜底时必须保留 yt-dlp 的失败原文（`MediaArtifact.yt_dlp_error`）——
    V1 早期一兜底成功就把原文丢掉，日志只剩"未拿到媒体"，
    等于没法判断该修什么。
    """

    advanced: DouyinAdvanced = Field(
        default_factory=DouyinAdvanced,
        description="高级参数，前端折叠渲染（`ui:advanced`）。",
        json_schema_extra={"ui:advanced": True},
    )
