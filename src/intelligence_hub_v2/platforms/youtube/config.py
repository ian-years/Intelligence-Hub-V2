"""YouTube 平台配置。

契约来源：`docs/specs/config-schema.md §3.3`。

这个平台的两条特点决定了字段很少：

1. **没有任何登录态**（`capabilities.needs_cookies=False`、`cookie_variants=("none",)`）。
   B站 那三档阶梯在这里不存在，所以 `cookies_file` 被显式隐藏而不是"留着给人填"。
2. **没有第二条媒体路径**（`media_strategy="yt_dlp"`，同 B站，不像抖音）。
   yt-dlp 不在 PATH 就是真的采不了，`healthcheck()` 因此报 `unreachable` 而不是
   `degraded` —— 报 degraded 会让人以为"跟抖音一样能跑，只是画质差一点"。

V1 那一组 `YOUTUBE_LIST_TIMEOUT` / `YOUTUBE_INFO_TIMEOUT` / `YOUTUBE_DOWNLOAD_TIMEOUT`
环境变量**没有搬**：V2 的预算走 `app.yaml` 的任务超时 + 这里的
`advanced.request_timeout_seconds`，三个环境变量键只会是第四处真相。
V1 的 `SHOW_YOUTUBE` 开关也没搬 —— 平台开关（`enabled`）天然覆盖那一格。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from intelligence_hub_v2.platforms.base import PlatformConfig

DEFAULT_FORMAT_PREFERENCE = "bv*[height<=1080]+ba/b[height<=1080]"
"""V1 的 `FORMAT_1080P`，一字未改。

为什么默认就限到 1080p：YouTube 的 4K 轨是 AV1/VP9，一条十分钟视频的字节数是
1080p 的数倍，而 V1 实测那批对标频道的口播内容 1080p 完全够转写与看板用。
"""

DEFAULT_SUBTITLE_LANGUAGES: tuple[str, ...] = ("zh-Hans", "zh", "en")
"""字幕轨的挑选顺序（`--sub-langs` 与"多轨时挑哪一条"共用这一份）。

