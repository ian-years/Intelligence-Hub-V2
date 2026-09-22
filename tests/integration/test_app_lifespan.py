"""`build_components` 默认装配 + `_sync_platform_mirror`（启动时把配置灌进运行态镜像）。

不用 `TestClient` 跑整条 ASGI lifespan：这版 starlette 的 TestClient 依赖未装的 `httpx2`
且一 import 就发弃用告警，撞上 `filterwarnings=["error"]`。lifespan 的编排（setup_logging /
起 APScheduler / 收孤儿）都是薄胶水，`reap_orphans` 在 `test_task_runner` 已验；
这里验真逻辑：**装配出来的 AppState 各件齐不齐 + 镜像同步对不对**。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.core.task_registry import DepsFactory
from intelligence_hub_v2.core.task_runner import TaskRunner, TaskScheduler
from intelligence_hub_v2.main import _sync_platform_mirror, build_components
from intelligence_hub_v2.platforms.registry import PlatformRegistry
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

_PLATFORMS_YAML = """\
douyin:
  enabled: true
bilibili:
  enabled: false
"""


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
