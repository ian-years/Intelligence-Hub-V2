"""`/api/platforms*`：列 / schema / 读配置 / 写配置（原子 + 热重载 + 广播 + 门控生效）。

写配置只在 tmp 目录那份 `platforms.yaml` 上做（fixture 造的），绝不碰仓库 `config/`。
"""

from __future__ import annotations

import json

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState

pytestmark = pytest.mark.integration


async def test_list_platforms(client: httpx.AsyncClient, app_state: AppState) -> None:
    """端点列的平台 = 这份 fixture 配置里的平台，一家不多不少。

    判据从 **state 的配置**推，不是写死名单：写死的那份每注册一个平台就要来改一次，
    而漏改的红长得像"端点坏了"，实际是名单过期。这里比的是"接口说的"与
    "配置层认为自己装了哪些"是不是同一件事 —— 那才是这条要看的东西。
    """
    resp = await client.get("/api/platforms")
    assert resp.status_code == 200
    body = resp.json()["platforms"]
    assert {p["name"] for p in body} == set(app_state.config_manager.platform_names())
    # 每一项都要能回答"实没实现"：前端靠这一位决定要不要给"跑一次"按钮。
    assert all({"name", "display_name", "enabled", "implemented"} <= set(p) for p in body)


async def test_platform_schema_is_json_schema(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/platforms/douyin/schema")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "object"
    assert "enabled" in body["properties"]


async def test_unknown_platform_404(client: httpx.AsyncClient) -> None:
    # 样本要挑**真的还没注册**的那家。T2.1 之前 xiaohongshu 是"未注册"的现成例子，
    # 注册进来之后它就成了合法值 —— 这条如果继续用它就会悄悄退化成"什么都没测"。
    assert (await client.get("/api/platforms/youtube/schema")).status_code == 404
    assert (await client.get("/api/platforms/youtube/config")).status_code == 404


async def test_get_platform_config_with_health(client, app_state: AppState) -> None:
    resp = await client.get("/api/platforms/douyin/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["enabled"] is True
    # 健康从没检查过 → None，不编 ok（V1 §7.20）
    assert body["health"] is None or body["health"]["status"] != "ok"


async def test_put_config_persists_and_gates(client, app_state: AppState, config_dir) -> None:
    # douyin_collect 现在可运行
    before = {t["name"] for t in (await client.get("/api/tasks")).json()}
    assert "douyin_collect" in before

    resp = await client.put("/api/platforms/douyin/config", json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["enabled"] is False
    assert "enabled" in body["changed_fields"]

    # 磁盘那份 tmp platforms.yaml 被原子改写
    text = (config_dir / "platforms.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in text

    # 关掉之后：/api/platforms 反映，且 douyin_collect 从可运行任务里消失
    after = {t["name"] for t in (await client.get("/api/tasks")).json()}
    assert "douyin_collect" not in after
    assert "bilibili_collect" in after

    # **每一份**握着配置快照的运行期组件都要落到位。
    # 原来这里只断言 scheduler 那一份。但 `single_link` / `add_creator` 的 `platforms=()`
    # 让调度器那道门对它们形同不存在 —— 它们唯一的开关判断在 registry 那份上
    # （`tasks/dispatch.py` 走 `detect_platform` → `registry.enabled_platforms()`）。
    # 只查 scheduler 的话，`PlatformRegistry.update_config` 退化成空操作也照样全绿。
    assert app_state.scheduler._configs["douyin"].enabled is False
    assert app_state.registry._configs["douyin"].enabled is False
    assert app_state.deps_factory._configs["douyin"].enabled is False
    assert "douyin" not in app_state.registry.enabled_platforms()


async def test_put_config_keeps_the_platforms_mirror_in_sync(client, app_state: AppState) -> None:
    """`platforms` 表是"开关 + 健康"的运行态镜像，平时只在 lifespan 灌一次。

    PUT 不跟着更新它，这一列就从写入那一刻起是过期的：`list_all(enabled_only=True)`
    会返回一个已经关掉的平台，而前端只要按这一列渲染开关，界面上就会出现
    "看着开着、其实采不动"。
    DB 写不能挂在热重载订阅者上 —— `reload_platform` 跑在线程池里，回调没有事件循环
    可以 await；所以写配置的路由必须走 `AppState.refresh_platform_state`。
    """
    await client.put("/api/platforms/douyin/config", json={"enabled": False})

    douyin = await app_state.storage.platforms.get("douyin")
    assert douyin is not None and douyin.enabled is False
    assert json.loads(douyin.config_json)["enabled"] is False  # 整份配置也要跟着走
    other = await app_state.storage.platforms.get("bilibili")  # 没被点的那个不许动
    assert other is not None and other.enabled is True


async def test_mirror_sync_does_not_reset_the_health_columns(client, app_state: AppState) -> None:
    """同步开关不许把健康三列刷成"未检查"。

    健康与配置无关（由 preflight / 采集写）。每次改配置都闪一下"未检查"，
    看板就永远回答不了"上次是什么时候测的"。
    """
    await app_state.storage.platforms.set_health("douyin", "ok", detail="桥在跑")

    await client.put("/api/platforms/douyin/config", json={"enabled": False})

    row = await app_state.storage.platforms.get("douyin")
    assert row is not None
    assert row.enabled is False
    assert row.health_status == "ok" and row.health_detail == "桥在跑"


async def test_put_config_rejects_unknown_field(client: httpx.AsyncClient) -> None:
    # PlatformConfig 是 extra=forbid：多一个没定义的字段 → 422
    resp = await client.put("/api/platforms/douyin/config", json={"bogus_field": 1})
    assert resp.status_code == 422
