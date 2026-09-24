"""作品的"厚度"数据：评论与多检查点指标快照。

契约来源：`docs/specs/data-model.md §2.11 / §2.12`，表定义在 `storage/schema.py`。

这两样是 V1 有、V2.0 没排期的东西（`docs/plans/v2.1-migration-plan.md` 的 T4.1 / T4.2）。
放在同一个模块里是因为它们共享一条判断：**它们都是"某一条作品在某个时刻的读数"**，
不是独立实体 —— 所以两张表都 `ON DELETE CASCADE`，都不进 Feed 的检索面，
也都允许"平台今天没有这一项"。

**为什么指标四项可空而不是填 0**（本模块最容易写错的一处）：
"没有"与"是零"在增长率上是两个答案。小红书公开主页的卡片上没有 `view_count`，
写成 0 就得到一条"发布时 0 播放"的快照，下一轮算 `7d / publish` 时要除以它 ——
得到一个要么无穷大要么 0 的"增长率"，而它在看板上长得像一条正常的坏数据。
NULL 的好处是**它不参与计算**：任何派生指标都必须先跳过 NULL，而不是先把它当 0。
看护见 `tests/unit/test_engagement_models.py`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

MetricCheckpoint = Literal["publish", "24h", "72h", "7d", "manual"]
"""指标检查点。

`publish` 是"刚收到这条作品时"那一档；`24h` / `72h` / `7d` 是相对发布时刻的窗口；
`manual` 给存量视频补抓 —— 一条三年前的作品永远等不到它的 `24h` 窗口，
把那种读数塞进 `7d` 会让"发布 7 天破千"这类判断被污染。

与 `storage.schema.METRIC_CHECKPOINTS` 是同一份清单的两处定义（Python 层与 DB 层 CHECK），
与 `models/media.py` 的 `MediaSource` 同一个形状；**两边相等由
`tests/unit/storage/test_schema_types.py` 看护**，改一边不改另一边就会红。
"""

METRIC_CHECKPOINT_VALUES: tuple[MetricCheckpoint, ...] = get_args(MetricCheckpoint)

_READINGS: tuple[str, ...] = ("view_count", "like_count", "comment_count", "share_count")
"""四项读数的字段名，**草稿与行共用这一份清单**。

写成两份（一边四项、另一边四项）的失败方式是：加第五项时只加了一边，
于是 `is_empty()` 与 `has_any_reading` 对"这条快照有没有内容"给出相反的答案，
而这两个答案分别决定"要不要落库"与"要不要补抓"—— 一个静默的死循环。
"""


class VideoComment(BaseModel):
    """`video_comments` 的 DB 行。"""

    model_config = ConfigDict(frozen=True)

    id: int
    video_id: int
    platform: str
    platform_comment_id: str
    parent_platform_comment_id: str | None = None
    author_platform_id: str | None = None
    author_name: str = ""
    content: str
    like_count: int | None = None
    reply_count: int | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    metadata_json: str = "{}"


class VideoCommentDraft(BaseModel):
    """写入 DB 前的评论草稿。没有 id / `fetched_at`（那两个由库与 `_now` 给）。"""

    platform: str
    platform_comment_id: str
    content: str
    parent_platform_comment_id: str | None = None
    author_platform_id: str | None = None
    author_name: str = ""
    like_count: int | None = None
    reply_count: int | None = None
    published_at: datetime | None = None
    metadata_json: str = "{}"

    def model_post_init(self, __context: object) -> None:
        """**空 id 直接拒**。

        唯一键是 `(video_id, platform, platform_comment_id)`，空串在里面是一个**合法值**
        —— 于是第一条"平台没给 id"的评论会占住那个键，之后所有没 id 的评论都撞上它，
        表现为"这一轮只抓到 1 条评论"而一条错误都没有。宁可在这里红。
        """
        if not self.platform_comment_id.strip():
            msg = (
                "platform_comment_id 为空不许入库：唯一键会把它当成一个合法值，"
                "之后所有没有 id 的评论都撞在同一条上"
            )
            raise ValueError(msg)


class MetricSnapshot(BaseModel):
    """`video_metric_snapshots` 的 DB 行。"""

    model_config = ConfigDict(frozen=True)

    id: int
    video_id: int
    checkpoint: MetricCheckpoint
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    collected_at: datetime
    metadata_json: str = "{}"

    @property
    def has_any_reading(self) -> bool:
        """这一条快照里至少有一项不是 NULL。

        四项全 NULL 的快照没有信息量，但它**是**"我们确实在这个检查点看过这条作品"的凭据。
        要不要落这么一条是 `tasks/enrich_metrics.py` 的决定（它选择不落，理由写在那儿），
        而读的一侧要能一句话问出这件事，所以判据放在模型上。
        """
        return any(getattr(self, name) is not None for name in _READINGS)


class MetricSnapshotDraft(BaseModel):
    """写入 DB 前的指标快照草稿。

    `ge=0` 是**给了值之后**的下界：负播放量一定是解析错了（`compact_number_to_int`
    把"1.2万"读成 -12000 之类的），而这种值一旦入库，增长率会算出一个符号相反的结果。
    不给值（None）是合法的，见模块 docstring。
    """

    checkpoint: MetricCheckpoint
    view_count: int | None = Field(default=None, ge=0)
    like_count: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    share_count: int | None = Field(default=None, ge=0)
    metadata_json: str = "{}"

    def is_empty(self) -> bool:
        """四项全 NULL。落库之前问一次，别让一条空快照冒充"这个窗口已经抓过"。"""
        return all(getattr(self, name) is None for name in _READINGS)
