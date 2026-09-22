"""FastAPI 应用入口：装配单例（lifespan）、挂路由、映射异常、生产下托管前端静态文件。

装配链（`docs/architecture.md` 的"配置热重载 / 启动"）：
`ConfigManager.load()` → `SqliteStorage` → `InProcessEventBus` → `DepsFactory`+`PlatformRegistry`
→ `TaskRunner` → `TaskScheduler`，全塞进一个 `AppState` 挂到 `app.state.ih`。
路由通过 `api/deps.py` 的访问器取，测试能整体换一个内存库版 `AppState`。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from intelligence_hub_v2 import __version__
from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.api.v1.router import api_router
from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.core.event_bus import InProcessEventBus
from intelligence_hub_v2.core.task_registry import DepsFactory
from intelligence_hub_v2.core.task_runner import TaskRunner, TaskScheduler
from intelligence_hub_v2.errors import (
    ConflictError,
    IntelligenceHubError,
    NotFoundError,
    PlatformError,
    StorageError,
    TaskRejected,
)
from intelligence_hub_v2.logging import get_logger, setup_logging
from intelligence_hub_v2.platforms.registry import PlatformRegistry
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

if TYPE_CHECKING:
    from intelligence_hub_v2.platforms.base import PlatformConfig

logger = get_logger(__name__)


def _platform_configs(manager: ConfigManager) -> dict[str, PlatformConfig]:
    return {name: manager.get_platform(name) for name in manager.platform_names()}


def build_components(
    manager: ConfigManager,
    *,
    storage: SqliteStorage | None = None,
    files: FileStorage | None = None,
    http: httpx.AsyncClient | None = None,
) -> AppState:
    """装配运行期单例（**不** initialize storage —— 那是 lifespan 的 async 活）。"""
    config = manager.app
    storage = storage or SqliteStorage.from_config(config)
    files = files or FileStorage.from_config(config)
    events = InProcessEventBus(events=storage.events)
    owns_http = http is None
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(config.http_client.timeout_seconds))

    configs = _platform_configs(manager)
    deps_factory = DepsFactory(
        configs=configs,
        app_config=config,
        storage=storage,
        events=events,
        files=files,
        http=http,
        logger=get_logger("tasks.deps"),
    )
    registry = PlatformRegistry(configs, deps_factory)
    runner = TaskRunner(
        storage=storage,
        events=events,
        files=files,
        registry=registry,
        deps_factory=deps_factory,
        app_config=config,
    )
    scheduler = TaskScheduler(runner=runner, configs=configs, app_config=config, storage=storage)
    return AppState(
        config=config,
        config_manager=manager,
        storage=storage,
        events=events,
        files=files,
        registry=registry,
        runner=runner,
        scheduler=scheduler,
        deps_factory=deps_factory,
        http=http,
        owns_http=owns_http,
    )


async def _sync_platform_mirror(state: AppState) -> None:
    """把 ConfigManager 校验过的配置灌进 `platforms` 运行态镜像 + 清掉不再支持的行。

    镜像不是权威源（`platforms.yaml` 才是）；这张表只为让前端一次查询拿到"开关 + 健康"。
    `prune_unknown` 清的是换版本后残留的行（V2.1 跑过、换回 V2.0），否则界面有个点了没反应的平台。
    """
    manager = state.config_manager
    for name in manager.platform_names():
        cfg = manager.get_platform(name)
        await state.storage.platforms.upsert(
            name, enabled=cfg.enabled, config=cfg.model_dump(mode="json")
        )
    await state.storage.platforms.prune_unknown(manager.platform_names())


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = app.state.ih
    setup_logging(
        level=state.config.logging.level,
        fmt=state.config.logging.format,
        log_file=(
            state.files.log_file(state.config.logging.file) if state.config.logging.file else None
        ),
        rotate_max_bytes=state.config.logging.rotate_max_bytes,
        rotate_backup_count=state.config.logging.rotate_backup_count,
    )
    await state.storage.initialize()
    state.files.ensure_dirs()
    await _sync_platform_mirror(state)

    # 上次进程崩了会留下永远停在 running 的行 —— 启动时标成 failed，比让它们假装还活着诚实。
    orphans = await state.runner.reap_orphans(reason="服务重启，上一次进程内的任务已中断")
    if orphans:
        logger.warning("startup.reaped_orphans", count=orphans)

    # 配置热重载之后：换掉注册表/依赖袋/调度器各自缓存的配置 + 丢掉适配器实例。
    # 只 invalidate 不够 —— 那三处都握着 configs 快照，改开关必须同时落到它们。
    state.config_manager.subscribe(state.apply_platform_config)

    _start_background_jobs(state)
    logger.info(
        "startup.complete", version=__version__, platforms=state.registry.enabled_platforms()
    )
    try:
        yield
    finally:
        if state.apscheduler is not None:
            state.apscheduler.shutdown(wait=False)
        await state.scheduler.shutdown()
        await state.events.shutdown()
        if state.owns_http:
            await state.http.aclose()
        await state.storage.close()


def _start_background_jobs(state: AppState) -> None:
    """APScheduler：每天清一次过期事件。cron 采集调度留到前端暴露那版（`ROADMAP` 待办池）。"""
    if not state.config.scheduler.enabled:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    async def _prune() -> None:
        removed = await state.storage.events.prune(state.config.storage.event_retention_days)
        if removed:
            logger.info("jobs.events_pruned", removed=removed)

    scheduler = AsyncIOScheduler(timezone=state.config.scheduler.timezone)
    scheduler.add_job(_prune, "interval", days=1, id="prune_events", coalesce=True)
    scheduler.start()
    state.apscheduler = scheduler


def _register_exception_handlers(app: FastAPI) -> None:
    mapping: dict[type[Exception], int] = {
        NotFoundError: 404,
        ConflictError: 409,
        TaskRejected: 422,
        PlatformError: 400,
        StorageError: 500,
    }

    async def _domain_error(_request: Request, exc: Exception) -> JSONResponse:
        code = 500
        for exc_type, status_code in mapping.items():
            if isinstance(exc, exc_type):
                code = status_code
                break
        return JSONResponse(
            status_code=code, content={"detail": str(exc), "kind": type(exc).__name__}
        )

    async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
        # 注册在 ValidationError 上，签名必须是 (Request, Exception) 才合 Starlette 的类型；
        # 这里 narrow 一次拿 errors，别的异常类型给个空列表（正常走不到）。
        errors = exc.errors(include_url=False) if isinstance(exc, ValidationError) else []
        return JSONResponse(status_code=422, content={"detail": "参数校验失败", "errors": errors})

    app.add_exception_handler(IntelligenceHubError, _domain_error)
    app.add_exception_handler(ValidationError, _validation_error)


def create_app(
    *,
    config_manager: ConfigManager | None = None,
    state: AppState | None = None,
    config_dir: Path | str = "config",
) -> FastAPI:
    """应用工厂。测试传 `state=`（内存库 + tmp 文件 + mock http）注入自己装配好的 `AppState`。

    不传 state 时走生产路径：`ConfigManager(config_dir).load()` → `build_components`。
    """
    if state is None:
        if config_manager is None:
            config_manager = ConfigManager(config_dir)
            config_manager.load()
        state = build_components(config_manager)

    app = FastAPI(
        title="Intelligence Hub V2",
        version=__version__,
        lifespan=_lifespan,
        docs_url=state.config.app.docs_url,
        redoc_url=state.config.app.redoc_url,
        openapi_url=state.config.app.openapi_url,
    )
    app.state.ih = state

    if state.config.app.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=state.config.app.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    _register_exception_handlers(app)

    app.include_router(api_router, prefix="/api")

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """生产：`vite build` 产物存在就挂到 `/`（单端口）。开发时 `frontend/dist` 不存在 → 跳过。"""
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")


def cli() -> None:
    """`intelligence-hub` 启动入口（uvicorn factory 模式）。"""
    import uvicorn

    manager = ConfigManager(Path("config"))
    app_config = manager.load()
    if app_config.app.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"⚠️  警告：host={app_config.app.host} 不是回环地址。CDP 桥会在你已登录的浏览器里"
            "执行任意 JS，把工作台暴露到局域网等于交出账号（V1 §1.2 硬约束）。",
            file=sys.stderr,
        )
    uvicorn.run(
        "intelligence_hub_v2.main:create_app",
        factory=True,
        host=app_config.app.host,
        port=app_config.app.port,
    )


__all__ = ["build_components", "cli", "create_app"]
