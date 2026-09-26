"""全局共享 fixtures 与最小假件。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from intelligence_hub_v2.core.config import AppConfig
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage


@pytest.fixture
def anyio_backend():
    return "asyncio"


#: 会改变"模型在不在"这件事的环境变量（`asr/engine.py` 认这两个，见 `config/app.yaml` 的注释）。
_ASR_ENV_VARS = ("SENSEVOICE_MODEL_DIR", "SHERPA_ONNX_MODEL_DIR")


@pytest.fixture(autouse=True)
def _no_ambient_model_dir(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """单元测试**不许继承开发机上的环境变量**（`real_network` 那一档除外）。

    这台机器上 `SENSEVOICE_MODEL_DIR` 是**用户级持久**的，指向 V1 的真权重目录 ——
    那是 09-25 为真机转写那一跑设的，设完没人撤。于是任何构造 `AppConfig()` 的用例
    都会"意外地发现模型在"，`tests/unit/test_asr_engine.py` 那四条断言"找不到模型"的
    用例当场红了三条不同的形状（found == 真目录 / is None 拿到真目录 / == tmp 拿到真目录）。

    这类失效最难缠的地方是**方向**：它在 CI 上永远是绿的（CI 没有这个变量），
    只在开发机上红 —— 与 §7.12「预检主库错位」是同一族（测试读到了真世界的状态）。
    正确修法不是让某个人 `setx /r`，而是把这一层挡在测试外面：
    需要真权重的用例走 `-m real_network`，那里保留环境变量的效力。
    """
    if request.node.get_closest_marker("real_network"):
        return
    for name in _ASR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return


@pytest.fixture
async def storage() -> AsyncIterator[SqliteStorage]:
    """内存库，初始化后预置四个平台的运行态镜像行。

    `creators.platform` / `videos.platform` 都外键到 `platforms`（平台是受管实体，
    V1 §7.12 之后的设计），不先建行插博主/作品就 `FOREIGN KEY constraint failed`。
    采集/入库类用例到处都要这一步，收成一份公共 fixture。

    注意：`tests/unit/storage/` 与部分 core 用例自己定义了同名 `storage`，
    模块级 fixture 覆盖 conftest —— 它们各自的建库路径不受这里影响。
    """
    store = SqliteStorage.in_memory()
    await store.initialize()
    for name in ("douyin", "bilibili", "xiaohongshu", "youtube"):
        await store.platforms.upsert(name, enabled=True)
    yield store
    await store.close()


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    fs = FileStorage(tmp_path / "data")
    fs.ensure_dirs()
    return fs


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig()
