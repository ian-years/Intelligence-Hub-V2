"""B站 评论：抓取（假 httpx transport）+ 入库（真 SQLite）。ADR-0020 / 计划 T4.1。

这一档为什么是"真 HTTP 客户端 + 假 transport"而不是 mock 到函数层：
`httpx.MockTransport` 走的是真的请求构造与响应解析，所以"URL 拼错""参数名写错"
"翻页步长与 `ps` 不一致"这类错误仍然会红；而 mock 掉 `_get_page` 就把这些一起放过了。

三条断言写成了关系而不是样本，因为它们是这条链上唯一值钱的东西：
接口给几行、有多少行带 id，落库就应该有几条；同一批再抓一次，新增必须是 0。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from intelligence_hub_v2.errors import ListError
from intelligence_hub_v2.models.engagement import VideoCommentDraft
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.platforms.bilibili.comments import (
    MAX_PAGE_SIZE,
    CommentApiError,
    fetch_top_level_comments,
    parse_reply_rows,
)
from intelligence_hub_v2.storage.db import SqliteStorage

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "bilibili"

pytestmark = pytest.mark.integration


def _row(
    comment_id: object = "111",
    *,
    content: object = "写得好",
    uname: object = "某人",
    mid: object = 4242,
    like: object = 7,
    ctime: object = 1_719_000_000,
) -> dict[str, Any]:
    """一行评论，**按 2026-09-25 现网真响应的键名写**。

    这里原来是 `"idstr"` + `"like_count"` —— 那两个键名是**编的**（真接口给的是
    `rpid`/`rpid_str` 与 `like`）。合成 fixture 把猜测钉成了契约：解析器读编的键、
    用例喂编的键，两边自洽地全绿，而真接口一来就 100% 丢行。
    现在这份的键名以 `tests/fixtures/bilibili/reply_page.json`（真捕获）为准。

    `comment_id=None` 与 `""` 要落成**空串**而不是 `"None"`：`str(None)` 是个非空字符串，
    用它当 id 等于替"没有身份的那一行"编一个身份，而这一族用例的存在理由就是
    "没身份的行必须被丢掉"（第一版就栽在这里，症状是"该丢的没丢"）。
    """
    sid = "" if comment_id is None else str(comment_id)
    row: dict[str, Any] = {
        "rpid": int(sid) if sid.isdigit() else sid,
        "rpid_str": sid,
        "content": {"message": content},
        "member": {"uname": uname, "mid": mid},
        "like": like,
        "rcount": 0,
        "ctime": ctime,
        "root": 0,
        "parent": 0,
        "up_action": {"like": False, "reply": False},
    }
    return row


def _transport(pages: list[dict[str, Any]]) -> tuple[httpx.AsyncClient, list[str]]:
    """按请求顺序发不同页，并把问到的 URL 记下来。"""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        index = len(seen) - 1
        body = pages[index] if index < len(pages) else {"code": 0, "data": {"replies": []}}
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def _video(storage: SqliteStorage, bvid: str) -> int:
    row = await storage.videos.insert(
        VideoDraft(platform="bilibili", platform_video_id=bvid, title=f"t-{bvid}", creator_id=None)
    )
    return row.id


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_a_row_without_an_id_is_dropped_and_the_rest_survive() -> None:
    """**没有 id 的行不收，其它一行不丢**。

    判据是关系：`rows` 里有几个非空 id → 草稿就有几条。写死"3 条"的那种断言
    挡不住"顺手把没有 id 的也收了" —— 而那正是会撞唯一键的那一类（ADR-0020 决定一）。
    """
    rows = [_row("a"), _row(""), _row(None), _row("b"), {"content": {"message": "没身份"}}]
    with_id = sum(1 for item in rows if str(item.get("rpid_str") or "").strip())
    drafts = parse_reply_rows(rows)
    assert len(drafts) == with_id == 2


def test_the_platform_s_two_content_shapes_both_land_as_plaintext() -> None:
    """`content` 既可能是 `{"message": …}` 也可能是裸字符串 —— 两种都不许变成 repr。

    把它 `str()` 一把梭会得到一整坨 `{'message': '…'}` 进库，
    那比丢文案更难查：看板上有内容，只是没人读得懂。
    """
    drafts = parse_reply_rows(
        [
            {"rpid_str": "1", "content": {"message": "对象形状"}},
            {"rpid_str": "2", "content": "字符串形状"},
        ]
    )
    assert [item.content for item in drafts] == ["对象形状", "字符串形状"]


def test_a_missing_count_stays_none_and_does_not_become_zero() -> None:
    """ "平台没回这个字段"与"这个字段是 0"是两个答案。"""
    # 键名是 `like`（真响应），不是 `like_count`：读错键的 symptoms 是"永远 None"。
    drafts = parse_reply_rows([{"rpid_str": "1", "content": "x", "like": 0}])
    assert drafts[0].like_count == 0
    without = parse_reply_rows([{"rpid_str": "2", "content": "x"}])
    assert without[0].like_count is None
    assert without[0].reply_count is None


def test_the_epoch_time_comes_back_timezone_aware() -> None:
    """`ctime` 是秒级 epoch，交回来必须是 aware —— 库里那一列是 UTCDateTime。

    naive 的时间戳会在本机时区与 UTC 之间二选一，而选错的那一方会把所有评论
    排到发布时刻之前（`published_at < videos.published_at`），且没有任何异常。
    """
    drafts = parse_reply_rows([_row("1")])
    stamp = drafts[0].published_at
    assert isinstance(stamp, datetime) and stamp.tzinfo is not None


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #


async def test_the_page_size_never_exceeds_what_the_api_honours() -> None:
    """`limit=200` 也只按 `MAX_PAGE_SIZE` 翻页。

    接口自己会把 `ps` 缩到 20，而我们这边按 200 算步长 —— 结果是"要 200 条
    只拿到 20 条且只翻了一页"，看起来像评论区就只有 20 条。
    """
    one_page = {"code": 0, "data": {"replies": [_row(f"i{n}") for n in range(20)]}}
    client, seen = _transport([one_page] * 10)
    async with client:
        drafts, pages = await fetch_top_level_comments(client, aid="998", bvid="BV1xx", limit=200)
    assert len(drafts) == 200
    assert seen and f"ps={MAX_PAGE_SIZE}" in seen[0]
    assert len(pages) == 10


async def test_a_business_failure_inside_http_200_raises_with_the_original_text() -> None:
    """`code:-400` 混在 200 里。**只看状态码会把它读成"这条作品没人评论"**。

    异常消息要带 code 与 message 原文 —— "评论接口失败"那句没法用来判断
    是 aid 传错、视频不存在，还是被风控了，而那三种要修的东西完全不同。
    """
    client, _seen = _transport([{"code": -400, "message": "请求错误", "data": None}])
    async with client:
        with pytest.raises(CommentApiError) as caught:
            await fetch_top_level_comments(client, aid="x", bvid="BV1xx", limit=5)
    assert "-400" in str(caught.value) and "请求错误" in str(caught.value)


async def test_a_json_that_is_not_json_reports_the_body_snippet() -> None:
    """风控时这一路回的是一整页 HTML（与 `fetch_view` 同一形状），要能一眼看出来。"""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>安全检测"))
    ) as client:
        with pytest.raises(ListError, match="不是 JSON"):
            await fetch_top_level_comments(client, aid="1", bvid="BV1xx", limit=5)


async def test_it_stops_paging_once_a_page_returns_nothing() -> None:
    """空页 = 到底了。继续翻只会在同一个空页上打转。"""
    client, seen = _transport(
        [
            {"code": 0, "data": {"replies": [_row("a")]}},
            {"code": 0, "data": {"replies": []}},
            {"code": 0, "data": {"replies": [_row("b")]}},
        ]
    )
    async with client:
        drafts, pages = await fetch_top_level_comments(client, aid="1", bvid="BV1xx", limit=50)
    assert [item.platform_comment_id for item in drafts] == ["a"]
    assert len(seen) == len(pages) == 2


def test_the_real_reply_payload_produces_real_drafts() -> None:
    """现网真响应（`reply_page.json`，2026-09-25 捕获）→ 三条草稿，身份是 `rpid_str`。

    这一条是那个 bug 的直接看护：真响应的键名是 `rpid`/`rpid_str`/`like`，
    解析器读错键不会红在语法上、只会红在这里 —— 所以**必须有一份真捕获**，
    合成件挡不住（它自己就是照错的键名编的）。
    断言写成"与 fixture 里的行数/值逐项相等"，不写死数字：换一份捕获它还在看同一件事。
    """
    payload = json.loads((FIXTURES / "reply_page.json").read_text(encoding="utf-8"))
    rows = payload["data"]["replies"]
    drafts = parse_reply_rows(rows)

    assert len(drafts) == len(rows) == 3, "真响应三行、草稿三条，一条都不许丢"
    assert [d.platform_comment_id for d in drafts] == [str(r["rpid_str"]) for r in rows]
    assert [d.like_count for d in drafts] == [r["like"] for r in rows]
    assert all(d.content.strip() for d in drafts), "正文要落在 content.message，不是空串"
    assert all(d.author_name for d in drafts)
    assert drafts[0].published_at is not None and drafts[0].published_at.tzinfo is not None


async def test_a_page_whose_rows_all_lack_an_identity_is_a_failure_not_a_zero() -> None:
    """接口给了行、我们一条身份都认不出来 → **抛**，不许回"0 条，一切正常"。

    这一条就是那个 bug 的第二次机会。原来的形状是：行有 3 条、草稿 0 条、
    清单上 `comments_new: 0` 且 failures 为空 —— 而同一轮的 stat 明明写着 11 条评论。
    那是 AGENTS.md §1.3 说的"看起来在跑"：唯一的处置是把这种"全丢"变成一条响的失败。
    """
    rows = [{"content": {"message": "有正文但没身份"}, "like": 0} for _ in range(3)]
    client, _seen = _transport([{"code": 0, "data": {"replies": rows, "page": {"count": 3}}}])
    async with client:
        with pytest.raises(CommentApiError) as caught:
            await fetch_top_level_comments(client, aid="1", bvid="BV1xx", limit=5)
    text = str(caught.value)
    assert "3" in text, f"要把丢掉的行数说出去：{text}"


# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #


async def test_the_number_of_rows_on_disk_equals_the_number_the_api_gave(
    storage: SqliteStorage,
) -> None:
    """抓来几条 → 库里就该有几条。第二次抓同一批 → 新增 0、更新 N。

    这条是"幂等"的全部含义：不是"第二次不报错"（那用 `INSERT OR IGNORE` 也能做到，
    代价是评论被编辑之后再也不会更新），而是**内容跟上了、行数没长**。
    """
    video_id = await _video(storage, "BV1AA")
    drafts = [
        VideoCommentDraft(platform="bilibili", platform_comment_id=f"c{n}", content=f"第{n}层")
        for n in range(4)
    ]
    inserted, updated = await storage.video_comments.upsert_many(video_id, drafts)
    assert (inserted, updated) == (4, 0)
    assert await storage.video_comments.count_for_video(video_id) == 4

    again = [
        VideoCommentDraft(platform="bilibili", platform_comment_id=f"c{n}", content=f"改过的{n}")
        for n in range(4)
    ]
    inserted2, updated2 = await storage.video_comments.upsert_many(video_id, again)
    assert (inserted2, updated2) == (0, 4)
    assert await storage.video_comments.count_for_video(video_id) == 4
    rows = await storage.video_comments.list_for_video(video_id)
    assert {row.content for row in rows} == {"改过的0", "改过的1", "改过的2", "改过的3"}


async def test_two_videos_comments_do_not_share_a_key_row(storage: SqliteStorage) -> None:
    """唯一键含 `video_id`：同一个评论 id 出现在两条作品下是两行，不是一行的冲突。"""
    first = await _video(storage, "BV1BB")
    second = await _video(storage, "BV1CC")
    draft = [VideoCommentDraft(platform="bilibili", platform_comment_id="same", content="x")]
    await storage.video_comments.upsert_many(first, draft)
    await storage.video_comments.upsert_many(second, draft)
    assert await storage.video_comments.count_for_video(first) == 1
    assert await storage.video_comments.count_for_video(second) == 1


async def test_comments_disappear_with_the_video(storage: SqliteStorage) -> None:
    """`ON DELETE CASCADE` 不是装饰：删作品不许留下孤儿评论。

    判据是"父行没了之后子表计数为 0"，而且要先确认外键约束真的开着
    （`PRAGMA foreign_keys` 是每连接设的，见 `storage/db.py`）——
    没开的话 SQLite 会安静地留着孤儿行，而这条用例照样绿。
    """
    video_id = await _video(storage, "BV1DD")
    await storage.video_comments.upsert_many(
        video_id, [VideoCommentDraft(platform="bilibili", platform_comment_id="x", content="y")]
    )
    async with storage.sessionmaker() as session:
        enabled = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
    assert enabled == 1, "外键没开，这条看护是瞎的"
    await storage.videos.delete(video_id)
    assert await storage.video_comments.count_for_video(video_id) == 0


async def test_an_empty_comment_id_is_refused_at_the_model_not_in_the_db() -> None:
    """空 id 在**构造草稿**时就被拒，而不是等到撞上唯一键。

    后者会表现成"这一轮只抓到 1 条"，而前者会告诉你是接口没给 id —— 同一个原因，
    两种可见度，选贵的那个。
    """
    with pytest.raises(ValueError, match="platform_comment_id 为空"):
        VideoCommentDraft(platform="bilibili", platform_comment_id="  ", content="x")


def test_the_metadata_column_only_keeps_what_has_a_purpose() -> None:
    """`metadata_json` 里只留有值的几项。

    全 None 也要留一个 `{}` 而不是 `null` —— 读的一侧 `json.loads(...)["liked"]`
    与 `... .get("liked")` 是两种写法，前者在这一列被写成 `null` 时会炸。
    """
    drafts = parse_reply_rows([_row("1")])
    payload = json.loads(drafts[0].metadata_json)
    assert isinstance(payload, dict)
    # 只有身份与正文的一行：现网的 `up_action` 全 False、root/parent 都是 0
    # → 一个都不留，而必须是 `{}` 不是 `null`。
    empty = parse_reply_rows([{"rpid_str": "2", "content": "x", "up_action": {"like": False}}])
    assert json.loads(empty[0].metadata_json) == {}
