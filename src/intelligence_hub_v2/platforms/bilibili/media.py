"""B站的媒体下载：cookie 三档阶梯 + 未合并 DASH 分片。

两条 V1 经验在这里交汇，都关于"下到的是什么"：

- **§7.15 三档阶梯**：导出文件 > `--cookies-from-browser` > 匿名。
  档位差别**往往不是能不能下，是画质**（实测记录：同一条 `BV1cSec6tEux`
  登录档 21 条视频轨/最高 1772p@59.94，匿名只有 15 条/886p@29.97）。
  所以"降级成功"必须在产物里说清是哪一档 —— 这就是 `MediaArtifact.yt_dlp_error`
  与清单 note 要装阶梯轨迹的理由。
  顺序的**唯一真源**是 `BilibiliAdapter.capabilities.cookie_variants`（ADR-0011）；
  本模块只回答"导出文件档用哪个路径、浏览器档用哪个浏览器"。
- **§7.21 DASH 分片**：`-f bv*+ba/b` 要求 yt-dlp 把视频轨与音频轨合并。
  它合不成时会留下 `media.f30064.mp4`（**纯视频**）+ `media.f30280.m4a`（纯音频）。
  本机实测过这两个 format id 真的存在（`yt-dlp -J` 匿名回 15 条 formats，
  视频轨 ext=mp4/vcodec=avc1…/acodec=none，音频轨 ext=m4a/acodec=mp4a…）。
  这种情况下**必须**用 `VideoAudioPairArtifact` 表达，不能：
  ① 拿 `rglob("*.mp4")` 扫目录（会把中间产物当源媒体，一条作品转两遍），
  ② 或者把纯视频轨交给 `ffmpeg -vn` —— 那报出来的
  `Output file does not contain any stream` 长得和"ffmpeg 没装"一模一样，方向完全不同。

V1 那边 `--write-info-json` 会落一份 `<bvid>.info.json` 当作品的档案。
V2 不落：标题/时长/指标走 `view` 接口 + `videos` 表，"档案"只有一处真相。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.infra.ytdlp import (
    YtDlpCookieVariant,
    YtDlpResult,
    pick_exported_cookie_file,
    plan_cookie_variants,
)
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.platforms.base import CookieVariant
from intelligence_hub_v2.platforms.bilibili.config import BilibiliConfig

logger = get_logger(__name__)

__all__ = [
    "ENV_COOKIES_FILE",
    "ENV_COOKIES_FROM_BROWSER",
    "CookieLadder",
    "resolve_cookie_ladder",
    "ytdlp_failure_reason",
]

PLATFORM = "bilibili"

ENV_COOKIES_FILE = "BILI_YTDLP_COOKIES_FILE"
ENV_COOKIES_FROM_BROWSER = "BILI_YTDLP_COOKIES_FROM_BROWSER"
"""V1 的环境变量名。与抖音那一对同构：只决定"这一档用哪个资源"，不决定顺序。

