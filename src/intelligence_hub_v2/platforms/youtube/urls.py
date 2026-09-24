"""YouTube 的身份与链接：`channel_id` / `handle` / `video_id` 提取与规范 URL。

与 B站/抖音那份 `urls.py` 同构：**纯解析、不碰网络**。身份要靠跳转才知道的
那一类（`@手柄`、`youtu.be/<id>`）在这里只判"认不出"，跟一次请求是适配器的事 ——
只有那里能注入 `deps.http`，测试才能验"短链真的跟了一次"。

三种身份词汇，别混（V1 只用到了前两种）：

- `channel_id`：`UC` + 22 位，**唯一稳定的博主身份**，入库的就是它。
- `handle`：`@name`，人类可读、可被博主改，改了之后同一个 `UC…` 换名字。
  所以它只能是"解析过程中的中间量"，不能当 `platform_id` —— 那正是 V1 §7.1
  那一族（拿一个会变的东西当主键）。
- `video_id`：11 位 `[0-9A-Za-z_-]`，作品身份。
"""

from __future__ import annotations

import re

from pydantic import HttpUrl

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.platforms.urls import absolute_http_url

__all__ = [
    "as_http_url",
    "canonical_video_url",
    "channel_url",
    "channel_videos_url",
    "enumeration_url",
    "extract_channel_id",
    "extract_handle",
    "extract_video_id",
    "is_channel_id",
    "is_video_id",
    "require_model_url",
]

PLATFORM = "youtube"

_BASE = "https://www.youtube.com"

_CHANNEL_ID_RE = re.compile(r"\b(UC[0-9A-Za-z_-]{6,})\b")
"""频道 ID。`{6,}` 而不是定长 22 —— **V1 同一条**（`CHANNEL_ID_RE`）。

真实频道 ID 是 `UC` + 22 位定长，看着像可以判死，但判紧的后果比判松更糟：
一条被前端截断、或被 yt-dlp 以别的形状给出的 ID 会变成"认不出身份"，
症状是"这位博主一直解析失败"，而用户看不出是我们太挑。
代价写在 docstring 里而不是藏着：被截断的串可能被认成另一个合法 ID，
所以**详情那一步必须拿页面/yt-dlp 自报的 `channel_id` 对一次**
（见 `adapter.fetch_creator_profile` 里那次校验）。
"""

_VIDEO_ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")
"""作品 ID 的形状：**恰好 11 位**。

两条用处，都判得紧是有意的：

1. 从用户粘贴的文本里认作品 —— 判松的代价是拿着一个残缺/超长的串去下载，
   报出来的是"视频不存在"而不是"链接被切坏了"（B站 那个 `BV` + 正好 10 位同源）。
2. `listing.entry_to_video` 也用它筛 yt-dlp 报回来的 `id`。判紧的代价写在
   `listing.entry_to_video` 里（YouTube 真要改长度时整批会被丢），
   换来的是"整份 playlist 的对象被当成一条作品"这种孤儿进不了库
   —— 那是 2026-09-24 那条 `-J`/`-j` P0 的镜像形状。
"""

_HANDLE_RE = re.compile(r"(?:^|[/?])@([0-9A-Za-z._-]{2,})")
"""`youtube.com/@handle`。`-` 与 `.` 都在合法字符集里。"""

_USER_OR_C_RE = re.compile(r"(?:^|/)((?:user|c)/[0-9A-Za-z._%-]+)(?:[/?#]|$)")
"""老式 `/{user|c}/<name>` 链接。V1 的 `channel_videos_url` 认这两种，
它们**不含 UC 身份**，所以只能当"去哪儿查"的线索。"""

_WATCH_PATH_RE = re.compile(r"/(?:watch|shorts|live|embed)/([0-9A-Za-z_-]{6,})")
_SHORT_WATCH_RE = re.compile(r"youtu\.be/([0-9A-Za-z_-]{6,})")
_ID_QUERY_RE = re.compile(r"[?&]v=([0-9A-Za-z_-]{6,})")

_TAB_SEGMENTS = frozenset({"videos", "shorts", "live", "playlists", "streams", "featured"})
"""频道主页的几个标签页。已经在其中任何一个上就**不再补** `/videos`
（`/videos/videos` 是 404，而它的症状是"这位博主一条作品都没有"）。"""


def is_channel_id(value: object) -> bool:
    """这一串是不是一个频道 ID（`UC…`）。"""
    text = str(value or "").strip()
    return bool(_CHANNEL_ID_RE.fullmatch(text))


def is_video_id(value: object) -> bool:
    """这一串是不是一个作品 ID（正好 11 位）。"""
    text = str(value or "").strip()
    return bool(_VIDEO_ID_RE.fullmatch(text))


def extract_channel_id(value: object) -> str:
    """从主页链接、`platform_id` 或任意文本里认 `UC…`，认不出返回空串。

    先 `fullmatch` 再 `search`（B站 那份 `extract_bvid` 同一顺序）：
    裸 ID 是最常见的输入，而 `search` 那一步是为了带噪声的粘贴内容
    （分享文案、表格里的一列、yt-dlp 的 `--print` 输出行）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if is_channel_id(text):
        return text
    match = _CHANNEL_ID_RE.search(text)
    return match.group(1) if match else ""


def extract_handle(value: object) -> str:
    """`@手柄`（不含 `@`），认不出返回空串。

    **不带 `@` 返回**是为了让调用方能直接拼 URL 与比较；
    要还原成可展示的形式自己补（`display_name` 那类）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _HANDLE_RE.search(text)
    return match.group(1) if match else ""


