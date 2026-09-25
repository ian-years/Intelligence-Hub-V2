"""`core/task_registry.py`：13 个任务全登记、9 个真实现、过滤与依赖袋装配。

两个集合都是**快照式**看护（改注册表就要在这里留痕）。它们存在的意义不是数数，
而是"登记了一条但没有实现"与"实现了但没登记"这两种漂法各红一次：前者让前端
按钮点开就 `TaskRejected`，后者让一个已经能跑的任务在 `/api/tasks` 里不存在。
"""

from __future__ import annotations

import httpx
import pytest
from tests.unit.tasks.conftest import FakeBus, FakeLogger

from intelligence_hub_v2.core.config import AppConfig
from intelligence_hub_v2.core.task_registry import (
    TASKS,
    DepsFactory,
    available_task_names,
    build_registry,
    get_task,
    resolve_timeout,
    task_is_available,
)
from intelligence_hub_v2.errors import TaskRejected
from intelligence_hub_v2.models.task import TaskKind
from intelligence_hub_v2.platforms.bilibili.config import BilibiliConfig
from intelligence_hub_v2.platforms.douyin.config import DouyinConfig
from intelligence_hub_v2.platforms.registry import PlatformRegistry

IMPLEMENTED_NOW = {
    "preflight",
    "douyin_collect",
    "bilibili_collect",
    "xiaohongshu_collect",
    "youtube_collect",
    "single_link",
    "add_creator",
    "postprocess",
    "enrich_metrics",
}


def test_thirteen_registered_nine_implemented() -> None:
    assert set(TASKS) == {
        "preflight",
        "douyin_collect",
        "bilibili_collect",
        "xiaohongshu_collect",
        "youtube_collect",
        "all_platforms",
        "single_link",
        "add_creator",
        "backfill",
        "postprocess",
        "feishu_sync",
        "migrate_from_v1",
        "enrich_metrics",
    }
    implemented = {name for name, d in TASKS.items() if d.implemented}
    assert implemented == IMPLEMENTED_NOW


def test_registry_name_matches_definition_name() -> None:
    """注册表 key 与 TaskDefinition.name 必须一致（否则 `task_id`/事件对不上）。"""
    for key, definition in TASKS.items():
        assert definition.name == key


def test_definition_names_are_unique_across_kind() -> None:
    assert TASKS["douyin_collect"].kind is TaskKind.PLATFORM_COLLECT
    assert TASKS["add_creator"].kind is TaskKind.ADD_CREATOR


async def test_get_task_unknown_rejects() -> None:
    with pytest.raises(TaskRejected, match="未知任务"):
        get_task("nope")


def _configs(*, douyin: bool = True, bilibili: bool = True) -> dict:
    return {
        "douyin": DouyinConfig(enabled=douyin),
        "bilibili": BilibiliConfig(enabled=bilibili),
    }


def test_available_tasks_respect_platform_switch_and_implementation() -> None:
    both = available_task_names(_configs(), master_enabled=True)
    assert {
        "preflight",
        "douyin_collect",
        "bilibili_collect",
        "single_link",
        "add_creator",
        "postprocess",
        # 跨平台且已实现：它不属于任何一家，所以关掉平台也不该消失
        "enrich_metrics",
    } == set(both)

    only_douyin = set(available_task_names(_configs(bilibili=False), master_enabled=True))
    assert "bilibili_collect" not in only_douyin
    assert "douyin_collect" in only_douyin
    # 跨平台的实现任务永远在
    assert {"preflight", "single_link", "add_creator", "postprocess"} <= only_douyin
    assert "enrich_metrics" in only_douyin, "补读数挑活是全平台的，不该跟着平台开关消失"


def test_the_master_switch_removes_platform_tasks_and_keeps_the_rest() -> None:
    """总闸关掉 = "**每一家**都不可用"，所以这里应当与"四家逐个关掉"同一结果。

    留着的四个正是那条边界的另一侧：它们不属于任何一家，
    关掉四家采集不该连"给本地已有视频跑一次转写"一起停掉（ADR-0025）。
    """
    off = set(available_task_names(_configs(), master_enabled=False))
    assert not (off & {"douyin_collect", "bilibili_collect"})
    assert {"preflight", "single_link", "add_creator", "postprocess", "enrich_metrics"} <= off
    # 与"逐个关"同一份答案 —— 两条路径如果各算各的，这里就会分叉
    assert off == set(
        available_task_names(_configs(douyin=False, bilibili=False), master_enabled=True)
    )
    assert not task_is_available(TASKS["douyin_collect"], _configs(), master_enabled=False)
    assert task_is_available(TASKS["preflight"], _configs(), master_enabled=False)


def test_unimplemented_never_available_even_if_platform_turned_on() -> None:
    # xiaohongshu 从没在 configs 里，且未实现 → 双重不可用
    assert not task_is_available(TASKS["xiaohongshu_collect"], _configs(), master_enabled=True)
    assert "all_platforms" not in available_task_names(_configs(), master_enabled=True)


def test_resolve_timeout_prefers_scheduler_override() -> None:
    definition = TASKS["preflight"]
    assert resolve_timeout(definition, {TaskKind.PREFLIGHT.value: 5}) == 5
    assert resolve_timeout(definition, {}) == definition.timeout_seconds
    assert resolve_timeout(TASKS["all_platforms"], {}) is None


def _factory(app_config: AppConfig, storage, files) -> DepsFactory:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    return DepsFactory(
        configs=_configs(),
        app_config=app_config,
        storage=storage,
        events=FakeBus(),  # type: ignore[arg-type]
        files=files,
        http=httpx.AsyncClient(transport=transport),
        logger=FakeLogger(),  # type: ignore[arg-type]
    )


async def test_douyin_gets_bridge_bilibili_does_not(storage, files) -> None:
    """桥客户端只给声明了 `needs_browser=True` 的平台（V1 §7.20 装配错误的分水岭）。"""
    factory = _factory(AppConfig(), storage, files)
    assert factory("douyin").bridge is not None
    assert factory("bilibili").bridge is None


async def test_bridge_none_when_cdp_disabled(storage, files) -> None:
    factory = _factory(AppConfig(cdp_bridge={"enabled": False}), storage, files)
    assert factory("douyin").bridge is None


async def test_default_deps_picks_a_configured_platform(storage, files) -> None:
    factory = _factory(AppConfig(), storage, files)
    deps = factory.default()
    assert deps.http is not None and deps.cookies is not None


def test_build_registry_is_a_registry(storage, files) -> None:
    registry = build_registry(
        configs=_configs(),
        app_config=AppConfig(),
        storage=storage,
        events=FakeBus(),  # type: ignore[arg-type]
        files=files,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )
    assert isinstance(registry, PlatformRegistry)
    assert registry.enabled_platforms() == ["bilibili", "douyin"]
