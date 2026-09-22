"""清单相关数据模型。

契约来源：docs/specs/task-runner.md §2.6（Manifest / ManifestBuilder）

V1 §2 契约二看护：ManifestBuilder.finalize() 强制写终态，
配合 manifest_writer ctx manager，写不出半截清单。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from intelligence_hub_v2.models.task import ArtifactRef, FailureRecord, TaskKind

StageLiteral = Literal["parse_url", "list", "download", "transcribe", "store", "task"]
_KNOWN_STAGES: frozenset[str] = frozenset(
    {"parse_url", "list", "download", "transcribe", "store", "task"}
)


def _coerce_stage(stage: str) -> StageLiteral:
    """把 `PlatformError.stage` 收窄成清单认得的取值。

    适配器可以抛任意 stage 字符串（比如 V1 §7.22 那次的 `creators`），
    但清单的 `stage` 是固定枚举 —— 认不出来就归到 `"task"`，
    **并且原文留在 `error` 里**，不因为收窄而丢信息。
    """
    return stage if stage in _KNOWN_STAGES else "task"  # type: ignore[return-value]


class Manifest(BaseModel):
    """任务清单（终态审计）。

    比 docs/specs/task-runner.md §2.6 多一个 `error` 字段（实施期补，见 docs/lessons.md）：
    任务级失败（不是某个 item 失败）的原文必须有地方落。
    少了它，`ManifestBuilder.fail(exc)` 收下的异常文本会在 `finalize()` 里被丢掉 ——
    而"把错误原文丢掉"正是 V1 §1.3 那条硬约束要防的事。
    `failures[]` 装的是**逐条 item** 的失败，`error` 装的是**整个任务**为什么没成。
    """

    schema_version: Literal["2.0"] = "2.0"
    task_name: str
    task_id: str
    kind: TaskKind
    status: Literal["success", "partial", "failed", "timeout", "cancelled"]
    started_at: datetime
    ended_at: datetime
    summary: dict[str, int | str] = Field(default_factory=dict)
    platforms: list[str] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)

    error: str | None = None
    """任务级失败的原文（含超时秒数 / 取消原因）。成功时为 None。

    **不许吞错**：这里存的必须是异常/原因的原始文本，不是"失败了"这种概括。
    """


class ManifestBuilder:
    """构建 Manifest，强制终态。配合 manifest_writer ctx manager 使用。

    V1 §2 契约二的结构性保证：任何分支退出（成功/异常/取消/超时）
    都走 finalize()，ended_at 必填。
    """

    def __init__(
        self,
        task_name: str,
        task_id: str,
        kind: TaskKind,
        config_snapshot: dict[str, Any],
    ) -> None:
        self._task_name = task_name
        self._task_id = task_id
        self._kind = kind
        self._config_snapshot = config_snapshot
        self._started_at = datetime.now(UTC)
        self._status: Literal["success", "partial", "failed", "timeout", "cancelled"] | None = None
        self._summary: dict[str, int | str] = {}
        self._platforms: list[str] = []
        self._failures: list[FailureRecord] = []
        self._artifacts: list[ArtifactRef] = []
        self._error: str | None = None

    def succeed(self, summary: dict[str, int | str] | None = None) -> None:
        self._status = "success"
        if summary:
            self._summary = summary

    def partial(self, summary: dict[str, int | str]) -> None:
        """部分完成：主体成功但有 item 失败。失败明细必须在 `failures[]` 里。"""
        self._status = "partial"
        self._summary = summary

    def fail(self, exc: Exception) -> None:
        """任务级失败。**异常原文进 `Manifest.error`，不许概括、不许丢。**

        如果 exc 是 `PlatformError`，顺手补一条 `failures[]`，
        这样前端不用去解析 error 字符串就能拿到 platform / stage。
        """
        self._status = "failed"
        self._error = str(exc) or f"{type(exc).__name__}（无消息文本）"
        platform = getattr(exc, "platform", None)
        stage = getattr(exc, "stage", None)
        if isinstance(platform, str) and isinstance(stage, str):
            self._failures.append(
                FailureRecord(
                    platform=platform,
                    stage=_coerce_stage(stage),
                    error=self._error,
                    error_kind=type(exc).__name__,
                )
            )

    def timeout(self, after_seconds: float | None = None) -> None:
        """超时。记下超时秒数 —— "超时了"不说多久等于没说。"""
        self._status = "timeout"
        if after_seconds is None:
            self._error = "task timed out"
        else:
            self._error = f"task timed out after {after_seconds:.1f}s"

    def cancel(self, reason: str | None = None) -> None:
        """用户取消。reason 可空（前端点取消按钮时通常不给理由）。"""
        self._status = "cancelled"
        self._error = reason

    def add_failure(self, record: FailureRecord) -> None:
        self._failures.append(record)

    def add_artifact(self, ref: ArtifactRef) -> None:
        self._artifacts.append(ref)

    def set_platforms(self, platforms: list[str]) -> None:
        self._platforms = list(platforms)

    def set_summary(self, summary: dict[str, int | str]) -> None:
        self._summary = dict(summary)

    def finalize(self) -> Manifest:
        """强制写终态。`ended_at` 必填。

        没调过 succeed/partial/fail/timeout/cancel 就自动记 `failed` ——
        这是 V1 §2 契约二「清单必须写终态」的结构性保证：
        停在"没有 status"的初稿会被下游的计数兜底猜成绿灯（全 0 = 成功）。
        """
        if self._status is None:
            self._status = "failed"
            self._error = self._error or "manifest finalized without explicit status"
        return Manifest(
            task_name=self._task_name,
            task_id=self._task_id,
            kind=self._kind,
            status=self._status,
            started_at=self._started_at,
            ended_at=datetime.now(UTC),
            summary=self._summary,
            platforms=self._platforms,
            failures=self._failures,
            artifacts=self._artifacts,
            config_snapshot=self._config_snapshot,
            error=self._error,
        )
