# Spec: Task Runner

> **状态**：Locked（V2.0 起契约稳定，改动需走 ADR）
> **源文件**：`src/intelligence_hub_v2/tasks/`
> **相关 ADR**：[0005](../adr/0005-task-runner-and-event-bus.md)

任务调度层的契约。V3 重写调度器时，这份 spec 不能动。

---

## 1. 总览

```
┌──────────────┐
│  TaskRegistry │  ← 显式注册表（dict[str, TaskDefinition]）
└──────┬───────┘
       │ uses
       ▼
┌──────────────┐    ┌─────────────┐    ┌──────────────┐
│ TaskDefinition│ →  │ TaskRunner  │ →  │ EventBus     │
│ (Pydantic)    │    │ (asyncio)   │    │ (SSE + SQLite)│
└──────────────┘    └──────┬──────┘    └──────────────┘
                           │ uses
                           ▼
                    ┌──────────────┐
                    │ TaskContext  │  ← 注入：deps / adapters / events / storage / cancel_token
                    └──────┬───────┘
                           │ calls
                           ▼
                    ┌──────────────┐
                    │ PlatformAdapter│  ← ADR-0004
                    └──────────────┘
```

---

## 2. 核心类型

### 2.1 `TaskKind`

```python
from enum import StrEnum

class TaskKind(StrEnum):
    PLATFORM_COLLECT = "platform_collect"      # 单平台采集
    ALL_PLATFORMS = "all_platforms"            # 一键全平台
    SINGLE_LINK = "single_link"                # 收一条作品
    ADD_CREATOR = "add_creator"                # 收录博主
    BACKFILL = "backfill"                      # 爆款回溯
    POSTPROCESS = "postprocess"                # ASR 转写（跨平台统一）
    SYNC = "sync"                              # 飞书同步
    PREFLIGHT = "preflight"                    # 健康检查
    MIGRATE = "migrate"                        # V1→V2 数据迁移
```

### 2.2 `TaskDefinition`

```python
from pydantic import BaseModel
from typing import Callable, Awaitable

class TaskDefinition(BaseModel):
    name: str
    """任务标识，全小写下划线，如 'douyin_collect'。必须与 TASKS 注册表 key 一致。"""

    display_name: str
    """人类可读名称，前端展示用。"""

    kind: TaskKind

    params_schema: type[BaseModel]
    """Pydantic 模型类，前端据此自动渲染参数表单。
    /api/tasks/{name}/schema 返回它的 JSON Schema。"""

    platforms: tuple[str, ...]
    """涉及哪些平台。关掉平台时这个任务自动从 /api/tasks 消失。
    跨平台任务（如 postprocess / preflight）写空 tuple。"""

    requires: tuple[str, ...]
    """能力依赖。调度器跑前检查，缺了直接拒。
    取值：'cdp_bridge' / 'ffmpeg' / 'ffprobe' / 'node' / 'yt_dlp' /
          'asr_engine' / 'lark_cli' / 'cookies:<platform>' / 'network'"""

    timeout_seconds: int | None
    """超时。None = 不限。超时记 status='timeout'。"""

    cancellable: bool
    """是否支持取消。False 的任务前端不显示取消按钮。"""

    runner: Callable[["TaskContext", BaseModel], Awaitable["TaskResult"]]
    """实际执行函数。第二个参数是 params_schema 的实例。"""

    model_config = {"arbitrary_types_allowed": True}   # runner 是 Callable
```

### 2.3 `TaskContext`

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass
class TaskContext:
    """注入给 runner 的依赖袋。"""

    task_id: str
    """UUID，用于关联事件流与清单。"""

    deps: "AdapterDeps"
    """桥客户端、HTTP、cookie 管理器、ASR 引擎。"""

    adapters: "PlatformRegistry"
    """拿到对应平台的 adapter：ctx.adapters.get('douyin')。"""

    events: "EventBus"
    """发进度事件：await ctx.events.publish(Event(...))。"""

    storage: "Storage"
    """SQLite + 文件。"""

    cancel_token: "CancelToken"
    """协作式取消。runner 在每个 await 点检查：
    if ctx.cancel_token.is_cancelled: raise TaskCancelled()"""

    workdir: Path
    """任务专属临时目录（data/tmp/<task_id>/），结束时自动清理。"""

    logger: "structlog.BoundLogger"
    """已绑定 task_id / task_name 字段的 logger。"""

    config_snapshot: dict
    """任务启动时的平台配置快照（审计用，进清单）。"""
