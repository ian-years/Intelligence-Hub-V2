"""抖音的身份识别：`sec_uid` 提取、规范 URL 与中文计数法解析。

**这个模块不碰网络**。跟 302 是 `adapter.py:resolve_share_url()` 的事（那里能注入
`deps.http`，测试用 `httpx.MockTransport` 就能验），这里只负责"给我一个字符串，
能不能认出 sec_uid"。分开是因为 §7.1 那条坑的本质是**纯解析**：
把整条分享短链当 `platform_id` 入库，后面每一步都会"看起来对"却什么都写不下去。

V1 出处：`download_douyin_latest.py` 的 `extract_sec_uid` / `_sec_uid_in_url` /
`canonical_profile_url` / `format_follower_count`。最后那个在 V1 里是**反向**的
（int → "1.2万"，终点是飞书表格的展示单元格），V2 要的是解析，见 `parse_cn_count`。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from pydantic import HttpUrl, TypeAdapter, ValidationError

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.platforms.urls import parse_cn_count  # ADR-0016 转发，见文件末尾

__all__ = [
    "PROFILE_URL_PREFIX",
    "SEC_UID_PREFIX",
    "SHARE_LINK_HOSTS",
    "VIDEO_URL_PREFIX",
    "as_http_url",
    "canonical_profile_url",
    "canonical_video_url",
    "extract_sec_uid",
    "is_douyin_host",
    "is_http_url",
    "is_share_link",
    "parse_cn_count",
    "require_http_url",
    "sec_uid_in_url",
]

SEC_UID_PREFIX = "MS4w"
"""sec_uid 的前缀。base64 的 `"MS4wLjABAAAA"`（版本号 1.0 的 JSON 头部）。

V1 用它做"这串已经是裸 ID 了，不用再从 URL 里认"的判据。
**它不是完整校验**：真 sec_uid 也可能不带这个前缀（平台侧改版过），所以
`extract_sec_uid()` 在前缀不匹配时仍会退回去认 URL 路径段，而不是直接判空。
"""

PROFILE_URL_PREFIX = "https://www.douyin.com/user/"
VIDEO_URL_PREFIX = "https://www.douyin.com/video/"

SHARE_LINK_HOSTS = frozenset({"v.douyin.com"})
"""App 里「复制链接」给的短码域名。

