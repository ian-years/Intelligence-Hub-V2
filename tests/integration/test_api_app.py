"""`/api/health` · `/openapi.json` · `/api/preflight`：起服务这一层的冒烟。"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration


async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "version", "time"}


async def test_openapi_lists_the_contract_paths(client: httpx.AsyncClient) -> None:
    """OpenAPI 必须自动出，且带上 specs 里钉死的公共路径（V1 §7.10 的结构性解法）。"""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in (
        "/api/health",
        "/api/preflight",
        "/api/creators",
        "/api/videos",
        "/api/tasks",
        "/api/events",
        "/api/platforms/{platform}/schema",
        "/api/platforms/{platform}/config",
    ):
        assert path in paths, f"缺少契约路由 {path}"


async def test_preflight_endpoint_returns_traffic_light(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/preflight")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"success", "partial", "failed"}
    assert "platform_status" in body["summary"]
