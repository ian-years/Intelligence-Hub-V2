"""/api/schedule：定时采集的**现状**、改配置、以及"现在就跑一次"。

ADR-0017 把"什么时候跑"定成了配置里的一个 cron 串，而配置层已有的
`ConfigManager` 原子写盘 + 热重载那套路本来就是给这件事用的。这一层不发明第二份排期状态：
**没有 `schedules` 表，也没有 sidecar JSON**（理由见 ADR-0017 的选项表）。

四条这个文件特有的规矩，全都是"看板会说谎"的那个形状：

1. **改完当场生效，不说"重启后生效"**。`reschedule_collect_jobs` 先删后加，所以
   `requires_restart` 今天是恒 `false` —— 而它保留成响应字段而不是省略：等哪一天真有
   一类配置必须重启，那个 `true` 得有个地方放，而不是到时候再改契约。
2. **"排上了"与"配了但没排"是两件事**。`collect_platforms` 里被关掉的平台不会被排 job
   （与"关掉平台 → 它的任务消失"同一条语义），但必须出现在 `skipped_platforms` 里。
   静默少排一个平台的症状是"那位博主永远不更新而配置看着没问题"。
3. **`next_run_time` 来自 APScheduler 本身**，不是我们拿 cron 串二次算出来的。自己算就
   得到第二个"下次几点"的真相，而它与真正会触发的那个值一分叉，看板上那句"下次 08:00"
   就成了假凭据。拿不到（job 没排上）交回 `None`，前端写"没有排上的 job"，不许写"明天 08:00"。
4. **"手动立即跑一次"走的是 cron 那条同一个任务名**。同名不是省事，是判据：那条路才有
   开关闸门、`requires` 检查、限流信号量、清单终态与事件流。手动触发如果另开一条路，
   那条路就会长成绕过闸门的后门（V1 的 launcher 字典调度就是这么歪的）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.core.collect_jobs import (
    COLLECT_JOB_PREFIX,
    collect_job_platforms,
    collect_task_name,
    diff_fields,
    parse_collect_trigger,
    platform_from_job_id,
    reschedule_collect_jobs,
)
from intelligence_hub_v2.core.config import SchedulerSection
from intelligence_hub_v2.errors import ConfigError
from intelligence_hub_v2.models.event import ConfigChangedPayload, Event, EventType
from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS

if TYPE_CHECKING:
    from typing import Any

    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["schedule"])


class CollectJobInfo(BaseModel):
    """一条已经排在 APScheduler 里的采集 job。"""

    id: str
    platform: str
    next_run_time: datetime | None = None
    """APScheduler 给的那个值。`None` = 这条 job 还没 pending（调度器没 start），
    **不是**"明天不会跑"。"""


class ScheduleStatus(BaseModel):
    """`GET /api/schedule`：配置 + 运行态两份**并排**。

    两份分开是必须的：只看配置会说谎（改了没生效），只看 job 也会说谎
    （`scheduler.enabled: false` 时一条 job 都没有，而配置里明明写着 08:00）。
    """

    scheduler_enabled: bool
    timezone: str
    collect_cron: str | None
    collect_platforms: list[str] = Field(default_factory=list)
    """配置里的名单。空 = "所有启用的平台"，所以看这一栏不足以判断会跑谁，要看下一栏。"""
    effective_platforms: list[str] = Field(default_factory=list)
    """这次真会去采的平台（与排 job 那一路同一个函数 `collect_job_platforms` 的答案）。"""
    skipped_platforms: list[str] = Field(default_factory=list)
    """名单里但被关掉了、因此**没有**被排上的平台。"""
    collect_limit: int | None
    jobs: list[CollectJobInfo] = Field(default_factory=list)
    scheduler_running: bool = False
    """APScheduler 实例在不在。`false` 时 `jobs` 必空，而配置可以看着完全正常。"""
    master_enabled: bool = True
    """平台总闸（ADR-0025）。这一栏是**补上"为什么空"那块信息**的，不是装饰：

    `skipped_platforms` 只在 `collect_platforms` 点过名的时候才有内容，而默认那份是空的
    （"空 = 所有启用的平台"）。于是关掉总闸会得到 `effective_platforms: []` 且
    `skipped_platforms: []` —— cron 还写着 08:00，一条都不排，而没有任何一栏说原因。
    与本文件规矩 2 同一件事：静默少排平台，症状是"那位博主永远不更新而配置看着没问题"。
    """
    shadowed_by_env: list[str] = Field(default_factory=list)
    """这些键在**环境变量**里也设着，所以下一次启动会盖掉盘上这一份。

    优先级是 `默认 < yaml < env < CLI`，而这一格只能写 YAML。今天生效、重启后回到 env 那条
    —— 这句话必须说，否则设置页会制造一个"我明明改过"的悬案。空 = 没有遮挡。
    """


class ScheduleUpdate(BaseModel):
    """PUT 的请求体：**没出现的键保持原值**。

    所以 `collect_cron: null` 是"关掉定时"，而整个键不写是"这次不改它"。这两件事必须分得开，
    否则前端只能"总是提交全表"，那等于每次保存都把别人的字段顺手写一遍。
    """

    collect_cron: str | None = None
    collect_platforms: list[str] | None = None
    collect_limit: int | None = Field(default=None, ge=1)


class ScheduleUpdateResponse(BaseModel):
    schedule: ScheduleStatus
    changed_fields: list[str] = Field(default_factory=list)
    requires_restart: bool = False


class RunNowRequest(BaseModel):
    platform: str


class RunNowResponse(BaseModel):
    task_id: str
    task_name: str
    platform: str


def _status(state: AppState) -> ScheduleStatus:
    """把"配置里写的"与"调度器上真有的"并成一份响应。

    两个来源一次读、**不交叉校验**：这里发现不一致时不去修任何一侧，只把两份都交出去。
    自动修会把一个真实的装配 bug 抹平（比如热重载只换了配置没重排 job），
    而那种 bug 只能靠看板上这两栏不一样才看得见。
    """
    section = state.config.scheduler
    enabled = state.config_manager.enabled_platforms()
    scheduled, skipped = collect_job_platforms(section, enabled)
    jobs = _collect_jobs(state)
    return ScheduleStatus(
        scheduler_enabled=section.enabled,
        timezone=section.timezone,
        collect_cron=section.collect_cron,
        collect_platforms=list(section.collect_platforms),
        effective_platforms=list(scheduled),
        skipped_platforms=skipped,
        collect_limit=section.collect_limit,
        jobs=jobs,
        scheduler_running=state.apscheduler is not None,
        master_enabled=state.config_manager.app.platform_control.enabled,
        shadowed_by_env=state.config_manager.scheduler_keys_shadowed_by_env(section),
    )


def _collect_jobs(state: AppState) -> list[CollectJobInfo]:
    """调度器上那些 `collect:<platform>` job。别的 job（`prune_events`）不在这一栏里。"""
    scheduler: Any = state.apscheduler
    if scheduler is None:
        return []
    out: list[CollectJobInfo] = []
    for job in scheduler.get_jobs():
        job_id = getattr(job, "id", None)
        if not isinstance(job_id, str) or not job_id.startswith(COLLECT_JOB_PREFIX):
            continue
        next_run = getattr(job, "next_run_time", None)
        out.append(
            CollectJobInfo(
                id=job_id,
                platform=platform_from_job_id(job_id),
                # 不是 datetime 就当没有：APSchedier 在 pending 状态给的是 None，
                # 而我们不许替它编一个"看起来是下次"的值。
                next_run_time=next_run if isinstance(next_run, datetime) else None,
            )
        )
    return sorted(out, key=lambda item: item.platform)


def _merged_section(state: AppState, body: ScheduleUpdate) -> SchedulerSection:
    """当前段 + 只覆盖**请求里出现过的那几个键** → 校验过的新段。

    五段 cron、平台名必须已知、limit≥1 这些判据全交给 `SchedulerSection` 自己 ——
    在这一层重判一次就是多一处会漂的判据（§7.11 那一族）。
    """
    current = state.config.scheduler.model_dump()
    merged = {**current, **body.model_dump(exclude_unset=True)}
    try:
        section = SchedulerSection.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if section.collect_cron:
        # 配置层的"五段、字符集"检查挡不住语义上不是合法 cron 的串（`99 99 * * *` 是五个
        # token）。用**排 job 那同一个解析器**先问一遍，否则会写出一个
        # "PUT 返回 200、下一次启动红在 `_add_collect_jobs`"的配置 —— 那正是最难查的时序。
        #
        # `ConfigError` 在这一层必须翻成 422：全局处理器把它映射成 500 是有道理的
        # （服务自己读到的配置坏了是服务端的事），但这里红的原因是**请求体里那个人填的串**，
        # 回 500 会让人去翻服务端日志而不是改自己填的那一格。
        try:
            parse_collect_trigger(section.collect_cron, section.timezone)
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    return section


@router.get("/schedule", response_model=ScheduleStatus)
async def get_schedule(state: AppState = Depends(get_state)) -> ScheduleStatus:
    """定时采集的当前配置，与已经排上的 job。"""
    return _status(state)


@router.put("/schedule", response_model=ScheduleUpdateResponse)
async def update_schedule(
    body: ScheduleUpdate,
    state: AppState = Depends(get_state),
) -> ScheduleUpdateResponse:
    """写 `app.yaml` 的 scheduler 段 → 热更内存 → 当场重排采集 job → 发 `CONFIG_CHANGED`。

    与平台配置那条 PUT 同一条纪律：**运行时不听文件变化**，改了不生效是设计而不是 bug。
    所以"生效"这一步必须显式做，不许留给下一次启动。
    """
    section = _merged_section(state, body)
    old = state.config.scheduler.model_dump(mode="json")

    try:
        await run_in_threadpool(state.config_manager.write_scheduler, section)
    except (ConfigError, OSError) as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"写 app.yaml 失败：{exc}"
        ) from exc

    # `write_scheduler` 已经原地换掉了那个共享的 `AppConfig.scheduler`（见它的 docstring
    # 为什么这里没有 `reload_app`）。重排必须发生在**之后**：`reschedule_collect_jobs`
    # 读的就是那份配置，顺序倒了会得到"配置是新的、job 是旧的"。
    reschedule_collect_jobs(
        section,
        state.config_manager.enabled_platforms(),
        state.apscheduler,
        state.scheduler.submit,
    )
    new = section.model_dump(mode="json")
    changed = diff_fields(old, new)

    await state.events.publish(
        Event(
            type=EventType.CONFIG_CHANGED,
            timestamp=datetime.now(UTC),
            payload=ConfigChangedPayload(
                scope="app",
                platform=None,
                changed_fields=[f"scheduler.{name}" for name in changed],
            ).model_dump(mode="json"),
        )
    )
    return ScheduleUpdateResponse(
        schedule=_status(state), changed_fields=changed, requires_restart=False
    )


@router.post(
    "/schedule/run-now",
    response_model=RunNowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_now(body: RunNowRequest, state: AppState = Depends(get_state)) -> RunNowResponse:
    """立刻排队一次该平台的采集，参数用定时任务那一套（`collect_limit`）。

    202 说的只是"已排队"，不是"已采到"。参数校验失败 / 平台被关掉 → `submit` 抛
    `ValidationError` / `TaskRejected`，交全局异常处理器翻成 422 —— 不在这里 catch，
    否则每个入口都要抄一遍映射（V1 §7.10 那一族）。
    """
    if body.platform not in PLATFORM_CONFIG_SCHEMAS:
        known = ", ".join(PLATFORM_CONFIG_SCHEMAS)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"平台 {body.platform!r} 在当前构建里没有适配器实现。可用的：{known}",
        )
    task_name = collect_task_name(body.platform)
    task_id = state.scheduler.submit(task_name, {"limit": state.config.scheduler.collect_limit})
    return RunNowResponse(task_id=task_id, task_name=task_name, platform=body.platform)