```

### 2.4 `TaskResult`

```python
class TaskResult(BaseModel):
    status: Literal["success", "partial", "failed"]
    """success: 全部完成
    partial: 部分完成（有失败但主体成功）
    failed: 主体失败"""

    summary: dict[str, int | str]
    """如 {'downloaded': 5, 'failed': 1, 'skipped': 2}"""

    artifacts: list["ArtifactRef"] = []
    """产物引用（媒体 / 口播稿 / metadata 路径）。"""

    failures: list["FailureRecord"] = []
    """失败记录（每条带 platform / stage / 原文）。"""
```

### 2.5 `CancelToken`

```python
class CancelToken:
    """协作式取消。"""

    @property
    def is_cancelled(self) -> bool: ...

    def cancel(self) -> None: ...
    """请求取消。runner 在下一个 await 点检查 is_cancelled。"""

    async def wait(self) -> None:
    """阻塞直到取消（用于 select / wait_first 模式）。"""
```

### 2.6 `Manifest`

```python
class Manifest(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    task_name: str
    task_id: str
    kind: TaskKind
    status: Literal["success", "partial", "failed", "timeout", "cancelled"]
    started_at: datetime
    ended_at: datetime                        # 终态必填
    summary: dict[str, int | str]
    platforms: list[str]
    failures: list[FailureRecord]
    artifacts: list["ArtifactRef"]
    config_snapshot: dict[str, Any]           # 任务启动时的配置快照
    error: str | None = None                  # 任务级错误原文（实施期补，见下）

class ArtifactRef(BaseModel):
    kind: Literal["media", "transcript", "metadata", "cover", "manifest"]
    path: Path                                # 相对 data/
    platform: str | None = None
    video_id: str | None = None
    size_bytes: int | None = None
```

> **实施期修订（2026-09-22，Task 2）**：`error` 字段是实施时补的，原 spec 漏了。
> `ManifestBuilder.fail(exc)` 拿到了异常原文却没地方放，`failures[]` 装的是**逐条 item** 的失败、
> `summary` 的语义是计数 —— 两个都不该塞任务级错误。缺这个字段等于违反 V1 §1.3「不许吞错」，
> 也正是 V1 §7.22 那次事故的样子（用户只看到"退出码 1"+ 一屏 traceback，清单里没有原因）。
> 同时 `FailureRecord` 放宽两处：`platform` 可为 `None`（runner 级失败不归任何单一平台），
> `stage` 的 Literal 多一个 `"task"`（不是某个 item 的流水线阶段挂了，而是任务本身挂了）。
> 详见 `docs/lessons.md` 坑 2。

**强制终态**：用上下文管理器实现，**任何分支退出**（成功/异常/取消/超时）都走 `finalize()`：

```python
@asynccontextmanager
async def manifest_writer(task_run: TaskRun, storage: Storage) -> AsyncIterator[ManifestBuilder]:
    builder = ManifestBuilder(task_run)
    try:
        yield builder
    except asyncio.CancelledError:
        builder.cancel()
        raise
    except asyncio.TimeoutError:
        builder.timeout()
        raise
    except Exception as e:
        builder.fail(e)
        raise
    else:
        builder.succeed()          # ← 这一行有 bug，见下面的实施期修订
    finally:
        manifest = builder.finalize()         # 强制写终态，ended_at 必填
        await storage.manifests.write(manifest)
        await storage.task_runs.update(task_run.id, status=manifest.status, ended_at=manifest.ended_at)
```

> **实施期修订（2026-09-22，Task 4）**，三处，都是"照草图抄会留下 bug"的那种：
>
> 1. **`else: builder.succeed()` 会把 handler 设的 `partial` 改成 `success`。**
>    正常退出与"跑完了但有些 item 失败"是同一件事的两个说法，草图只看到前者。
>    症状就是 V1 看板的老问题：两条爆款下载失败，界面显示全成功。
>    实际实现是一条判据（`core/manifest.py::_may_settle`）：
>    **没人设过 → 按退出方式补；handler 设过 `success` 却又抛异常 → 异常赢；
>    handler 设过 `partial` / `failed` / `cancelled` / `timeout` → 不动。**
>    看护：`test_handler_set_partial_survives` 与 `test_success_then_raise_never_reports_success`
>    —— 两条方向相反，缺一条就会把规则改回草图那样而全绿。
> 2. **收尾要 `await` 两次写，而草图里的方法名与实际 Repository 不一致。**
>    实际是 `storage.manifests.record(task_id, manifest, file_path)`（文件是权威源、
>    表是索引，所以要给相对路径）与 `storage.task_runs.finish(task_id, status=...,
>    summary=..., manifest_path=..., error_text=..., ended_at=...)`，
>    两者放在**同一个 `storage.transaction()`** 里：一半成功会得到"列表有记录、任务还在 running"。
> 3. **`except TaskCancelled` 要单独一档，落到 `cancelled` 而不是 `failed`。**
>    V2 的取消是协作式的（§2.5 `CancelToken` → runner 抛 `TaskCancelled`），
>    所以它走的是 `Exception` 那条路，会被草图的 `builder.fail(e)` 标成失败。
>    人主动停的不算失败，而且 `EventRepository.prune()` 给 `failed`/`timeout` 三倍保留期。
>
> 另外签名与实际不同（实际）：
> `manifest_writer(task_name, task_id, kind, config_snapshot, *, storage, files, bus=None, timeout_seconds=None)`
> —— 要收 `files` 才能算出清单路径（双写里的"盘"那一半），
> `TaskRun` 对象不必传，`builder` 只要那四个字段。

V1 §2 契约二（清单必须写终态）由此**结构性保证**，写不出半截清单。
实现侧还有两条草图没写的纪律，见 `core/manifest.py` 的模块 docstring
（文件写不成就不登记索引；收尾代码绝不盖掉正在传播的异常）。

---

## 3. 任务注册表

```python
TASKS: dict[str, TaskDefinition] = {
    "preflight":           TaskDefinition(name="preflight", kind=PREFLIGHT, platforms=(), requires=(), ...),
    "douyin_collect":      TaskDefinition(name="douyin_collect", kind=PLATFORM_COLLECT, platforms=("douyin",), requires=("cdp_bridge", "cookies:douyin"), ...),
    "bilibili_collect":    TaskDefinition(name="bilibili_collect", kind=PLATFORM_COLLECT, platforms=("bilibili",), requires=("cookies:bilibili",), ...),
    "xiaohongshu_collect": TaskDefinition(name="xiaohongshu_collect", kind=PLATFORM_COLLECT, platforms=("xiaohongshu",), requires=("cdp_bridge",), ...),
    "youtube_collect":     TaskDefinition(name="youtube_collect", kind=PLATFORM_COLLECT, platforms=("youtube",), requires=("ffmpeg",), ...),
    "all_platforms":       TaskDefinition(name="all_platforms", kind=ALL_PLATFORMS, platforms=(), requires=(), ...),
    "single_link":         TaskDefinition(name="single_link", kind=SINGLE_LINK, platforms=(), requires=(), ...),
    "add_creator":         TaskDefinition(name="add_creator", kind=ADD_CREATOR, platforms=(), requires=(), ...),
    "backfill":            TaskDefinition(name="backfill", kind=BACKFILL, platforms=(), requires=(), ...),
    "postprocess":         TaskDefinition(name="postprocess", kind=POSTPROCESS, platforms=(), requires=("ffmpeg", "asr_engine"), ...),
    "feishu_sync":         TaskDefinition(name="feishu_sync", kind=SYNC, platforms=(), requires=("lark_cli",), ...),
    "migrate_from_v1":     TaskDefinition(name="migrate_from_v1", kind=MIGRATE, platforms=(), requires=(), ...),
}
```

**V2.0 范围**：只实现 `preflight / douyin_collect / bilibili_collect / single_link / add_creator / postprocess` 6 个。

---

## 4. 调度器执行模型

### 4.1 in-process async（不再 spawn Python 子进程）

V1 的 `TASK_DEFS` + 子进程 spawn + stdout 末行 JSON 契约**作废**。V2 所有任务都是 `async def runner(...)`，FastAPI 用 `asyncio.create_task` 起。

**外部二进制仍走子进程**：yt-dlp / ffmpeg / sherpa-onnx / lark-cli 这些走 `asyncio.create_subprocess_exec`，但用 async 流读 stdout/stderr，进度实时推 EventBus，**不再依赖"末行 JSON"**（结果用 Pydantic 模型直接构造）。

### 4.2 并发限流

每个平台一个 `asyncio.Semaphore`（配置 `scheduler.max_concurrent_per_platform`），全局一个 Semaphore（`scheduler.max_concurrent_global`）。

```python
class TaskScheduler:
    def __init__(self, ...):
        self._platform_sems: dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(config.max_concurrent_per_platform)
            for name in PLATFORMS
        }
        self._global_sem = asyncio.Semaphore(config.max_concurrent_global)

    async def run(self, task_name: str, params: BaseModel) -> str:
        task_def = TASKS[task_name]
        # 检查平台开关
        for p in task_def.platforms:
            if not self._platform_enabled(p):
                raise TaskRejected(f"platform {p} disabled")
        # 检查 requires
        await self._check_requires(task_def.requires)
        # 起任务
        task_id = str(uuid4())
        async with self._global_sem:
            sems = [self._platform_sems[p] for p in task_def.platforms]
            async with ExitStack() as stack:
                for s in sems: await stack.enter_async_context(s)
                asyncio.create_task(self._execute(task_def, task_id, params))
        return task_id