`ENV_COOKIES_FROM_BROWSER` 的默认值是 `"chrome"`（V1 就这个默认），
而抖音那侧默认**没有**浏览器档。差别不是随手写的：
V1 §7.3 的实测里 Chrome 库在 Windows 上对谁都是读不出来的，
但 B站 在非 Windows 上这一档是真能用的登录态，所以留着一个能用的默认。
"""


MEDIA_FILE_TEMPLATE = "media.%(ext)s"
"""与 `data-model.md §1` 的 Locked 布局一致：合并成功 → `media.mp4`，
没合并 → `media.f30064.mp4` + `media.f30280.m4a`（正是 `FileStorage.dash_part_file` 的名字）。"""

YTDLP_EXTRA_ARGS: tuple[str, ...] = (
    "--no-update",
    "--no-write-comments",
    "-f",
    "bv*+ba/b",
    "--merge-output-format",
    "mp4",
)


# cookie 阶梯


@dataclass(frozen=True)
class CookieLadder:
    """解析好的阶梯 + 给人看的说明（同抖音那个形状，理由也一样：
    档位差别是画质，"这次没带上登录 cookie"必须能被看见）。"""

    variants: tuple[YtDlpCookieVariant, ...]
    cookie_file: Path | None
    browser: str | None
    note: str | None = None

    @property
    def rung_kinds(self) -> tuple[CookieVariant, ...]:
        return tuple(variant.kind for variant in self.variants)

    @property
    def logged_in(self) -> bool:
        """这一轮有没有可能带上登录态。预检与清单 note 用。"""
        return self.cookie_file is not None or bool(self.browser)


def _candidate_cookie_files(
    *, config: BilibiliConfig, cookies: CookieManager, environ: Mapping[str, str]
) -> list[tuple[str, Path]]:
    """env > 配置 > `data/cookies/` 约定路径。理由与抖音那份完全同构，
    只换域名与变量名（第三项救的是"相对路径按 CWD 判断存在性"那一坑）。"""
    out: list[tuple[str, Path]] = []
    env_file = str(environ.get(ENV_COOKIES_FILE) or "").strip()
    if env_file:
        out.append((f"环境变量 {ENV_COOKIES_FILE}", Path(env_file)))
    if config.cookies_file is not None:
        out.append(("配置 cookies_file", Path(config.cookies_file)))
    managed = cookies.exported_path("bilibili.com")
    if managed is not None:
        out.append(("data/cookies 约定路径", managed))
    return out


def resolve_cookie_ladder(
    order: Sequence[CookieVariant],
    *,
    config: BilibiliConfig,
    cookies: CookieManager,
    environ: Mapping[str, str] | None = None,
) -> CookieLadder:
    """把"声明的档位顺序"落成"能执行的 argv"，并说清丢了哪一档、为什么。"""
    env = os.environ if environ is None else environ
    candidates = _candidate_cookie_files(config=config, cookies=cookies, environ=env)
    cookie_file, winner, rejections = pick_exported_cookie_file(candidates)
    warnings = list(rejections)
    if cookie_file is not None and winner.startswith("环境变量") and len(candidates) > 1:
        warnings.append(f"环境变量 {ENV_COOKIES_FILE} 覆盖了配置里的 cookies_file")

    browser = str(env.get(ENV_COOKIES_FROM_BROWSER) or "").strip()
    browser_source = f"环境变量 {ENV_COOKIES_FROM_BROWSER}"
    if not browser and ENV_COOKIES_FROM_BROWSER not in env:
        # env 里"设成空串"是显式关掉这一档（V1 同一取舍），不能再用配置默认值顶回去
        browser = (config.ytdlp_cookies_from_browser or "").strip()
        browser_source = "配置 ytdlp_cookies_from_browser"
    if browser and browser_source.startswith("环境变量"):
        # 只有 env 覆盖时才报告：配置里 `ytdlp_cookies_from_browser: "chrome"` 是 B站
        # 的默认档，每次健康检查都唠叨一句"浏览器档启用了"会让 detail 永远非空，
        # 而预检页那一行读起来像"有情况但没说清"（与抖音那边同一条判据）。
        warnings.append(
            f"浏览器档来自环境变量 {ENV_COOKIES_FROM_BROWSER}（{browser}）—— "
            f"Windows 上这一档多半读不出来"
        )
    # 浏览器档被关掉**不是**一件要报告的事：V1 §7.3 说它在 Windows 上本来就不可用，
    # 每次采集都唠叨一句只会让人忽略整条 note。

    variants = tuple(plan_cookie_variants(order, cookies_file=cookie_file, browser=browser or None))
    note = _ladder_note(variants=variants, cookie_file=cookie_file, warnings=warnings)
    if note:
        logger.warning("bilibili.cookie_ladder", note=note)
    return CookieLadder(
        variants=variants, cookie_file=cookie_file, browser=browser or None, note=note
    )


def _ladder_note(
    *, variants: Sequence[YtDlpCookieVariant], cookie_file: Path | None, warnings: Sequence[str]
) -> str | None:
    if not warnings and cookie_file is not None:
        return None
    rungs = " > ".join(f"{v.kind}（{v.label}）" for v in variants) or "空"
    parts = [f"cookie 阶梯：{rungs}"]
    if cookie_file is None:
        parts.append("没有可用的导出 cookie，只能匿名或走浏览器档（4K/高帧率不可用，V1 §7.15）")
    parts.extend(warnings)
    return "；".join(parts)


def ytdlp_failure_reason(result: YtDlpResult) -> str:
    """每一档的退出码 + stderr 尾行。V1 §7.2 的同一条纪律：降级过程要能自己讲完故事。"""
    tail = [line for line in result.stderr.strip().splitlines() if line.strip()]
    last = tail[-1] if tail else f"exit {result.returncode}"
    return f"{result.attempts_note()}｜{last}"
