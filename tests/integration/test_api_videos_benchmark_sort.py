"""`/api/videos?sort=benchmark`：按点赞数回溯历史爆款（T6.6 的入口，ADR-0020 的 NULL≠0 口径）。

顺序**不写样本、写关系**：把库里的 (点赞数, id) 全拿出来，用一条独立算出来的期望顺序
去比对返回的顺序。理由是这个端点的全部风险都在排序键的边角上 —— NULL 排哪儿、
两条同为 500 赞谁在前、换了 sort 会不会顺手把"只看未隐藏"这条口径也带走。
拿三行样本钉一个"第二行是那条 800 赞的"守不住这些。
"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.video import VideoDraft

pytestmark = pytest.mark.integration

#: 一组刻意带刺的读数：两个 NULL（没读到赞数）、一对完全相同的 500（比 tiebreak）、
#: 一个 0（"真的是 0 赞"，必须与 NULL 分家）。
_READINGS: tuple[tuple[str, int | None], ...] = (
    ("v-null-1", None),
    ("v-800", 800),
    ("v-null-2", None),
    ("v-500-a", 500),
    ("v-0", 0),
    ("v-500-b", 500),
    ("v-1200", 1200),
)

#: 发布时间全部相同：这样"recent"与"benchmark"两种顺序**只可能**由排序键决定。
#: 时间有先后时，"benchmark 其实还在按时间排"这种错会被掩盖掉。
_SAME_PUBLISHED = "2026-01-01T00:00:00+00:00"


async def _seed(app_state: AppState) -> list[tuple[str, int | None, int]]:
    """插那一组作品，返回 `(platform_video_id, like_count, id)`，按插入顺序。"""
    rows: list[tuple[str, int | None, int]] = []
    for platform_video_id, likes in _READINGS:
        row = await app_state.storage.videos.insert(
            VideoDraft(
                platform="douyin",
                platform_video_id=platform_video_id,
                title=f"标题-{platform_video_id}",
                like_count=likes,
                published_at=_SAME_PUBLISHED,
            )
        )
        rows.append((platform_video_id, likes, row.id))
    return rows


def _expected_benchmark_order(rows: list[tuple[str, int | None, int]]) -> list[str]:
    """期望顺序，独立算：点赞数降序 → NULL 最后 → 同值按 id 降序。

    与 `_order_clauses` 是"同一判据、两种写法"。这里**不**照抄 SQL 的 `nulls_last`：
    那是方言行为，这一式子显式把 None 排到最后，两边不一致就是红。
    """
    return [
        name
        for name, _likes, _id in sorted(
            rows, key=lambda row: (row[1] is None, -(row[1] or 0), -row[2])
        )
    ]


def _titles_in_order(body: dict) -> list[str]:
    return [str(item["platform_video_id"]) for item in body["items"]]


async def test_benchmark_sort_orders_by_likes_with_nulls_last(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    rows = await _seed(app_state)
    resp = await client.get("/api/videos", params={"sort": "benchmark", "size": 50})
    assert resp.status_code == 200
    got = _titles_in_order(resp.json())
    assert got == _expected_benchmark_order(rows)
    # 再把方向钉一次：0 赞（真有读数）必须在两条 NULL 之前。
    # 期望式子若被写成"NULL 当 0"，红的落点在中间那一段，光看这一句说不清。
    assert got.index("v-0") < got.index("v-null-1")
    assert got.index("v-0") < got.index("v-null-2")


async def test_default_sort_is_still_by_publish_time(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """加了新排序值，**默认值不许被挤掉**。

    判据不是"返回 200"，而是"默认那一条的顺序与显式 `sort=recent` 逐字相同"，
    且它与 benchmark 的顺序**不同**（同一批数据、同一发布时间）。
    """
    await _seed(app_state)
    default = (await client.get("/api/videos", params={"size": 50})).json()
    recent = (await client.get("/api/videos", params={"size": 50, "sort": "recent"})).json()
    benchmark = (await client.get("/api/videos", params={"size": 50, "sort": "benchmark"})).json()

    assert _titles_in_order(default) == _titles_in_order(recent)
    assert _titles_in_order(default) != _titles_in_order(benchmark)


async def test_sort_changes_only_the_order_not_the_roster(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """两种顺序给的是**同一批作品**：排序不许顺手改筛选口径。

    `total` 一起比 —— 分页的 total 来自另一条 count 语句，那里漏了条件会让
    "总数与列表各说一套"只在一种 sort 下出现。
    """
    await _seed(app_state)
    recent = (await client.get("/api/videos", params={"size": 50})).json()
    benchmark = (await client.get("/api/videos", params={"size": 50, "sort": "benchmark"})).json()
    assert {v["id"] for v in recent["items"]} == {v["id"] for v in benchmark["items"]}
    assert recent["total"] == benchmark["total"] == len(_READINGS)


async def test_hidden_videos_stay_out_under_both_orders(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """墓碑（§7.25）与排序正交：一条 9999 赞的隐藏作品不该因为"按赞排"就浮上来。

    新读径（排序 / 聚合）最容易只接到默认那条查询路径上，所以这一条单独存在。
    """
    await _seed(app_state)
    star = await app_state.storage.videos.insert(
        VideoDraft(
            platform="douyin",
            platform_video_id="v-hidden-star",
            title="隐藏的高赞",
            like_count=9999,
        )
    )
    await app_state.storage.videos.hide(star.id, "用例隐藏")

    body = (await client.get("/api/videos", params={"sort": "benchmark", "size": 50})).json()
    assert "v-hidden-star" not in _titles_in_order(body)
    assert body["total"] == len(_READINGS)

    everything = (
        await client.get("/api/videos", params={"sort": "benchmark", "hidden": "all", "size": 50})
    ).json()
    assert _titles_in_order(everything)[0] == "v-hidden-star"


async def test_an_unknown_sort_is_a_422_not_a_silent_fallback(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """不认识的排序值必须响，不能"当作没给"。

    回落成 recent 的话，一个拼错的 `sort=likes` 会安静地给出按时间排的结果，
    而调用方以为自己在看爆款榜 —— 界面上那一句"按点赞数"就成了假话。
    """
    await _seed(app_state)
    resp = await client.get("/api/videos", params={"sort": "likes"})
    assert resp.status_code == 422


async def test_creator_filter_plus_benchmark_sort_is_what_the_entry_sends(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """`creator_id` + `sort=benchmark` 就是 T6.6 入口发的那一次请求。

    两个条件**必须同时生效**：只看"这个人"的、且按赞数排。少了前者会把别人的爆款
    排进来 —— 那才是这个入口最容易出的错，因为它看起来只是"少筛了一个条件"。
    """
    await _seed(app_state)
    creator = await app_state.storage.creators.insert(
        CreatorDraft(
            platform="douyin",
            platform_id="sec-mine",
            name="这一位",
            profile_url="https://www.douyin.com/user/sec-mine",
        )
    )
    mine: list[str] = []
    for name, likes in (("v-mine-low", 10), ("v-mine-high", 70), ("v-mine-mid", 40)):
        await app_state.storage.videos.insert(
            VideoDraft(
                platform="douyin",
                platform_video_id=name,
                title=f"标题-{name}",
                creator_id=creator.id,
                like_count=likes,
            )
        )
        mine.append(name)

    body = (
        await client.get(
            "/api/videos", params={"creator_id": creator.id, "sort": "benchmark", "size": 50}
        )
    ).json()
    assert _titles_in_order(body) == ["v-mine-high", "v-mine-mid", "v-mine-low"]
    assert body["total"] == len(mine), "只该看到这一位的作品"
