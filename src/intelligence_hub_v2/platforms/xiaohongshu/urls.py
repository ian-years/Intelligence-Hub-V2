"""小红书的身份与链接：`note_id` / `user_id` 提取、带 `xsec_token` 的笔记 URL、发布时间。

**这个模块不碰网络**（与抖音、B站 那两份 `urls.py` 同构，理由也一样：只有适配器能注入
`deps.http` / 桥，纯函数里藏一次跳转就没法离线验）。

V1 出处：`download_xiaohongshu_latest.py` 的 `extract_note_id` / `extract_user_id` /
`build_note_url` / `build_profile_url` / `infer_published_at` / `format_publish_time`。

两处与 V1 不一样，都是 V2 契约逼出来的：

1. **时间交出的是 aware `datetime`，不是字符串**。V1 一路传 `"%Y-%m-%d %H:%M:%S"` 这种
   无时区文本，V2 的 `videos.published_at` 是 `UTCDateTime`（`data-model.md §2.3`）——
   Feed 排序与 `since` 过滤都靠它。迁移 V1 数据时那批 naive 文本按本机时区解读并把这个
   假设记进 `metadata_json`（`tools/migrate_from_v1.py` 同一口径），这里也照同一口径：
   **页面给的裸时间戳按本机时区解释**，不假装它是没有来源的 UTC。
2. **认不出就返回 `None`，不返回空串**。V1 用空串当"没有"，而空串在 Pydantic 模型里
   是一个合法字符串值 —— 传下去就有人拿 `if published_at:` 判它，两种"没有"会分叉。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, tzinfo
from urllib.parse import quote, urlparse

__all__ = [
    "build_note_url",
    "build_profile_url",
    "extract_note_id",
    "extract_user_id",
    "note_id_from_timestamp",
    "parse_publish_time",
    "published_at_from_note_id",
]

NOTE_ID_PATTERN = re.compile(r"[0-9a-zA-Z]{18,}")
"""笔记 ID 的形状：**至少** 18 位字母数字（V1 同一条）。

与 B站 的 `BV` 定长不同，小红书这里是 `{18,}` 而不是 `{18}`：现网实测既有 24 位的
（`64a…` 后面还跟一截），定死长度会把真笔记判成"不是 ID"。代价是被截断的链接可能被
当成另一条合法 ID —— 所以详情页那一步还要拿页面自报的 `note_id` 对一次
（V1 `XHS:971` 那条"串号就整条丢弃"的判据，住在 `listing.py` 那一层）。
"""

_NOTE_PATH_PATTERN = re.compile(
    r"xiaohongshu\.com/(?:user/profile/[0-9a-zA-Z]+/|explore/|item/|discovery/entry/|search_result/)"
    r"([0-9a-zA-Z]+)"
)
"""从笔记链接里认 ID。五种真实落地页都覆盖（V1 同一张表）。

`user/profile/<uid>/` 这个前缀要吃掉：主页链接里那段数字不是笔记 ID，
不吃掉它会先把 uid 当成 `note_id` 认下来。
"""

_PROFILE_PATH_PATTERN = re.compile(r"xiaohongshu\.com/user/profile/([0-9a-zA-Z]+)")
"""博主主页：路径里那段就是身份 `user_id`（24 位 hex 最常见，但不设长度上限去猜）。"""

_HEX8_PATTERN = re.compile(r"^[0-9a-fA-F]{8}")
"""`note_id` 的前 8 位是十六进制的发布时间戳（V1 的启动器也按同一规则还原）。"""

_UNSAFE_IN_QUERY = ""","""
"""`xsec_token` 里会出现 `=` `/` `+` 这些字符，必须转义后再拼 ——
V1 用的是 `quote(safe="")`，照搬。"""

_LOCAL_ZONE: tzinfo = datetime.now().astimezone().tzinfo or UTC
"""裸时间戳按它解释。算一次就够：这是进程启动时那一刻的本机时区，
跨夏令时边界的长驻进程里它可能过期 —— 本机在 `Asia/Shanghai`，没有夏令时。
`or UTC` 只是给 mypy 的兜底：`astimezone()` 之后的 `.tzinfo` 按类型是可为 None 的。"""

_MILLIS_CUTOFF = 10**12
"""超过这个数的当作**毫秒**时间戳（约等于公元 33658 年的秒数，不会与秒混淆）。"""

_YEAR_2100_EPOCH = 4102444800
"""2100-01-01 的 epoch 秒。超过它就不认为是一个时间：
挡住的是"这串 ID 的前 8 位根本不是时间"，而它长得很像一个合法整数。"""

_PROFILE_PATH_PARTS = 3
"""`/user/profile/<uid>` 这条路径至少要有这几段。"""


def extract_note_id(value: object) -> str:
    """从笔记链接 / 分享串里取 `note_id`；已经是裸 ID 的输入原样识别。认不出返回空串。

    三条来路按顺序试（V1 同一顺序）：

    1. URL 里那五种落地页路径段；
    2. **整串本身就是裸 ID** —— 用户从表格里复制一列出来经常就是这个样子；
    3. 前面两条都没中，再按 `urlparse` 切路径段找 `explore` / `item` / `note` 的下一段。

    第 3 条不是冗余：分享文案里 URL 前面会带一串中文（"复制打开小红书看看…"），
    那条正则吃的是 `xiaohongshu.com/…` 的字面量，被前缀噪声挡掉时第 3 条还能救一次。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _NOTE_PATH_PATTERN.search(text)
    if match and NOTE_ID_PATTERN.fullmatch(match.group(1)):
        return str(match.group(1))
    if NOTE_ID_PATTERN.fullmatch(text):
        return text
    parts = [part for part in (urlparse(text).path or "").split("/") if part]
    for index, part in enumerate(parts):
        if part not in {"explore", "item", "note"} or index + 1 >= len(parts):
            continue
        candidate = parts[index + 1]
        if NOTE_ID_PATTERN.fullmatch(candidate):
            return str(candidate)
    return ""


