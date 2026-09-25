"""L6 端到端：真 Chromium + 真 uvicorn + 真装配，只有"外部世界"是替身。

为什么还要这一层（Vitest 已经覆盖组件与查询钩子）：这里验的是**跨页、跨进程边界**的东西 ——
SSE 从服务线程推到浏览器、`<video>` 自己去打 `/api/videos/{id}/media` 并带 `Range`、
`data-theme` 换下去之后 computed style 真变了没有、`PUT` 配置之后磁盘上到底长了什么。
这些在 jsdom 里问不出来（它不加载样式表、不实现媒体、也没有 HTTP）。

四条边界纪律：

1. **一个字节都不出本机**。适配器全换成 `tests/unit/tasks/conftest.py` 里那个 `FakeAdapter`，
   健康探测走 `httpx.MockTransport`，采集 cron 与启动预检都在 tmp 配置里关掉。
   所以"跑一次采集"是真任务、真清单、真入库，但没有一条真实外网请求
   （真网那一路是 `-m real_network`，要 cookie 与桥）。
2. **绝不碰仓库的 `config/` 与 `data/`**：配置与数据都建在 tmp 目录里，
   `PUT /api/platforms/...` 写的也是那一份。
3. **不起 dev server，也不静默跳过**。`frontend/dist` 不在就先 `npm run build` ——
   跳过一条 e2e 等于没有那一条（V1 §7.14 同一个坑）。
4. **服务跑在后台线程 + 它自己的事件循环**上。`TaskScheduler.submit` 起的
   `asyncio.create_task` 要一个常驻循环可依附，而 pytest 这条线程上的循环是每用例借还的。
   因此：`AppState` 建在测试线程、由 lifespan 在服务线程里初始化并关闭
   （`owns_http=True`，让 lifespan 一并关掉那个 mock 客户端 —— 它建在服务那条循环上）。
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import structlog
import uvicorn
from playwright.async_api import async_playwright
from tests.unit.tasks.conftest import FakeAdapter

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.logging import QUIETED_LOGGERS
from intelligence_hub_v2.main import build_components, create_app
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.models.transcript import Transcript, TranscriptSegment
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

if TYPE_CHECKING:
    from playwright.async_api import Page

pytestmark = pytest.mark.e2e

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIST = _REPO_ROOT / "frontend" / "dist"

_PLATFORMS_YAML = """\
douyin:
  enabled: true
bilibili:
  enabled: true
"""

#: 这份 `app.yaml` 管的是"这个进程会不会自己动手做事"。
#: `collect_cron` 留空 = 不排定时采集；`enabled: false` = 连清理事件的那个 job 也不起。
#:
#: `paths.ffmpeg` 指到一个**测试目录下现写的假 ffmpeg**：这台机器 `where ffmpeg` 实测为空
#: （2026-09-25），而工坊页那条"截图包"的链路要真的走完
#: POST → 子进程 → 磁盘上有帧 → GET 出图 → `<img>` 解码。
#: **替身只有这一个**：进程是真的 `create_subprocess_exec` 起的、字节是真写进 tmp 的
#: 媒体树里的、图是真由 Chromium 去取并解码的（用例断 `naturalWidth > 0`，不是断 DOM 里
#: 有个 img）。"画面是不是那一秒的内容"仍然没验 —— 那要真 ffmpeg，属 `-m real_network`。
_APP_YAML_TEMPLATE = """\
scheduler:
  enabled: false
  collect_cron: null
  health_check_on_startup: false
logging:
  file: null
paths:
  ffmpeg: "@@FFMPEG@@"
"""

#: 一字节 1x1 的真 JPEG（Chromium 能解，实测 `naturalWidth == 1`）。
_FAKE_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)

_FAKE_FFMPEG_SHIM = '''"""假的 ffmpeg：只做"看得见的形状"，不做编解码。

