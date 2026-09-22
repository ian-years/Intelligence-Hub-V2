"""`/api/tasks*`：可运行任务列表、参数 schema、启动、运行历史、取消、单任务事件流。

路由顺序有讲究：`/tasks/runs*`（字面量段）必须在 `/tasks/{name}*`（参数段）之前注册，
否则 `/tasks/runs` 会被 `/tasks/{name}` 抢先匹配。这里按"静态在前"排。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.api.v1.events import _parse_types, stream_events
from intelligence_hub_v2.core.task_registry import TASKS, available_task_names, get_task
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.task import TaskRunRecord

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["tasks"])


class TaskInfo(BaseModel):
    name: str
    display_name: str
    kind: str
    platforms: list[str]
    cancellable: bool
    timeout_seconds: int | None
    params_schema: dict[str, Any] = Field(default_factory=dict)


class TaskAccepted(BaseModel):
    task_id: str


class CancelResponse(BaseModel):
    task_id: str
    cancelled: bool


class RunDetail(BaseModel):
    run: TaskRunRecord
    manifest: dict[str, Any] | None = None


def _enabled_map(state: AppState) -> dict[str, Any]:
    return {
        name: state.config_manager.get_platform(name)
        for name in state.config_manager.platform_names()
    }


def _definition_or_404(name: str) -> Any:  # noqa: ANN401 - TaskDefinition
    try:
        return get_task(name)
    except Exception as exc:  # TaskRejected（未知任务）
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/tasks", response_model=list[TaskInfo])
async def list_tasks(state: AppState = Depends(get_state)) -> list[TaskInfo]:
    """当前可运行的任务（平台开关 + `implemented` 过滤后）。"""
    enabled = _enabled_map(state)
    names = set(available_task_names(enabled))
    out: list[TaskInfo] = []
    for name, definition in TASKS.items():
        if name not in names:
            continue
        out.append(
            TaskInfo(
                name=definition.name,
                display_name=definition.display_name,
                kind=definition.kind.value,
                platforms=list(definition.platforms),
                cancellable=definition.cancellable,
                timeout_seconds=definition.timeout_seconds,
                params_schema=definition.params_schema.model_json_schema(),
            )
        )
    return out


@router.get("/tasks/runs", response_model=list[TaskRunRecord])
async def list_runs(
    state: AppState = Depends(get_state),
    limit: int = Query(default=50, ge=1, le=500),
    run_status: str | None = Query(default=None, alias="status"),
) -> list[TaskRunRecord]:
    return await state.storage.task_runs.list_recent(limit=limit, status=run_status)


@router.get("/tasks/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: str, state: AppState = Depends(get_state)) -> RunDetail:
    record = await state.storage.task_runs.get_or_raise(run_id)
    manifest_row = await state.storage.manifests.latest_for_task(run_id)
    manifest = manifest_row.content().model_dump(mode="json") if manifest_row else None
    return RunDetail(run=record, manifest=manifest)


@router.post("/tasks/runs/{run_id}/cancel", response_model=CancelResponse)
async def cancel_run(run_id: str, state: AppState = Depends(get_state)) -> CancelResponse:
    """协作式取消：置位 `CancelToken`。返回 False 表示这条已经不在跑了（诚实回执，不转圈）。"""
    cancelled = state.scheduler.cancel(run_id)
    return CancelResponse(task_id=run_id, cancelled=cancelled)


@router.get("/tasks/runs/{run_id}/events")
async def run_events(
    run_id: str,
    state: AppState = Depends(get_state),
    types: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
) -> EventSourceResponse:
    parsed: set[EventType] | None = _parse_types(types)
    return EventSourceResponse(stream_events(state, task_id=run_id, types=parsed, since=since))


@router.get("/tasks/{name}/schema")
async def task_schema(name: str) -> dict[str, Any]:
    definition = _definition_or_404(name)
    return {
        "name": definition.name,
        "display_name": definition.display_name,
        "kind": definition.kind.value,
        "params_schema": definition.params_schema.model_json_schema(),
    }


@router.post("/tasks/{name}/run", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
async def run_task(
    name: str,
    body: dict[str, Any],
    state: AppState = Depends(get_state),
) -> TaskAccepted:
    """启动任务，立即返回 task_id（202）。

    参数校验失败 / 平台禁用 / 未实现 → `scheduler.submit` 抛 `ValidationError` / `TaskRejected`，
    交全局异常处理器翻成 422（不在这里 catch —— 那样每个入口都要抄一遍映射）。
    """
    _definition_or_404(name)
    task_id = state.scheduler.submit(name, body)
    return TaskAccepted(task_id=task_id)
