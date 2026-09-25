"""B站 评论抓取（V2.1 T4.1）。

V1 源：`download_bili_following_latest.py::fetch_comments`（`x/v2/reply`，翻到够数为止）。
这里保留它的三件事，改掉它的两件事：

**保留**
1. **先拿 `aid` 再取评论**。`oid` 要的是数字 aid，传 bvid 会回
   `code:-400 请求错误` —— 那句报错长得像"接口挂了"，实际是身份用错了。
   V1 在 aid 缺失时直接抛而不发那个注定失败的请求，这里照做。
2. **每一页的 `code` / `message` / `ttl` 都留档**。V1 留这一手是因为 B站 的
   "业务失败"编在 HTTP 200 里，不留档就只剩一句"评论是空的"。
3. **翻到够数为止 + 分页参数**。`ps` 上限 20 是 V1 实测的（给更大值接口自己会缩），
   所以这里 `page_size = min(20, ...)` 与 V1 同档。

**改掉**
1. **不用 `urllib`，走 `deps.http`**（那个 `httpx.AsyncClient` 是全平台共用的一个，
   带代理与超时预算）。V1 每条链路自己开连接。
2. **产出 `VideoCommentDraft`，不落文件**。V1 直接把 dict 交给飞书那条路，
   V2 的落点是 `video_comments` 表（`ADR-0020`），而"平台到底给了哪些字段"这件事
   由 `parse_reply_payload` 的返回类型说，不由下游猜。

一条**故意没做**的事：不抓楼中楼。V1 也只抓顶层（`fetch_comments` 的注释写的是
"Max top-level comments"），而子回复是另一个接口、另一份配额。要抓的话它是新的一格，
不是在这里顺手 —— 顺手做会得到一个"limit=50 实际拿到 300 条"的行为变化，
而调用方看不出来。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from intelligence_hub_v2.errors import ListError
from intelligence_hub_v2.models.engagement import VideoCommentDraft
from intelligence_hub_v2.platforms.bilibili import listing
from intelligence_hub_v2.platforms.bilibili.urls import (
    media_comment_url,
    reply_api_url,
)

PLATFORM = "bilibili"

SORT_BY_NAME: dict[str, int] = {"hot": 2, "new": 0}
"""`fetch_comments(sort=...)` 那两个名字 → B站 `sort` 参数（2 按赞、0 按时）。

契约面（`platforms/base.py`）只承认 `"hot"` / `"new"` 两个词：平台的数字档位是这一家的
实现细节，不该漏到 handler 与任务参数里去。认不出来时**由调用方决定怎么报错**，
这一层用 `.get(sort, 2)` 之前先由适配器显式判（见 `BilibiliAdapter.fetch_comments`），
免得一个拼错的 `"newst"` 静默按赞排 —— 那会长得完全像"热评就是这些"。
"""

MAX_PAGE_SIZE = 20
"""`ps` 的实测上限。超过它接口会自己按 20 给，而我们这边的翻页步长就会与它不一致 ——
表现为"要 50 条只拿到 40 条且再也没翻页"。"""

MAX_PAGES = 50
"""一趟最多翻多少页。**这是一道保险，不是一个语义**：`limit=2000` 这种输入
在没有它的情况下会把一个博主的评论区刷成一次压测。触顶时如实说"到页数上限为止"，
不许装作拿全了。"""


class CommentApiError(ListError):
    """评论接口没给出可用的 `data`。与 `ViewApiError` 同一族，只是发生在评论这一路。"""


def _dict_of(value: object) -> dict[str, Any]:
    """接口里"应该是个对象"的那几处。**不是对象就当空**，而不是让它冒成一个 AttributeError。

    写成 `x if isinstance(x, dict) else {}` 的内联版本会让 mypy 把类型收窄成
    `Any | dict | None` 并在每个 `.get()` 上报 union-attr —— 这个助手同时解决
    读起来与查出来两件事。
    """
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int | None:
    """平台给的计数可能是 int、数字串，也可能**根本没有这个键**。

    缺键返回 None 而不是 0：`ADR-0020` 里那条"没有 ≠ 是零"。评论的点赞数为 0
    和"这一版接口没回这个字段"在两件事上答案不同 —— 后者不该被当成前者。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("+").isdigit():
        return int(value.strip().lstrip("+"))
    return None


def _as_aware(value: object) -> datetime | None:
    """B站 的评论时间是**秒级 epoch**（UTC 语义的 Unix 时间戳）。"""
    stamp = _as_int(value)
    if stamp is None or stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp, tz=UTC)


