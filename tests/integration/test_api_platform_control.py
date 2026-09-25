"""/api/platform-control 与它牵动的四处口径（ADR-0025 的接口侧）。

这里看护的重心不是"能不能写那个布尔"，而是**一次 PUT 之后各处口径是否同步**：

- `/api/platforms` 每一行的 `enabled`（它自己的开关）**不许**被总闸改写 ——
  改了的话"重新打开总闸"就把你之前单独关掉的某家悄悄复原，而界面上分不出来。
- 同一行要新增 `availability`，让"你自己关了"与"被总闸盖住了"是两种看得见的状态。
- `/api/tasks` 少掉带平台标签的那几个，`platforms=()` 的那几个**必须还在**。
- `/api/schedule` 的 `effective_platforms` 变空，`skipped_platforms` 说清为什么空。

装配沿用 `tests/integration/conftest.py`：真 app + 内存库 + tmp 配置目录 + mock http，
**不跑 lifespan**（所以 `state.apscheduler is None`，采集 job 那一路只能验到"该排谁"的名单）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

# 装配与收尾都**借隔壁那两个函数**，不在这里复刻一遍：`_state` 带的是真 `start()` 过的
# `AsyncIOScheduler` 且按产品代码排好采集 job，`_close` 带的是那套收尾顺序
# （先停 job 再关 http 再关库）。复刻一份的常态是漏掉 `shutdown()`，
# 然后 `filterwarnings=error` 让**下一条**用例红，报的还是无辜的那一条。
#
# 为什么借函数而不是直接 import 那个 `harness` fixture：pytest 按**参数名**注入，
# 用例的参数就得叫 `harness`，于是「模块里导入的那个名字」与「同名参数」必然撞
# （ruff F811）。本地包一层薄 fixture 之后，这一格要什么状态是写在本文件里的。
from tests.integration.test_api_schedule import _close, _state

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.main import create_app
from intelligence_hub_v2.models.event import EventType

_PLATFORM_NAMES = ("douyin", "bilibili", "xiaohongshu", "youtube")
_COLLECT_TASKS = {f"{name}_collect" for name in _PLATFORM_NAMES}
_CROSS_PLATFORM_TASKS = {
    "preflight",
    "single_link",
    "add_creator",
    "postprocess",
    "enrich_metrics",
}


async def _get_master(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/platform-control")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _set_master(client: httpx.AsyncClient, enabled: bool) -> dict[str, Any]:
    response = await client.put("/api/platform-control", json={"enabled": enabled})
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _rows(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    body = (await client.get("/api/platforms")).json()
    return {row["name"]: row for row in body["platforms"]}


async def _own_switch(client: httpx.AsyncClient, platform: str, enabled: bool) -> None:
    """只改这一家自己的开关（PUT 的 body 允许只写变化的那几格）。"""
    response = await client.put(f"/api/platforms/{platform}/config", json={"enabled": enabled})
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 翻转要真的重排 APScheduler 上的 job
# --------------------------------------------------------------------------- #


def _collect_job_ids(state: AppState) -> list[str]:
    scheduler: Any = state.apscheduler
    if scheduler is None:
        return []
    return sorted(str(job.id) for job in scheduler.get_jobs() if str(job.id).startswith("collect:"))


@pytest.fixture
async def scheduler_state(tmp_path: Path) -> AsyncIterator[AppState]:
    """带**真 APScheduler**（已 `start()`）且按产品代码排好采集 job 的一份 state。

    本文件默认那份 `app_state` 里 `apscheduler is None` —— 那一格只能验到
    "该排谁"的名单，验不到"调度器上真有什么"。R6 那次变异（PUT 里不重排 job）
    正是从这一缝里活下来的：名单每次现算，所以看着全对，
    而 job 还挂在调度器上，到点提交再被跑前那道门拒成一条失败的 run。
    """
    state = await _state(tmp_path)
    try:
        yield state
    finally:
        await _close(state)


async def test_the_flip_reschedules_the_jobs_on_the_scheduler(
    scheduler_state: AppState,
) -> None:
    """总闸关掉之后，调度器上**不许还留着**采集 job。

    这条是变异试出来的（`.scratch/mutation/run_master_switch_r2.py` 的 R6）：
    把 PUT 里那句 `reschedule_collect_jobs(...)` 换成 `pass`，
    本文件其余 14 条**全绿** —— 于是"翻转当场生效"这句话今天只由名单兑现，
    调度器上真有什么没人看。
    """
    before = _collect_job_ids(scheduler_state)
    assert before, "前置不成立：装配没排出任何采集 job，这条用例会在空转"

    transport = httpx.ASGITransport(app=create_app(state=scheduler_state))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        await _set_master(ac, False)
        assert _collect_job_ids(scheduler_state) == [], "关总闸没重排：job 还挂在调度器上"

        await _set_master(ac, True)
        assert _collect_job_ids(scheduler_state) == before, "开回总闸没把 job 排回去"


async def test_the_master_switch_defaults_to_on(client: httpx.AsyncClient) -> None:
    body = await _get_master(client)
    assert body["enabled"] is True
    assert body["shadowed_by_env"] == []


async def test_turning_it_off_writes_app_yaml_and_survives_a_reload(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """写盘之后重新 load 必须读回 `false`，否则重启后总闸自己弹回开。"""
    await _set_master(client, False)

    text = app_state.config_manager.app_yaml_path.read_text(encoding="utf-8")
    assert "platform_control" in text

    reloaded = ConfigManager(app_state.config_manager.config_dir)
    reloaded.load()
    assert reloaded.app.platform_control.enabled is False


async def test_it_does_not_touch_the_platform_file(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """总闸写在 `app.yaml`。写到 `platforms.yaml` 的实现也能让别的日子全绿，
    而那意味着 PUT 顺手动了四家配置那份**逐家**的审计边界。
    """
    before = app_state.config_manager.platforms_yaml_path.read_text(encoding="utf-8")
    await _set_master(client, False)
    assert app_state.config_manager.platforms_yaml_path.read_text(encoding="utf-8") == before


async def test_an_env_var_outranks_the_file_and_the_endpoint_says_so(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """优先级是 `默认 < yaml < env < CLI`，而 PUT 只写得动 yaml。

    两半都要说：现值（这个进程此刻按什么跑）与"重启后回到 env 那一条"。
    只报现值的实现是**半真**的 —— 与 `scheduler_keys_shadowed_by_env` 同一条纪律、
    同一份症状（"设置页明明改成 08:00，重启后又变回 06:30"）。
    """
    monkeypatch.setenv("INTELLIGENCE_HUB_PLATFORM_CONTROL__ENABLED", "true")
    # env 压着 yaml：这一段 yaml 里根本没有，值来自环境
    assert (await _get_master(client))["enabled"] is True
    assert (await _get_master(client))["shadowed_by_env"] == ["enabled"]

    turned_off = await _set_master(client, False)
    control = turned_off["platform_control"]
    assert control["enabled"] is False, "写进去之后本进程按新值跑（与 scheduler 段同形）"
    assert control["shadowed_by_env"] == ["enabled"], "但必须同时说出重启后会翻回去"
    assert turned_off["changed_fields"] == ["platform_control.enabled"]


# --------------------------------------------------------------------------- #
# 一次翻转之后，各处口径同步
# --------------------------------------------------------------------------- #


async def test_the_platform_rows_keep_their_own_switch_and_gain_availability(
    client: httpx.AsyncClient,
) -> None:
    """关总闸**不改写任何一家自己的 `enabled`** —— 这是总闸存在的全部理由。

    如果实现是"把四家批量写成 false"，`enabled` 这一栏会跟着变，于是重新打开总闸时
    B站那种"我本来就单独关了"的选择被悄悄复原。断的是两半：
    `enabled` 一字未动 + `availability` 说得出是谁挡着。
    """
    before = await _rows(client)
    assert set(before) == set(_PLATFORM_NAMES)
    assert all(row["availability"] == "available" for row in before.values()), before

    await _set_master(client, False)
    after = await _rows(client)

    for name, row in after.items():
        assert row["enabled"] == before[name]["enabled"], f"{name} 自己的开关被总闸改写了"
        assert row["availability"] == "master_off", row

    # 总闸还关着的时候再单独关一家：自身那一位照实记，但先报挡在前面的那一道。
    # 顺序在这里是有意义的 —— 一个人此时能做的动作只有"开总闸"。
    await _own_switch(client, "bilibili", False)
    rows = await _rows(client)
    assert rows["bilibili"]["enabled"] is False
    assert rows["bilibili"]["availability"] == "master_off"

    # 开回总闸，那一家才显出"是你自己关的"
    await _set_master(client, True)
    rows = await _rows(client)
    assert rows["bilibili"]["availability"] == "own_off"
    assert {n: r["availability"] for n, r in rows.items() if n != "bilibili"} == {
        n: "available" for n in _PLATFORM_NAMES if n != "bilibili"
    }


async def test_the_root_of_the_platform_list_carries_the_master(
    client: httpx.AsyncClient,
) -> None:
    """`master_enabled` 在响应**根**上：它是四行共用的那一个事实。

    每行重复一遍的话，前端就有四处可以各渲染各的，而"这一格开关现在该给谁"只有一个答案。
    """
    body = (await client.get("/api/platforms")).json()
    assert body["master_enabled"] is True
    await _set_master(client, False)
    assert (await client.get("/api/platforms")).json()["master_enabled"] is False


async def test_collect_tasks_disappear_but_cross_platform_ones_stay(
    client: httpx.AsyncClient,
) -> None:
    listed = {t["name"] for t in (await client.get("/api/tasks")).json()}
    assert listed >= _COLLECT_TASKS, "四家齐装着，采集任务本该都在"
    assert listed >= _CROSS_PLATFORM_TASKS

    await _set_master(client, False)
    after = {t["name"] for t in (await client.get("/api/tasks")).json()}

    assert not (after & _COLLECT_TASKS), f"总闸关着却还给按钮：{after & _COLLECT_TASKS}"
    assert after >= _CROSS_PLATFORM_TASKS, "跨平台的任务被总闸带走了：它们不属于任何一家"


@pytest.mark.parametrize("task_name", sorted(_COLLECT_TASKS))
async def test_naming_a_collect_task_anyway_is_rejected_naming_the_master(
    client: httpx.AsyncClient, task_name: str
) -> None:
    """/api/tasks 里没有按钮 ≠ 闸门。绕过界面直接 POST 也要被挡住，

    而且报错要说清是**总闸**挡的 —— 否则人会去 Settings 打开那一家的开关，
    开完发现还是这一句，而那一格确实是开着的。
    """
    await _set_master(client, False)
    response = await client.post(f"/api/tasks/{task_name}/run", json={})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "总闸" in detail, detail
    assert "platforms.yaml 的 enabled: false" not in detail, detail


async def test_the_cron_list_and_run_now_follow_the_master(
    client: httpx.AsyncClient,
) -> None:
    """定时那一路吃的是同一份"可用名单"。

    `master_enabled` 在响应里是**必需**的一栏，不是顺手加的：`skipped_platforms`
    只在名单点过名（`collect_platforms` 非空）时才有内容，而默认那份是空的 ——
    关掉总闸就会得到"`effective_platforms` 空、`skipped_platforms` 也空"，
    没有任何一栏解释为什么 08:00 那条 cron 一家都不跑。那正是本仓库最恨的
    "看板看着正常"的形状（对照 `api/v1/schedule.py` 文件头的规矩 2）。
    """
    before = (await client.get("/api/schedule")).json()
    assert set(before["effective_platforms"]) == set(_PLATFORM_NAMES), before
    assert before["master_enabled"] is True

    await _set_master(client, False)
    after = (await client.get("/api/schedule")).json()
    assert after["effective_platforms"] == []
    assert after["master_enabled"] is False, "空名单必须有个可指认的原因"

    # 手动"现在就跑一次"走的是与 cron 同一个任务名、同一道门
    response = await client.post("/api/schedule/run-now", json={"platform": "douyin"})
    assert response.status_code == 422, response.text
    assert "总闸" in response.json()["detail"]


async def test_opening_the_master_restores_the_original_combination(
    client: httpx.AsyncClient,
) -> None:
    """关掉再打开，回到**原来那一份**组合：不是四家全开，也不是四家全关。

    这条是"批量写四次"那种实现最容易糊过去的地方 —— 它在"关"那一半也全绿。
    """
    await _own_switch(client, "youtube", False)

    await _set_master(client, False)
    await _set_master(client, True)

    rows = await _rows(client)
    assert {n: r["enabled"] for n, r in rows.items()} == {
        "douyin": True,
        "bilibili": True,
        "xiaohongshu": True,
        "youtube": False,
    }
    assert {n: r["availability"] for n, r in rows.items()} == {
        "douyin": "available",
        "bilibili": "available",
        "xiaohongshu": "available",
        "youtube": "own_off",
    }
    names = {t["name"] for t in (await client.get("/api/tasks")).json()}
    assert "youtube_collect" not in names
    assert "douyin_collect" in names


async def test_the_flip_is_broadcast_as_config_changed(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """翻转要发 `CONFIG_CHANGED`，与两条平台配置 PUT 同一个形状。

    不发的事件流症状是"另一个开着的页面还显示旧值"，而这一格改的是**所有页面**的按钮。

    订总线而不是查库：`config.changed` 是**全局事件、只广播不落库**
    （`task_events.task_id` 是 NOT NULL，见 `event-schema.md §4` 的实施期修订）——
    查库那条路对任何实现都返回空，是个看起来严格的假判据。

    `scope` 用 `"app"` 而不是新造一个 `"platform_control"`：这一段确实住在 `app.yaml` 里，
    而 `scheduler` 那条 PUT 早就是这么写的（`changed_fields` 带段名前缀）。
    点名 `changed_fields` 而不是"有任何一条 config.changed"：后者对什么都没发的实现
    也算过（别的用例早发过），那是假证据。
    """
    subscription = app_state.events.subscribe(types=[EventType.CONFIG_CHANGED])
    try:
        await _set_master(client, False)
        event = await asyncio.wait_for(subscription.__anext__(), timeout=2.0)
    finally:
        await subscription.aclose()

    payload = event.payload
    assert payload["changed_fields"] == ["platform_control.enabled"], payload
    assert payload["scope"] == "app", payload
    assert payload["platform"] is None, "总闸不属于任何一家，填个平台名会把按平台过滤带偏"
