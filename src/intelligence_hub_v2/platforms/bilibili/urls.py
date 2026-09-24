"""B站的身份与链接：`mid` / `bvid` 提取与规范 URL。

与抖音那份 `urls.py` 同构：**不碰网络**，只做纯解析。
两边都不许在这里发请求 —— 那是适配器的事（只有那里能注入 `deps.http`，
测试才能用 `httpx.MockTransport` 验"短链真的跟了一次 302"）。

B站这边有一个抖音没有的便利：**`bvid` 自带校验形状**（`BV` + 10 位 base58 字符），
所以"这是不是一条作品链接"可以纯离线判定，不需要像抖音那样先跟一次跳转。
反过来 `mid` 是纯数字，`space.bilibili.com/<数字>` 里那个数字**就是**身份，
所以 `parse_creator_url` 对主页链接完全不需要联网 —— 只有 `b23.tv` 短链要。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import HttpUrl

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.platforms.urls import absolute_http_url

__all__ = [
    "BILI_COOKIE_DOMAIN",
    "as_http_url",
    "canonical_video_url",
    "card_api_url",
    "extract_bvid",
    "extract_mid",
    "is_bvid",
    "is_short_link",
    "media_comment_url",
    "player_api_url",
    "require_http_url",
    "require_model_url",
    "space_url",
    "space_video_url",
    "view_api_url",
]

BILI_COOKIE_DOMAIN = "bilibili.com"
"""cookie 文件的域名键 → `data/cookies/bilibili.com.txt`。"""

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
"""`bvid` 的形状。V1 的 `extract_bvid` 同一条正则（`BV` + 恰好 10 位）。

为什么是"恰好 10 位"而不是"至少"：B站的 av 号时代之后所有作品 ID 都是定长的，
`{10,}` 会让一条被截断的链接（前端拼接、日志换行）也匹配成功，
然后拿着一个残缺 ID 去下载，报出来的是"视频不存在"而不是"链接被切坏了"。
"""

_MID_IN_PATH = re.compile(r"(?:space\.bilibili\.com|m\.bilibili\.com/space)/(\d+)", re.IGNORECASE)

_SHORT_LINK_HOSTS = frozenset({"b23.tv"})
"""App 分享出来的短码域名。与抖音的 `v.douyin.com` 同一类：**不含身份信息**。"""

_HTTP_PREFIXES = ("http://", "https://")


def is_bvid(value: object) -> bool:
    """这一串是不是一个 `bvid`。"""
    text = str(value or "").strip()
    return bool(_BVID_RE.fullmatch(text))


def extract_bvid(value: object) -> str:
    """从 URL、裸 ID 或任意文本里认出 `bvid`，认不出返回空串。

    顺序：先看是不是裸 ID，再按 URL 里的 `/video/BV…` 认，最后**整段扫一次正则**
    —— 最后那一步是有意的宽松：清单文件、剪贴板、用户手抄都可能带前后噪声，
    而 `BV…` 这个形状误报的代价极低（BV 后必须正好 10 位字母数字）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _BVID_RE.fullmatch(text)
    if match:
        return match.group(1)
    match = _BVID_RE.search(text)
    return match.group(1) if match else ""


def extract_mid(value: object) -> str:
    """从博主链接里认 `mid`（数字 ID），认不出返回空串。

    三种真实形状：

    1. `https://space.bilibili.com/486906719`（可带 `/video`、`?spm_id_from=…`）
    2. `https://m.bilibili.com/space/486906719`
    3. 裸数字 `486906719` —— 用户从表格里复制一列出来经常就是这个样子

    **不认 `b23.tv` 短链**（那条要联网，见 `is_short_link` 与适配器）。
    也不从作品链接反推 mid：那需要一次 view 接口调用，属于适配器的编排，
    纯函数不该藏着一个网络往返。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    match = _MID_IN_PATH.search(text)
    return match.group(1) if match else ""


def is_short_link(url: object) -> bool:
    """是不是必须先展开才知道指向谁的 `b23.tv` 短链。"""
    host = urlparse(str(url or "").strip()).hostname or ""
    return host.lower() in _SHORT_LINK_HOSTS


def space_url(mid: str) -> str:
    return f"https://space.bilibili.com/{mid}"


def space_video_url(mid: str) -> str:
    """作品列表页。枚举要的是这一条而不是 `space_url(mid)`：
    后者默认落在首页（动态流），yt-dlp 在那上面抽不出作品。"""
    return f"{space_url(mid)}/video"


def canonical_video_url(bvid: str) -> str:
    """**结尾那个斜杠不是装饰**：B站的网页地址带尾斜杠，
    不带时 yt-dlp 与部分接口会先做一次 301 再抽，白多一个来回。"""
    return f"https://www.bilibili.com/video/{bvid}/"


def media_comment_url(bvid: str) -> str:
    """api 侧的 referer 用。"""
    return f"https://www.bilibili.com/video/{bvid}"


def view_api_url(bvid: str) -> str:
    """逐条作品的元数据。**实测匿名可访问**（2026-09-22，BV1GJ411x7h7 回 code:0）。"""
    return f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"


def player_api_url(bvid: str, cid: str | int) -> str:
    """播放器信息，含字幕轨列表。"""
    return f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"


def card_api_url(mid: str) -> str:
    """博主资料（昵称 / 头像 / 粉丝数 / 签名）。**实测匿名可访问**。"""
    return f"https://api.bilibili.com/x/web-interface/card?mid={mid}"


def require_http_url(value: object, *, context: str) -> str:
    """外部字符串必须是 http(s) URL，否则抛 `PlatformError`。

    B站的接口里有一批"协议相对"地址（`//i0.hdslb.com/…`，头像与字幕正文都是），
    直接塞给下游会被当成相对路径 —— 所以在这里统一补 `https:`，
    而不是到下载那一步才发现"URL 打不开"。
    """
    text = str(value or "").strip()
    if text.startswith("//"):
        return f"https:{text}"
    if text.lower().startswith(_HTTP_PREFIXES):
        return text
    msg = f"{context} 应该是一个 http(s) URL，实际是 {text[:120]!r}"
    raise PlatformError("bilibili", "parse", msg)


def require_model_url(value: object, *, context: str) -> HttpUrl:
    """`as_http_url()` 的"这里不给 None"版本（模型里的必填 URL 字段用）。

    失败抛 `PlatformError` 而不是 `ValidationError`：前者带 platform/stage，
    能进清单的 `failures[]`；后者是一串 Pydantic 字段名，读的人不知道
    是哪条作品、哪一步出的问题。
    """
    url = as_http_url(value)
    if url is None:
        msg = f"{context} 应该是一个 http(s) URL，实际是 {str(value)[:120]!r}"
        raise PlatformError("bilibili", "parse", msg)
    return url


def as_http_url(value: object) -> HttpUrl | None:
    """接口给的字符串 → `HttpUrl`，认不出来返回 None（必填字段别用这个，用上面那个）。

    实现住在 `platforms/urls.py::absolute_http_url`（ADR-0016：第三个平台进来时，
    第三份复制就该变成一次转发）。名字留在这里不动 —— `subtitles.py`、`listing.py`
    与 `tests/contracts/test_bilibili_helpers.py` 都指着它。

    **抖音那份 `as_http_url()` 不补协议头**（`//…` 在那边直接得到 None）。
    这个差异是有意的保留，不是待修的 bug：统一它是一次真实行为变更，得拿抖音自己的
    用例当证据，判据写在 `platforms/urls.py` 的 docstring 里。
    """
    return absolute_http_url(value)
