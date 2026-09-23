"""`build_components` 默认装配 + `_sync_platform_mirror`（启动时把配置灌进运行态镜像）。

不用 `TestClient` 跑整条 ASGI lifespan：这版 starlette 的 TestClient 依赖未装的 `httpx2`
且一 import 就发弃用告警，撞上 `filterwarnings=["error"]`。lifespan 的编排（setup_logging /
起 APScheduler / 收孤儿）都是薄胶水，`reap_orphans` 在 `test_task_runner` 已验；
这里验真逻辑：**装配出来的 AppState 各件齐不齐 + 镜像同步对不对**。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.core.task_registry import DepsFactory
from intelligence_hub_v2.core.task_runner import TaskRunner, TaskScheduler
from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.main import _sync_platform_mirror, build_components, create_app
from intelligence_hub_v2.platforms.registry import PlatformRegistry
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

_PLATFORMS_YAML = """\
douyin:
  enabled: true
bilibili:
  enabled: false
"""


async def test_repository_handles_resolve_before_initialize_but_queries_do_not(
    tmp_path: Path,
) -> None:
    """Repository 握的是"取 session 的工厂"，构造它不需要连接池。

    真正的门在第一次查询上（`_require_sessionmaker`）。把门放在"拿句柄"这一步，
    等于让 `create_app()` 在 lifespan 之前无法装配 —— 见下一条用例。
    """
    storage = SqliteStorage(tmp_path / "not-yet.sqlite3")

    videos = storage.videos  # 拿句柄不许抛

    with pytest.raises(StorageError, match="不可用"):
        await videos.count()


def test_create_app_boots_without_an_injected_state(tmp_path: Path) -> None:
    """`uv run intelligence-hub` 走的就是这条路（uvicorn factory → `create_app()`）。

    以前 `create_app()` 在 `storage.initialize()` 之前就去取 `storage.events`
    （`build_components` 里 `InProcessEventBus(events=storage.events)`），
    生产启动路径当场抛 `SqliteStorage 还没 initialize()`。
    而这里原有的用例都是"先 initialize 再 build_components"或"注入 state="，
    所以这条路一次都没被跑过 —— 验收判据 2（服务能起来）当时是纸面的。
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "app.yaml").write_text(f"data:\n  dir: {tmp_path.as_posix()}/data\n", encoding="utf-8")
    (cfg / "platforms.yaml").write_text(_PLATFORMS_YAML, encoding="utf-8")

    app = create_app(config_dir=cfg)

    paths = set(app.openapi()["paths"])
    assert "/api/health" in paths and "/api/events" in paths


async def test_build_components_wires_everything(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "platforms.yaml").write_text(_PLATFORMS_YAML, encoding="utf-8")
    manager = ConfigManager(cfg)
    manager.load()

    storage = SqliteStorage.in_memory()
    await storage.initialize()
    files = FileStorage(tmp_path / "data")
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    try:
        state = build_components(manager, storage=storage, files=files, http=http)

        assert isinstance(state.registry, PlatformRegistry)
        assert isinstance(state.deps_factory, DepsFactory)
        assert isinstance(state.runner, TaskRunner)
        assert isinstance(state.scheduler, TaskScheduler)
        # 只有 bilibili 关掉，enabled_platforms 应该只剩 douyin
        assert state.registry.enabled_platforms() == ["douyin"]
        # owns_http=False：注入了客户端，lifespan 不该把它关掉
        assert state.owns_http is False
    finally:
        await http.aclose()
        await storage.close()


async def test_sync_platform_mirror_upserts_and_prunes(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "platforms.yaml").write_text(_PLATFORMS_YAML, encoding="utf-8")
    manager = ConfigManager(cfg)
    manager.load()

    storage = SqliteStorage.in_memory()
    await storage.initialize()
    files = FileStorage(tmp_path / "data")
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    try:
        # 先塞一条换版本后残留的行（youtube 在本构建已不支持），镜像同步必须把它清掉。
        await storage.platforms.upsert("youtube", enabled=False)

        state = build_components(manager, storage=storage, files=files, http=http)
        await _sync_platform_mirror(state)

        rows = {r.name: r for r in await storage.platforms.list_all()}
        assert set(rows) == {"douyin", "bilibili"}
        assert rows["douyin"].enabled is True
        assert rows["bilibili"].enabled is False
        assert "youtube" not in rows  # prune_unknown 清掉了不在当前配置里的残留行
    finally:
        await http.aclose()
        await storage.close()
