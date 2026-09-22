"""`/api/videos*`：分页 / 过滤 / 隐藏（V1 §7.25 的墓碑）。"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.models.video import VideoDraft

pytestmark = pytest.mark.integration


async def _seed(
    client: httpx.AsyncClient, app_state: AppState, *, platform="douyin", vid="v1", hidden=False
) -> int:
    row = await app_state.storage.videos.insert(
        VideoDraft(platform=platform, platform_video_id=vid, title=f"标题-{vid}")
    )
    if hidden:
        await app_state.storage.videos.hide(row.id, "测试隐藏")
    return row.id


async def test_list_videos_pagination_and_hidden_filter(client, app_state: AppState) -> None:
    a = await _seed(client, app_state, vid="a")
    b = await _seed(client, app_state, vid="b", hidden=True)

    default = await client.get("/api/videos")
    assert default.status_code == 200
    body = default.json()
    assert body["total"] == 1  # 默认只回可见
    assert [i["id"] for i in body["items"]] == [a]

    all_mode = await client.get("/api/videos", params={"hidden": "all"})
    assert all_mode.json()["total"] == 2

    only_hidden = await client.get("/api/videos", params={"hidden": "hidden"})
    assert [i["id"] for i in only_hidden.json()["items"]] == [b]


async def test_search_escapes_like_wildcards(client, app_state: AppState) -> None:
    await _seed(client, app_state, vid="x")
    # 搜 "%" 不该匹配全表（Repository 里转义了 LIKE 通配符）
    resp = await client.get("/api/videos", params={"search": "%"})
    assert resp.json()["total"] == 0


async def test_get_video_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/videos/12345")
    assert resp.status_code == 404


async def test_hide_then_unhide(client, app_state: AppState) -> None:
    vid = await _seed(client, app_state, vid="h")

    hide = await client.patch(f"/api/videos/{vid}/hide", json={"reason": "不感兴趣"})
    assert hide.status_code == 200
    assert hide.json()["is_hidden"] is True

    unhide = await client.patch(f"/api/videos/{vid}/unhide")
    assert unhide.json()["is_hidden"] is False

    # 隐藏的作品详情仍能打开（§7.25：墓碑只管列表）
    detail = await client.get(f"/api/videos/{vid}")
    assert detail.status_code == 200


async def test_single_link_enqueues_task(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/videos/single-link", json={"url": "https://www.bilibili.com/video/BV1xx411c7mD"}
    )
    assert resp.status_code == 202
    assert "task_id" in resp.json()