写这份替身的理由与边界都在 `tests/e2e/conftest.py` 的 `_write_fake_ffmpeg` 上面。
"""
import base64
import os
import sys

argv = sys.argv[1:]
if "-version" in argv:
    print("ffmpeg version 7.1 FAKE-E2E Copyright (c) the FFmpeg project")
    raise SystemExit(0)
if "-i" not in argv:
    sys.stderr.write("fake ffmpeg: 没有 -i，看不懂这行命令\\n")
    raise SystemExit(2)
source = argv[argv.index("-i") + 1]
target = argv[-1]
if not os.path.isfile(source):
    # 与真 ffmpeg 同一形：输入不在就给非零码，不静默产出一张图。
    sys.stderr.write("fake ffmpeg: 输入不在磁盘上：" + source + "\\n")
    raise SystemExit(1)
with open(target, "wb") as handle:
    handle.write(base64.b64decode("@@JPEG_B64@@"))
'''


def _write_fake_ffmpeg(bin_dir: Path) -> Path:
    """在 tmp 里现写一对 `ffmpeg.bat` + `fake_ffmpeg.py`，返回 .bat 的路径。

    为什么是 .bat 包一层 python：`paths.ffmpeg` 要的是一个**可执行文件**，
    它会被直接放到 argv[0]（`infra/ffmpeg.py::ffmpeg_binary` 只做 `is_file()` 判断，
    不经 shell）。`.bat` 是这台机器上不需要额外权限就能被
    `asyncio.create_subprocess_exec` 直接起起来的脚本载体（2026-09-25 实测），
    而真 JPEG 的字节用 batch 写不出来，所以里面转一手 python。

    字节常量用 `@@JPEG_B64@@` 这种**替换记号**注入而不是 `str.format`：
    那份 shim 里有 `{source}` 这样的 f-string 花括号，`.format` 会把它们当成字段名
    （第一次就这么炸在 `KeyError: 'source'` 上，报的还是装配那一步，看不出是模板冲突）。
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "fake_ffmpeg.py"
    shim.write_text(_FAKE_FFMPEG_SHIM.replace("@@JPEG_B64@@", _FAKE_JPEG_B64), encoding="utf-8")
    bat = bin_dir / "ffmpeg.bat"
    body = f'@echo off\r\n"{sys.executable}" "{shim}" %*\r\nexit /b %ERRORLEVEL%\r\n'
    bat.write_text(body, encoding="utf-8")
    return bat


