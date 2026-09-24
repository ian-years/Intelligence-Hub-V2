"""草稿（`drafts` 表）的数据模型。

契约来源：`docs/specs/data-model.md §2.9` + ADR-0021。

**命名说明**（这一处绕不开，写清楚省得下一个人改名）：本仓库的约定是
`XxxDraft` = 入库前的入参、`Xxx` = DB 行（`models/__init__.py` 的 docstring）。
而这张表的实体本身就叫 draft，`DraftDraft` 是同一个词读三遍，所以这里取
`DraftRecord`（行，跟 `PlatformRecord` / `ManifestRecord` 同一形状）与
`DraftInput`（入参）。约定要能为读服务，不是为套而套。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

DraftStatus = Literal["draft", "published", "archived"]
"""草稿状态。与 `storage.schema.DRAFT_STATUSES` 是同一份清单的两处定义
（Python 层与 DB 层 CHECK），两边相等由
`tests/unit/storage/test_schema_types.py` 看护，改一边不改另一边就会红。

**为什么"改状态"是普通字段而不是一个 `publish()` 方法**：`is_tracking` /
`is_hidden` 那种要走专门方法，是因为它们要**连带**写别的字段或发事件
（V1 §7.24 / §7.25）。状态在这里没有连带动作，多开一个入口只是多一处能写它的地方。
"""

DRAFT_STATUS_VALUES: tuple[DraftStatus, ...] = get_args(DraftStatus)


def assert_non_blank_text(field: str, value: str) -> None:
    """`title` / `content` 不许只有空白字符。**这一条规则的唯一定义处。**

    为什么不是一个私有方法留在 `DraftInput` 里：路由上那个"部分更新"的请求体
    （`api/v1/drafts.py:DraftUpdate`）要判的是同一件事，而它**不能**借 `DraftInput` 来判 ——
    部分更新时手里只有两栏，凑不出一份合法的 `DraftInput`。
    各写一遍的失败方式是"改了模型那一半忘了改路由那一半"，症状是同一个输入
    有时 422 有时 200（然后列表上多出一条只有空白的草稿）。

    为什么库里的 CHECK 不算这道闸：`data-model.md §2.9` 只有 `status` 那条枚举 CHECK，
    NOT NULL 拦不住空串与空格（AGENTS §6「字段加了 CHECK 约束」是问"该不该有"，
    不是给每一列配一条）。
    """
    if not value.strip():
        msg = f"{field} 只有空白字符：NOT NULL 拦不住它，列表上就是一条空白草稿"
        raise ValueError(msg)


class DraftRecord(BaseModel):
    """`drafts` 的 DB 行。"""

    model_config = ConfigDict(frozen=True)

    id: int
    title: str
    content: str
    source_video_id: int | None = None
    status: DraftStatus
    created_at: datetime
    updated_at: datetime


class DraftInput(BaseModel):
    """写入 DB 前的草稿。

    `title` / `content` 是 `min_length=1` 而不是 `min_length=2`：库里这两列
    NOT NULL 但**不查空串**（spec §2.9 没有 CHECK），于是空标题能写进去，
    列表上一片空白而 API 回 200。空串与"还没写完"是两回事 —— 后者由 `content` 表达。
    """

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_video_id: int | None = None
    status: DraftStatus = "draft"

    @model_validator(mode="after")
    def _reject_blank_text(self) -> DraftInput:
        for field in ("title", "content"):
            assert_non_blank_text(field, getattr(self, field))
        return self


class DraftUpdatableFields(TypedDict, total=False):
    """`DraftRepository.update_fields()` 允许改的字段集（V1 §7.4 的结构性解法）。

    四个键都是"编辑一篇稿子"真的会改的东西，所以**没有**排除项 ——
    与 `TopicUpdatableFields` 排除 `name` 相反，理由是这里没有身份列
    （`id` 是主键，本来就不在这个集合里）。
    `created_at` / `id` 不可改：改了等于伪造一篇稿子的历史。
    """

    title: str
    content: str
    source_video_id: int | None
    status: DraftStatus


UPDATABLE_DRAFT_FIELDS: frozenset[str] = frozenset(DraftUpdatableFields.__annotations__)
"""运行期白名单，理由同 `UPDATABLE_VIDEO_FIELDS`。"""
