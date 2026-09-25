"""任务相关数据模型。

契约来源：docs/specs/task-runner.md §2（TaskKind / TaskResult / ArtifactRef / FailureRecord）
         docs/specs/platform-adapter.md §2.4（FailureRecord / ProgressCallback）
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class TaskKind(StrEnum):
    """任务类型。"""

    PLATFORM_COLLECT = "platform_collect"
    ALL_PLATFORMS = "all_platforms"
    SINGLE_LINK = "single_link"
    ADD_CREATOR = "add_creator"
    BACKFILL = "backfill"
    ENRICH_METRICS = "enrich_metrics"
    """给已有作品补一次读数快照（T4.2 / ADR-0020）。

    为什么不复用 `BACKFILL`：那一档在 V1 指的是"按 URL 回溯历史作品"（§7.22），
    产物是**新作品行**；这一档产物是**旧作品的第 N 条读数**。合成一个的话
    "`backfill` 跑了一小时写了多少作品"这个问题就没有答案了。
    `task_runs.kind` 是无 CHECK 的 `String(32)`（见 `storage/schema.py`），
    所以加这一档不需要迁移。
    """
    POSTPROCESS = "postprocess"
    SYNC = "sync"
    PREFLIGHT = "preflight"
    MIGRATE = "migrate"


class ArtifactRef(BaseModel):
    """产物引用（媒体 / 口播稿 / metadata 路径）。"""

    kind: Literal["media", "transcript", "metadata", "cover", "manifest"]
    path: Path
    platform: str | None = None
    video_id: str | None = None
    size_bytes: int | None = None


class FailureRecord(BaseModel):
    """清单里的失败记录。

    V1 §1.3 看护：error 字段必须是原文，不许吞错。

    与 docs/specs/task-runner.md §2.4 的两处**放宽**（实施期发现，见 docs/lessons.md）：
    - `platform` 可为 None：runner 级失败（配置坏了 / 博主库读不出来 / 存储打不开）
      不归任何单一平台。V1 §7.22 那次 `RuntimeError` 一路甩成 traceback 就是这类。
    - `stage` 多了 `"task"`：不是某个 item 的流水线阶段挂了，而是任务本身挂了。
    - `stage` 多了 `"metrics"` / `"comments"`（V2.1 T4.2）：补读数与抓评论是**两个新阶段**，
      与 `list`/`download` 不同处在于它们发生在"作品早就在库里"之后。
      为什么不复用 `store`：清单按 stage 分组，"接口没给数"与"库写不进去"要做的动作不同，
      合成一格就等于把两类红混成一堆（V1 的 `stage` 就是一路混到没人看的）。
    """

    platform: str | None = None
    stage: Literal[
        "parse_url", "list", "download", "transcribe", "store", "task", "metrics", "comments"
    ]
    video_id: str | None = None
    creator_id: str | None = None
    error: str
    error_kind: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskResult(BaseModel):
    """任务执行结果。"""

    status: Literal["success", "partial", "failed"]
    summary: dict[str, int | str] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)


ProgressCallback = Callable[[float], None]
"""进度回调，0.0~1.0。"""


class TaskRunRecord(BaseModel):
    """DB 行（task_runs 表的 Pydantic 映射）。

    与 `TaskResult` 的区别：`TaskResult` 是**一次执行的产出**（handler 返回它），
    `TaskRunRecord` 是**这次执行的档案**（含 id / 时间戳 / 进度 / 清单路径）。
    前者活在一次调用里，后者要能被"任务历史"页面翻出来。

    V1 §2 契约二看护：`status` 为终态时 `ended_at` 必填，DB 层有 CHECK 约束
    `status = 'running' OR ended_at IS NOT NULL`。
    """

    id: str
    task_name: str
    kind: str
    status: Literal["running", "success", "partial", "failed", "timeout", "cancelled"]
    params_json: str = "{}"
    config_snapshot_json: str = "{}"
    started_at: datetime
    ended_at: datetime | None = None
    summary_json: str | None = None
    manifest_path: str | None = None
    error_text: str | None = None
    progress: float = 0.0

    @property
    def is_terminal(self) -> bool:
        return self.status != "running"

    @property
    def duration_seconds(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()
