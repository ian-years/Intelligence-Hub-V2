"""小红书平台配置。

契约来源：docs/specs/config-schema.md §3.3。V1 实测经验都写在字段 description 里 ——
那些是踩出来的，不是设计出来的。

**这一节里的每一个键都必须有人读**（ADR-0012，看护
`tests/unit/platforms/test_config_fields_have_readers.py`）。`config/platforms.yaml`
里那段注释掉的示例本来写着 `media_strategy: "yt_dlp"`，那是设计期的猜测：
V1 实测里 yt-dlp 对小红书视频笔记**经常**产不出片（同抖音那一族的签名/风控），
真正走得通的是页面 `__INITIAL_STATE__` 里的 `video.media.stream` masterUrl，
所以 `capabilities.media_strategy` 是 `yt_dlp_with_fallback`，yaml 也跟着改成它。
留一个"能配但适配器不按它分支"的取值就是在给前端撒谎。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from intelligence_hub_v2.platforms.base import PlatformConfig

SortBy = Literal["page_order", "likes"]
"""列表枚举的产出顺序。

- `page_order`: 页面给什么顺序就交什么顺序（日更采集要的：新在最前）
- `likes`: 按点赞数从大到小（V1 `--sort-by likes` 的爆款回溯那一族）

V1 还有 `collect`（收藏数）与 `fan`（博主维度，按名搜主页挑人用的）两档：
`fan` 在"一位博主的作品列表"里没有对应对象（V1 自己的注释同一说法），
`collect` 要的是卡片上的收藏数，而**主页网格里的那张卡片不显示收藏数**
（DOM 上只有 `.like-wrapper .count`），所以两档都不做 ——
做一个恒等于"全 0 排序"的取值比不做更坏。
"""


class XiaohongshuAdvanced(BaseModel):
    """小红书高级参数。前端默认折叠（`ui:advanced`）。

    `include_image_notes` / `sort_notes_by` / `max_images_per_note` 三个**有真实读取路径**
    （分别在 `adapter.py` 的分流判据、`listing.py` 的爆款序、`media.py` 的原图上限）。
    另外三个（`retry_max` / `retry_backoff_seconds` / `request_timeout_seconds`）
    今天没人读，逐个标了 `ui:hidden`（判据见 ADR-0012）—— 与抖音那三个同一批欠账。
    """

    model_config = ConfigDict(extra="forbid")

    include_image_notes: bool = Field(
        default=True,
        description="是否收图文笔记的原图。False 时**在枚举这一步就不交出**页面明确标为"
        '图文的卡片（`note_type == "normal"`），视频与类型认不出的卡片照旧放行 —— '
        "「判得了才跳过，判不了就放行」与抖音 `since` 那条同一口径，"
        "拿「猜不出类型」当「不是图文」会整批丢作品。",
    )

    sort_notes_by: SortBy = Field(
        default="page_order",
        description="作品列表的顺序。`likes` = 按点赞数从大到小（V1 `--sort-by likes` "
        "的爆款回溯）。**`likes` 会多滚几屏**：要在「这一位博主的历史里挑最火的」，"
        "只拿首屏十来个样本挑出来的不是爆款，是「最新发布里的相对热门」。"
        "样本量见 `adapter.LIKES_LOOKAHEAD_CARDS`。",
    )

    max_images_per_note: int = Field(
        default=18,
        ge=1,
        le=18,
        description="一条图文笔记最多落几张原图。上限 18 是 V1 实测的现网值"
        "（小红书一条笔记最多 18 张），超出部分按页面顺序保留、尾部丢弃并记一条 warning。",
    )

    retry_max: int = Field(
        default=3,
        ge=0,
        description="单条笔记的重试次数。**V2.1 未实现**：媒体下载失败即按原样失败，"
        "重试预算由适配器写死（与抖音、B站 同一批欠账，见 ADR-0012）。",
        json_schema_extra={"ui:hidden": True},
    )

    retry_backoff_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="重试退避基数。**V2.1 未实现**，与 retry_max 同一批。",
        json_schema_extra={"ui:hidden": True},
    )

    request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="单次请求超时。**小红书侧未实现**：实际预算是适配器里的 "
        "`MEDIA_BUDGET_SECONDS`(600) / `DIRECT_BUDGET_SECONDS`(300)，"
        "那是整段下载的预算不是单次请求超时，把 30 接上去会把下载掐死，所以不接。",
        json_schema_extra={"ui:hidden": True},
    )


class XiaohongshuConfig(PlatformConfig):
    """小红书配置。

    三条与 V1 实测强绑定的默认值，别"顺手优化"：

    1. `media_strategy = 'yt_dlp_with_fallback'` —— 视频笔记先试 yt-dlp，
       失败退到页面 `video.media.stream.h264[].masterUrl`。V1 同一顺序，
       而且**兜底成功时它会把 yt-dlp 那轮的错清零**（`media["errors"] = []`）；
       V2 反过来：`yt_dlp_error` 必须留着原文，否则后来人判断不出该修什么（V1 §7.2 同一条）。
    2. `list_strategy = 'browser_scroll'` + `use_cdp_bridge = True` ——
       列表枚举与详情都只能在已登录浏览器的页面上下文里做。小红书对无登录请求
       直接拦（V1 `bridge_failure_message` 原话），Python 侧连页面都拿不到。
    3. `ytdlp_cookies_from_browser = None`（B站 那边是 `"chrome"`）——
       V1 §7.3：Windows 上这一档对谁都不友好，而小红书这条路上 yt-dlp 本来就是兜底位，
       不值得为它多扔一个子进程。

    **这里没有任何字段决定 cookie 阶梯的顺序**：顺序是
    `XiaohongshuAdapter.capabilities.cookie_variants` 的声明（ADR-0011），
    本模型只回答"这一档用哪个文件、哪个浏览器"。
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui:order": ["enabled"]})

    display_name: str = "小红书"

    media_strategy: Literal["yt_dlp_with_fallback"] = Field(
        default="yt_dlp_with_fallback",
        description="媒体下载策略。只有一个合法取值，因此它是文档不是配置：真源是 "
        "`XiaohongshuAdapter.capabilities.media_strategy`（ADR-0011）。改它不产生任何效果。",
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
        "（装配点 `core/task_registry.py`）。小红书恒为 True —— 关掉不会让桥不被使用，"
        "只会让每一条笔记都采不到。",
        json_schema_extra={"ui:hidden": True},
    )

    ytdlp_cookies_from_browser: str | None = None
    """`--cookies-from-browser` 的目标浏览器。**默认 None = 没有浏览器档**。

    需要的人显式填 `"chrome"`，或者设 `XHS_YTDLP_COOKIES_FROM_BROWSER`（兼容 V1 的
    同名 env，env 优先于本字段）。**它只决定"浏览器档用哪个浏览器"，不决定阶梯顺序** ——
    顺序的唯一真源是 `capabilities.cookie_variants`（ADR-0011）。
    """

    fallback_to_page_play_url: bool = True
    """yt-dlp 失败时是否兜底到页面视频直链（`masterUrl`）。

    关掉它 = 只信 yt-dlp，而小红书这一侧 yt-dlp 失败是常态 ——
    所以关掉之后大部分视频笔记会**如实失败**，这是明确的选择而不是 bug。
    兜底时必须保留 yt-dlp 的失败原文（`MediaArtifact.yt_dlp_error`，V1 §7.2）。
    """

    advanced: XiaohongshuAdvanced = Field(
        default_factory=XiaohongshuAdvanced,
        description="高级参数，前端折叠渲染（`ui:advanced`）。",
        json_schema_extra={"ui:advanced": True},
    )