def parse_reply_rows(rows: object) -> list[VideoCommentDraft]:
    """接口 `data.replies` → 草稿列表。

    **键名以现网真响应为准**（`tests/fixtures/bilibili/reply_page.json`，2026-09-25 捕获）：
    身份是 `rpid_str`（字符串形状，优先）或 `rpid`（同一值的 int 形状），点赞是 `like`，
    楼中楼条数是 `rcount`。这里曾经读的是 `idstr` 与 `like_count` —— **那两个键名是编的**，
    于是真接口一来每条行都没身份、全被丢掉，症状是"抓到 0 条评论"而接口 `code=0`，
    同一条作品的 `stat.reply` 明明写着 11。合成 fixture 挡不住它，因为它自己就是照
    那份猜测写的（这一条已进 `docs/lessons.md`）。

    两条判据分开，因为要做的动作不同：
    - **一条评论没有 `rpid_str`/`rpid` → 整条丢掉并计数**。它进不了唯一键（`ADR-0020`
      决定一第 1 条会把后面的没 id 评论全撞上），所以宁可不收。
      "整批都没收"是另一件事，由 `fetch_top_level_comments` 那道闸响出来。
    - 内容空（被删/被折叠）→ **保留**，它是"这条作品下有 N 条评论"里的一条。
      丢掉会让计数与平台的 `data.page.count` 对不上，那个差值是有信息量的。

    返回的顺序就是接口给的顺序（`sort` 由调用方决定，这里不重排 —— 重排等于替调用方
    做了一个它没做的决定）。
    """
    if not isinstance(rows, list):
        return []
    out: list[VideoCommentDraft] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        comment_id = str(item.get("rpid_str") or item.get("rpid") or "").strip()
        if not comment_id:
            continue
        member = _dict_of(item.get("member"))
        out.append(
            VideoCommentDraft(
                platform=PLATFORM,
                platform_comment_id=comment_id,
                content=_plain_text(item.get("content")),
                author_platform_id=_author_id(member, item),
                author_name=str(member.get("uname") or ""),
                like_count=_as_int(item.get("like")),
                reply_count=_as_int(item.get("rcount")),
                published_at=_as_aware(item.get("ctime")),
                metadata_json=_metadata_of(item),
            )
        )
    return out


def _plain_text(content: object) -> str:
    """`content` 正常是一个对象（`{"message": "..."}`），不是字符串。

    两种形状都见过（接口版本差异），所以两边都要吃：把它当字符串直接 `str()`
    会得到一整坨 repr 进库，那比丢文案更难查。
    """
    if isinstance(content, dict):
        return str(content.get("message") or "")
    return str(content or "")


def _author_id(member: dict[str, Any], item: dict[str, Any]) -> str | None:
    """评论作者的 mid。**顶层 `mid`/`mid_str` 优先，`member.mid` 兜底**（V1 生产代码同序）。

    顶层那两个是真响应里有的（`mid_str` 是字符串形状，避开 19 位 mid 过 int 的写法分歧），
    而 `member` 里那一份在某些折叠行上会缺 —— 只读一处的话作者身份会静默变 NULL，
    而"这条评论是谁说的"一旦为 NULL，跨平台身份归并那一族就再也接不上了。
    """
    for source in (item.get("mid_str"), item.get("mid"), member.get("mid"), member.get("userid")):
        if source is not None and str(source).strip():
            return str(source).strip()
    return None


def _metadata_of(item: dict[str, Any]) -> str:
    """留几条"平台说了但我们没有列去放"的信息，**全部取现网真有的键**。

    - `up_action`：UP 主是否回复/点赞过这条（真响应里是个对象，不是布尔）。
      它的价值是"这条是 UP 亲自回过的"，看热评排序时想跳过它跳不过。
    - `root` / `parent`：楼中楼归属。顶层列表里 `root=0` 就是主楼，
      但折叠上来的子行不是 —— 没有这两项，"11 条"与"库里 3 条"的差值解释不了。
    - `invisible`：平台标了不可见（删/折叠），仍然保留这一条，理由同 docstring 第二条。

    原来这三项写的是 `liked` / `reply_tag` / `floor` —— 现网**一个都不存在**，
    于是这一列永远写成 `{}`，看起来像"没有附加信息"，实际是字段名错了。
    只留有值的几项；全空时留 `{}` 而不是 `null`（读的一侧两种写法会炸）。
    """
    up_action = item.get("up_action")
    payload: dict[str, object] = {}
    if isinstance(up_action, dict) and any(up_action.values()):
        payload["up_action"] = {str(k): bool(v) for k, v in sorted(up_action.items())}
    for key in ("root", "parent", "invisible"):
        value = item.get(key)
        if value not in (None, 0, "", False):
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


