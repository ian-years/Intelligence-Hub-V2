"""小红书的媒体落盘：图文原图（ADR-0019）+ 视频直链兜底 + cookie 档位阶梯。

三条线在这个文件里交汇，先把每条的**判据**说清，因为它们看着像同一件事
（"这条笔记的媒体拿没拿到"），要修的东西完全不同：

- **图文不是失败**（ADR-0019）。一条图文笔记的正确产物是一个
  `SingleFileArtifact(path=第一张图, extra_paths=其余, has_audio=False, has_video=False,
  media_source="page_play_url")`，而 `size_bytes` 是**全部原图之和** ——
  报 `MediaDownloadError` 会把一条什么都没失败的笔记记成采集失败，
  而 `collect._Tally.to_result()` 在 `downloaded==0 and failed>0` 时直接把整轮判红：
  一个图文占多数的博主会让看板常年红着（AGENTS.md §1.3 点名要防的形状）。
- **`has_video=False` 必须显式填**。`media_path` 上躺的是一张 3 MB 的真 JPG，
  拿"有没有 `media_path`"当"能不能播"的判据会把 JPG 喂给 `<video>`，
  而黑屏不报错（ADR-0019 后果 2）。
- **§7.3 那一族在小红书同样成立**：Windows 上 yt-dlp 读不出 Chrome 的 cookie 库
  （开着 → `Could not copy Chrome cookie database`，关着 → `Failed to decrypt with DPAPI`），
  唯一稳定路径是 `--cookies <文件>`，而文件只能从桥导。

三份职责分开住，别往一处收：退档判据在 `infra.ytdlp`，**取哪个 cookie 文件 /
哪个浏览器**在本模块，**阶梯顺序**在 `XiaohongshuAdapter.capabilities`（ADR-0011）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.infra.ytdlp import (
    YtDlpCookieVariant,
    pick_exported_cookie_file,
    plan_cookie_variants,
)
from intelligence_hub_v2.infra.ytdlp import (
    progress_from_ytdlp_line as progress_from_ytdlp_line_imported,
)
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.task import ProgressCallback
from intelligence_hub_v2.platforms.base import CookieVariant
from intelligence_hub_v2.platforms.xiaohongshu.config import XiaohongshuConfig

logger = get_logger(__name__)

__all__ = [
    "ENV_COOKIES_FILE",
    "ENV_COOKIES_FROM_BROWSER",
    "MIN_MEDIA_BYTES",
    "XHS_COOKIE_DOMAIN",
    "CookieLadder",
    "DirectDownload",
    "ImageSetDownload",
    "download_image_set",
    "download_single_file",
    "image_extension",
    "progress_from_ytdlp_line",
    "report_progress",
    "report_ytdlp_line",
    "resolve_cookie_ladder",
]

PLATFORM = "xiaohongshu"

XHS_COOKIE_DOMAIN = "xiaohongshu.com"
"""cookie 文件的域名键 → `data/cookies/xiaohongshu.com.txt`（`FileStorage.cookies_path`）。

名字必须是 `*_COOKIE_DOMAIN` 这个形状：`tools/refresh_bridge_cookies.py` 的默认域名
清单是从 `platforms/` 里**扫**这个后缀的常量得来的（看护
`test_the_default_domains_are_the_platforms_own_constants`），
换一个名字等于"实现了适配器但没人导它的 cookie"。
"""

ENV_COOKIES_FILE = "XHS_YTDLP_COOKIES_FILE"
ENV_COOKIES_FROM_BROWSER = "XHS_YTDLP_COOKIES_FROM_BROWSER"
"""V1 的两个环境变量名（`download_xiaohongshu_latest.py:1023-1024`）。

它们决定的是"**这一档用哪个文件 / 哪个浏览器**"，不是阶梯顺序（ADR-0011）。
env 优先于 YAML 里的同名字段，因为会去设 env 的人明确知道自己要覆盖配置文件。
"""

# 小红书的 CDN（`sns-webpic-qc.xhscdn.com` 那一族）对没有 UA/Referer 的请求回 403。
# 这不是"伪装成浏览器"：这些直链本来就是页面里发出去的请求，
# 字节流这边得表明同一个身份才拿得到原图（V1 `_fetch_binary` 的 referer 同一取值）。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.xiaohongshu.com/"

MIN_MEDIA_BYTES = 1024
"""小于这个体积的响应判为无效。

