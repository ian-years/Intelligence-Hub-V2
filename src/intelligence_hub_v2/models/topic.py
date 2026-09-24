"""选题（`topics` 表）的数据模型。

契约来源：`docs/specs/data-model.md §2.8`（表）+ ADR-0021（为什么这一版只有 topics、
没有 §2.8 里的关联表 `video_topics`）。

这一张表比 `creators` / `videos` 都薄：一条选题就是一个名字。薄不等于可以随手写 ——
`name` 是唯一的身份判定，而它在 UNIQUE 里**接受空串**（见 `TopicDraft` 的拒收理由）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict


class Topic(BaseModel):
    """`topics` 的 DB 行。"""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    description: str | None = None
    created_at: datetime


class TopicDraft(BaseModel):
    """写入 DB 前的选题草稿。没有 id / `created_at`（那两个由库与 `_now` 给）。"""

    name: str
    description: str | None = None

    def model_post_init(self, __context: object) -> None:
        """**空白名直接拒**，与 `VideoCommentDraft` 对 `platform_comment_id` 的做法同形。

        `name` 是唯一键，而空串在 SQLite 的 UNIQUE 里是一个**合法值**：
        收下它的后果不是"这一条存歪了"，是"之后所有空白选题全部撞上同一条"，
        症状是"第二张空选题报 409"而第一条看起来存成功了。
        库里那一半没有 CHECK（spec §2.8 没写，也不该由实现偷偷加），所以这一道是**唯一**一道。

        存的是 strip 过的名字：`"  AI 选题"` 与 `"AI 选题"` 在 UNIQUE 里是两条，
        而用户在列表里看得出它们是同一个选题 —— 手工判重比拒收更贵。
        """
        if not self.name.strip():
            msg = "选题名不能为空：空串在唯一键里是合法值，会占住它并让后续空名全部撞车"
            raise ValueError(msg)
        # model_config 默认可变（未 frozen），这里允许改写；post_init 里改字段是
        # 本仓库既有的写法（`VideoCommentDraft` 只读不写，因为它没有需要归一化的字段）。
        object.__setattr__(self, "name", self.name.strip())


class TopicUpdatableFields(TypedDict, total=False):
    """`TopicRepository.update_fields()` 允许改的字段集（V1 §7.4 同款纪律）。

    **`name` 故意不在里面** —— 它是这张表唯一的身份判定，改名字等于换一条选题，
    而"改名撞了已有的另一条"和"新建撞了"是两种不同的用户预期（前者要 409 +
    "已经有这条选题了，要不要合并"，那是另一个格）。今天没有合并语义，
    所以只留 `description` 一个可改字段；要放开 `name` 时**连带**那件事一起决定。
    """

    description: str | None


UPDATABLE_TOPIC_FIELDS: frozenset[str] = frozenset(TopicUpdatableFields.__annotations__)
"""运行期白名单：`**dict` 展开与来自请求体的字段名 mypy 拦不住，见 `videos.py` 同名常量。"""


def topic_payload(topic: Topic) -> dict[str, Any]:
    """给清单/事件流用的窄字典。目前只有一个调用方（`api/v1/topics.py` 的删除事件）。"""
    return topic.model_dump(mode="json")
