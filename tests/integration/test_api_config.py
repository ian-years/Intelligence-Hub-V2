"""`/api/platforms*`：列 / schema / 读配置 / 写配置（原子 + 热重载 + 广播 + 门控生效）。

写配置只在 tmp 目录那份 `platforms.yaml` 上做（fixture 造的），绝不碰仓库 `config/`。
"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState

pytestmark = pytest.mark.integration


async def test_list_platforms(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/platforms")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()["platforms"]}
    assert names == {"douyin", "bilibili"}


async def test_platform_schema_is_json_schema(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/platforms/douyin/schema")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "object"
    assert "enabled" in body["properties"]


async def test_unknown_platform_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/platforms/xiaohongshu/schema")).status_code == 404
    assert (await client.get("/api/platforms/xiaohongshu/config")).status_code == 404


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
    # （证明 reload 推到了调度器/注册表，而不只是 manager）
    after = {t["name"] for t in (await client.get("/api/tasks")).json()}
    assert "douyin_collect" not in after
    assert "bilibili_collect" in after
    assert app_state.scheduler._configs["douyin"].enabled is False  # 门控那份快照也更新了


async def test_put_config_rejects_unknown_field(client: httpx.AsyncClient) -> None:
    # PlatformConfig 是 extra=forbid：多一个没定义的字段 → 422
    resp = await client.put("/api/platforms/douyin/config", json={"bogus_field": 1})
    assert resp.status_code == 422
