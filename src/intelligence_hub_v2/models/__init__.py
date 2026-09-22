"""共享 Pydantic 数据模型。

所有跨层传递的数据类型在这里定义。models/ 是纯数据，无业务逻辑，
可被任何层引用（api / core / platforms / storage / infra / tasks）。

契约来源：docs/specs/platform-adapter.md §2.4 + docs/specs/event-schema.md §3
"""

from intelligence_hub_v2.models.creator import Creator, CreatorDraft, CreatorProfile, CreatorRef
from intelligence_hub_v2.models.event import Event, EventType
from intelligence_hub_v2.models.manifest import Manifest, ManifestBuilder
from intelligence_hub_v2.models.task import (
    ArtifactRef,
    FailureRecord,
    ProgressCallback,
    TaskKind,
    TaskResult,
)
from intelligence_hub_v2.models.transcript import Transcript, TranscriptDraft, TranscriptSegment
from intelligence_hub_v2.models.video import (
    Page,
    PagedResult,
    Video,
    VideoDraft,
    VideoFilters,
    VideoMeta,
)

__all__ = [
    "ArtifactRef",
    "Creator",
    "CreatorDraft",
    "CreatorProfile",
    "CreatorRef",
    "Event",
    "EventType",
    "FailureRecord",
    "Manifest",
    "ManifestBuilder",
    "Page",
    "PagedResult",
    "ProgressCallback",
    "TaskKind",
    "TaskResult",
    "Transcript",
    "TranscriptDraft",
    "TranscriptSegment",
    "Video",
    "VideoDraft",
    "VideoFilters",
    "VideoMeta",
]