```

### 4.3 协作式取消

`CancelToken` 注入到 `TaskContext`，runner 在每个 await 点检查：

```python
async def runner(ctx: TaskContext, params: Params) -> TaskResult:
    async for video in adapter.list_creator_videos(...):
        if ctx.cancel_token.is_cancelled:
            raise TaskCancelled()
        await adapter.download_media(video, ...)
```

外部子进程取消：`process.terminate()` + 5s 后 `process.kill()`。

### 4.4 超时

`asyncio.wait_for(runner, timeout)`，超时记 `status='timeout'`，写终态清单。

### 4.5 失败语义（V1 §1.3）

runner 抛异常 → 调度器捕获 → 写终态清单 → 推 `TASK_FAILED` 事件 → 前端如实显示。**绝不静默吞错**，绝不允许"看起来在跑"。

---

## 5. API

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/tasks` | 列可用任务（按平台 enabled 过滤） |
| GET | `/api/tasks/{name}/schema` | 任务参数 JSON Schema |
| POST | `/api/tasks/{name}/run` | 启动任务，返回 `{"task_id": "..."}` |
| GET | `/api/tasks/runs` | 历史运行（分页，`?page=&size=&status=&platform=`） |
| GET | `/api/tasks/runs/{id}` | 单次运行详情 + 清单 |
| GET | `/api/tasks/runs/{id}/events` | SSE 流（实时进度/日志） |
| POST | `/api/tasks/runs/{id}/cancel` | 取消 |
| GET | `/api/events` | 全局 SSE 流（订阅平台健康/配置变更等） |

