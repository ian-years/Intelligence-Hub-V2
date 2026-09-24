"""ADR-0022：preflight 把探测结论写回 `platforms` 镜像，`/api/platforms` 把它带出去。

这一条链在只读版本里是**测不出来**的 —— 三列恒 NULL，任何"灯的颜色对不对"的断言
都只能拿手工塞进去的行当输入。所以这里的用例全部从"跑一次真 preflight"开始，
断的是**跑完之后库里/接口上多了什么**，不是"set_health 会不会写"（那是 Repository
自己的用例，早就有）。

三条判据写成关系：
- 探了几个平台，镜像里就该有几行非 NULL；
- 接口给的 status 与库里那行**是同一个值**（不是"看起来像"）；
- 没探过的平台一个字节都不写 —— 这条是"没探"与"探到 unknown"的分界，
  合并了它们就等于把 §7.20 那个形状的灰灯写进库里。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.models.platform import HEALTH_STATUSES
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.schema import platforms_table

pytestmark = pytest.mark.integration


async def _run_preflight(state: AppState) -> None:
    await state.scheduler.run_to_completion("preflight", {})


async def _health_rows(storage: SqliteStorage) -> dict[str, tuple[str, object]]:
    async with storage.sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(
                        platforms_table.c.name,
                        platforms_table.c.health_status,
                        platforms_table.c.health_checked_at,
                    ).where(platforms_table.c.health_status.is_not(None))
                )
            )
            .mappings()
            .all()
        )
    return {str(row["name"]): (str(row["health_status"]), row["health_checked_at"]) for row in rows}


async def test_a_preflight_run_leaves_a_health_row_per_platform_probed(
    app_state: AppState,
) -> None:
    """探过的平台，镜像里必须有非 NULL 的结论 —— 否则那三列仍然没有人写。

    这条是 ADR-0022 的全部主张。它红的两种方式各有含义：整张表还是 NULL（没写），
    或者行数少于被探的平台数（写漏了某一家，而那一家在界面上永远"从没检查过"）。
    """
    probed = set(app_state.registry.enabled_platforms())
    assert probed, "没有启用的平台 → 这条用例会空转"
    before = await _health_rows(app_state.storage)
    assert before == {}, "开局就该是'从没检查过'，否则下面测不出是这次写的"

    await _run_preflight(app_state)

    after = await _health_rows(app_state.storage)
    assert set(after) == probed
    for status, _checked_at in after.values():
        assert status in HEALTH_STATUSES


async def test_the_api_returns_the_same_value_the_database_holds(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """接口那一侧不许是"另算一次"：它给的状态与库里的必须逐字相同。

    断言写成两边相等，而不是"接口里有 status 字段" —— 后者对任何值都成立。
    """
    await _run_preflight(app_state)
    rows = await _health_rows(app_state.storage)
    body = (await client.get("/api/platforms")).json()["platforms"]
    assert len(body) == len(rows), "接口给的平台数与镜像里的行数不一致"
    for platform in body:
        name = platform["name"]
        assert name in rows, f"{name} 在接口里出现却没被探过"
        assert platform["health_status"] == rows[name][0]
        assert platform["health_checked_at"] is not None


async def test_a_platform_that_was_never_probed_stays_null(
    app_state: AppState, client: httpx.AsyncClient
) -> None:
    """ "没探过"与"探到 unknown"是两件事，不许合并。

    做法：把一家平台关掉再跑一次预检。它的镜像行必须**仍然**是 NULL
    （而不是被顺手写成 unknown/unreachable）—— 一旦写了，前端那个灯就会拿着
    一个从没人得出的结论亮着，而那正是 §7.20 的形状。
    """
    await client.put("/api/platforms/bilibili/config", json={"enabled": False})
    await _run_preflight(app_state)
    rows = await _health_rows(app_state.storage)
    assert "bilibili" not in rows
    assert "douyin" in rows, "关一家不该把另一家的探测一起挡掉"


async def test_the_write_survives_a_config_reload(app_state: AppState) -> None:
    """改配置的热重载不许把健康三列抹回 NULL。

    `upsert` 刻意不动这三列（它的 docstring 就是这么写的），而这条断言是那句
    "刻意"唯一的证明 —— 没有它，谁都能在重构时把三列顺手加进 `set_` 里，
    症状是"每次改设置，总览页的灯全灭一下"。
    """
    await _run_preflight(app_state)
    rows = await _health_rows(app_state.storage)
    assert rows

    await app_state.refresh_platform_state(
        "douyin", app_state.config_manager.get_platform("douyin")
    )
    after = await _health_rows(app_state.storage)
    assert set(after) == set(rows)
    assert after["douyin"][0] == rows["douyin"][0]


async def test_all_platforms_off_still_finishes_and_writes_nothing(
    app_state: AppState, client: httpx.AsyncClient
) -> None:
    """全部平台关掉时预检仍要跑完，且**不写任何健康行**。

    这一条同时挡两种相反的坏：
    - 给没探的平台写行（上面那条）；
    - "没有平台可探 → 预检自己炸掉" —— 那是 review P0-4 的形状（环境已经坏了，
      用来检查环境的那件事本身也坏了），而它的红长得像"预检有 bug"，不像"配置关了"。
    """
    for name in list(app_state.config_manager.enabled_platforms()):
        await client.put(f"/api/platforms/{name}/config", json={"enabled": False})

    record = await app_state.scheduler.run_to_completion("preflight", {})
    assert record.status in {"success", "partial", "failed"}
    assert record.ended_at is not None, "预检必须落终态，不许留一条永不终结的 running"
    assert await _health_rows(app_state.storage) == {}
