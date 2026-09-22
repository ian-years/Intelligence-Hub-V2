"""抖音的媒体获取：yt-dlp 档位阶梯 + 页面播放直链兜底。

V1 §7.2 与 §7.3 两条经验在这一层交汇，先各说清一次，因为它们**看着像同一件事**
（"抖音下不下来"），实际要修的东西完全不同：

- **§7.2**：抖音对非浏览器客户端做风控。同一条 `/aweme/v1/web/aweme/detail/` 请求，
  Python 直接发是 403 `Fresh cookies (not necessarily logged in) are needed`，
  在浏览器页面里发是 200 —— 缺的是页面里那层 `a_bogus` 签名，与 cookie 新不新鲜无关。
  所以"问地址"这一步交给桥在页面上下文里做，**字节流仍由 Python 落盘**
  （CDN 直链本身带签名、不认 cookie，不需要浏览器）。
  结论：`yt_dlp_with_fallback` 是常态，`media_source == "page_play_url"` 不是故障。
- **§7.3**：Windows 上 yt-dlp 读不了 Chrome 的 cookie 库（开着 → `Could not copy
  Chrome cookie database`，关着 → `Failed to decrypt with DPAPI`），
  唯一稳定路径是 `--cookies <文件>`，而文件只能从桥导。
  这条管的是"yt-dlp 那一档带不带得上登录态"。

三份职责分开住，别往一处收：退档判据在 `infra.ytdlp`，**取哪个 cookie 文件 /
哪个浏览器**在本模块，**阶梯顺序**在 `DouyinAdapter.capabilities` —— 理由见 `docs/adr/0011`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from intelligence_hub_v2.errors import MediaDownloadError
from intelligence_hub_v2.infra.cdp_bridge import BridgeClient
from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.infra.ytdlp import (
    YtDlpCookieVariant,
    netscape_file_blocker,
    pick_exported_cookie_file,
    plan_cookie_variants,
)
from intelligence_hub_v2.infra.ytdlp import (
    progress_from_ytdlp_line as progress_from_ytdlp_line_imported,
)
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.task import ProgressCallback
from intelligence_hub_v2.platforms.base import CookieVariant
from intelligence_hub_v2.platforms.douyin.config import DouyinConfig
from intelligence_hub_v2.platforms.douyin.listing import decode_page_result
from intelligence_hub_v2.platforms.douyin.urls import canonical_video_url, is_http_url

logger = get_logger(__name__)

__all__ = [
    "DOUYIN_COOKIE_DOMAIN",
    "ENV_COOKIES_FILE",
    "ENV_COOKIES_FROM_BROWSER",
    "MIN_MEDIA_BYTES",
    "CookieLadder",
    "DirectDownload",
    "download_first_play_url",
    "fetch_play_urls",
    "parse_play_urls",
    "progress_from_ytdlp_line",
    "render_detail_js",
    "report_progress",
    "report_ytdlp_line",
    "resolve_cookie_ladder",
]

PLATFORM = "douyin"

DOUYIN_COOKIE_DOMAIN = "douyin.com"
"""cookie 文件的域名键 → `data/cookies/douyin.com.txt`（`FileStorage.cookies_path`）。"""

ENV_COOKIES_FILE = "DOUYIN_YTDLP_COOKIES_FILE"
ENV_COOKIES_FROM_BROWSER = "DOUYIN_YTDLP_COOKIES_FROM_BROWSER"
"""V1 的两个环境变量名。

它们决定的是"**这一档用哪个文件 / 哪个浏览器**"，不是阶梯顺序（`docs/adr/0011`）。
env 优先于 YAML 里的同名字段，因为会去设 env 的人明确知道自己要覆盖配置文件。

**未收口**：`config-schema.md §6` 把这两个名字写成"映射到平台配置字段"，而
`ConfigManager` 的平台配置那一层只吃 YAML、没接 env source，所以兼容行为暂时落在这一层。
配置层接上之后这里要改回读配置，否则同一件事两处真相 —— 记账见 `docs/lessons.md`。
"""

# 抖音 CDN 对没有 UA/Referer 的请求回 403，对浏览器 UA 放行。这不是"伪装成浏览器"，
# 而是这条直链本来就是页面里发出去的请求，字节流这边得表明同一个身份。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.douyin.com/"

MIN_MEDIA_BYTES = 1024
"""小于这个体积的响应判为无效。