### 5.1 `POST /api/tasks/{name}/run`

**请求体**：`params_schema` 的实例（JSON）

**响应**：
- `202 Accepted` + `{"task_id": "<uuid>"}`
- `422 Unprocessable Entity`：参数校验失败 / 平台禁用 / requires 缺失
- `409 Conflict`：同名任务已在跑（如果定义里 `singleton=True`）

### 5.2 `GET /api/tasks/runs/{id}/events`（SSE）

```
event: task.started
data: {"task_id": "...", "timestamp": "...", "payload": {...}}

event: task.progress
data: {"task_id": "...", "timestamp": "...", "payload": {"progress": 0.3, "message": "下载中 3/10"}}

event: task.log
data: {"task_id": "...", "timestamp": "...", "payload": {"level": "info", "message": "..."}}

event: task.finished
data: {"task_id": "...", "timestamp": "...", "payload": {"status": "success", "summary": {...}}}
```

SSE 自动重连，前端用 `EventSource`。

---

## 6. V1 契约对应

| V1 契约 | V2 状态 |
|---|---|
| §2 契约一：stdout 末行 JSON | **作废**（不再 spawn Python 子进程） |
| §2 契约二：清单必须写终态 | **强化**（Pydantic + 上下文管理器，写不出半截清单） |
| §2 契约三：页面 JS 占位符不加引号 | **保留**（桥里的 JS 注入还是这个模式，进 `BridgeClient.evaluate()` 的契约测试） |
| §7.10：路由靠记忆 | **解决**（FastAPI 自动出 OpenAPI） |
| §7.22：按位扫描 vs 跟踪开关 | **解决**（`BackfillTask` 参数 `creator_url` 必填，调度器走 `parse_creator_url` 直接定位，不再退化全库扫描） |

---

## 7. V3 重写时的契约

V3 即使重写调度器（换语言、换并发模型），只要：

1. `TaskDefinition` Pydantic 模型不变
2. `Event` schema 不变（见 `event-schema.md`）
3. 清单 JSON Schema 不变
4. API 路径与响应格式不变
5. 失败语义（如实失败、不静默吞错）不变

→ 前端零改动，契约测试套件零改动。
