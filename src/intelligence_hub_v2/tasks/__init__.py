"""任务层公共出口。

契约类型（`TaskDefinition` / `TaskContext` / `CancelToken`）在 `definition.py`，
每个 handler 各住一个模块（V2.0 那六个 + V2.1 的 `enrich_metrics` 与 `backfill`）。
`TASKS` 注册表与 `TaskRunner` / `TaskScheduler`
在 `core/`（`AGENTS.md §3` 的分工：数据与契约在 `models`/`tasks`，运行机器在 `core`）。
"""

from __future__ import annotations

from intelligence_hub_v2.tasks.add_creator import run_add_creator
from intelligence_hub_v2.tasks.backfill import run_backfill
from intelligence_hub_v2.tasks.collect import make_collect_handler
from intelligence_hub_v2.tasks.definition import CancelToken, TaskContext, TaskDefinition
from intelligence_hub_v2.tasks.dispatch import canonical_video_url, detect_platform
from intelligence_hub_v2.tasks.enrich_metrics import run_enrich_metrics
from intelligence_hub_v2.tasks.params import (
    AddCreatorParams,
    BackfillParams,
    CollectParams,
    EnrichMetricsParams,
    PostprocessParams,
    PreflightParams,
    SingleLinkParams,
)
from intelligence_hub_v2.tasks.postprocess import run_postprocess
from intelligence_hub_v2.tasks.preflight import run_preflight
from intelligence_hub_v2.tasks.single_link import run_single_link

__all__ = [
    "AddCreatorParams",
    "BackfillParams",
    "CancelToken",
    "CollectParams",
    "EnrichMetricsParams",
    "PostprocessParams",
    "PreflightParams",
    "SingleLinkParams",
    "TaskContext",
    "TaskDefinition",
    "canonical_video_url",
    "detect_platform",
    "make_collect_handler",
    "run_add_creator",
    "run_backfill",
    "run_enrich_metrics",
    "run_postprocess",
    "run_preflight",
    "run_single_link",
]
