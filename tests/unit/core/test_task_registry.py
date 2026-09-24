"""`core/task_registry.py`：12 个任务全登记、6 个真实现、过滤与依赖袋装配。"""

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
}


def test_twelve_registered_eight_implemented() -> None:
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
    both = available_task_names(_configs())
    assert {
        "preflight",
        "douyin_collect",
        "bilibili_collect",
        "single_link",
        "add_creator",
        "postprocess",
    } == set(both)

    only_douyin = set(available_task_names(_configs(bilibili=False)))
    assert "bilibili_collect" not in only_douyin
    assert "douyin_collect" in only_douyin
    # 跨平台的实现任务永远在
    assert {"preflight", "single_link", "add_creator", "postprocess"} <= only_douyin


def test_unimplemented_never_available_even_if_platform_turned_on() -> None:
    # xiaohongshu 从没在 configs 里，且未实现 → 双重不可用
    assert not task_is_available(TASKS["xiaohongshu_collect"], _configs())
    assert "all_platforms" not in available_task_names(_configs())


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
