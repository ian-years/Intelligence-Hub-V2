"""`platforms` 表的 Repository 测试（运行态镜像）。

三条要钉住的：

1. **`upsert()` 不动健康状态三列**（V1 §7.20 的延伸）：配置热重载时把
   `health_status` 一起刷成 NULL，前端的健康灯会在每次改配置后闪一下"未检查"。
   健康状态与配置无关，它由 preflight / 采集任务写。
2. **`is_healthy` 只认显式 `ok`**：`None`（从没检查过）与 `unknown`（检查了但判不出来）
   **都不是绿灯**。V1 §7.20 那次事故就是桥的浏览器早死了、`/health` 却回 `ok:true`，
   于是三连"未登录"其实是"测不到"。
3. **`enabled` 必须是真 bool**（V1 §7.24 同一类坑）：落成 `0` / `"false"`
   会让读开关的两条路径读出相反的结果。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intelligence_hub_v2.errors import NotFoundError, StorageError
from intelligence_hub_v2.models.creator import Creator
from intelligence_hub_v2.storage.db import SqliteStorage

# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


async def test_upsert_inserts_a_new_row(storage: SqliteStorage) -> None:
    record = await storage.platforms.upsert(
        "douyin", enabled=True, config={"display_name": "抖音", "enabled": True}
    )
    assert record.name == "douyin"
    assert record.enabled is True
    assert record.config() == {"display_name": "抖音", "enabled": True}
    assert record.health_status is None
    assert record.health_checked_at is None
    assert record.health_detail is None


async def test_upsert_without_config_lands_an_empty_object(storage: SqliteStorage) -> None:
    record = await storage.platforms.upsert("bilibili", enabled=False)
    assert record.config_json == "{}"
    assert record.config() == {}


async def test_upsert_accepts_a_pre_serialized_string(storage: SqliteStorage) -> None:
    payload = json.dumps({"display_name": "B站"}, ensure_ascii=False)
    record = await storage.platforms.upsert("bilibili", enabled=True, config=payload)
    assert record.config_json == payload
    assert record.config() == {"display_name": "B站"}


async def test_upsert_serializes_paths_as_posix(storage: SqliteStorage) -> None:
    """`Path` 字段要能序列化，而且落库是 **posix 分隔符**。

    `DouyinConfig` 里有几个路径字段（cookie 文件、桥 profile），
    `model_dump()` 之后它们仍然是 `Path`；不加 `default=` 就 TypeError。

    分隔符这条是 Windows 上真会踩的：`str(Path("data/cookies/x.txt"))`
    在 Windows 上是 `data\\cookies\\x.txt`，而库里其他所有路径都走
    `FileStorage.rel()`（`as_posix()`）。同一张库两种分隔符，
    "按路径前缀筛媒体"这类查询会静默匹配不到。
    """
    record = await storage.platforms.upsert(
        "douyin", enabled=True, config={"cookies_file": Path("data/cookies/douyin.com.txt")}
    )
    assert record.config()["cookies_file"] == "data/cookies/douyin.com.txt"
    assert "\\" not in record.config_json


async def test_upsert_updates_enabled_and_config(storage: SqliteStorage) -> None:
    await storage.platforms.upsert("douyin", enabled=True, config={"a": 1})
    updated = await storage.platforms.upsert("douyin", enabled=False, config={"a": 2})
    assert updated.enabled is False
    assert updated.config() == {"a": 2}
    assert await storage.platforms.count() == 1


async def test_upsert_with_config_none_keeps_the_existing_config(
    storage: SqliteStorage,
) -> None:
    """`config=None` 的意思是"不改配置"，不是"把配置清空"。

    这条区分很重要：`ConfigManager` 只想同步开关时不该顺手抹掉配置副本。
    """
    await storage.platforms.upsert("douyin", enabled=True, config={"a": 1})
    updated = await storage.platforms.upsert("douyin", enabled=False, config=None)
    assert updated.enabled is False
    assert updated.config() == {"a": 1}


async def test_upsert_does_not_clobber_health_columns(storage: SqliteStorage) -> None:
    """**这条是本文件最重要的一条**（V1 §7.20 的延伸）。

    `on_conflict_do_update` 的 `set_` 里刻意没有那三列。
    把它们一起刷成 NULL 的话，每次改配置前端的健康灯都会闪一下"未检查"。
    """
    await storage.platforms.upsert("douyin", enabled=True, config={"a": 1})
    await storage.platforms.set_health("douyin", "degraded", detail="桥的浏览器已关闭")
    checked_at = (await storage.platforms.get_or_raise("douyin")).health_checked_at

    reloaded = await storage.platforms.upsert("douyin", enabled=True, config={"a": 2})

    assert reloaded.health_status == "degraded"
    assert reloaded.health_detail == "桥的浏览器已关闭"
    assert reloaded.health_checked_at == checked_at
    assert reloaded.config() == {"a": 2}


@pytest.mark.parametrize("bad", [0, 1, "true", "false", None, "", []])
async def test_upsert_rejects_a_non_bool_enabled(storage: SqliteStorage, bad: object) -> None:
    """**V1 §7.24 同一类坑**：落成 `0` / `"false"` 会让开关读出相反的结果。

    前端 JSON 里传字符串要在这里红掉，而不是静默存进去等到采集时才发现。
    """
    with pytest.raises(TypeError, match="bool"):
        await storage.platforms.upsert("douyin", enabled=bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 健康状态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["ok", "degraded", "unreachable", "unknown"])
async def test_set_health_writes_all_three_columns(storage: SqliteStorage, status: str) -> None:
    await storage.platforms.upsert("douyin", enabled=True)
    at = datetime(2026, 9, 22, 5, 56, tzinfo=UTC)
    record = await storage.platforms.set_health(
        "douyin",
        status,
        detail="yt-dlp: Request is blocked by server (412)",
        checked_at=at,  # type: ignore[arg-type]
    )
    assert record.health_status == status
    assert record.health_detail == "yt-dlp: Request is blocked by server (412)"
    assert record.health_checked_at == at


async def test_set_health_defaults_checked_at_to_now(storage: SqliteStorage) -> None:
    await storage.platforms.upsert("douyin", enabled=True)
    before = datetime.now(UTC)
    record = await storage.platforms.set_health("douyin", "ok")
    assert record.health_checked_at is not None
    assert record.health_checked_at >= before


async def test_set_health_rejects_an_unknown_status(storage: SqliteStorage) -> None:
    """入口先拦一道，文案能说明白"这是枚举值写错了"，
    而不是抛一句 `CHECK constraint failed: platforms.health_status_enum`。"""
    await storage.platforms.upsert("douyin", enabled=True)
    with pytest.raises(StorageError, match="未知的健康状态"):
        await storage.platforms.set_health("douyin", "green")  # type: ignore[arg-type]


async def test_set_health_on_a_missing_platform_raises(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError, match="platform"):
        await storage.platforms.set_health("ghost", "ok")


async def test_clear_health_returns_to_never_checked(storage: SqliteStorage) -> None:
    """换掉平台的实现档位后，上一次的绿灯已经不代表现在 ——
    留着它比显示"未检查"更危险。"""
    await storage.platforms.upsert("douyin", enabled=True)
    await storage.platforms.set_health("douyin", "ok", detail="一切正常")

    cleared = await storage.platforms.clear_health("douyin")
    assert cleared.health_status is None
    assert cleared.health_checked_at is None
    assert cleared.health_detail is None
    assert cleared.is_healthy is False


async def test_clear_health_on_a_missing_platform_raises(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError, match="platform"):
        await storage.platforms.clear_health("ghost")


@pytest.mark.parametrize(
    ("enabled", "status", "expected"),
    [
        (True, "ok", True),
        (True, "degraded", False),
        (True, "unreachable", False),
        (True, "unknown", False),
        (True, None, False),
        (False, "ok", False),
    ],
)
async def test_is_healthy_only_trusts_an_explicit_ok(
    storage: SqliteStorage, enabled: bool, status: str | None, expected: bool
) -> None:
    """**V1 §7.20**："测不到"绝不能显示成"正常"。"""
    await storage.platforms.upsert("douyin", enabled=enabled)
    if status is not None:
        await storage.platforms.set_health("douyin", status)  # type: ignore[arg-type]
    record = await storage.platforms.get_or_raise("douyin")
    assert record.is_healthy is expected


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


async def test_set_enabled_only_touches_the_switch(storage: SqliteStorage) -> None:
    """Settings 页保存开关时**不该**顺带改 `config_json` ——
    那份配置可能刚被热重载更新过，用页面里的旧副本覆盖等于回滚用户的改动。"""
    await storage.platforms.upsert("douyin", enabled=True, config={"a": 1})
    await storage.platforms.set_health("douyin", "ok")

    turned_off = await storage.platforms.set_enabled("douyin", False)
    assert turned_off.enabled is False
    assert turned_off.config() == {"a": 1}
    assert turned_off.health_status == "ok"
    assert turned_off.is_healthy is False


@pytest.mark.parametrize("bad", [0, 1, "true", "false", None])
async def test_set_enabled_rejects_a_non_bool(storage: SqliteStorage, bad: object) -> None:
    await storage.platforms.upsert("douyin", enabled=True)
    with pytest.raises(TypeError, match="bool"):
        await storage.platforms.set_enabled("douyin", bad)  # type: ignore[arg-type]


async def test_set_enabled_on_a_missing_platform_raises(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError, match="platform"):
        await storage.platforms.set_enabled("ghost", True)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


async def test_get_and_get_or_raise(storage: SqliteStorage) -> None:
    assert await storage.platforms.get("ghost") is None
    with pytest.raises(NotFoundError, match="platform"):
        await storage.platforms.get_or_raise("ghost")

    await storage.platforms.upsert("douyin", enabled=True)
    assert (await storage.platforms.get("douyin")) is not None
    assert (await storage.platforms.get_or_raise("douyin")).name == "douyin"


async def test_list_all_is_ordered_by_name(storage: SqliteStorage) -> None:
    """排序不是为了好看：改了 platforms.yaml 的书写顺序，界面不该跳。"""
    for name in ("youtube", "bilibili", "douyin"):
        await storage.platforms.upsert(name, enabled=True)
    assert [r.name for r in await storage.platforms.list_all()] == [
        "bilibili",
        "douyin",
        "youtube",
    ]


async def test_list_all_enabled_only(storage: SqliteStorage) -> None:
    await storage.platforms.upsert("douyin", enabled=True)
    await storage.platforms.upsert("youtube", enabled=False)
    assert [r.name for r in await storage.platforms.list_all(enabled_only=True)] == ["douyin"]
    assert len(await storage.platforms.list_all()) == 2


async def test_count(storage: SqliteStorage) -> None:
    await storage.platforms.upsert("douyin", enabled=True)
    await storage.platforms.upsert("youtube", enabled=False)
    assert await storage.platforms.count() == 2
    assert await storage.platforms.count(enabled_only=True) == 1


async def test_config_on_corrupt_json_raises(storage: SqliteStorage) -> None:
    """静默返回 `{}` 会让前端显示"该平台什么参数都没配"，
    而真正的原因是数据损坏（V1 §1.3）。"""
    await storage.platforms.upsert("douyin", enabled=True, config="{broken")
    record = await storage.platforms.get_or_raise("douyin")
    with pytest.raises(json.JSONDecodeError):
        record.config()


# ---------------------------------------------------------------------------
# 删 / 清理
# ---------------------------------------------------------------------------


async def test_delete_removes_a_platform_without_creators(storage: SqliteStorage) -> None:
    await storage.platforms.upsert("douyin", enabled=True)
    assert await storage.platforms.delete("douyin") is True
    assert await storage.platforms.get("douyin") is None
    assert await storage.platforms.delete("douyin") is False  # 幂等


async def test_delete_is_translated_when_creators_still_reference_it(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    """`ON DELETE RESTRICT` → `StorageError`，不是 SQLAlchemy traceback。

    调用方（Settings 页的"移除平台"）需要的是"这个平台下还有 N 位博主"，
    一屏栈对它毫无用处。
    """
    assert creator_row.platform == "douyin"
    with pytest.raises(StorageError, match="外键不成立"):
        await storage.platforms.delete("douyin")
    assert await storage.platforms.get("douyin") is not None


async def test_prune_unknown_removes_rows_outside_the_keep_set(
    storage: SqliteStorage,
) -> None:
    """场景：在 V2.1 上跑过、库里留了 `xiaohongshu` 行，然后换回 V2.0 的构建。

    配置层会因为未知 key 报 `ConfigError`，但**库里**的残留行不会自己消失 ——
    前端会显示一个点了没反应的平台开关。
    """
    for name in ("douyin", "bilibili", "xiaohongshu"):
        await storage.platforms.upsert(name, enabled=True)

    removed = await storage.platforms.prune_unknown(["douyin", "bilibili"])
    assert removed == 1
    assert [r.name for r in await storage.platforms.list_all()] == ["bilibili", "douyin"]


async def test_prune_unknown_with_an_empty_keep_set_deletes_nothing(
    storage: SqliteStorage,
) -> None:
    """防御：传错参数把整张表清空，然后所有平台的健康状态归零，
    看着像"全部平台挂了"。"""
    await storage.platforms.upsert("douyin", enabled=True)
    assert await storage.platforms.prune_unknown([]) == 0
    assert await storage.platforms.count() == 1


async def test_prune_unknown_accepts_any_iterable(storage: SqliteStorage) -> None:
    await storage.platforms.upsert("douyin", enabled=True)
    await storage.platforms.upsert("youtube", enabled=True)
    assert await storage.platforms.prune_unknown(iter(["douyin"])) == 1
