"""DI 容器与路由依赖：把 lifespan 装配好的单例交给各个路由。

`AppState` 是**运行期单例的袋子**，在 `main.lifespan` 里建一次、挂在 `app.state.ih` 上。
路由通过 `Depends(get_storage)` 这类访问器拿，而不是各自 `import` 一个全局 —— 这样测试能
整体替换（`app.state.ih = 一个内存库版 AppState`），生产与测试走同一套路由代码。

依赖方向：`api → core/platforms/storage`。本模块只**取**对象，不构造（构造在 `main`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request

if TYPE_CHECKING:
    import httpx

    from intelligence_hub_v2.core.config import AppConfig, ConfigManager
    from intelligence_hub_v2.core.event_bus import EventBus
    from intelligence_hub_v2.core.task_registry import DepsFactory
    from intelligence_hub_v2.core.task_runner import TaskRunner, TaskScheduler
    from intelligence_hub_v2.platforms.registry import PlatformRegistry
    from intelligence_hub_v2.storage.db import SqliteStorage
    from intelligence_hub_v2.storage.files import FileStorage

__all__ = [
    "AppState",
    "get_app_config",
    "get_config_manager",
    "get_events",
    "get_files",
    "get_registry",
    "get_runner",
    "get_scheduler",
    "get_state",
    "get_storage",
]


@dataclass
class AppState:
    """lifespan 装配出来的运行期单例集合。"""

    config: AppConfig
    config_manager: ConfigManager
    storage: SqliteStorage
    events: EventBus
    files: FileStorage
    registry: PlatformRegistry
    runner: TaskRunner
    scheduler: TaskScheduler
    deps_factory: DepsFactory
    http: httpx.AsyncClient
    owns_http: bool = True
    """True = 这个 http 客户端是本状态建的，shutdown 时要关；注入的（测试）不关。"""

    apscheduler: Any | None = None
    """`AsyncIOScheduler` 实例（事件清理定时任务）。lifespan 起、shutdown 停。
    用 `Any` 是想把 apscheduler 的导入留在 `main`（它可选、且会拉起一堆后台线程），
    不污染这个纯数据容器。
    """

    def apply_platform_config(self, name: str, config: Any) -> None:  # noqa: ANN401 - PlatformConfig
        """把某平台的热重载后配置推到三个握有快照的运行期组件里（**内存那三处**）。

        `ConfigManager` 是唯一权威源，但 `PlatformRegistry` / `DepsFactory` / `TaskScheduler`
        各自缓存了一份 configs 用于门控与装配 —— 只更新 manager 的话，改完开关"看着生效了
        （/api/tasks 走 manager）但照样能采（scheduler 走自己那份快照）"。一个 reload 必须
        同时落到这几处，这个方法是那三份的唯一入口。

        **不包含 `platforms` 数据库镜像**：那一笔要 await，而本方法被热重载订阅者调用，
        订阅者跑在线程池里（`reload_platform` 走 `run_in_threadpool`）没有事件循环可用。
        写平台配置的路径因此必须调 `refresh_platform_state`，不是这个。
        """
        self.registry.update_config(name, config)
        self.deps_factory.update_config(name, config)
        self.scheduler.update_config(name, config)

    async def refresh_platform_state(self, name: str, config: Any) -> None:  # noqa: ANN401
        """`apply_platform_config` 的完整版：三份内存快照 + `platforms` 运行态镜像。

        镜像不跟着更新的话，`platforms.enabled` 从 PUT 那一刻起就是过期的：
        `list_all(enabled_only=True)` 会返回一个已经关掉的平台，而前端一按这一列渲染开关
        就会出现"看着开着、其实采不动"。`upsert` 刻意不动健康三列（与配置无关，
        由 preflight / 采集写），所以这里顺带写 `config` 也不会让健康灯闪一下"未检查"。
        """
        self.apply_platform_config(name, config)
        await self.storage.platforms.upsert(
            name, enabled=config.enabled, config=config.model_dump(mode="json")
        )


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "ih", None)
    if state is None:  # pragma: no cover - lifespan 没跑就说明装配错了，该响
        msg = "app.state.ih 未装配：create_app 的 lifespan 没跑，或 state 没注入"
        raise RuntimeError(msg)
    return state  # type: ignore[no-any-return]


def get_storage(state: AppState = Depends(get_state)) -> SqliteStorage:
    return state.storage


def get_events(state: AppState = Depends(get_state)) -> EventBus:
    return state.events


def get_files(state: AppState = Depends(get_state)) -> FileStorage:
    return state.files


def get_registry(state: AppState = Depends(get_state)) -> PlatformRegistry:
    return state.registry


def get_runner(state: AppState = Depends(get_state)) -> TaskRunner:
    return state.runner


def get_scheduler(state: AppState = Depends(get_state)) -> TaskScheduler:
    return state.scheduler


def get_config_manager(state: AppState = Depends(get_state)) -> ConfigManager:
    return state.config_manager


def get_app_config(state: AppState = Depends(get_state)) -> AppConfig:
    return state.config
