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
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.errors import ConfigError
from intelligence_hub_v2.models.event import ConfigChangedPayload, Event, EventType
from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS, config_schema_for
from intelligence_hub_v2.platforms.base import PlatformConfig

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["config"])


class PlatformSummary(BaseModel):
    name: str
    display_name: str
    enabled: bool
    implemented: bool


class PlatformsResponse(BaseModel):
    platforms: list[PlatformSummary]


class ConfigUpdateResponse(BaseModel):
    platform: str
    config: dict[str, Any]
    requires_restart: bool = False
    changed_fields: list[str] = Field(default_factory=list)


@router.get("/platforms", response_model=PlatformsResponse)
async def list_platforms(state: AppState = Depends(get_state)) -> PlatformsResponse:
    implemented = set(state.registry.implemented_platforms())
    items = [
        PlatformSummary(
            name=name,
            display_name=cfg.display_name,
            enabled=cfg.enabled,
            implemented=name in implemented,
        )
        for name, cfg in (
            (n, state.config_manager.get_platform(n)) for n in state.config_manager.platform_names()
        )
    ]
    return PlatformsResponse(platforms=items)


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

    # 热重载之后把新配置推到注册表 / 依赖袋 / 调度器三份快照（`apply_platform_config`）。
    # 只 invalidate 适配器实例不够：那三处各握一份 configs，改开关必须同时落到它们，
    # 否则"关掉平台"只改了 manager，采集门控还看着旧值 —— 关了的平台照样能被采。
    state.apply_platform_config(platform, manager.get_platform(platform))

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
