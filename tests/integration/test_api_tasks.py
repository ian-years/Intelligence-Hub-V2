"""`/api/tasks*`：可运行任务列表、schema、启动、运行历史、取消。"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration


async def test_list_tasks_shows_v2_implemented(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert {
        "preflight",
        "douyin_collect",
        "bilibili_collect",
        "xiaohongshu_collect",
        "single_link",
        "add_creator",
        "postprocess",
    } <= names
    # 未实现 / 未移植平台的任务不该出现在列表里（不撒谎原则）。
    # 样本会随注册表翻向而失效：`xiaohongshu_collect` 在 T2.1 之前、`youtube_collect` 在
    # T2.2 之前、`backfill` 在 V2.2 补上 handler 之前都当过这个样本 —— 它们各有真 handler
    # 之后继续拿它们举例，"不该出现"那半条就悄悄变成零断言。现在用 `feishu_sync`
    # （`ported/feishu/` 搬完了但 `tasks/` 里还没有 handler，见 ROADMAP V2.2 那一格）。
    assert "youtube_collect" in names
    assert "backfill" in names  # §7.22 那条陷阱的正面实现，平台由链接决定所以 platforms=()
    assert "all_platforms" not in names
    assert "feishu_sync" not in names


async def test_task_schema_returns_params(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/tasks/add_creator/schema")
    assert resp.status_code == 200
    assert "url" in resp.json()["params_schema"]["properties"]


async def test_unknown_task_schema_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/tasks/nope/schema")).status_code == 404
    assert (await client.post("/api/tasks/nope/run", json={})).status_code == 404


async def test_run_preflight_returns_task_id(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/tasks/preflight/run", json={})
    assert resp.status_code == 202
    assert "task_id" in resp.json()


async def test_run_with_bad_params_is_422(client: httpx.AsyncClient) -> None:
    # CollectParams.limit 有 ge=1；0 应被 pydantic 拒 → 全局 ValidationError 处理器 → 422
    resp = await client.post("/api/tasks/douyin_collect/run", json={"limit": 0})
    assert resp.status_code == 422


async def test_runs_list_and_cancel_unknown(client: httpx.AsyncClient) -> None:
    runs = await client.get("/api/tasks/runs")
    assert runs.status_code == 200
    assert isinstance(runs.json(), list)

    cancel = await client.post("/api/tasks/runs/does-not-exist/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is False
