# ADR-0005: 任务调度层与事件总线

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q6、`docs/specs/task-runner.md`、`docs/specs/event-schema.md`

## 背景

V1 的痛点：

- 调度器是 `launcher_server.py` 里一份字典 `TASK_DEFS` + 子进程 spawn
- 靠 stdout 末行 JSON 拿结果（V1 §2 契约一），中间多打一行 JSON 前端就拿错
- 靠扫描 `downloads/manifests/` 拿历史（V1 §2 契约二，清单必须写终态，半路抛异常会被启动器的"计数兜底"猜成绿灯）
- 前后端没有实时事件流，进度靠轮询 `/api/tasks/<id>/stream`
- 已知遗留：`postprocess_bili_videos.py` 至今不写终态，看板靠 `MANIFEST_TS_PREFIX` + `MANIFEST_TASK_LABELS` 归一化名字、靠 summary 猜状态

V2 要：
1. 任务模型显式化（Pydantic）
2. 实时事件流（SSE）
3. 清单**强制**终态（上下文管理器禁止半截清单）
4. 平台禁用时任务自动从列表消失
5. V3 重写调度器时事件 schema 不变

## 决定

### 任务模型

```python
class TaskKind(StrEnum):
    PLATFORM_COLLECT = "platform_collect"
    ALL_PLATFORMS = "all_platforms"
    SINGLE_LINK = "single_link"
    ADD_CREATOR = "add_creator"
    BACKFILL = "backfill"
    POSTPROCESS = "postprocess"
    SYNC = "sync"
    PREFLIGHT = "preflight"
    MIGRATE = "migrate"

class TaskDefinition(BaseModel):
    name: str
    display_name: str
    kind: TaskKind
    params_schema: type[BaseModel]
    platforms: tuple[str, ...]          # 涉及哪些平台 → 关掉平台时任务自动禁用
    requires: tuple[str, ...]           # 能力依赖："cdp_bridge" / "ffmpeg" / "cookies:douyin" / "asr_engine"
    timeout_seconds: int | None
    cancellable: bool
    runner: Callable[[TaskContext, BaseModel], Awaitable[TaskResult]]
```

### 任务注册表

```python
TASKS: dict[str, TaskDefinition] = {
    "preflight":           TaskDefinition(...),
    "douyin_collect":      TaskDefinition(platforms=("douyin",), requires=("cdp_bridge", "cookies:douyin"), ...),
    "bilibili_collect":    TaskDefinition(platforms=("bilibili",), requires=("cookies:bilibili",), ...),
    "xiaohongshu_collect": TaskDefinition(platforms=("xiaohongshu",), requires=("cdp_bridge",), ...),
    "youtube_collect":     TaskDefinition(platforms=("youtube",), requires=("ffmpeg",), ...),
    "all_platforms":       TaskDefinition(kind=ALL_PLATFORMS, ...),
    "single_link":         TaskDefinition(...),
    "add_creator":         TaskDefinition(...),
    "backfill":            TaskDefinition(...),
    "postprocess":         TaskDefinition(kind=POSTPROCESS, requires=("ffmpeg", "asr_engine"), ...),
    "feishu_sync":         TaskDefinition(kind=SYNC, requires=("lark_cli",), ...),
    "migrate_from_v1":     TaskDefinition(kind=MIGRATE, ...),
}
```

V2.0 范围只实现 6 个核心任务：`preflight / douyin_collect / bilibili_collect / single_link / add_creator / postprocess`。

### 执行模型

- **编排层 in-process async**：所有任务都是 `async def runner(...)`，FastAPI 用 `asyncio.create_task` 起，**不再 spawn Python 子进程**（V1 stdout 末行 JSON 契约因此作废）
- **重活外包给子进程**：yt-dlp / ffmpeg / sherpa-onnx / lark-cli 这些**外部二进制**仍走 `asyncio.create_subprocess_exec`，但用 async 流读 stdout/stderr，进度实时推 EventBus，**不再依赖"末行 JSON"**（结果用 Pydantic 模型直接构造）
- **并发限流**：每个平台一个 `asyncio.Semaphore`（配置里 `max_concurrent_per_platform: 2`），避免同时跑两个抖音任务把风控踩炸
- **协作式取消**：`CancelToken` 注入到 runner，runner 在每个 await 点检查；外部子进程用 `process.terminate()` + 5s 后 `kill()`
- **超时**：`asyncio.wait_for(runner, timeout)`，超时记 `TaskResult.status="timeout"`
- **失败语义保留 V1 §1.3**：runner 抛异常 → 调度器捕获 → 写终态清单 → 推 `TaskFailed` 事件 → 前端如实显示，**绝不静默吞错**