def _app_yaml(ffmpeg: Path) -> str:
    """那份 tmp `app.yaml`。路径加引号：`as_posix()` 里是 `C:/Users/...`，
    YAML 的 plain scalar 对裸冒号并不总是宽容（换成引号版是为了不依赖这一点）。"""
    return _APP_YAML_TEMPLATE.replace("@@FFMPEG@@", ffmpeg.as_posix())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """把 `setup_logging()` 冲掉的 handler 与 structlog 配置还原回去。

    不是洁癖，是**必要的隔离**：服务的 lifespan 会调 `setup_logging()`，它会清掉
    root 上所有 handler（包括 pytest 自己的 caplog）并全局重配 structlog。
    不还原的话，同一趟 `pytest` 里排在 e2e 之后的每条日志断言都替它背红 ——
    `make e2e` 单独起一次进程看不出来，裸跑 `pytest` 就是几百条 E。
    同一套还原动作在 `tests/unit/test_logging_setup.py` 里已经写过一份，照搬。
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in QUIETED_LOGGERS}
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
        structlog.reset_defaults()


@pytest.fixture(scope="session")
def dist_dir() -> Path:
    """**每次都构建**一次的 `frontend/dist`。

    为什么不"存在就复用"（第一版就是这么写的）：e2e serve 的是那份产物，产物过期时
    用例测的是"上一次构建的那个应用"。今天就真撞上过 —— 前端改了 `useRuns` 之后
    两条 e2e 红，而红的原因不是代码坏，是 dist 旧。那种红比红本身贵得多。
    vite 是增量构建（本机实测一秒级），代价可以接受。
    """
    # `npm.cmd` 而不是 `shell=True`：命令与 cwd 全是写死的，没有一处来自输入。
    # npm 在 PATH 上（AGENTS.md §4 的前置条件就是它可用），这里不猜绝对路径。
    subprocess.run(
        ["npm.cmd", "run", "build"],  # noqa: S607
        cwd=_REPO_ROOT / "frontend",
        check=True,
        timeout=900,
    )
    index = _DIST / "index.html"
    assert index.is_file(), f"构建跑完了但 {index} 还是不在"
    return _DIST


class _Server:
    """后台线程里的 uvicorn。"""

    def __init__(self, app: Any, port: int) -> None:  # FastAPI 实例
        self.base_url = f"http://127.0.0.1:{port:d}"
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        )
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        ready = threading.Event()

        def _wait() -> None:
            while not self.server.started:
                threading.Event().wait(0.05)
            ready.set()

        watcher = threading.Thread(target=_wait, daemon=True)
        watcher.start()
        assert ready.wait(30), "uvicorn 30 秒内没起来"

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=30)
        assert not self._thread.is_alive(), "服务线程没退：用例留下的任务还在跑"


def _meta(platform: str, video_id: str) -> VideoMeta:
    return VideoMeta.model_validate(
        {
            "platform": platform,
            "platform_video_id": video_id,
            "creator_ref": {
                "platform": platform,
                "platform_id": "c-e2e",
                "profile_url": f"https://{platform}.com/user/c-e2e",
            },
            "title": f"端到端作品 {video_id}",
            "webpage_url": f"https://{platform}.com/video/{video_id}",
            "duration_seconds": 12.0,
            "like_count": 1000,
        }
    )


def _transcript() -> Transcript:
    """两句话、带时间戳的字幕轨：转写那一步走的是"字幕优先"，不碰 ASR 与音频。"""
    segments = [
        TranscriptSegment(start_seconds=0.0, end_seconds=5.0, text="第一句，端到端。"),
        TranscriptSegment(start_seconds=5.0, end_seconds=12.0, text="第二句，验跳转。"),
    ]
    return Transcript(
        engine="bilibili_subtitle",
        language="zh",
        text="".join(segment.text for segment in segments),
        char_count=14,
        sentence_count=2,
        segments=segments,
    )


def _stub_adapter(name: str) -> FakeAdapter:
    """一家平台的假适配器：能认主页链接、能枚举两条、能"下"一个文件、能出字幕，且不出本机。

    `ref` / `profile` 这两个入参位是 `add_creator` 那条链要的（本文件 docstring 的"下一步
    第一步"）：没有它们，收录博主会在 `parse_creator_url` 上炸，于是"收录 → 采集 → 入库"
    这条只能在集成层验的链，在 L6 也有了一条真的。
    """

    def artifact(_meta: VideoMeta, dest: Path) -> SingleFileArtifact:
        # 不是可解码的媒体（见 `test_detail_plays_the_served_file` 里那句"验的是那一趟往返"），
        # 但字节是真的落在 tmp 的媒体树下、真的由 `/media` 端点交出去。
        #
        # `media_source` 必须是 `models/media.MediaSource` 那四个字面量之一：这里原来写的
        # 是 `"e2e_stub"`，而**没有任何一条 e2e 真的走到 `download_media`**，所以它一直没人发现
        # （`SingleFileArtifact` 当场 pydantic 判失败，症状是"采集 failed + 两条 download 失败"，
        # 2026-09-25 加工坊那条长链时才炸出来）。选 `page_play_url` 是因为它是"直链下载"，
        # 与这个替身的形状一致。
        path = dest / "media.webm"
        path.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 32)
        return SingleFileArtifact(
            path=path, size_bytes=36, media_source="page_play_url", has_audio=False
        )

    return FakeAdapter(
        name,
        videos=[_meta(name, f"{name}-e2e-1"), _meta(name, f"{name}-e2e-2")],
        artifact_factory=artifact,
        subtitles=_transcript() if name == "bilibili" else None,
        ref=CreatorRef.model_validate(
            {
                "platform": name,
                "platform_id": "c-e2e",
                "profile_url": f"https://{name}.com/user/c-e2e",
            }
        ),
        profile=CreatorProfile(
            ref=CreatorRef.model_validate(
                {
                    "platform": name,
                    "platform_id": "c-e2e",
                    "profile_url": f"https://{name}.com/user/c-e2e",
                }
            ),
            name="端到端博主",
        ),
    )


@pytest.fixture
def served(tmp_path: Path, dist_dir: Path) -> Iterator[tuple[AppState, str]]:
    """注入好的 `AppState` + 已起好的 base_url。

    `dist_dir` 这个参数不是摆设：它保证"构建产物存在"这件事在起服务之前就被确立，
    而服务的静态挂载看的就是那一个目录（顺序反了会挂出一个 404 的 `/`）。
    """
    del dist_dir  # 只要它跑过
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "platforms.yaml").write_text(_PLATFORMS_YAML, encoding="utf-8")
    fake_ffmpeg = _write_fake_ffmpeg(tmp_path / "fakebin")
    (config_dir / "app.yaml").write_text(_app_yaml(fake_ffmpeg), encoding="utf-8")

    manager = ConfigManager(config_dir)
    manager.load()

    files = FileStorage(tmp_path / "data")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    # 库**不在这里 initialize**：`lifespan="on"` 会在服务那条循环上建连接池，
    # 测试线程先建就变成"连接建在 A 循环、活在 B 循环"（症状是没规律的 loop closed）。
    storage = SqliteStorage(tmp_path / "data" / "ih.sqlite3")
    state = build_components(manager, storage=storage, files=files, http=http)
    # 客户端由 lifespan 关（它跑在服务的循环上），所以这里交给它所有权。
    state.owns_http = True
    for name in manager.platform_names():
        # `PlatformRegistry.get()` 认这张缓存 → 任务线程拿到的是假适配器，
        # 而路由、清单、SSE、库、事件总线全是真的。走缓存不改进程级的 `PLATFORMS`。
        state.registry._instances[name] = _stub_adapter(name)

    server = _Server(create_app(state=state), _free_port())
    server.start()
    try:
        yield state, server.base_url
    finally:
        server.stop()


@pytest.fixture
async def page(served: tuple[AppState, str]) -> AsyncIterator[Page]:
    """一条用例一个浏览器，跑在 **pytest-asyncio 自己那个 loop** 上。

    为什么必须是 async API（第一版用 `sync_playwright`，代码更短，但那是错的）：
    sync API 会在主线程里再引入一个 loop 主人，退出时把 asyncio 记录"当前跑着哪个 loop"
    的那个 ContextVar 留在脏值上。之后同一进程里每一次 `Runner.run()` / `asyncio.run()`
    都在入口检查上炸 —— 报的不是"这条 e2e 红"，而是**后面每条 async 用例**红
    （实测一次混跑 161 failed + 1604 errors）。
    试过清 policy、清 `policy._local._loop`、在 fixture teardown 里复位那个变量，全部无效：
    Playwright 自己的 `close()` 在那之后又脏一次。**从源头不要第二个 loop 主人**才是解，
    所以这里换成 async API，并且不往全局配置里塞任何排除规则。

    浏览器是**函数级**的：会话级的 async fixture 需要 `loop_scope="session"`，
    那会把全场 async 用例都拖进同一个 loop —— 为了省每次 0.4 秒去换一个新的隐式全局，
    正是这一篇要找的东西。
    """
    _state, base_url = served
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--disable-gpu"])
        context = await browser.new_context(base_url=base_url)
        fresh = await context.new_page()
        try:
            yield fresh
        finally:
            await fresh.close()
            await context.close()
            await browser.close()


@pytest.fixture
def api(served: tuple[AppState, str]) -> Iterator[httpx.Client]:
    """同一份装配的**同步** HTTP 出口：给"先塞数据再开浏览器"的用例。

    刻意走真 HTTP 而不是直接调 repository：那条路要和生产一致（同一套校验、同一个 OpenAPI）。
    """
    _state, base_url = served
    with httpx.Client(base_url=f"{base_url}/api", timeout=30.0) as client:
        yield client


@pytest.fixture
def data_dir(served: tuple[AppState, str], tmp_path: Path) -> Path:
    """这份装配的 `data/` 根（检查"配置文件被写成什么样"的用例用）。"""
    return tmp_path / "data"