与抖音同一条实测：CDN 直链失效时回的不是 404 而是**几百字节的错误页**，HTTP 还是 200。
按体积拦掉才不会把错误页当媒体入库 —— 图文那一侧尤其要紧，因为一张"下载成功"的
几百字节 JPG 在后面**没有任何一步会发现它坏**（不喂 ffmpeg、不进 ASR、
前端只会显示一张破图）。
"""

IMAGE_SUBDIR = "images"
"""图文原图的落点子目录，`media/images/01.jpg`。

不摊平到作品目录根上是为了两件：`(cover_path)` 那一条独立列将来要能指到别处，
以及 V1 那批 `notes/<id>/images/*.jpg` 迁进来时**目录形状不用重排**
（ADR-0019 的"迁移侧"那一段）。
"""

MEDIA_FILE_NAME = "media.mp4"
"""视频笔记的主媒体名，与抖音那条兜底路同一个名字（`data-model.md §1` 的 Locked 布局）。

两条路（yt-dlp / 页面 masterUrl）必须写同一个名字，否则后处理要按来源分支找文件。
"""

_PLACEHOLDER_IN_JS = re.compile(r"__[A-Z][A-Z0-9_]*__")
_IMAGE_SUFFIX = re.compile(r"\.(jpe?g|png|webp|gif)(?:[?@]|$)", re.IGNORECASE)
"""原图扩展名（V1 :1075 同一条）。

`(?:[?@]|$)` 那一半不是装饰：小红书的图床会在文件名后面挂 `@...` 的缩放参数
（`…!nd_dft_wlteh.jpg@...`），只看 `endswith('.jpg')` 会全部落到默认 `.jpg` ——
对 JPG 恰好没错，而 **webp / gif 会被存成 .jpg 后缀**，
于是前端 `<img>` 拿到一个内容类型不符的文件。
"""
_CHUNK_BYTES = 1 << 16
HTTP_CLIENT_ERROR = 400
"""4xx/5xx 都算"这个地址不行"，但**不算整条失败** —— 还有下一张图 / 下一个候选。"""


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
# 三个平台共用一份（V1 §7.15 的教训原文就是"两家各写一份 cookie 阶梯，漂过一次"）。
# 本模块因此**不实现任何 cookie 有效性判断**，只通过下面的 `pick_exported_cookie_file` 用它。


def _candidate_cookie_files(
    *, config: XiaohongshuConfig, cookies: CookieManager, environ: Mapping[str, str]
) -> list[tuple[str, Path]]:
    """按优先级排好的候选 cookie 文件：env > 配置 > `data/cookies/` 约定路径。

    第三项是 `tools/refresh_bridge_cookies.py` 的落点。它排在**最后**而不是最前，
    因为配置文件里写了显式路径的人是在说"用这个"；但必须有它 ——
    `cookies_file: "data/cookies/xiaohongshu.com.txt"` 是相对路径，而
    `Path.is_file()` 按当前 CWD 判断，从别的目录启动服务时这条会静默失效，
    整条链路退成匿名档，而那正是 §7.15 说的"症状要隔好几天才浮出来"。
    """
    out: list[tuple[str, Path]] = []
    env_file = str(environ.get(ENV_COOKIES_FILE) or "").strip()
    if env_file:
        out.append((f"环境变量 {ENV_COOKIES_FILE}", Path(env_file)))
    if config.cookies_file is not None:
        out.append(("配置 cookies_file", Path(config.cookies_file)))
    managed = cookies.exported_path(XHS_COOKIE_DOMAIN)
    if managed is not None:
        out.append(("data/cookies 约定路径", managed))
    return out


def resolve_cookie_ladder(
    order: Sequence[CookieVariant],
    *,
    config: XiaohongshuConfig,
    cookies: CookieManager,
    environ: Mapping[str, str] | None = None,
) -> CookieLadder:
    """把"声明的档位顺序"落成"能执行的 argv"，并说清中间丢了哪几档、为什么。

    `order` 来自 `XiaohongshuAdapter.capabilities.cookie_variants` —— **唯一真源**
    （ADR-0011）。本函数只回答两件事：导出文件档用哪个路径、浏览器档用哪个名字。
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
    if not browser:
        browser = (config.ytdlp_cookies_from_browser or "").strip()
    else:
        warnings.append(
            f"浏览器档已启用（{browser}，来自环境变量 {ENV_COOKIES_FROM_BROWSER}）—— "
            f"V1 §7.3：Windows 上这一档通常读不出来，只是它的错会触发退档"
        )

    variants = tuple(plan_cookie_variants(order, cookies_file=cookie_file, browser=browser or None))
    note = _ladder_note(variants=variants, cookie_file=cookie_file, warnings=warnings)
    if note:
        logger.warning("xiaohongshu.cookie_ladder", note=note)
    return CookieLadder(
        variants=variants, cookie_file=cookie_file, browser=browser or None, note=note
    )