def extract_user_id(value: object) -> str:
    """取博主 `user_id`：先认主页链接里的路径段，再退到裸 ID。认不出返回空串。

    V1 在这里第一手调 `platform_schema.infer_xhs_user_id()`（V1 自己的一份规则），
    V2 没有那个模块，直接把它的两条判据抄进来：主页 URL 路径段、以及
    "没有域名且长度 8–40 的字母数字"当作已经是 ID。

    **不认分享短链**（`xhslink.com`）：那要先跟一次 302 才知道指向谁，
    属于适配器 `parse_creator_url()` 的编排，纯函数不该藏着网络往返。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _PROFILE_PATH_PATTERN.search(text)
    if match:
        return str(match.group(1))
    parts = [part for part in (urlparse(text).path or "").split("/") if part]
    if len(parts) >= _PROFILE_PATH_PARTS and parts[0] == "user" and parts[1] == "profile":
        return parts[2]
    if not urlparse(text).netloc and re.fullmatch(r"[0-9a-zA-Z]{8,40}", text):
        return text
    return ""


def build_note_url(note_id: str, xsec_token: str = "", *, source: str = "pc_user") -> str:
    """拼**带 `xsec_token` 的**笔记链接。

    这个 token 不是装饰：缺了它，站内会把详情链接跳去风控页，等于一条笔记都取不到
    （V1 注释原话）。它也不是身份 —— 同一条笔记换个 token 还是同一条笔记，
    所以库里存的 `platform_video_id` 只有 `note_id`，token 只在取详情那一刻用。
    """
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        safe = quote(str(xsec_token), safe=_UNSAFE_IN_QUERY)
        url += f"?xsec_token={safe}&xsec_source={source}"
    return url


def build_profile_url(user_id: str) -> str:
    """规范主页 URL。空 `user_id` 给空串 —— 这一条与 V1 一致，因为调用方要写进可空列。

    V1 §7.1 那条坑在小红书同样成立：**库里存的链接必须含得上 `platform_id`**，
    否则下一轮采集对不上号，同一个人被收录两次。所以这个函数是唯一的拼法，
    不接受"传进来什么就存什么"。
    """
    return f"https://www.xiaohongshu.com/user/profile/{user_id}" if user_id else ""


def published_at_from_note_id(note_id: str) -> datetime | None:
    """`note_id` 前 8 位十六进制 = 发布时间戳（页面给不出时间时的第二手）。

    两道合理性闸：`stamp <= 0` 与 `stamp > 410244800`（2100-01-01）都判"不是时间"。
    后者挡的是"这串 ID 根本不是时间开头" —— 没有它，一条 1998 年的笔记会安静地
    排到 Feed 最前面，而库里看不出任何异常。
    """
    stamp = note_id_from_timestamp(str(note_id or ""))
    return _from_epoch(stamp)


def note_id_from_timestamp(note_id: str) -> int | None:
    """`note_id` 前 8 位十六进制 → epoch 秒；不像时间返回 None。"""
    match = _HEX8_PATTERN.match(str(note_id or ""))
    if match is None:
        return None
    try:
        stamp = int(match.group(0), 16)
    except ValueError:  # pragma: no cover - 正则已经保证了字符集
        return None
    return stamp if 0 < stamp <= _YEAR_2100_EPOCH else None


def parse_publish_time(value: object) -> datetime | None:
    """页面给的时间 → aware datetime。认不出返回 None。

    现网一共见过四种形状，全都得吃下（V1 的 `format_publish_time` 同一批）：

    - 毫秒时间戳（`1719000000000`，前端 JS 里最常见）；
    - 秒时间戳（int 或纯数字串）；
    - `2026-01-02` / `2026-01-02 03:04` / `2026-01-02 03:04:05`；
    - 上面几种带斜杠或 `T` 分隔（`2026/01/02`、`2026-01-02T03:04`），
      以及混在一句话里的（"发布于 2026-01-02 03:04"）—— 所以是 `search` 不是 `fullmatch`。

    裸时间戳按 `_LOCAL_ZONE` 解释；已经带日期的那几种**没有时区信息**，
    同样按本机时区补 —— 这是"我们不知道平台怎么说"的诚实读法，
    而不是假装它是 UTC（那会把整批笔记的时间挪 8 小时，`since` 过滤跟着错）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(int(value))
    text = str(value).strip()
    if text.isdigit():
        return _from_epoch(int(text))
    return _from_date_text(text)


def _from_date_text(text: str) -> datetime | None:
    """人读的那几种日期形状（`2026-01-02 03:04:05` 一族）→ aware datetime。"""
    match = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", text)
    if match is None:
        return None
    cleaned = match.group(0).replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=_LOCAL_ZONE)
        except ValueError:
            continue
    return None


def _from_epoch(stamp: int | None) -> datetime | None:
    """epoch 秒或毫秒 → aware datetime；越界与非正数返回 None。"""
    if stamp is None or stamp <= 0:
        return None
    if stamp > _MILLIS_CUTOFF:  # 毫秒时间戳
        stamp //= 1000
    if stamp > _YEAR_2100_EPOCH:  # 认定不是时间
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=_LOCAL_ZONE)
    except (OSError, OverflowError, ValueError):
        return None
