# Spec: Event Schema

> **状态**：Locked（V2.0 起契约稳定，改动需走 ADR）
> **源文件**：`src/intelligence_hub_v2/events/`
> **相关 ADR**：[0005](../adr/0005-task-runner-and-event-bus.md)

事件总线的 schema。前端 SSE 订阅、SQLite 持久化、V3 重写时的事件契约。

---

## 1. 总览

```
┌─────────────┐
│ TaskRunner  │ ─── publish ───▶ ┌─────────────┐
│ Adapter     │                  │  EventBus   │
│ ConfigLayer │                  │ (in-proc)   │
└─────────────┘                  └──────┬──────┘
                                        │ multicast
                              ┌─────────┼─────────┐
                              ▼         ▼         ▼
                         ┌────────┐ ┌────────┐ ┌────────┐
                         │ SSE    │ │ SQLite │ │ Logger │
                         │ Handler│ │ Persist│ │        │
                         └────────┘ └────────┘ └────────┘
```

实现：in-process `asyncio.Queue` 多播 + SQLite 持久化（用于历史回放）。前端通过 SSE 订阅。

---

## 2. `EventType`

```python
from enum import StrEnum


class EventType(StrEnum):
    # 任务生命周期
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_LOG = "task.log"
    TASK_FINISHED = "task.finished"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"

    # 清单
    MANIFEST_WRITTEN = "manifest.written"

    # 平台
    PLATFORM_HEALTH_CHANGED = "platform.health_changed"

    # 配置
    CONFIG_CHANGED = "config.changed"

    # 数据变更（前端实时刷新）
    CREATOR_ADDED = "creator.added"
    CREATOR_UPDATED = "creator.updated"
    VIDEO_ADDED = "video.added"
    VIDEO_HIDDEN = "video.hidden"
    VIDEO_UNHIDDEN = "video.unhidden"
    TRANSCRIPT_READY = "transcript.ready"
```

---

## 3. `Event` 模型

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any, Literal


class Event(BaseModel):
    """所有事件的基类。payload 按 type 用 Pydantic 判别联合。"""

    type: EventType
    task_id: str | None = None
    """关联的任务 ID（任务相关事件必填，其他可空）。"""

    timestamp: datetime
    """事件产生时间（UTC，ISO 8601）。"""

    payload: dict[str, Any]
    """按 type 用下方对应的 Payload 模型校验。"""
```

### 3.1 任务生命周期 payload

```python
class TaskStartedPayload(BaseModel):
    task_name: str
    kind: str
    params: dict[str, Any]
    config_snapshot: dict[str, Any]


class TaskProgressPayload(BaseModel):
    progress: float = Field(ge=0.0, le=1.0)
    message: str | None = None
    """如 '下载中 3/10' / '转写中' / '枚举博主视频'"""
    stage: str | None = None
    """当前阶段：'list' / 'download' / 'transcribe' / 'store'"""
    current_item: str | None = None
    """当前处理的对象（视频标题/博主名）"""


class TaskLogPayload(BaseModel):
    level: Literal["debug", "info", "warning", "error"]
    message: str
    logger: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class TaskFinishedPayload(BaseModel):
    status: Literal["success", "partial"]
    summary: dict[str, int | str]
    manifest_path: str
    duration_seconds: float


class TaskFailedPayload(BaseModel):
    status: Literal["failed", "timeout"]
    error: str  # 原文，不许吞错
    error_kind: str | None = None
    manifest_path: str | None = None
    duration_seconds: float


class TaskCancelledPayload(BaseModel):
    reason: str | None = None
    manifest_path: str
    duration_seconds: float
```

### 3.2 清单 payload

```python
class ManifestWrittenPayload(BaseModel):
    manifest_path: str
    schema_version: str
    status: str
    summary: dict[str, int | str]
```

### 3.3 平台 payload

```python
class PlatformHealthChangedPayload(BaseModel):
    platform: str
    previous_status: Literal["ok", "degraded", "unreachable", "unknown"]
    new_status: Literal["ok", "degraded", "unreachable", "unknown"]
    detail: str | None = None
    components: dict[str, str] = Field(default_factory=dict)
```

### 3.4 配置 payload

```python
class ConfigChangedPayload(BaseModel):
    scope: Literal["app", "platform"]
    platform: str | None = None
    """scope='platform' 时必填。"""
    changed_fields: list[str]
    """改了哪些字段（不暴露值，避免泄露敏感配置）。"""
    requires_restart: bool = False
    """部分字段（如 app.host / app.port）改了要重启服务。"""
