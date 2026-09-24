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

    两条判据分开，因为要做的动作不同：
    - **一条评论没有 `id`/`idstr` → 整条丢掉并计数**。它进不了唯一键（`ADR-0020` 决定一
      第 1 条会把后面的没 id 评论全撞上），所以宁可不收。
    - 内容空（被删/被折叠）→ **保留**，它是"这条作品下有 N 条评论"里的一条。
      丢掉会让计数与平台的 `data.cursor.all_count` 对不上，那个差值是有信息量的。

    返回的顺序就是接口给的顺序（`sort` 由调用方决定，这里不重排 —— 重排等于替调用方
    做了一个它没做的决定）。
    """
    if not isinstance(rows, list):
        return []
    out: list[VideoCommentDraft] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        comment_id = str(item.get("idstr") or item.get("id") or "").strip()
        if not comment_id:
            continue
        member = _dict_of(item.get("member"))
        out.append(
            VideoCommentDraft(
                platform=PLATFORM,
                platform_comment_id=comment_id,
                content=_plain_text(item.get("content")),
                author_platform_id=_author_id(member),
                author_name=str(member.get("uname") or ""),
                like_count=_as_int(item.get("like_count")),
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


def _author_id(member: dict[str, Any]) -> str | None:
    """评论作者的 mid。`mid` 与 `userid` 两种键都见过，`mid` 优先（它是平台内稳定身份）。"""
    mid = member.get("mid")
    if mid is not None and str(mid).strip():
        return str(mid).strip()
    user_id = member.get("userid")
    return str(user_id).strip() if str(user_id or "").strip() else None


def _metadata_of(item: dict[str, Any]) -> str:
    """留几条"平台说了但我们没有列去放"的信息：是否点赞、是否主楼、回复类型。"""
    payload = {
        "liked": _as_int(item.get("liked")),
        "is_author": _as_int(item.get("reply_tag")),
        "floor": _as_int(item.get("floor")),
    }
    return json.dumps({k: v for k, v in payload.items() if v is not None}, sort_keys=True)


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
        drafts.extend(parse_reply_rows(data.get("replies")))
        if not pages[-1]["rows"] or len(drafts) >= limit:
            break
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
