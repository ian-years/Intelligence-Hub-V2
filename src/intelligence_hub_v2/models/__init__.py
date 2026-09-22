"""共享 Pydantic 数据模型。

所有跨层传递的数据类型在这里定义。models/ 是纯数据，无业务逻辑，
可被任何层引用（api / core / platforms / storage / infra / tasks）。

契约来源：docs/specs/platform-adapter.md §2.4 + docs/specs/event-schema.md §3
         + docs/specs/data-model.md §2-§3

**命名约定**：
- `XxxDraft` = 写入 DB 前的入参（没有 id / 时间戳）
- `Xxx` 或 `XxxRecord` = DB 行的映射（有 id / 时间戳）
- `XxxMeta` / `XxxProfile` / `XxxRef` = 采集层的产出（适配器返回值）
- `XxxUpdatableFields` = `update_fields()` 的白名单 TypedDict（V1 §7.4）
"""

from intelligence_hub_v2.models.creator import (
    UPDATABLE_CREATOR_FIELDS,
    Creator,
    CreatorDraft,
    CreatorProfile,
    CreatorRef,
    CreatorUpdatableFields,
)
from intelligence_hub_v2.models.event import Event, EventType, StoredEvent
from intelligence_hub_v2.models.manifest import Manifest, ManifestBuilder, ManifestRecord
from intelligence_hub_v2.models.platform import HEALTH_STATUSES, PlatformRecord
from intelligence_hub_v2.models.task import (
    ArtifactRef,
    FailureRecord,
    ProgressCallback,
    TaskKind,
    TaskResult,
    TaskRunRecord,
)
from intelligence_hub_v2.models.transcript import (
    Transcript,
    TranscriptDraft,
    TranscriptRecord,
    TranscriptSegment,
)
from intelligence_hub_v2.models.video import (
    UPDATABLE_VIDEO_FIELDS,
    Page,
    PagedResult,
    Video,
    VideoDraft,
    VideoFilters,
    VideoMeta,
    VideoUpdatableFields,
)

__all__ = [
    "HEALTH_STATUSES",
    "UPDATABLE_CREATOR_FIELDS",
    "UPDATABLE_VIDEO_FIELDS",
    "ArtifactRef",
    "Creator",
    "CreatorDraft",
    "CreatorProfile",
    "CreatorRef",
    "CreatorUpdatableFields",
    "Event",
    "EventType",
    "FailureRecord",
    "Manifest",
    "ManifestBuilder",
    "ManifestRecord",
    "Page",
    "PagedResult",
    "PlatformRecord",
    "ProgressCallback",
    "StoredEvent",
    "TaskKind",
    "TaskResult",
    "TaskRunRecord",
    "Transcript",
    "TranscriptDraft",
    "TranscriptRecord",
    "TranscriptSegment",
    "Video",
    "VideoDraft",
    "VideoFilters",
    "VideoMeta",
    "VideoUpdatableFields",
]