```

### 3.5 数据变更 payload

```python
class CreatorAddedPayload(BaseModel):
    creator_id: int
    platform: str
    platform_id: str
    name: str


class CreatorUpdatedPayload(BaseModel):
    creator_id: int
    changed_fields: list[str]


class VideoAddedPayload(BaseModel):
    video_id: int
    platform: str
    platform_video_id: str
    title: str
    creator_id: int | None


class VideoHiddenPayload(BaseModel):
    video_id: int
    reason: str


class VideoUnhiddenPayload(BaseModel):
    video_id: int


class TranscriptReadyPayload(BaseModel):
    video_id: int
    engine: str
    char_count: int
    sentence_count: int
```

---

## 4. `EventBus` Protocol

```python
from typing import AsyncIterator, Protocol


class EventBus(Protocol):
    async def publish(self, event: Event) -> None:
        """发布事件。多播到所有订阅者；**带 `task_id` 的**才持久化到 SQLite。

        > **实施期修订（2026-09-22, Task 3）**：`task_events.task_id` 是
        > `TEXT NOT NULL`（§6），所以 `platform.health_changed` 与 `config.changed`
        > 这类**全局事件只广播、不落库**。
        >
        > 为什么不为了持久化把 `task_id` 放宽成可空：这两类事件没有"历史回放"的价值
        > （健康状态看当下，配置看 `config/*.yaml`），而硬挂到某个任务上更糟 ——
        > 一个与任务无关的事件会因为"它属于哪次任务"这个问题被随意回答，
        > 于是任务详情页的日志里出现不属于这个任务的行。
        > `EventRepository.append()` 对这种事件直接抛（外键 + NOT NULL），不静默丢弃。
        """

    def subscribe(
        self,
        types: set[EventType] | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """订阅事件流。
        types 为空 = 订阅所有类型。
        task_id 不为空 = 只订阅该任务的事件。
        返回的 AsyncIterator 用 aclose() 关闭。"""

    async def replay(
        self,
        task_id: str,
        since: datetime | None = None,
    ) -> list[Event]:
        """从 SQLite 回放历史事件（前端刷新页面后用）。"""
```

> **实施期修订（2026-09-23，Task 9）· SSE 装配的顺序是「先 `subscribe`、再 `replay`」。**
>
> 原先这一节写的是"先 replay 追平、再 subscribe 接活的"。反过来会**漏掉一条事件**：
> replay 进行的那几十毫秒里任务刚好跑完，那条 `task.finished` 既不在已回放的窗口内、
> 也没被（尚未建立的）订阅收到 —— 前端就永远停在"还在跑"。订阅先行之后，replay 期间
> 到达的活事件会先进缓冲，再由后面的实时循环补上。顺带堵了一个洞：原写法在 replay
> 阶段就被 `aclose()` 时，`finally` 引用的是尚未绑定的订阅句柄。
>
> **代价是重叠**：同一条事件可能同时出现在回放段与实时段。V2 服务端不发 `id:` 字段
> （§5.1），SSE 层不去重，由消费方按 `(task_id, timestamp, type)` 自行收敛。
> 判据是「漏一条比重一条严重得多」—— 重一条只是日志多一行，漏一条是状态永久错。
>
> `replay()` 的签名**没有** `types`：过滤器由装配层在内存里对回放结果补一遍，
> 使回放段与实时段共用同一套过滤条件（见 §5.1）。

---

## 5. SSE 端点

### 5.1 `GET /api/events`

全局事件流。Query 参数：
- `types`：逗号分隔的 EventType 列表，空 = 全部。**回放段与实时段共用这一套过滤**
- `since`：ISO 8601 时间戳，回放该时间之后的事件
- `task_id`：只看这一次任务。**只有带上它才会回放历史**（`replay()` 按 `task_id` 取，
  没有 task_id 就没有可回放的键），不带 `task_id` 时 `since` 不起作用 —— 这点必须写明，
  否则前端会以为传了 `since` 就有追历史的能力
- 认不出的 `types` 取值被忽略而不是整个流 400（前端与后端版本不一致是常态）

响应：`text/event-stream`

```
event: task.started
data: {"type":"task.started","task_id":"abc","timestamp":"2026-09-22T12:00:00Z","payload":{...}}

event: task.progress
data: {"type":"task.progress","task_id":"abc","timestamp":"2026-09-22T12:00:01Z","payload":{"progress":0.1,"message":"枚举博主视频"}}

:ping - 2026-09-22 12:00:16+00:00

event: task.finished
data: {"type":"task.finished","task_id":"abc","timestamp":"2026-09-22T12:05:00Z","payload":{...}}
```

**保活行**：`sse-starlette` 的 `EventSourceResponse` 默认每 **15 秒**发一行 SSE **注释**
（`:ping - <UTC 时间戳>`），防代理超时。原文写的是「每 30 秒一次 `:keepalive`」——
形状对（都是注释行），**文字与周期都不对**，以上面这行为准。
注释行不会触发 `EventSource.onmessage`，所以前端收不到额外事件，也不用处理它。
没有传 `ping=` 覆写，也就是说这是库的默认值：换库或升级 `sse-starlette` 时这一条会漂，
看护点在 `tests/integration/test_api_events.py` 只钉了事件流本身，没钉保活行。

### 5.2 `GET /api/tasks/runs/{id}/events`

单任务事件流。等价于 `/api/events?task_id={id}`，但路径更直观。

> 两条路由共用同一个装配函数（`api/v1/events.py:stream_events`），参数语义与
> 回放/保活行为完全一致 —— 不是"一个优化版一个简易版"。
> （实施期修订 2026-09-23：`/api/events` 原先**不吃** `task_id`，那句"等价于"是假的，
> 而 §7 的前端示例就是照这句写的。）

---

## 6. SQLite 持久化

```sql
CREATE TABLE task_events (
    id           INTEGER PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    timestamp    TIMESTAMP NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX idx_task_events_task_time ON task_events(task_id, timestamp);
CREATE INDEX idx_task_events_type_time ON task_events(type, timestamp);
```

**保留策略**：
- 已完成任务的事件保留 `storage.event_retention_days`（默认 30 天）
- 后台 APScheduler job 每天清理过期事件
- 失败任务的事件保留更久（90 天，方便排查）

> **实施期补细（2026-09-22, Task 3）**，三条 `prune()` 的具体口径
> （看护在 `tests/unit/storage/test_events_repo.py`）：
> - **享受 3 倍保留期的是 `failed` 与 `timeout`**。`cancelled` 不算 ——
>   那是人主动停的，没有"为什么挂了"要查。
> - **停在 `running` 的任务一条都不删**，哪怕它已经跑了很久。
>   删掉正在推流的任务的历史，等于让前端日志凭空断一段，而没人知道断了。
> - `retention_days < 1` 在**入口**就抛 `ValueError`。
>   配成 0 的字面意思是"全删"，那不是配置而是事故，不该被静默执行。

---

## 7. 前端订阅模式

```typescript
// frontend/src/hooks/useTaskEvents.ts
import { useEffect, useState } from 'react';
import type { Event, EventType } from '@/lib/api-types';

export function useTaskEvents(taskId: string, types?: EventType[]) {
  const [events, setEvents] = useState<Event[]>([]);

  useEffect(() => {
    const params = new URLSearchParams({ task_id: taskId });
    if (types?.length) params.set('types', types.join(','));

    const es = new EventSource(`/api/events?${params}`);
    es.onmessage = (e) => setEvents((prev) => [...prev, JSON.parse(e.data)]);
    es.onerror = () => { /* EventSource 自动重连 */ };

    return () => es.close();
  }, [taskId, types?.join(',')]);

  return events;
}
```

**断线重连**：`EventSource` 自动重连，重连时浏览器会带 `Last-Event-ID` 头（如果服务端发了 `id:` 字段）。V2 服务端**不发** `id:` 字段（事件 ID 是 SQLite 自增，前端用不上），重连后从 `since=<last_timestamp>` 拉历史回放。

---

## 8. V1 契约对应

V1 没有事件总线（前端轮询 `/api/tasks/<id>/stream`）。V2 的 SSE 是新增能力，但语义上对应 V1 的：

- V1 stdout 中间打印的进度行 → V2 `TASK_LOG` 事件
- V1 stdout 末行 JSON → V2 `TASK_FINISHED` / `TASK_FAILED` 事件
- V1 清单写盘 → V2 `MANIFEST_WRITTEN` 事件

---

## 9. V3 重写时的契约

V3 即使换语言、换并发模型，只要：

1. `EventType` 枚举值不变
2. 各 Payload 模型字段不变
3. SSE 端点路径与响应格式不变
4. SQLite `task_events` 表 schema 不变

→ 前端零改动，历史事件可回放。