async def fetch_top_level_comments(
    http: httpx.AsyncClient,
    *,
    aid: str | int,
    bvid: str,
    limit: int = 50,
    sort: int = 2,
    budget_seconds: float = 30.0,
) -> tuple[list[VideoCommentDraft], list[dict[str, Any]]]:
    """翻到够数为止。返回 `(草稿, 每一页的 code/message/ttl 留档)`。

    `sort=2` 是按点赞（V1 同档），`sort=0` 是按时间 —— 由调用方决定，
    因为"看热评"与"看最新"是两个问题，这一层不替它选。

    **`code != 0` 直接抛**，且把 code/message 原文带进异常：B站 把业务失败编在
    HTTP 200 里，只看状态码会把"啥都木有"当成"评论是空的"（那是最容易被读成
    "这条作品没人评论"的一种假绿）。
    """
    page_size = max(1, min(MAX_PAGE_SIZE, limit))
    drafts: list[VideoCommentDraft] = []
    pages: list[dict[str, Any]] = []
    rows_seen = 0
    # 接口给过的行数总和（不论能不能解析）。它只为下面那道"全丢"闸存在。
    for page_no in range(1, MAX_PAGES + 1):
        payload = await _get_page(
            http,
            aid=aid,
            bvid=bvid,
            page_no=page_no,
            page_size=page_size,
            sort=sort,
            budget_seconds=budget_seconds,
        )
        data = _dict_of(payload.get("data"))
        pages.append(
            {
                "pn": page_no,
                "code": payload.get("code"),
                "message": payload.get("message"),
                "ttl": payload.get("ttl"),
                "rows": len(data.get("replies") or [])
                if isinstance(data.get("replies"), list)
                else 0,
            }
        )
        rows_seen += pages[-1]["rows"]
        drafts.extend(parse_reply_rows(data.get("replies")))
        if not pages[-1]["rows"] or len(drafts) >= limit:
            break
    if rows_seen and not drafts:
        # **接口给了行、我们一条都没认出身份 → 这是解析器坏了，不是"这条作品没评论"。**
        # 少了这道闸，形状就是 2026-09-25 那次：真响应的键名从 `idstr` 变成 `rpid_str`
        # 之后，每一批行都被静默丢掉，清单上只留 `comments_new: 0` 且 failures 为空 ——
        # 而同一轮写的快照里 `comment_count` 明明是 11。AGENTS.md §1.3 禁的就是这个形状。
        msg = (
            f"评论接口给了 {rows_seen} 行，但一条都没有可入库的身份"
            f"（`rpid_str`/`rpid` 全为空，bvid={bvid}）："
            "这是接口换了字段名，不是这条作品没人评论"
        )
        raise CommentApiError(PLATFORM, "comment", msg)
    return drafts[:limit], pages


async def _get_page(
    http: httpx.AsyncClient,
    *,
    aid: str | int,
    bvid: str,
    page_no: int,
    page_size: int,
    sort: int,
    budget_seconds: float,
) -> dict[str, Any]:
    url = reply_api_url(aid, page_no=page_no, page_size=page_size)
    try:
        response = await http.get(
            f"{url}&sort={sort}",
            headers=listing.api_headers(referer=media_comment_url(bvid)),
            timeout=budget_seconds,
        )
    except httpx.HTTPError as exc:
        msg = f"取 {bvid} 的评论失败（第 {page_no} 页）：{type(exc).__name__}: {exc}"
        raise CommentApiError(PLATFORM, "comment", msg) from exc
    try:
        payload: Any = response.json()
    except ValueError:
        snippet = response.text[:200]
        msg = f"{bvid} 的评论接口回的不是 JSON（HTTP {response.status_code}）：{snippet!r}"
        raise CommentApiError(PLATFORM, "comment", msg) from None
    if not isinstance(payload, dict):
        msg = f"{bvid} 的评论接口形状不对：拿到 {type(payload).__name__}"
        raise CommentApiError(PLATFORM, "comment", msg)
    # **业务失败编在 HTTP 200 里**：`{"code": -400, "message": "请求错误"}`。
    # 只看状态码会把它读成"这条作品没人评论" —— 那是最容易骗过所有人的一种假绿，
    # 所以这一层就红，且 code/message 原文必须进消息（三种原因要修的东西完全不同）。
    code = payload.get("code")
    if code != 0:
        msg = f"B站 评论接口回 code={code} message={payload.get('message')!r}（{bvid}）"
        raise CommentApiError(PLATFORM, "comment", msg)
    return payload