def _ladder_note(
    *, variants: Sequence[YtDlpCookieVariant], cookie_file: Path | None, warnings: Sequence[str]
) -> str | None:
    """有话要说才说；没问题时返回 **None**。

    理由不在"少打字"：健康检查的 `detail` 会直接显示在预检页那一行上。
    全绿却挂一句"cookie 阶梯：exported_file > none"的流水账，读的人会把那行
    当成"有情况但没说清" —— 这正是 V1 §7.20 那个红灯形状的另一个面。
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
# 落盘
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DirectDownload:
    """一条视频直链的落盘产出。"""

    path: Path | None
    attempted: int
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageSetDownload:
    """一叠原图的落盘产出。

    `dropped_by_limit` 是"配置的上限切掉了最后几张"这一件事的记账位。
    它**不是失败**（用户就是要求少存），但也不许静默 —— 否则"这条笔记有 9 张图、
    库里只有 8 张"这件事在清单上完全 invisible，而那正是 ADR-0019 想避免的
    "静默少一半功能"的小号版本。
    """

    paths: tuple[Path, ...]
    attempted: int
    failures: tuple[str, ...] = ()
    dropped_by_limit: int = 0

    @property
    def total_bytes(self) -> int:
        """盘上真实字节数之和。**每次现算，不缓存**：这条数字要进 `size_bytes`，
        而"记下来的那一刻的大小"与"入库时的大小"之间可能隔着一次重写。
        """
        return sum(path.stat().st_size for path in self.paths)


class _LinkRejected(Exception):
    """一个候选地址失败。模块内部用，不外抛 —— 对外统一收成 `MediaDownloadError`。"""


async def _discard(partial: Path) -> None:
    with contextlib.suppress(OSError):  # pragma: no cover - 文件本来就不在，没什么可清理的
        await asyncio.to_thread(partial.unlink, missing_ok=True)


async def _stream_to(
    http: httpx.AsyncClient, url: str, partial: Path, *, budget_seconds: float
) -> int:
    """把一个直链落成 `.part`，返回写下的字节数；失败抛 `_LinkRejected`。

    写成"记一次 `failure` 文本、函数末尾统一 discard + raise"，而不是每个分支就地
    raise，是为了让**清理点只有一个**：三处 raise 配三处 `_discard`，漏一处就是磁盘上
    留一个看起来完整的 `.part` —— 而下次扫目录时它会被当成"已经下好了"跳过。

    体积判据看**实际写下的字节数**，不看 `Content-Length`（CDN 常常不给，或给的是压缩前的）。
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

    `ProgressCallback` 是任务层递进来的（发 SSE 事件），它自己挂了不该让
    已经落到磁盘的媒体变成"下载失败"。异常照记日志、不重抛 ——
    反过来（让它冒泡）会造成"文件下好了但任务红了"，那是最难查的一种假失败。
    """
    if on_progress is None:
        return
    try:
        on_progress(min(1.0, max(0.0, fraction)))
    except Exception as exc:  # noqa: BLE001 - 回调是外部代码，见上
        logger.warning("xiaohongshu.progress_callback_failed", error=f"{type(exc).__name__}: {exc}")


def report_ytdlp_line(on_progress: ProgressCallback | None, line: str) -> None:
    """yt-dlp 的一行输出 → 一次进度回调。认不出比例就不回调（不是回调 0）。"""
    fraction = progress_from_ytdlp_line(line)
    if fraction is not None:
        report_progress(on_progress, fraction)


def image_extension(url: str) -> str:
    """图床地址 → 落盘后缀。认不出来默认 `.jpg`（V1 :1074 同一条，`jpeg`→`jpg` 也同一条）。"""
    match = _IMAGE_SUFFIX.search(str(url or ""))
    if match is None:
        return ".jpg"
    return f".{match.group(1).lower().replace('jpeg', 'jpg')}"


async def download_image_set(
    http: httpx.AsyncClient,
    urls: Sequence[str],
    *,
    dest: Path,
    limit: int,
    budget_seconds: float,
    on_progress: ProgressCallback | None = None,
) -> ImageSetDownload:
    """把图文笔记的原图按页面顺序逐张落进 `dest/images/`。

    四条判据，前两条与抖音的直链下载同源，后两条是图文独有的：

    1. **先写 `.part` 再 `replace`**，失败不留半成品（同一份 `_stream_to`）。
    2. 单个地址失败**不掀掉整条笔记**：每一次的原文收进 `failures` 交出去。
       V1 这里只 `print` 一句就继续，V2 要的是那些原文能进清单。
    3. **文件名按最终存下来的序号**（`01.jpg`、`02.jpg`…），不是按"页面第几张"。
       按页面序号会在一中间张失败时留下 `01, 03, 05` 这样的洞，
       而 `extra_paths` 的契约是"按页面上的顺序"（ADR-0019）—— 洞会被读成"缺了文件"。
       页面原始位置在 `attempted` 与日志里留痕，不在文件名里。
    4. 一张都没拿到才算失败（调用方判），拿到 N 张就交 N 张。
    """
    kept = [str(url).strip() for url in urls if str(url or "").strip()]
    dropped = max(0, len(kept) - limit)
    targets = kept[:limit]
    folder = dest / IMAGE_SUBDIR
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)

    paths: list[Path] = []
    failures: list[str] = []
    total = len(targets)
    for index, url in enumerate(targets, start=1):
        target = folder / f"{len(paths) + 1:02d}{image_extension(url)}"
        partial = target.with_name(f"{target.name}.part")
        try:
            written = await _stream_to(http, url, partial, budget_seconds=budget_seconds)
        except _LinkRejected as exc:
            failures.append(f"第 {index}/{total} 张: {exc}")
            continue
        await asyncio.to_thread(partial.replace, target)
        paths.append(target)
        report_progress(on_progress, len(paths) / max(1, total))
        logger.info(
            "xiaohongshu.media.image",
            bytes=written,
            index=len(paths),
            total_images=len(targets),
            path=str(target),
        )
    return ImageSetDownload(
        paths=tuple(paths), attempted=total, failures=tuple(failures), dropped_by_limit=dropped
    )


async def download_single_file(
    http: httpx.AsyncClient,
    urls: Sequence[str],
    *,
    dest: Path,
    budget_seconds: float,
    on_progress: ProgressCallback | None = None,
) -> DirectDownload:
    """按给定顺序试候选直链，第一个够大的就赢（视频笔记的兜底路）。

    与抖音那条 `download_first_play_url` 同一套落盘纪律（`.part` → `replace`、
    失败清半成品、体积看实写字节）。所有候选都失败时**不抛**，把每一次的原文交出去：
    调用方要把它并进 `MediaDownloadError`，那里还得同时带上 yt-dlp 那一轮的原文
    （§7.2 的老规矩 —— 两条路都走过了，各自的错都得留一份，
    否则又变回"未拿到媒体"那种讲不完故事的日志）。
    """
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
    target = dest / MEDIA_FILE_NAME
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
            "xiaohongshu.media.direct_url",
            bytes=written,
            candidate_index=index,
            total_candidates=total,
            path=str(target),
        )
        return DirectDownload(path=target, attempted=index)
    return DirectDownload(path=None, attempted=total, failures=tuple(failures))


# 页面 JS 不在这个文件里（与抖音那份 `media.VIDEO_DETAIL_FUNCTION` 的分工不同）：
# 小红书的详情 JS 一次带回元数据 + 图片列表 + 视频直链，两边都要用，
# 所以它住在 `detail.py`。这里只负责"拿到地址之后怎么落盘"。


# yt-dlp 输出行的解析住在 `infra.ytdlp`（那是"外部命令的输出格式"知识，
# 三个平台要的是同一份）。这里转一道，保持 `xiaohongshu.media` 这个入口有效。
progress_from_ytdlp_line = progress_from_ytdlp_line_imported
