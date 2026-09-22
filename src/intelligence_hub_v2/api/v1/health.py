"""`/api/health` 与 `/api/preflight`。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.tasks.definition import CancelToken, TaskContext
from intelligence_hub_v2.tasks.params import PreflightParams
from intelligence_hub_v2.tasks.preflight import run_preflight

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["health"])

_PROBE_LOGGER = get_logger("api.preflight")


class HealthResponse(BaseModel):
    status: str
    version: str
    time: datetime


@router.get("/health", response_model=HealthResponse)
async def health(state: AppState = Depends(get_state)) -> HealthResponse:
    """进程活着 + 版本号。前端侧边栏的连接指示灯用。

    **不打数据库**：`/health` 是"进程在不在"，不是"依赖好不好的" —— 后者是 `/preflight`。
    把 DB 探活塞这里会让一次 SQLite 卡顿看起来像服务挂了。
    """
    return HealthResponse(status="ok", version=state.config.app.version, time=datetime.now(UTC))


@router.get("/preflight")
async def preflight(state: AppState = Depends(get_state)) -> dict[str, Any]:
    """同步跑一遍预检并返回红绿灯。**不建 task_run、不写清单**（这是一次只读探针）。

    与"跑一个 preflight 任务"的区别：那个会留下一条运行历史，而 Settings 页只想问一句
    "现在环境怎么样"刷新显示。用一个临时 task_id 的 `TaskContext` 直接调 handler。
    """
    ctx = TaskContext(
        task_id="preflight-probe",
        deps=None,  # type: ignore[arg-type]  # preflight handler 不读 deps
        adapters=state.registry,
        events=state.events,
        storage=state.storage,
        files=state.files,
        cancel_token=CancelToken(),
        workdir=state.files.tmp_root,
        logger=_PROBE_LOGGER,
        config_snapshot={},
        task_name="preflight",
    )
    result = await run_preflight(ctx, PreflightParams())
    return {
        "status": result.status,
        "summary": result.summary,
        "failures": [f.model_dump(mode="json") for f in result.failures],
    }