**这种链接里没有任何身份信息**（V1 §7.1）：`https://v.douyin.com/iJxYzAbC/` 只是
一个跳转令牌，必须跟一次 302 才知道它指向谁。拿它当 `platform_id` 入库的后果是
"回写命中 1 条但其实只刷了 updated_at" —— 博主资料永远落不上去，而且不报错。
"""

_HTTP_PREFIXES = ("http://", "https://")

_SEC_UID_IN_PATH = re.compile(r"douyin\.com/(?:share/)?user/([^/?#\s)\]]+)", re.IGNORECASE)
_SEC_UID_IN_QUERY = re.compile(r"(?:^|[?&])sec_uid=([^&#\s)\]]+)", re.IGNORECASE)
_USER_PATH = re.compile(r"^user/([^/?#\s)\]]+)", re.IGNORECASE)

_DOUYIN_HOST = re.compile(r"(?:^|\.)(?:ies)?douyin\.com$", re.IGNORECASE)
"""`douyin.com` 与它的子域，外加 `iesdouyin.com`（端上分享落地页用的就是这一家）。"""


def is_douyin_host(url: object) -> bool:
    """这条 URL 的域名是不是抖音系。**没有主机名的相对路径算 True**。

    页面 JS 里 `a[href]` 有时给的是 `user/MS4w…` 这种相对形式，那时没有 netloc 可比。

    这道闸门是 V2 补的，V1 没有：V1 的 `_sec_uid_in_url` 最后一条兜底是
    "路径以 `user/` 开头就取第二段"，于是 `https://example.com/user/x` 会被认成
    sec_uid=`x` 的合法抖音主页。脏数据一旦入库，后面每一轮都要靠人去猜。
    """
    host = urlparse(str(url or "").strip()).hostname or ""
    # search 而不是 match：`match` 从 0 开始锚，"www.douyin.com" 会因为开头的
    # "www." 被判成非抖音域，于是**所有正常主页链接都认不出**（写完这条时被
    # 契约测试当场抓出来的）。模式里的 `(?:^|\.)` 已经挡住了 "notdouyin.com"。
    return not host or bool(_DOUYIN_HOST.search(host))


def is_http_url(value: object) -> bool:
    """像不像一个 http(s) URL。

    V1 §7.1 的"脏行识别器"：`platform_id` 字段里出现以 `http` 开头的值，
    就说明那是一整条链接被当成 ID 存进去了（历史数据里成批存在）。
    """
    return str(value or "").strip().lower().startswith(_HTTP_PREFIXES)


def sec_uid_in_url(url: object) -> str:
    """从一条 URL 里认 sec_uid，认不出返回空串。

    三种真实形状，按可靠性排序：

    1. `douyin.com/user/<sec_uid>`（含 `/share/user/`）—— 主页链接，最常见。
       **只看路径段**，`?from_tab_name=main` 这类查询串要剥掉。
    2. `...?sec_uid=<sec_uid>&u_code=...` —— 端上分享**落地页**把 ID 挂在 query 上，
       主页链接没有这一项。少了这条分支，落地页链接会被判成"认不出"。
    3. 相对形式 `user/<sec_uid>/` —— 页面 JS 里 `a[href]` 有时给的是相对路径。

    顺序不能反：先 query 再路径的话，一条同时带两者的链接会优先取到 query 里
    那个（往往是"当前登录者"而不是"页面主角"）。

    三条分支之前还有一道**域名闸门**（`is_douyin_host`）：不属于抖音系的链接
    一律认不出，别让 `example.com/user/x` 变成一个看起来合法的 sec_uid。
    """
    text = str(url or "").strip()
    if not text or not is_douyin_host(text):
        return ""
    match = _SEC_UID_IN_PATH.search(text)
    if match:
        return unquote(match.group(1))
    match = _SEC_UID_IN_QUERY.search(text)
    if match:
        return unquote(match.group(1))
    # 没有域名前缀时（相对路径）只剩这一条路
    path = (urlparse(text).path or text).lstrip("/")
    match = _USER_PATH.match(path)
    return unquote(match.group(1)) if match else ""


def extract_sec_uid(value: object) -> str:
    """从裸 ID 或 URL 里取 sec_uid，认不出返回空串。

    裸 ID 的判据是"以前缀开头且不含 `/`"：`/` 一定属于 URL 结构，
    带斜杠的输入还走前缀快路径会把 `MS4wLjABAAAA/whatever` 认成合法 ID。
    """
    text = str(value or "").strip()
    if text.startswith(SEC_UID_PREFIX) and "/" not in text:
        return text
    return sec_uid_in_url(text)


def is_share_link(url: object) -> bool:
    """是不是必须先展开才知道指向谁的短链。

    只对 `SHARE_LINK_HOSTS` 里的域名返回 True。其他域名一律 False ——
    **纯离线判定不该混进无意义的网络请求**（V1 的 `creator_sec_uid` 同一取舍：
    主页链接本来就读得到 sec_uid，跟一次 302 只是白白慢一个来回、
    并且让"离线跑 contract 测试"变成"要看运气"）。
    """
    return (urlparse(str(url or "").strip()).hostname or "").lower() in SHARE_LINK_HOSTS


def canonical_profile_url(sec_uid: str) -> str:
    """规范主页 URL。

    V1 §7.1 的落地形式：库里存的 `profile_url` 必须**含得上** `platform_id`。
    存了一条不含 sec_uid 的链接（分享短链、带 from 参数的落地页），
    下一轮采集就会对不上号，于是同一个人被收录两次。
    """
    return f"{PROFILE_URL_PREFIX}{sec_uid}"


def canonical_video_url(aweme_id: str) -> str:
    return f"{VIDEO_URL_PREFIX}{aweme_id}"


# --------------------------------------------------------------------------- #
# 外部字符串 → 受校验的 URL
# --------------------------------------------------------------------------- #

_HTTP_URL_ADAPTER: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


def as_http_url(value: object) -> HttpUrl | None:
    """页面/平台给出来的字符串 → `HttpUrl`，不像 URL 就返回 None。

    为什么要专门一个函数：`CreatorProfile.avatar_url` 与 `VideoMeta.cover_url`
    的类型是 `HttpUrl`，而抖音页面上这些字段经常是**没有协议头的**
    （`//p3.douyinpic.com/…`）或者干脆是空串。直接塞给 Pydantic 会得到一句
    `ValidationError`，从采集链路冒出来 —— 而"这一位博主的头像 URL 长得不一样"
    不该让整轮采集红掉。None 是合法答案（头像列可空）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _HTTP_URL_ADAPTER.validate_python(text)
    except ValidationError:
        return None


def require_http_url(value: object, *, context: str) -> HttpUrl:
    """`as_http_url()` 的"这里不给 None"版本。

    用于模型里**必填**的 URL（`VideoMeta.webpage_url`）。失败抛 `PlatformError`
    而不是 `ValidationError`：前者带 platform/stage 能进清单的 `failures[]`，
    后者是一条 Pydantic 的字段列表，读的人不知道是哪一条作品、哪个平台出的问题。
    """
    url = as_http_url(value)
    if url is None:
        msg = f"{context} 里应该是一个 http(s) URL，实际是 {str(value)[:120]!r}"
        raise PlatformError("douyin", "parse", msg)
    return url


# --------------------------------------------------------------------------- #
# 粉丝数 / 点赞数：中文计数法 → int
# --------------------------------------------------------------------------- #

# 实现已提到 `platforms/urls.py::parse_cn_count`（ADR-0016：第三个平台进来时，
# 三份重复就不再是权衡）。抖音的 `listing.py` 与契约测试仍从本模块 import 这个名字，
# 所以顶上那次转发要留着 —— 判据、形状清单与"为什么返回 None 而不是 0"都写在那边。