def extract_video_id(value: object) -> str:
    """从作品链接里认 `video_id`，认不出返回空串。

    四种真实落地页 + 裸 ID：`watch?v=`、`/shorts/<id>`、`/live/<id>`、
    `/embed/<id>`、`youtu.be/<id>`。

    最后那个"裸 ID"是**故意不做**的：11 位 `[0-9A-Za-z_-]` 的裸串太容易撞
    （一个随机短码、一个被截断的 handle 都像），而"用户粘了个什么都不像的东西，
    我们硬说它是某条作品"的排查成本比"认不出，请粘完整链接"高得多。
    裸 ID 的合法来路是 yt-dlp 的 JSON 输出，那条走 `listing.entry_to_card`。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    for pattern in (_ID_QUERY_RE, _WATCH_PATH_RE, _SHORT_WATCH_RE):
        match = pattern.search(text)
        if match is None:
            continue
        candidate = match.group(1)
        return candidate if is_video_id(candidate) else ""
    return ""


def channel_url(channel_id: str) -> str:
    """频道主页。`profile_url` 用它，**不带 `/videos`** —— 那是给用户看的地址。"""
    return f"{_BASE}/channel/{channel_id}"


def channel_videos_url(channel_id: str) -> str:
    """作品标签页。枚举要的是这一条：主页落在精选/动态流上，yt-dlp 抽不出作品。"""
    return f"{channel_url(channel_id)}/videos"


def handle_videos_url(handle: str) -> str:
    """`@手柄` 的作品标签页。只在"还没有 UC 身份、要靠它反查"时用。"""
    name = handle if handle.startswith("@") else f"@{handle}"
    return f"{_BASE}/{name}/videos"


def legacy_path_videos_url(path: str) -> str:
    """老式 `/user/<name>` 与 `/c/<name>` 链接补上 `/videos`。"""
    clean = str(path or "").strip().strip("/")
    return f"{_BASE}/{clean}/videos"


def enumeration_url(platform_id: str, *, homepage_url: str | None = None) -> str:
    """把"入库的那个身份"落成一个可以枚举的地址。

    三条来路，顺序是**有意的**：

    1. `UC…` → `/channel/UC…/videos`（V1 的第一优先，也是唯一稳定的那条）
    2. 一条**作品**链接（`watch?v=` / `youtu.be/` / `/shorts/` / `/live/` / `/embed/`）
       → 那条作品本身。`parse_creator_url` 要的就是这一格：粘"这个 UP 发过这样一条"
       进来的人，身份要靠这条作品反查
    3. `@手柄` 或 `user/x`、`c/x` → 对应地址（救的是"V1 库里那批没有 UC 的记录"，
       V1 的 `list_tracked_channels` 正是因为这个才留了 `homepage_url` 那一条退路）
    4. 都不认时，传进来是完整 URL 就只取它的 scheme/主机/路径并补 `/videos`

    第 4 条**不会**为一条已经带 `/videos|/shorts|/live|/playlists` 的地址再加一次
    （V1 `channel_videos_url` 同一判据）：`/channel/UC…/videos/videos` 是一个不存在
    的页面，症状是"枚举恒空"而 yt-dlp 报的是 404，读起来像"这位博主没作品"。
    """
    text = str(platform_id or "").strip()
    channel_id = extract_channel_id(text)
    if channel_id:
        return channel_videos_url(channel_id)
    video_id = extract_video_id(text)
    if video_id:
        return canonical_video_url(video_id)
    if text.startswith("@"):
        return handle_videos_url(text)
    legacy = _USER_OR_C_RE.search(text)
    if legacy is not None:
        return legacy_path_videos_url(legacy.group(1))
    handle = extract_handle(text)
    if handle:
        return handle_videos_url(handle)
    return videos_tab_of_page(text, homepage_url)


def videos_tab_of_page(page: str, homepage_url: str | None) -> str:
    """最后一条退路：把"看起来是个页面"补成它的 `/videos` 标签页。

    单独一个函数是为了让 `enumeration_url` 的分支数留在可读范围内，
    也是因为这一条是**唯一**会在这里抛"不是合法 URL"的分支。
    """
    fallback = str(homepage_url or "").strip() or str(page or "").strip()
    if not fallback:
        msg = "既没有频道 ID 也没有主页链接，拼不出可枚举的地址"
        raise PlatformError(PLATFORM, "parse", msg)
    url = require_model_url(fallback, context="YouTube 频道主页")
    page_path = str(url.path).rstrip("/")
    if page_path.split("/")[-1] in _TAB_SEGMENTS:
        return str(url)
    return str(url).rstrip("/") + "/videos"


def canonical_video_url(video_id: str) -> str:
    """作品主页。查询串形式（`watch?v=`）而不是 `/shorts/…`：
    短视频与直播的专用路径都接受 `watch?v=`，反过来不成立。"""
    return f"{_BASE}/watch?v={video_id}"


def as_http_url(value: object) -> HttpUrl | None:
    """外部字符串 → `HttpUrl`，认不出返回 None。实现住在共用层（ADR-0016）。"""
    return absolute_http_url(value)


def require_model_url(value: object, *, context: str) -> HttpUrl:
    """`as_http_url()` 的"这里不给 None"版本（模型里的必填 URL 字段用）。

    抛 `PlatformError` 而不是 `ValidationError`：前者带 platform/stage，
    能进清单的 `failures[]`；后者是一串字段名，读的人不知道是哪一步出的问题。
    """
    url = as_http_url(value)
    if url is None:
        msg = f"{context} 应该是一个 http(s) URL，实际是 {str(value)[:120]!r}"
        raise PlatformError(PLATFORM, "parse", msg)
    return url