V1 实测：CDN 直链失效时回的不是 404 而是**几百字节的 JSON 错误页**，HTTP 状态还是 200。
按体积拦掉才不会把错误页当视频入库 —— 那种"下载成功"的文件后面会被 ASR 报成"音频为空"，
排查方向又绕远一圈。
"""

_CHUNK_BYTES = 1 << 16
_PLACEHOLDER_IN_JS = re.compile(r"__[A-Z][A-Z0-9_]*__")


# --------------------------------------------------------------------------- #
# 页面 JS：问播放直链
# --------------------------------------------------------------------------- #

# 同一条详情请求，Python 直接发是 403，浏览器页面里发是 200（V1 §7.2）。
# __AWEME_ID__ 走 json.dumps 注入，模板里**不带引号**（V1 §2 契约三）。
VIDEO_DETAIL_FUNCTION = """
async () => {
  const awemeId = __AWEME_ID__;
  const api = "/aweme/v1/web/aweme/detail/?aweme_id=" + encodeURIComponent(awemeId)
    + "&device_platform=webapp&aid=6383&channel=pc_professional&version_name=1.0.1.19";
  try {
    const res = await fetch(api, {credentials: "include"});
    if (!res.ok) return JSON.stringify({ok: false, error: "http_" + res.status});
    const data = await res.json();
    const detail = (data && data.aweme_detail) || {};
    const list = ((detail.video || {}).play_addr || {}).url_list || [];
    const urls = list.filter((u) => typeof u === "string" && u.indexOf("http") === 0);
    return JSON.stringify({ok: urls.length > 0, aweme_id: awemeId, play_urls: urls,
                           error: urls.length ? "" : "play_addr_empty"});
  } catch (err) {
    return JSON.stringify({ok: false, error: String((err && err.message) || err)});
  }
}
"""


def render_detail_js(*, aweme_id: str) -> str:
    """详情 JS 占位符注入：值走 `json.dumps`，模板里不能带引号。

    与 `listing.render_page_js()` 一样做**残留占位符检查**，理由同一份：
    漏替换的 `__AWEME_ID__` 会以 `ReferenceError` 的形式出现在桥那一头，
    而 Python 侧只看到"evaluate 失败"，排查的人会先怀疑"抖音改版了"。
    """
    rendered = VIDEO_DETAIL_FUNCTION.replace("__AWEME_ID__", json.dumps(str(aweme_id)))
    leftover = sorted(set(_PLACEHOLDER_IN_JS.findall(rendered)))
    if leftover:
        msg = f"详情 JS 里还剩未替换的占位符：{', '.join(leftover)}"
        raise ValueError(msg)
    return rendered


def parse_play_urls(payload: Mapping[str, Any], *, aweme_id: str) -> list[str]:
    """详情接口的回答 → 按页面给的顺序排好的候选直链。

    `http_403` 与 `play_addr_empty` 要分开看：前者是"页面上下文没带上有效登录态"
    （去桥里重新登录），后者是"这条作品的详情里真的没有播放地址"（图文、被删、审核中）。
    所以原文整段带上，不要压成一句"拿不到直链"。
    """
    if payload.get("ok") is not True:
        error = str(payload.get("error") or "empty")
        msg = f"播放直链解析失败（{aweme_id}）: {error}"
        raise MediaDownloadError(PLATFORM, "media", msg)
    raw = payload.get("play_urls")
    if not isinstance(raw, list):
        msg = f"播放直链列表形状不对（{aweme_id}）：拿到的是 {type(raw).__name__}"
        raise MediaDownloadError(PLATFORM, "media", msg)
    return [str(url) for url in raw if is_http_url(url)]


async def fetch_play_urls(bridge: BridgeClient, aweme_id: str) -> list[str]:
    """在页面上下文里问一次详情接口，按顺序返回可直连的播放地址。"""
    if not aweme_id.isdigit():
        msg = f"aweme_id 不是纯数字，不是合法的抖音作品 ID: {aweme_id!r}"
        raise MediaDownloadError(PLATFORM, "media", msg)
    await bridge.navigate(canonical_video_url(aweme_id))
    result = await bridge.evaluate(render_detail_js(aweme_id=aweme_id))
    payload = decode_page_result(result, stage="media")
    return parse_play_urls(payload, aweme_id=aweme_id)


# --------------------------------------------------------------------------- #
# cookie 档位阶梯
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CookieLadder:
    """解析好的阶梯，带**给人看的说明**。

    `note` 不是装饰品：档位差别往往是画质而不是"能不能下"（V1 §7.15），
    所以"这次其实没带上登录 cookie，因为配置指的路径不存在"这句话必须能出现在
    预检与清单里。丢了它，症状就是过几天发现这批视频糊了，而没人知道为什么。
    """

    variants: tuple[YtDlpCookieVariant, ...]
    cookie_file: Path | None
    browser: str | None
    note: str | None = None

    @property
    def rung_kinds(self) -> tuple[CookieVariant, ...]:
        return tuple(variant.kind for variant in self.variants)


# "这个路径能不能当导出文件档"的判据住在 `infra.ytdlp.netscape_file_blocker`，
# 两个平台共用一份（V1 §7.15 的教训原文就是"两家各写一份 cookie 阶梯，漂过一次"）。
_usable_netscape_file = netscape_file_blocker


def _candidate_cookie_files(
    *, config: DouyinConfig, cookies: CookieManager, environ: Mapping[str, str]
) -> list[tuple[str, Path]]:
    """按优先级排好的候选 cookie 文件：env > 配置 > `data/cookies/` 约定路径。

    第三项是 `tools/refresh_bridge_cookies.py` 的落点。它排在**最后**而不是最前，
    因为配置文件里写了显式路径的人是在说"用这个"；但必须有它 ——
    `cookies_file: "data/cookies/douyin.com.txt"` 是相对路径，而
    `Path.is_file()` 按当前 CWD 判断，从别的目录启动服务时这条会静默失效，
    整条链路退成匿名档，而那正是 §7.15 说的"症状要隔好几天才浮出来"。
    """
    out: list[tuple[str, Path]] = []
    env_file = str(environ.get(ENV_COOKIES_FILE) or "").strip()
    if env_file:
        out.append((f"环境变量 {ENV_COOKIES_FILE}", Path(env_file)))
    if config.cookies_file is not None:
        out.append(("配置 cookies_file", Path(config.cookies_file)))
    managed = cookies.exported_path(DOUYIN_COOKIE_DOMAIN)
    if managed is not None:
        out.append(("data/cookies 约定路径", managed))
    return out


def resolve_cookie_ladder(
    order: Sequence[CookieVariant],
    *,
    config: DouyinConfig,
    cookies: CookieManager,
    environ: Mapping[str, str] | None = None,
) -> CookieLadder:
    """把"声明的档位顺序"落成"能执行的 argv"，并说清中间丢了哪几档、为什么。

    `order` 来自 `DouyinAdapter.capabilities.cookie_variants` —— **唯一真源**
    （`docs/adr/0011`）。本函数只回答两件事：导出文件档用哪个路径、浏览器档用哪个名字。
    某档没有对应资源时由 `plan_cookie_variants()` 跳过那一档（不是退档重试，是没有这一档）。
    """
    env = os.environ if environ is None else environ

    candidates = _candidate_cookie_files(config=config, cookies=cookies, environ=env)
    warnings: list[str] = []
    cookie_file, winner, rejections = pick_exported_cookie_file(candidates)
    warnings.extend(rejections)
    if cookie_file is not None and winner.startswith("环境变量") and len(candidates) > 1:
        # 配置文件说"用 A"，环境变量的存在把答案换成了 B。这个覆盖本身是合法的
        # （V1 兼容），但**必须在报告里看得见** —— 否则排查的人会照着 YAML 那一行想。
        warnings.append(f"环境变量 {ENV_COOKIES_FILE} 覆盖了配置里的 cookies_file")

    browser = str(env.get(ENV_COOKIES_FROM_BROWSER) or "").strip()
    browser_source = f"环境变量 {ENV_COOKIES_FROM_BROWSER}"
    if not browser:
        browser = (config.ytdlp_cookies_from_browser or "").strip()
    else:
        warnings.append(
            f"浏览器档已启用（{browser}，来自 {browser_source}）—— "
            f"V1 §7.3：Windows 上这一档通常读不出来，只是它的错会触发退档"
        )

    variants = tuple(plan_cookie_variants(order, cookies_file=cookie_file, browser=browser or None))
    note = _ladder_note(variants=variants, cookie_file=cookie_file, warnings=warnings)
    if note:
        logger.warning("douyin.cookie_ladder", note=note)
    return CookieLadder(
        variants=variants, cookie_file=cookie_file, browser=browser or None, note=note
    )


def _ladder_note(
    *, variants: Sequence[YtDlpCookieVariant], cookie_file: Path | None, warnings: Sequence[str]
) -> str | None:
    """有话要说才说；没问题时返回 **None**。

    理由不在"少打字"：健康检查的 `detail` 会直接显示在预检页那一行上。
    全绿却挂一句"cookie 阶梯：exported_file > none"的流水账，读的人会把那行
    当成"有情况但没说清"—— 这正是 V1 §7.20 那个红灯形状的另一个面。
    """
    if not warnings and cookie_file is not None:
        return None
    rungs = " > ".join(f"{variant.kind}（{variant.label}）" for variant in variants) or "空"
    parts = [f"cookie 阶梯：{rungs}"]
    if cookie_file is None:
        parts.append("没有可用的导出 cookie，yt-dlp 只能匿名跑（登录档画质不可用）")
    parts.extend(warnings)
    return "；".join(parts)


# --------------------------------------------------------------------------- #
# 直链落盘
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DirectDownload:
    """一次直链下载的产出。

    `attempted` 要一起带出去：0 个候选与"试了 3 个都失败"是两种红
    （前者是"页面没给出地址"，后者是"地址过期了或被 CDN 拒了"），文案与要做的动作都不同。
    """

    path: Path | None
    attempted: int
    failures: tuple[str, ...] = ()


class _LinkRejected(Exception):
    """一个候选地址失败。模块内部用，不外抛 —— 对外统一收成 `MediaDownloadError`。"""


async def _discard(partial: Path) -> None:
    with contextlib.suppress(OSError):  # pragma: no cover - 文件本来就不在，没什么可清理的
        await asyncio.to_thread(partial.unlink, missing_ok=True)


HTTP_CLIENT_ERROR = 400
"""4xx/5xx 都算"这个地址不行"，但**不算失败** —— 还有下一个候选。"""


async def _stream_to(
    http: httpx.AsyncClient, url: str, partial: Path, *, budget_seconds: float
) -> int:
    """把一个直链落成 `.part`，返回写下的字节数；失败抛 `_LinkRejected`。

    写成"记一次 `failure` 文本、函数末尾统一 discard + raise"，而不是每个分支就地
    raise，是为了让**清理点只有一个**：三处 raise 配三处 `_discard`，漏一处就是磁盘上
    留一个看起来完整的 `.part` —— 而下次扫目录时它会被当成"已经下好了"跳过。
    """
    headers = {"User-Agent": USER_AGENT, "Referer": REFERER}
    written = 0
    failure: str | None = None
    try:
        async with http.stream(
            "GET", url, headers=headers, timeout=budget_seconds, follow_redirects=True
        ) as response:
            if response.status_code >= HTTP_CLIENT_ERROR:
                failure = f"HTTP {response.status_code}"
            else:
                sink = await asyncio.to_thread(partial.open, "wb")
                try:
                    async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                        if not chunk:
                            continue
                        written += len(chunk)
                        await asyncio.to_thread(sink.write, chunk)
                finally:
                    await asyncio.to_thread(sink.close)
    except httpx.HTTPError as exc:
        failure = f"{type(exc).__name__}: {exc}"
    if failure is None and written < MIN_MEDIA_BYTES:
        failure = f"内容只有 {written} 字节，判为无效响应（CDN 错误页就是这个体积）"
    if failure is not None:
        await _discard(partial)
        raise _LinkRejected(failure)
    return written


def report_progress(on_progress: ProgressCallback | None, fraction: float) -> None:
    """进度回调不许把调用方的异常倒灌进下载流程。

    `ProgressCallback` 是 Task 8 递进来的（发 SSE 事件），它自己挂了不该让
    已经落到磁盘的媒体变成"下载失败"。异常照记日志、不重抛 ——
    反过来（让它冒泡）会造成"文件下好了但任务红了"，那是最难查的一种假失败。
    """
    if on_progress is None:
        return
    try:
        on_progress(min(1.0, max(0.0, fraction)))
    except Exception as exc:  # noqa: BLE001 - 回调是外部代码，见上
        logger.warning("douyin.progress_callback_failed", error=f"{type(exc).__name__}: {exc}")


def report_ytdlp_line(on_progress: ProgressCallback | None, line: str) -> None:
    """yt-dlp 的一行输出 → 一次进度回调。认不出比例就不回调（不是回调 0）。"""
    fraction = progress_from_ytdlp_line(line)
    if fraction is not None:
        report_progress(on_progress, fraction)


async def download_first_play_url(
    http: httpx.AsyncClient,
    urls: Sequence[str],
    *,
    dest: Path,
    budget_seconds: float,
    on_progress: ProgressCallback | None = None,
) -> DirectDownload:
    """按页面给的顺序试候选直链，第一个够大的就赢。

    落盘纪律三条，全是 V1 的：

    1. **先写 `.part` 再 `replace`**。半途失败/超时不许留下一个看起来完整的 `media.mp4` ——
       后处理扫到它就会跳过重下，于是库里永久躺着一段坏文件。
    2. 失败时把 `.part` 删掉（`_discard`），磁盘上不留半成品。
    3. 体积判据看**实际写下的字节数**，不看 `Content-Length`（CDN 常常不给，或给的是压缩前的）。

    所有候选都失败时**不抛**，把每一次的原文交出去：调用方要把它并进 `MediaDownloadError`，
    那里还得同时带上 yt-dlp 那一轮的原文（§7.2 的老规矩 —— 两条路都走过了，
    各自的错都得留着一份，否则又变回"未拿到媒体"那种讲不完故事的日志）。
    """
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
    target = dest / "media.mp4"
    """主媒体文件名：`data-model.md §1` 那份 Locked 的目录布局写死了
    一条作品的目录里主媒体叫 `media.mp4`（DASH 分片才带 `.f<格式号>` 段）。
    V1 用的是 `<aweme_id>.mp4`，V2 统一由布局说了算，后处理与前端只认这一个名字。"""
    partial = target.with_name(f"{target.name}.part")

    failures: list[str] = []
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        try:
            written = await _stream_to(http, url, partial, budget_seconds=budget_seconds)
        except _LinkRejected as exc:
            failures.append(f"候选 {index}/{total}: {exc}")
            continue
        await asyncio.to_thread(partial.replace, target)
        report_progress(on_progress, 1.0)
        logger.info(
            "douyin.media.page_play_url",
            bytes=written,
            candidate_index=index,
            total_candidates=total,
            path=str(target),
        )
        return DirectDownload(path=target, attempted=index)
    return DirectDownload(path=None, attempted=total, failures=tuple(failures))


# yt-dlp 输出行的解析住在 `infra.ytdlp`（那是"外部命令的输出格式"知识，
# 而 B站 那条链路要的是同一份）。这里转一道，保持 `douyin.media` 这个入口有效。
progress_from_ytdlp_line = progress_from_ytdlp_line_imported
