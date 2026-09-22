"""`/api/creators*`：读 + 跟踪开关 + 收录（走任务）。"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.models.creator import CreatorDraft

pytestmark = pytest.mark.integration


async def _seed(state: AppState, *, pid: str = "sec1", platform: str = "douyin") -> int:
    creator = await state.storage.creators.insert(
        CreatorDraft(
            platform=platform,
            platform_id=pid,
            name="某博主",
            profile_url=f"https://{platform}.com/user/{pid}",
            is_tracking=True,
        )
    )
    return creator.id


async def test_list_creators_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/creators")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_creator_and_404(client, app_state: AppState) -> None:
    cid = await _seed(app_state)

    ok = await client.get(f"/api/creators/{cid}")
    assert ok.status_code == 200
    assert ok.json()["name"] == "某博主"

    missing = await client.get("/api/creators/99999")
    assert missing.status_code == 404  # NotFoundError → 全局处理器翻 404


async def test_filter_by_platform(client, app_state: AppState) -> None:
    await _seed(app_state, platform="douyin")
    await _seed(app_state, platform="bilibili", pid="m1")

    dy = await client.get("/api/creators", params={"platform": "douyin"})
    assert [c["platform"] for c in dy.json()] == ["douyin"]


async def test_toggle_tracking(client, app_state: AppState) -> None:
    cid = await _seed(app_state)

    resp = await client.patch(f"/api/creators/{cid}/tracking", json={"tracking": False})
    assert resp.status_code == 200
    assert resp.json()["is_tracking"] is False


async def test_tracking_rejects_non_boolean_with_422(client, app_state: AppState) -> None:
    """V1 §7.24 的第一道闸：非布尔值必须 422，绝不落到 `set_tracking` 存成 0 / 字符串。

    注意 pydantic 的 bool 会把 `"false"` / `0` 这类**规范化成真 bool**（于是 DB 里仍是真
    布尔，安全）；但 `"notabool"` 这种它认不出，直接 422 —— 这才是这里要钉的红。
    """
    cid = await _seed(app_state)
    resp = await client.patch(f"/api/creators/{cid}/tracking", json={"tracking": "notabool"})
    assert resp.status_code == 422


async def test_add_creator_enqueues_task_202(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/creators", json={"url": "https://douyin.com/user/x", "platform": "douyin"}
    )
    assert resp.status_code == 202
    assert "task_id" in resp.json()