顺序而不是集合：一条视频常同时有 `en` 自动轨与若干人工翻译轨，
没有稳定顺序就会"同一批作品两遍跑出两种稿子"，那时字幕变更看不出来
（与 B站 `choose_track` 把 `index` 塞进排序键同一个理由）。
"""


class YouTubeAdvanced(BaseModel):
    """YouTube 高级参数，前端默认折叠（`ui:advanced`）。

    这一组里每一个字段都有读取路径（判据见 ADR-0012 与
    `tests/unit/platforms/test_config_fields_have_readers.py`）：
    `format_preference` / `node_as_js_runtime` 在 `media.py` 拼 argv，
    `require_node` 在 `adapter.healthcheck()`，
    `subtitle_languages` 在 `media.py` 的字幕轨那一步，
    `request_timeout_seconds` 在 `adapter.py` 的每一趟预算。
    """

    model_config = ConfigDict(extra="forbid")

    request_timeout_seconds: int = Field(default=60, ge=1)
    """单次 HTTP / 字幕枚举的预算。比 B站 那 30 秒宽：
    YouTube 的 TLS 握手在跨境链路上本来就慢，卡在半途报"不可达"更没用。"""

    format_preference: str = Field(
        default=DEFAULT_FORMAT_PREFERENCE,
        min_length=1,
        description="yt-dlp 的 `-f` 取值，原样交给它。默认与 V1 一致（1080p 以内合并 mp4）。"
        "真源是 yt-dlp 的 format selection 语法，V2 不解释也不校验它 —— "
        "写错时 yt-dlp 的报错原文会进清单，那才是能看懂的说法。",
    )

    require_node: bool = Field(
        default=True,
        description="把「PATH 里有 node」当作**健康**条件：没有就 `healthcheck()` 报 "
        "`degraded` 并给出安装动作。真源是 V1 §4.1 与 yt-dlp-ejs（新版 YouTube "
        "签名/挑战要用 JS 运行时，缺 node 时症状是'每个视频都报同一句看不懂的话'）。"
        "关掉它只是不再提醒，**不会**让没装 node 的机器突然能采 —— argv 里加不加 "
        "`--js-runtimes node` 由 `node_as_js_runtime` 决定，不看本字段。",
    )

    node_as_js_runtime: bool = Field(
        default=True,
        description="探到 node 就在 yt-dlp 的 argv 里加 `--js-runtimes node`。"
        "默认关行是**有意的**：V1 那边老版本 yt-dlp 不认这个 flag，"
        "所以它带一次'去掉重试'；V2 不做那次重试（阶梯在 `infra.ytdlp` 里，"
        "只按 cookie 退档），于是这条只能由配着关。"
        "真源：`media.ytdlp_extra_args()`。",
    )

    subtitle_languages: tuple[str, ...] = Field(
        default=DEFAULT_SUBTITLE_LANGUAGES,
        min_length=1,
        description="取字幕轨时按这个**顺序**要：既是 `--sub-langs` 的内容，"
        "也是多轨时挑哪一条的依据。为什么这件事值得配：对标频道常见中英双轨，"
        "挑错语言的稿子会让后面的抽取式摘要整批变成英文。",
    )


class YouTubeConfig(PlatformConfig):
    """YouTube 配置。见模块 docstring 那两条特点。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str = "YouTube"

    media_strategy: Literal["yt_dlp"] = Field(
        default="yt_dlp",
        description="媒体下载策略。只有一个合法取值，因此它是文档不是配置：真源是 "
        "`YouTubeAdapter.capabilities.media_strategy`（ADR-0011）。改这个值不产生任何效果。",
        json_schema_extra={"ui:hidden": True},
    )

    list_strategy: Literal["yt_dlp_flat"] = Field(
        default="yt_dlp_flat",
        description="列表枚举策略：`yt-dlp --flat-playlist -j`（V1 实测唯一跑通的那条）。"
        "本构建里没有第二种取值，因此**不受本字段控制**；真源见 "
        "`capabilities.list_strategy`（ADR-0011/0012）。",
        json_schema_extra={"ui:hidden": True},
    )

    use_cdp_bridge: bool = Field(
        default=False,
        description="是否走 CDP 桥。**由 capabilities.needs_browser 决定，不受本字段控制**"
        "（装配点 `core/task_registry.py`）。YouTube 不需要桥，勾上也不会去连。",
        json_schema_extra={"ui:hidden": True},
    )

    cookies_file: Path | None = Field(
        default=None,
        description="YouTube 的枚举、下载、字幕**三条路都不带 cookie**"
        '（`capabilities.cookie_variants=("none",)`），所以本字段没有效果、'
        "也不会被传给 yt-dlp。为什么不留着：一个前端渲染得出来、后端没有实现路径的"
        "配置取值就是在对用户撒谎（ADR-0011/0012）。要登录态内容（会员限定/年龄限定）"
        "请改用 B站 那族的导出档路径 —— YouTube 这一族 V2 未实现。",
        json_schema_extra={"ui:hidden": True},
    )

    proxy: str | None = Field(
        default=None,
        description="给 yt-dlp 的 `--proxy`（如 `http://127.0.0.1:7890`），"
        "同时让 `healthcheck()` 的网络探活也走它。为什么这件事值得配：本机到 "
        "YouTube 大概率直连不通，不通时 healthcheck 会红成 `unreachable` 并交出原文"
        "（AGENTS.md §1.3：不许把 failed 记成 0）；配了代理之后那一格才分得出"
        "'网络不通'与'需要代理'。**留空 = 直连**，不是'自动探测系统代理'。",
    )

    advanced: YouTubeAdvanced = Field(
        default_factory=YouTubeAdvanced,
        description="高级参数，前端折叠渲染（`ui:advanced`）。",
        json_schema_extra={"ui:advanced": True},
    )
