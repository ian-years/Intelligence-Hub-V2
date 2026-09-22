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


class Manifest(BaseModel):
    """任务清单（终态审计）。"""

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
        self._status = "partial"
        self._summary = summary

    def fail(self, exc: Exception) -> None:
        self._status = "failed"
        self._error = str(exc)

    def timeout(self) -> None:
        self._status = "timeout"

    def cancel(self) -> None:
        self._status = "cancelled"

    def add_failure(self, record: FailureRecord) -> None:
        self._failures.append(record)

    def add_artifact(self, ref: ArtifactRef) -> None:
        self._artifacts.append(ref)

    def set_platforms(self, platforms: list[str]) -> None:
        self._platforms = platforms

    def set_summary(self, summary: dict[str, int | str]) -> None:
        self._summary = summary

    def finalize(self) -> Manifest:
        """强制写终态。ended_at 必填。

        如果没调过 succeed/partial/fail/timeout/cancel，自动记 failed。
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
        )
