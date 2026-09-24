"""API 集成测试的装配：真 app + 内存库 + tmp 配置目录 + mock http。

刻意**不跑 lifespan**（不 `async with LifespanManager`），改成 fixture 里手动 `storage.initialize()`
+ 预置平台镜像行，再用 `httpx.ASGITransport` 打路由。理由：lifespan 会 `setup_logging()`
重配全局 structlog handler，几十个 API 用例反复触发会污染其它测试的日志捕获；
而 lifespan 的活（建库 / 灌镜像 / 收孤儿）单独在 `test_app_lifespan` 里验一次即可。

配置用 **tmp 目录**里的一份最小 `platforms.yaml`，不是仓库的 `config/`：
`PUT /platforms/{name}/config` 会原子写盘，绝不能把仓库配置改了。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.main import build_components, create_app
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

_PLATFORMS_YAML = """\
douyin:
  enabled: true
bilibili:
  enabled: true
xiaohongshu:
  enabled: true
youtube:
  enabled: true
"""
# 这一份名单**跟着 `PLATFORM_CONFIG_SCHEMAS` 走**，不是"先给两个够用的"。
# 少一家会经由 `registry.inconsistencies()` 报"注册了 schema 却没有配置对象"，
# 而 preflight 把"自洽性坏了"直接算 unreachable → 两条 SSE 用例替它背红，
# 报错看起来像"SSE 漏事件"而不是"这份 fixture 少一段"。2026-09-24 注册小红书时踩过。


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "platforms.yaml").write_text(_PLATFORMS_YAML, encoding="utf-8")
    return cfg


@pytest.fixture
async def app_state(config_dir: Path, tmp_path: Path) -> AsyncIterator[AppState]:
    manager = ConfigManager(config_dir)
    manager.load()

    storage = SqliteStorage.in_memory()
    await storage.initialize()
    # lifespan  normally 会灌这张镜像表；这里手动补上，好让 create/insert 的外键成立。
    for name in manager.platform_names():
        cfg = manager.get_platform(name)
        await storage.platforms.upsert(
            name, enabled=cfg.enabled, config=cfg.model_dump(mode="json")
        )

    files = FileStorage(tmp_path / "data")
    files.ensure_dirs()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )

    state = build_components(manager, storage=storage, files=files, http=http)
    try:
        yield state
    finally:
        await http.aclose()
        await storage.close()


@pytest.fixture
async def client(app_state: AppState) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(state=app_state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
