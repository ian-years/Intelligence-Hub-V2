"""`/api/platforms*`：列平台、拿配置 JSON Schema、读写平台配置（原子 + 热重载 + 事件）。

契约来源：`docs/specs/config-schema.md §4 §5`。三条纪律都在这里：
- 写配置**只有这一个入口**（原子写盘 → 内存热重载 → 发 `CONFIG_CHANGED`），运行时不听文件变化。
- 不手改 YAML 等服务感知：改了不生效是设计，不是 bug。
- `changed_fields` 只报字段名不报值（配置里可能有 cookie 路径等，不往事件流里塞敏感值）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.core.collect_jobs import reschedule_collect_jobs
from intelligence_hub_v2.errors import ConfigError
from intelligence_hub_v2.models.event import ConfigChangedPayload, Event, EventType
from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS, config_schema_for
from intelligence_hub_v2.platforms.base import PlatformAvailability, PlatformConfig

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["config"])


class PlatformSummary(BaseModel):
    name: str
    display_name: str
    enabled: bool
    """这一家**自己**的开关（`platforms.yaml` 那一段）。总闸不改它 —— 见 `availability`。"""

    availability: PlatformAvailability
    """`总闸 AND 这一家`，外加两种"关"都轮不到的情况（四态的含义见
    `platforms/base.py::PlatformAvailability`）。

    答案来自**运行期那一份**（`state.registry.availability()`，与 `get()`、
    `/api/tasks` 的过滤、跑前那道门同一个谓词），不是"文件里怎么写的"。
    界面据此渲染三态：`available` 给开关、`own_off` 给关着的开关、
    `master_off` 给一个**点不动**的开关加一句"被总闸盖住" ——
    后者如果渲染成普通关着的开关，人会去翻 `platforms.yaml` 而那里写着 `enabled: true`。
    """

    implemented: bool
    # ADR-0022：这三列是 preflight 写进镜像的那一份**上次结论**，不是"现在"的探测结果。
    # 所以它们与 `health_checked_at` 必须成对出现 —— 一个没有时刻的绿灯就是 §7.20
    # 那个形状的灯。`health_status` 为 None 表示"从没检查过"，前端因此**不画灯**，
    # 而不是画一个灰色的"未知"（灰会被读成"检查过且没问题"）。
    health_status: str | None = None
    health_checked_at: datetime | None = None
    health_detail: str | None = None


class PlatformsResponse(BaseModel):
    platforms: list[PlatformSummary]
    master_enabled: bool = True
    """四家共用的那一个总闸（ADR-0025）。

    放在**根**上而不是每行重复：它是单一事实，而每行都带一份就等于给前端
    四处可以各渲染各的地方。每一行的 `availability` 已经把它算进去了。
    """


class ConfigUpdateResponse(BaseModel):
    platform: str
    config: dict[str, Any]
    requires_restart: bool = False
    changed_fields: list[str] = Field(default_factory=list)


@router.get("/platforms", response_model=PlatformsResponse)
async def list_platforms(state: AppState = Depends(get_state)) -> PlatformsResponse:
    implemented = set(state.registry.implemented_platforms())
    # 健康读镜像，**不在这里探测**：`/api/platforms` 是总览页与设置页每次进来都要打的端点，
    # 让它顺手连一次桥等于给首页加风控（Dashboard 的 docstring 同一条纪律）。
    mirror = {record.name: record for record in await state.storage.platforms.list_all()}
    items = [
        PlatformSummary(
            name=name,
            display_name=cfg.display_name,
            enabled=cfg.enabled,
            # 问的是**运行期那一份**（注册表里的 configs 快照 + 总闸闭包），
            # 与 `registry.get()`、采集门控同一个答案。这里读 manager 也能得到同样的值
            # 今天，但那是两份快照**恰好同步**的结果，不是同一处代码。
            availability=state.registry.availability(name),
            implemented=name in implemented,
            health_status=record.health_status if record else None,
            health_checked_at=record.health_checked_at if record else None,
            health_detail=record.health_detail if record else None,
        )
        for name, cfg, record in (
            (n, state.config_manager.get_platform(n), mirror.get(n))
            for n in state.config_manager.platform_names()
        )
    ]
    return PlatformsResponse(
        platforms=items,
        master_enabled=state.config_manager.app.platform_control.enabled,
    )


def _require_schema(platform: str) -> type[PlatformConfig]:
    try:
        return config_schema_for(platform)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"平台 {platform!r} 未注册。已知：{', '.join(PLATFORM_CONFIG_SCHEMAS)}",
        ) from exc


@router.get("/platforms/{platform}/schema")
async def platform_schema(platform: str) -> dict[str, Any]:
    """配置类的 JSON Schema，前端据此自动渲染表单（`ui:advanced` 折叠高级字段）。"""
    schema_cls = _require_schema(platform)
    return schema_cls.model_json_schema()


@router.get("/platforms/{platform}/config")
async def get_platform_config(
    platform: str,
    state: AppState = Depends(get_state),
) -> dict[str, Any]:
    """平台配置（权威源来自 ConfigManager）+ 运行态健康镜像。

    健康取不到就是 `None`（从没检查过），不编一个 ok —— `PlatformRecord.is_healthy`
    只认显式 `"ok"`（V1 §7.20 的同一口径）。
    """
    _require_schema(platform)
    config = state.config_manager.get_platform(platform)
    record = await state.storage.platforms.get(platform)
    health = (
        None
        if record is None
        else {"status": record.health_status, "checked_at": record.health_checked_at}
    )
    return {"platform": platform, "config": config.model_dump(mode="json"), "health": health}


@router.put("/platforms/{platform}/config")
async def update_platform_config(
    platform: str,
    payload: dict[str, Any],
    state: AppState = Depends(get_state),
) -> ConfigUpdateResponse:
    """校验 → 原子写盘 → 热重载 → 广播 `CONFIG_CHANGED` → 让适配器实例作废。"""
    schema_cls = _require_schema(platform)
    try:
        validated = schema_cls.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError 等：原样翻成 422，带上原因
        raise HTTPException(422, detail=str(exc)) from exc

    manager = state.config_manager
    try:
        old = manager.get_platform(platform)
    except KeyError:
        old = None
    changed = _diff_fields(
        old.model_dump(mode="json") if old else {}, validated.model_dump(mode="json")
    )

    try:
        await run_in_threadpool(manager.write_platform_config, platform, validated)
    except (ConfigError, OSError) as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"写盘失败：{exc}"
        ) from exc
    await run_in_threadpool(manager.reload_platform, platform)

    # 热重载之后把新配置推到注册表 / 依赖袋 / 调度器三份快照 **和 `platforms` 镜像**。
    # 只 invalidate 适配器实例不够：那三处各握一份 configs，改开关必须同时落到它们，
    # 否则"关掉平台"只改了 manager，采集门控还看着旧值 —— 关了的平台照样能被采。
    # 走 `refresh_platform_state`（不是 `apply_platform_config`）：镜像那一笔要 await。
    await state.refresh_platform_state(platform, manager.get_platform(platform))

    await state.events.publish(
        Event(
            type=EventType.CONFIG_CHANGED,
            timestamp=datetime.now(UTC),
            payload=ConfigChangedPayload(
                scope="platform", platform=platform, changed_fields=changed
            ).model_dump(mode="json"),
        )
    )
    return ConfigUpdateResponse(
        platform=platform,
        config=validated.model_dump(mode="json"),
        requires_restart=False,
        changed_fields=changed,
    )


def _diff_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    keys = set(old) | set(new)
    return sorted(k for k in keys if old.get(k) != new.get(k))


# --------------------------------------------------------------------------- #
# 平台总闸（ADR-0025）
# --------------------------------------------------------------------------- #


class PlatformControlStatus(BaseModel):
    enabled: bool
    """**当前生效**的值。优先级 `默认 < yaml < env`，所以 env 压着时这是 env 那一份，
    而 PUT 写的是 yaml —— 下一栏负责把这句话说出来。"""

    shadowed_by_env: list[str] = Field(default_factory=list)
    """这些键在环境变量里也设着，因此**下一次启动会盖掉盘上这一份**。

    与 `ScheduleStatus.shadowed_by_env` 同一条纪律，理由也一样：不说出来，
    症状是"我明明在设置页关掉了，重启后又开始采了"，而没有人会想到去查环境变量。
    """


class PlatformControlUpdate(BaseModel):
    """PUT 的请求体。只有一个字段，所以不搞 `exclude_unset` 那套"没写的保持原值" ——
    那套是为了一张表里多个字段准备的，这里只会让人以为可以省略。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class PlatformControlUpdateResponse(BaseModel):
    platform_control: PlatformControlStatus
    changed_fields: list[str] = Field(default_factory=list)
    requires_restart: bool = False
    """总闸**不需要**重启：三道消费点（任务列表过滤、跑前门、cron 名单）都是每次问现读。
    这一栏与 `/api/schedule` 那条同形 —— 留着是为了哪一天真有必须重启的配置时有地方放。"""


