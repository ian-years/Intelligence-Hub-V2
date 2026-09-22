"""B站平台配置。

契约来源：docs/specs/config-schema.md §3.2。
cookie 三档阶梯是 V1 §7.15 血泪换来的，顺序就是契约。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from intelligence_hub_v2.platforms.base import PlatformConfig


class BilibiliAdvanced(BaseModel):
    """B站高级参数。前端默认折叠（`ui:advanced`）。"""

    model_config = ConfigDict(extra="forbid")

    require_login_for_high_quality: bool = True
    """是否要求登录档才收高画质。

    V1 §7.15 实测：同一条 `BV1cSec6tEux`，带导出 cookie 回 21 条视频轨/最高
    1772p@59.94，匿名只有 15 条/886p@29.97；同一条 `BV1ZUYf6JEfo`（av1 1080x1920）
    登录档视频轨 34.6 MB + 音轨 2.5 MB，匿名档整片合并才 18.6 MB，码率差一倍。
    所以档位差别不是"能不能下"，是**画质**。
    """

    dash_split_handling: Literal["auto", "merge", "keep_split"] = "auto"
    """未合并 DASH 分片怎么处理（V1 §7.21）。

    - `auto`: 有 ffmpeg 就 merge，没有就 keep_split 并如实记进清单
    - `merge`: 强制合并，合不了就失败（不静默降级）
    - `keep_split`: 保留 `<名字>.f<格式号>.mp4` + `.m4a` 两条文件

    下游转写必须认音频轨：只 `rglob('*.mp4')` 会把纯视频流喂给
    `extract_audio()` 的 `-vn`，ffmpeg 回 `exit 4294967274`(-22)
    `Output file does not contain any stream`。
    """

    retry_max: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0.0)
    request_timeout_seconds: int = Field(default=30, ge=1)

    search_fallback_node_playwright: bool = False
    """搜索兜底是否启用 Node 版 playwright（V1 §7.16）。

    默认 False，因为这条路的依赖链很脏：`npm install -g playwright` 装的全局包
    **不在** `require` 的搜索路径里，要 `setx NODE_PATH %APPDATA%\\npm\\node_modules`；
    `npx playwright install chromium` 走官方 CDN 会 `Failed to install browsers`，
    要换 `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright`。
    pip 那个 `playwright` 不算数。

    开启后如果检测不到 Node 版 playwright，必须**如实失败**并把检测原文交出去，
    不许静默跳过（V1 §1.3）。
    """


class BilibiliConfig(PlatformConfig):
    """B站配置。

    与抖音的两条关键差别：不需要 CDP 桥（`use_cdp_bridge=False`），
    且**枚举与下载是两条独立的路，两条都得带导出 cookie**（V1 §7.15）。
    无 cookie 时 B站随机回 `Request is rejected by server (352)` /
    `Request is blocked by server (412)`，**同一台机器上一条过一条不过**
    —— 所以"我手动跑通了"不能证明链路稳。

    `list_strategy` 的取值在 Task 7 改过：设计文档写的是 `'api'`（博主作品列表走公开
    web-interface），但实测 `x/space/wbi/arc/search` **不带 WBI 签名时回的是一个 HTML
    风控页而不是 JSON**（2026-09-22 现场验的，见 `docs/adr/0011` 的"Task 7 追记"），
    而 V1 一直跑通的是 `yt-dlp --flat-playlist`。所以这里收窄成
    `yt_dlp_flat | external_manifest`，**留一个实现不了的 `'api'` 就是在骗前端**
    （同 `DouyinConfig.persist_play_url` 被删的理由）。
    公开 web-interface 还在，只是它的角色是**逐条作品的元数据**
    （`x/web-interface/view`，实测匿名可访问）而不是列表枚举。
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = "B站"

    media_strategy: Literal["yt_dlp"] = "yt_dlp"
    list_strategy: Literal["yt_dlp_flat", "external_manifest"] = "yt_dlp_flat"
    """`external_manifest` 是 V1 `bilibili-download` 技能那条路：
    吃一份外部浏览器清单（JSON），命中清单的博主跳过内置枚举。"""

    use_cdp_bridge: bool = False

    ytdlp_cookies_from_browser: str | None = "chrome"
    """`--cookies-from-browser` 的目标浏览器。置空即跳过浏览器档。

    兼容 V1 的 `BILI_YTDLP_COOKIES_FROM_BROWSER` 环境变量（env 优先于本字段）。

    与抖音默认值不同的理由：B站 这一档**不是必死**（非 Windows 上它是真能用的登录态），
    而抖音那一档在 V1 实测里从来没成功过。顺序仍不在这里 ——
    三档的顺序是 `BilibiliAdapter.capabilities.cookie_variants` 的声明
    （`exported_file > browser > anonymous`，V1 §7.15 血泪排出来的），
    见 `docs/adr/0011`。
    """

    external_browser_manifest_path: Path | None = None
    """外部浏览器清单路径（`list_strategy='external_manifest'` 时必填）。

    生产者是 `.agents/skills/bilibili-download/scripts/download_bilibili.py`。
    V1 §7.13：技能脚本物理上有两份（仓库 + 用户级），会漂 ——
    采集链路跑的必须是**仓库那份**。
    """

    prefer_subtitles: bool = True
    """有字幕优先字幕，没有再回落 ASR。省一整轮转写时间。"""

    advanced: BilibiliAdvanced = Field(default_factory=BilibiliAdvanced)