### EventBus

```python
class EventType(StrEnum):
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_LOG = "task.log"
    TASK_FINISHED = "task.finished"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    MANIFEST_WRITTEN = "manifest.written"
    PLATFORM_HEALTH_CHANGED = "platform.health_changed"
    CONFIG_CHANGED = "config.changed"

class Event(BaseModel):
    type: EventType
    task_id: str | None
    timestamp: datetime
    payload: dict[str, Any]              # 按 type 用 Pydantic 判别联合
```

实现：in-process `asyncio.Queue` 多播 + SQLite 持久化（用于历史回放）。前端通过 **SSE**（`GET /api/events?task_id=xxx`）订阅。

**SSE vs WebSocket**：选 SSE。理由：单向、自动重连、HTTP/2 友好；前端没什么需要主动推给后端的（取消走 POST 即可）。

### 清单契约

每个任务结束时**必须**写一份 `data/manifests/<YYYYMMDD>-<HHMMSS>-<task_name>.json`，由 Pydantic 模型强制：

```python
class Manifest(BaseModel):
    schema_version: Literal["2.0"]
    task_name: str
    task_id: str
    kind: TaskKind
    status: Literal["success", "partial", "failed", "timeout", "cancelled"]
    started_at: datetime
    ended_at: datetime                                    # 终态必填
    summary: dict[str, int | str]
    platforms: list[str]
    failures: list[FailureRecord]
    artifacts: list[ArtifactRef]
    config_snapshot: dict[str, Any]
```

**双写**（文件 + SQLite）：前端历史列表查 SQLite，详情/审计查文件。

**强制终态**：用上下文管理器实现，**任何分支退出**（成功/异常/取消/超时）都走 `Manifest.finalize()`，从根上禁止 V1 §2 那种"半路抛出没写终态"的 bug。

```python
@asynccontextmanager
async def manifest_writer(task: TaskRun, storage: Storage) -> AsyncIterator[ManifestBuilder]:
    builder = ManifestBuilder(task)
    try:
        yield builder
    except Exception as e:
        builder.fail(e)
        raise
    except asyncio.CancelledError:
        builder.cancel()
        raise
    finally:
        manifest = builder.finalize()        # 强制写终态
        await storage.manifests.write(manifest)
        await storage.task_runs.update(...)
```

### API

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/tasks` | 列可用任务（按平台 enabled 过滤） |
| GET | `/api/tasks/{name}/schema` | 任务参数 JSON Schema |
| POST | `/api/tasks/{name}/run` | 启动任务，返回 `task_id` |
| GET | `/api/tasks/runs` | 历史运行（分页） |
| GET | `/api/tasks/runs/{id}` | 单次运行详情 + 清单 |
| GET | `/api/tasks/runs/{id}/events` | SSE 流（实时进度/日志） |
| POST | `/api/tasks/runs/{id}/cancel` | 取消 |
| GET | `/api/events` | 全局 SSE 流 |

## 后果

**好处**：
- V1 §2 契约一（stdout 末行 JSON）**作废**：不再 spawn Python 子进程；外部二进制的输出走事件流，不靠末行
- V1 §2 契约二（清单终态）**强化**：Pydantic + 上下文管理器，写不出半截清单
- V1 §7.22（按位扫描 vs 跟踪开关）**解决**：`BackfillTask` 参数里 `creator_url` 必填，调度器走 `parse_creator_url` 直接定位，**不再退化成全库扫描**
- V1 §7.10（路由靠记忆）**解决**：FastAPI 自动出 OpenAPI，路由不再"靠记忆"
- 实时事件流让前端能看到任务进度（V1 只能轮询）

**代价**：
- in-process async 意味着 FastAPI 重启会中断跑了一半的任务。缓解：任务运行状态写 SQLite，FastAPI 重启时能恢复"上次跑到哪了"（断点续传）；重活拆成多个子任务而不是一个长跑任务
- 清单双写（文件 + SQLite）有写两份的开销，但每次任务结束才写一次，可忽略
- 事件持久化到 SQLite 写量大（一个采集任务可能几千条事件）。缓解：WAL 模式 + 按 `task_id` 定期清理已完成任务的事件（保留 30 天）

**对 V3 的意义**：
- `TaskDefinition` + `Event` 都是 Pydantic 模型 = V3 即使重写调度器，事件 schema 不变，前端零改动
- 清单 JSON Schema 是审计契约，V3 沿用同一份
- `TaskContext.requires` 那套能力声明，V3 可以扩展（加新能力如 `"proxy_pool"`），不会破老任务
