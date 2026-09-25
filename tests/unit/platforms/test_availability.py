"""「这一家现在能用吗」只有**一个**答案源（ADR-0025）。

`resolve_platform_availability` 那五行本身不值得单独立一个文件 —— 一眼看得完。
这个文件看的是另一件事：**四个入口拿到的是不是同一个答案**。

分叉的两种症状都真实存在过：

- `/api/tasks` 的过滤说"这一家关了"（读 ConfigManager，活的），跑前那道门却放行
  （读调度器自己 `dict(configs)` 的快照）—— 见 `docs/lessons.md` 那条"两份真相"。
- 反过来的更难查：列表里给了按钮，点下去注册表抛"已被关掉"。

所以这里的每条判据都是**并排问两三个入口**，而不是问一个入口两遍。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import get_args

import pytest

from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.core.task_registry import TASKS, available_task_names
from intelligence_hub_v2.platforms.base import (
    PlatformAvailability,
    resolve_platform_availability,
)
from intelligence_hub_v2.platforms.douyin.config import DouyinConfig
from intelligence_hub_v2.platforms.registry import PlatformRegistry

_TWO_PLATFORMS = (
    "douyin:\n  enabled: {douyin}\n  display_name: 抖音\n"
    "bilibili:\n  enabled: false\n  display_name: B站\n"
)


def _manager(tmp_path: Path, *, master: bool, douyin: bool) -> ConfigManager:
    (tmp_path / "platforms.yaml").write_text(
        _TWO_PLATFORMS.format(douyin=str(douyin).lower()), encoding="utf-8"
    )
    (tmp_path / "app.yaml").write_text(
        f"platform_control:\n  enabled: {str(master).lower()}\n", encoding="utf-8"
    )
    manager = ConfigManager(config_dir=tmp_path)
    manager.load()
    return manager


def _config(enabled: bool = True) -> DouyinConfig:
    return DouyinConfig(enabled=enabled)


# --------------------------------------------------------------------------- #
# 判据本身：四态穷举
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("master", "config", "expected"),
    [
        (True, _config(True), "available"),
        (True, _config(False), "own_off"),
        (False, _config(True), "master_off"),
        (False, _config(False), "master_off"),
        (True, None, "absent"),
        # 总闸关掉时**不许**把"没有配置对象"也一并说成"被总闸盖住"：
        # 那是装配漏了一环，去开总闸也不会好，而文案会停止进一步的排查。
        (False, None, "absent"),
    ],
)
def test_the_answer_names_the_switch_that_is_down(
    master: bool, config: DouyinConfig | None, expected: PlatformAvailability
) -> None:
    assert resolve_platform_availability(config, master_enabled=master) == expected


def test_available_is_the_only_state_that_means_yes() -> None:
    """四态里只有一个算"能采"。这条看护的是**穷举**：以后加第五态时这里会红，
    于是逼着加的人去想四个消费者各该把它算成是还是否 —— 而不是靠 `!= "absent"` 这种
    默认放行的写法混过去。
    """
    states = set(get_args(PlatformAvailability))
    assert states == {"available", "own_off", "master_off", "absent"}

    # 每个状态各造一次输入，再问"算不算可用"。少一个状态就有一行输入落不到任何断言上，
    # 上面那句穷举断言就是防这个的。
    cases: dict[PlatformAvailability, tuple[DouyinConfig | None, bool]] = {
        "available": (_config(True), True),
        "own_off": (_config(False), True),
        "master_off": (_config(True), False),
        "absent": (None, True),
    }
    assert set(cases) == states, "有一个状态没被上面的输入表覆盖"

    for state, (config, master) in cases.items():
        resolved = resolve_platform_availability(config, master_enabled=master)
        assert resolved == state
        assert (resolved == "available") is (state == "available")


# --------------------------------------------------------------------------- #
# 三个入口并排问
# --------------------------------------------------------------------------- #


def _registry(manager: ConfigManager, master: Callable[[], bool]) -> PlatformRegistry:
    configs = {name: manager.get_platform(name) for name in manager.platform_names()}
    return PlatformRegistry(
        configs,
        lambda name: object(),  # type: ignore[arg-type]
        master_enabled=master,
    )


@pytest.mark.parametrize(("master", "douyin"), [(True, True), (True, False), (False, True)])
def test_the_manager_the_registry_and_the_task_filter_give_the_same_answer(
    tmp_path: Path, master: bool, douyin: bool
) -> None:
    """同一个 (总闸, 自身开关) 组合，三处问出同一个"能/不能"。

    三处各自实现一次 AND 是这个功能最容易的死法：`/api/tasks` 少一个按钮、
    采集门多放行一条，或者反过来 —— 所以这里把三处并排问，而不是信任它们长得像。
    """
    manager = _manager(tmp_path, master=master, douyin=douyin)
    configs = {name: manager.get_platform(name) for name in manager.platform_names()}

    from_manager = manager.is_platform_available("douyin")
    from_registry = "douyin" in _registry(manager, lambda: master).enabled_platforms()
    listed = "douyin_collect" in available_task_names(configs, master_enabled=master)

    assert from_manager == (douyin and master), "输入组合与预期不符，用例白写"
    assert from_registry == from_manager, "注册表与配置层口径分叉"
    assert listed == from_manager, "/api/tasks 的过滤与配置层口径分叉"


def test_an_absent_platform_is_not_counted_available_by_the_registry(
    tmp_path: Path,
) -> None:
    """注册表只认**自己有配置**的平台。没配置对象的一家不能因为"总闸开着"就出现在名单里。"""
    manager = _manager(tmp_path, master=True, douyin=True)
    registry = _registry(manager, lambda: True)
    assert registry.enabled_platforms() == ["douyin"]
    assert "youtube" not in registry.enabled_platforms()


def test_the_master_switch_is_read_at_call_time_not_at_assembly(tmp_path: Path) -> None:
    """翻转总闸之后**不许**重建注册表才生效。

    `write_platform_control` 换的是共享 `AppConfig` 上那个属性（见它的 docstring 为什么
    是原地换），所以注册表拿到的必须是"每次问现读一次"的闭包。
    传一个 bool 进去也能让今天的用例全绿，症状是"设置页显示总闸已开，任务列表还是空的"，
    要等到重启才恢复 —— 那是这条用例存在的唯一理由。
    """
    manager = _manager(tmp_path, master=False, douyin=True)
    registry = _registry(manager, lambda: manager.app.platform_control.enabled)
    assert registry.enabled_platforms() == []

    manager.write_platform_control(True)
    assert registry.enabled_platforms() == ["douyin"], "总闸翻回来之后注册表还看着旧值"


def test_the_master_off_does_not_erase_cross_platform_tasks(tmp_path: Path) -> None:
    """`platforms=()` 的那几个（预检 / 收一条 / 收录博主 / 转写 / 补读数）不属于任何一家，
    总闸关掉它们必须还在名单里 —— 否则"关了采集就连不上本地已有视频的转写"这种
    连带伤害会被包装成"开关只管采不采"，而它实际管的是"整个系统还能不能用"。
    """
    manager = _manager(tmp_path, master=False, douyin=True)
    configs = {name: manager.get_platform(name) for name in manager.platform_names()}

    names = set(available_task_names(configs, master_enabled=manager.app.platform_control.enabled))
    assert {"preflight", "single_link", "add_creator", "postprocess", "enrich_metrics"} <= names
    assert not {n for n in names if TASKS[n].platforms}, "带平台标签的任务一家都不该留"
