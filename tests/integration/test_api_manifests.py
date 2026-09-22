"""`/api/manifests*`：跑一个 preflight 任务真写清单，再翻列表 / 详情。"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState

pytestmark = pytest.mark.integration


async def test_manifests_list_and_detail(client: httpx.AsyncClient, app_state: AppState) -> None:
    await app_state.scheduler.run_to_completion("preflight", {})
    rows = await app_state.storage.manifests.list_recent()
    assert len(rows) == 1

    lst = await client.get("/api/manifests")
    assert lst.status_code == 200
    body = lst.json()
    assert body[0]["task_id"] == rows[0].task_id
    assert body[0]["status"] in {"success", "partial", "failed"}
    assert "summary" in body[0]

    detail = await client.get(f"/api/manifests/{rows[0].id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == rows[0].id


async def test_manifest_detail_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/manifests/9999")).status_code == 404