def _platform_control_status(state: AppState) -> PlatformControlStatus:
    manager = state.config_manager
    return PlatformControlStatus(
        enabled=manager.app.platform_control.enabled,
        shadowed_by_env=manager.platform_control_keys_shadowed_by_env(),
    )


@router.get("/platform-control", response_model=PlatformControlStatus)
async def get_platform_control(
    state: AppState = Depends(get_state),
) -> PlatformControlStatus:
    """四家共用的那一个总闸。"""
    return _platform_control_status(state)


@router.put("/platform-control", response_model=PlatformControlUpdateResponse)
async def update_platform_control(
    body: PlatformControlUpdate,
    state: AppState = Depends(get_state),
) -> PlatformControlUpdateResponse:
    """写 `app.yaml` 的 `platform_control` 段 → 当场生效 → 重排采集 job → 发事件。

    与两条平台配置 PUT 同一条纪律：**运行时不听文件变化**，所以"生效"必须显式做。
    这里比它们少一步：没有需要推的快照 —— 注册表拿的是总闸的**闭包**、
    调度器与 runner 握着的是同一个 `AppConfig` 实例，而 `write_platform_control`
    原地换的就是那个实例上的字段（换指针会让没跟着换的持有者继续用旧值，
    症状是"设置页说总闸已开，任务列表还是空的"）。

    重排采集 job 必须在写盘**之后**：`reschedule_collect_jobs` 读的就是那份配置。
    """
    manager = state.config_manager
    old = manager.app.platform_control.model_dump(mode="json")

    try:
        await run_in_threadpool(manager.write_platform_control, body.enabled)
    except (ConfigError, OSError) as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"写 app.yaml 失败：{exc}"
        ) from exc

    # 名单变了（四家一起进/出），已排的 job 必须跟着重排。`apscheduler is None`
    # 时返回空清单而不是抛：配置写得住了，只是要等下一次启动才排得上，
    # 而这句话由响应里的 `scheduler_running` / 前端那一行显示，不许静默成功。
    reschedule_collect_jobs(
        manager.app.scheduler,
        manager.enabled_platforms(),
        state.apscheduler,
        state.scheduler.submit,
    )

    new = manager.app.platform_control.model_dump(mode="json")
    changed = [f"platform_control.{key}" for key in _diff_fields(old, new)]

    if changed:
        # `scope` 用 `"app"` 而不是新造一个 `"platform_control"`：这一格改的确实是
        # `app.yaml` 里的一段，而 payload 的 `scope` 是契约（`docs/specs/event-schema.md`）——
        # 为一个字段开第三个取值，等于让所有按 scope 过滤的下游多认识一种情况。
        # 区分靠 `changed_fields` 里的段名前缀，与 scheduler 那条 PUT 同一口径。
        await state.events.publish(
            Event(
                type=EventType.CONFIG_CHANGED,
                timestamp=datetime.now(UTC),
                payload=ConfigChangedPayload(
                    scope="app", platform=None, changed_fields=changed
                ).model_dump(mode="json"),
            )
        )
    return PlatformControlUpdateResponse(
        platform_control=_platform_control_status(state),
        changed_fields=changed,
        requires_restart=False,
    )
