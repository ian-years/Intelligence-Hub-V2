"""`TASKS` 注册表 + 适配器依赖袋的装配（`PlatformRegistry` / `AdapterDeps`）。

`task-runner.md §3` 的 13 个任务在这里全部登记，其中 **10 个有真 handler**：
preflight / 四家 collect / single_link / add_creator / postprocess / enrich_metrics /
**backfill（V2.2 补的这一格，§7.22 那条陷阱的正面实现）**。
其余 3 个（all_platforms / feishu_sync / migrate_from_v1）
仍进注册表、runner 是 `NotImplementedError` —— 两个理由：

1. 未实现平台的采集任务受平台开关过滤，本来就不会出现在 `/api/tasks`
   （`available_task_names` 按 `def.platforms ⊆ 可用家` 筛，
   而"可用"是 `总闸 AND 这一家自己的 enabled` —— ADR-0025）。
2. 万一被点名运行，也**如实红在 NotImplemented**，而不是"找不到任务"那种含糊的 404 ——
   把没做的东西藏起来正是 V1「看起来在跑」的形状。

`DepsFactory` 是"造 `AdapterDeps`"的唯一地方：桥客户端按平台 `capabilities.needs_browser`
决定给不给（V1 §7.20 装配错误要能被适配器在 healthcheck 里看见，而不是第一个请求 AttributeError）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog

from intelligence_hub_v2.errors import TaskRejected
from intelligence_hub_v2.infra.cdp_bridge import BridgeClient
from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.models.task import TaskKind
from intelligence_hub_v2.platforms.base import (
    AdapterDeps,
    PlatformConfig,
    resolve_platform_availability,
)
from intelligence_hub_v2.platforms.registry import PLATFORMS, PlatformRegistry
from intelligence_hub_v2.tasks import (
    AddCreatorParams,
    BackfillParams,
    CollectParams,
    EnrichMetricsParams,
    PostprocessParams,
    PreflightParams,
    SingleLinkParams,
    TaskDefinition,
    make_collect_handler,
    run_add_creator,
    run_backfill,
    run_enrich_metrics,
    run_postprocess,
    run_preflight,
    run_single_link,
)

if TYPE_CHECKING:
    import httpx

    from intelligence_hub_v2.core.config import AppConfig
    from intelligence_hub_v2.core.event_bus import EventBus
    from intelligence_hub_v2.storage.db import SqliteStorage
    from intelligence_hub_v2.storage.files import FileStorage

__all__ = [
    "TASKS",
    "DepsFactory",
    "available_task_names",
    "build_registry",
    "get_task",
    "resolve_timeout",
    "task_is_available",
]


def _not_implemented(name: str) -> Any:  # noqa: ANN401 - 返回一个协程函数，形状由 TaskDefinition 卡
    message = f"任务 {name!r} 在 V2.0 未实现（见 ROADMAP V2.1 / V2.2）"

    def runner(ctx: object, params: Any) -> Any:  # noqa: ANN401
        del ctx, params
        raise NotImplementedError(message)

    return runner


TASKS: dict[str, TaskDefinition] = {
    "preflight": TaskDefinition(
        name="preflight",
        display_name="环境预检",
        kind=TaskKind.PREFLIGHT,
        params_schema=PreflightParams,
        platforms=(),
        requires=(),
        timeout_seconds=60,
        cancellable=False,
        runner=run_preflight,
    ),
    "douyin_collect": TaskDefinition(
        name="douyin_collect",
        display_name="抖音采集",
        kind=TaskKind.PLATFORM_COLLECT,
        params_schema=CollectParams,
        platforms=("douyin",),
        requires=("cdp_bridge", "cookies:douyin"),
        timeout_seconds=1800,
        cancellable=True,
        runner=make_collect_handler("douyin"),
    ),
    "bilibili_collect": TaskDefinition(
        name="bilibili_collect",
        display_name="B站采集",
        kind=TaskKind.PLATFORM_COLLECT,
        params_schema=CollectParams,
        platforms=("bilibili",),
        requires=("cookies:bilibili",),
        timeout_seconds=1800,
        cancellable=True,
        runner=make_collect_handler("bilibili"),
    ),
    "single_link": TaskDefinition(
        name="single_link",
        display_name="收一条作品",
        kind=TaskKind.SINGLE_LINK,
        params_schema=SingleLinkParams,
        platforms=(),
        requires=(),
        timeout_seconds=600,
        cancellable=True,
        runner=run_single_link,
    ),
    "add_creator": TaskDefinition(
        name="add_creator",
        display_name="收录博主",
        kind=TaskKind.ADD_CREATOR,
        params_schema=AddCreatorParams,
        platforms=(),
        requires=(),
        timeout_seconds=120,
        cancellable=False,
        runner=run_add_creator,
    ),
    "postprocess": TaskDefinition(
        name="postprocess",
        display_name="字幕 / 转写",
        kind=TaskKind.POSTPROCESS,
        params_schema=PostprocessParams,
        platforms=(),
        # T1.2 起这条路真跑本地 ASR（字幕优先不变）。`requires` 仍留空：调度器执行前
        # 检查 requires 的那道闸还没实现（`_check_requires` 要一份带 TTL 的能力快照，
        # 见 ROADMAP 待办池与 ADR-0014），现在声明它等于声明一道纸面防护。
        # 引擎缺位由 handler 自己如实报红（`asr_blocked` + AsrUnavailable failure）。
        requires=(),
        timeout_seconds=900,
        cancellable=True,
        runner=run_postprocess,
    ),
    "enrich_metrics": TaskDefinition(
        name="enrich_metrics",
        display_name="补一次读数",
        kind=TaskKind.ENRICH_METRICS,
        params_schema=EnrichMetricsParams,
        platforms=(),
        # 与 postprocess 同口径：`requires` 那道闸还没实现（ADR-0014 的预留位），
        # 现在声明它等于声明一道纸面防护。缺什么由适配器自己红。
        requires=(),
        # 一条作品最多 = 一次 view + 一次评论翻页（`comments_limit` 给了才翻）。
        # 50 条 × 2 个请求按 `per_minute=30` 是三分钟出头，900 秒留了一倍余量。
        timeout_seconds=900,
        cancellable=True,
        runner=run_enrich_metrics,
    ),
    # ---- 以下 6 个：登记但不实现（见模块 docstring）----
    "xiaohongshu_collect": TaskDefinition(
        name="xiaohongshu_collect",
        display_name="小红书采集",
        kind=TaskKind.PLATFORM_COLLECT,
        params_schema=CollectParams,
        platforms=("xiaohongshu",),
        requires=("cdp_bridge",),
        timeout_seconds=1800,
        cancellable=True,
        runner=make_collect_handler("xiaohongshu"),
        implemented=True,
    ),
    "youtube_collect": TaskDefinition(
        name="youtube_collect",
        display_name="YouTube 采集",
        kind=TaskKind.PLATFORM_COLLECT,
        params_schema=CollectParams,
        platforms=("youtube",),
        # 没有 `cookies:youtube` 那一格：YouTubeAdapter 声明 needs_cookies=False。
        # `ffmpeg` 是**媒体合并**的前置（缺了 download_media 会判失败），
        # 与 B站 不同处在于这里不列也不会有人误以为能跑 —— 这道闸本身还没实现，
        # 声明它是纸面防护（见 ADR-0014 与本文件 `_check_requires` 那段）。
        requires=("ffmpeg",),
        timeout_seconds=1800,
        cancellable=True,
        runner=make_collect_handler("youtube"),
        implemented=True,
    ),
    "all_platforms": TaskDefinition(
        name="all_platforms",
        display_name="一键全平台",
        kind=TaskKind.ALL_PLATFORMS,
        params_schema=CollectParams,
        platforms=(),
        requires=(),
        timeout_seconds=None,
        cancellable=True,
        runner=_not_implemented("all_platforms"),
        implemented=False,
    ),
    "backfill": TaskDefinition(
        name="backfill",
        display_name="爆款回溯",
        kind=TaskKind.BACKFILL,
        params_schema=BackfillParams,
        # **故意留空**（与 single_link / add_creator 同形）：这一家是谁由链接决定。
        # 列上四家的话，"关掉一家"会让**另三家**的回溯按钮一起消失 —— 那是
        # `platforms ⊆ 可用家` 这条筛法配不上"运行时才知道平台"的任务形状。
        # 代价是：闸关着时这里不会在 `submit` 处 422，而是跑到 handler 里
        # 由 `detect_platform` 如实红（V1 §7.22 的分工只讲开关不看，不讲这道闸在哪一层）。
        platforms=(),
        # `requires` 同 postprocess / enrich_metrics 的口径：`_check_requires` 那道闸
        # 还没实现（ADR-0014 的预留位），声明它等于声明一道纸面防护。
        # 何况这里连该要哪一家的 cookie 都要先认了链接才知道 —— 静态声明给不出来。
        requires=(),
        timeout_seconds=1800,
        cancellable=True,
        runner=run_backfill,
    ),
    "feishu_sync": TaskDefinition(
        name="feishu_sync",
        display_name="飞书同步",
        kind=TaskKind.SYNC,
        params_schema=PreflightParams,
        platforms=(),
        requires=("lark_cli",),
        timeout_seconds=600,
        cancellable=True,
        runner=_not_implemented("feishu_sync"),
        implemented=False,
    ),
    "migrate_from_v1": TaskDefinition(
        name="migrate_from_v1",
        display_name="V1 数据迁移",
        kind=TaskKind.MIGRATE,
        params_schema=PreflightParams,
        platforms=(),
        requires=(),
        timeout_seconds=None,
        cancellable=False,
        runner=_not_implemented("migrate_from_v1"),
        implemented=False,
    ),
}


def get_task(name: str) -> TaskDefinition:
    """按名字取任务定义。未知名字抛 `TaskRejected`（API 层翻 404/422）。"""
    definition = TASKS.get(name)
    if definition is None:
        known = ", ".join(sorted(TASKS))
        msg = f"未知任务 {name!r}。已登记的：{known}"
        raise TaskRejected(msg)
    return definition


def task_is_available(
    definition: TaskDefinition,
    configs: Mapping[str, PlatformConfig],
    *,
    master_enabled: bool,
) -> bool:
    """任务能不能出现在 `/api/tasks`：得是真实现的，且它涉及的每个平台都**可用**。

    跨平台任务（`platforms=()`）只过"实现"这一关 —— 总闸关掉它们也在，
    这是"关掉四家采集"与"整个系统不能用"的分界线（ADR-0025）。

    `master_enabled` 没有默认值：漏传一次的后果是"界面少了按钮但点名还能跑"这类
    两边各说一半的谎，而默认值 `True` 恰好让新增调用点全都漏得无声无息。
    判据本身在 `platforms/base.py::resolve_platform_availability`，与注册表、
    跑前那道门共用一份。

    未实现的（V2.0 的另外 4 个）恒 False —— 不显示成一个点了会 500 的按钮。
    """
    if not definition.implemented:
        return False
    if definition.is_multiplatform:
        return True
    return all(
        resolve_platform_availability(configs.get(platform), master_enabled=master_enabled)
        == "available"
        for platform in definition.platforms
    )


def available_task_names(
    configs: Mapping[str, PlatformConfig], *, master_enabled: bool
) -> list[str]:
    """当前可运行的任务名，按注册顺序。`configs` 是"各家自己的开关"那份快照。"""
    return [
        name
        for name, definition in TASKS.items()
        if task_is_available(definition, configs, master_enabled=master_enabled)
    ]


def resolve_timeout(definition: TaskDefinition, overrides: Mapping[str, int]) -> int | None:
    """任务超时：`scheduler.task_timeout_seconds` 按 TaskKind 覆盖定义里的默认。

    覆盖优先于定义：调参（"这轮抖音为什么老超时"）不该要求改代码重发版。
    """
    return overrides.get(definition.kind.value, definition.timeout_seconds)


class DepsFactory:
    """按平台造 `AdapterDeps`。`PlatformRegistry` 的 `deps_factory` 就是它的 `__call__`。"""

    def __init__(
        self,
        *,
        configs: Mapping[str, PlatformConfig],
        app_config: AppConfig,
        storage: SqliteStorage,
        events: EventBus,
        files: FileStorage,
        http: httpx.AsyncClient,
        logger: structlog.BoundLogger,
    ) -> None:
        self._configs: dict[str, PlatformConfig] = dict(configs)
        self._app = app_config
        self._storage = storage
        self._events = events
        self._http = http
        self._logger = logger
        self._cookies = CookieManager(files)

    def update_config(self, name: str, config: PlatformConfig) -> None:
        """热重载后换掉某平台的配置。`PlatformRegistry` / `TaskScheduler` 各有一份同样的动作。"""
        self._configs[name] = config

    def __call__(self, platform: str) -> AdapterDeps:
        cls = PLATFORMS.get(platform)
        needs_browser = cls is not None and cls.capabilities.needs_browser
        bridge = None
        if needs_browser and self._app.cdp_bridge.enabled:
            bridge = BridgeClient(str(self._app.cdp_bridge.url), http=self._http)
        config = self._configs[platform]
        return AdapterDeps(
            config=config,
            storage=self._storage,
            events=self._events,
            http=self._http,
            logger=self._logger,
            cookies=self._cookies,
            bridge=bridge,
        )

    def default(self) -> AdapterDeps:
        """无平台任务（preflight / postprocess / single_link）的 `ctx.deps`。

        借第一个已配置平台造一份（只为拿到能用的 http / cookies / logger）；
        handler 用 `ctx.deps.http` 走展开短链这类通用请求，不碰平台专属字段。
        """
        for name in self._configs:
            return self(name)
        msg = "没有任何已配置的平台，无法为全局任务装配 AdapterDeps"
        raise TaskRejected(msg)


def build_registry(
    *,
    configs: Mapping[str, PlatformConfig],
    app_config: AppConfig,
    storage: SqliteStorage,
    events: EventBus,
    files: FileStorage,
    http: httpx.AsyncClient,
    logger: structlog.BoundLogger | None = None,
) -> PlatformRegistry:
    """装配一个 `PlatformRegistry`（把 `DepsFactory` 接进去）。Task 9 的 lifespan 用它。"""
    log = logger if logger is not None else structlog.get_logger("tasks.registry")
    factory = DepsFactory(
        configs=configs,
        app_config=app_config,
        storage=storage,
        events=events,
        files=files,
        http=http,
        logger=log,
    )
    return PlatformRegistry(
        configs,
        factory,
        # 闭包而不是 `app_config.platform_control.enabled` 的**值**：总闸会被设置页翻，
        # 而翻的是这个共享 `AppConfig` 实例上的属性（见 `write_platform_control` 为什么原地换）。
        master_enabled=lambda: app_config.platform_control.enabled,
    )
