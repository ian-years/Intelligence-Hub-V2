"""`POST /api/generate-draft-script`：按模板拼一份二创口播脚本（T5.3）。

契约来源：`docs/plans/v2.1-migration-plan.md` T5.3。V1 的对应物是
`launcher_server.py` 的 `POST /api/generate_draft_script`，body 是
`{id | record_id, topic | title, template_key | template, mode, custom_instructions}`。

入参形状照 V1，但**双键名各留一个**（`id`/`record_id` → `video_id`，
`topic`/`title` → `topic`，`template`/`template_key` → `template_key`）：
V1 那三对"两个名字都试一下"正是 `AGENTS.md` §5 里 §7.11 的形状，V2 没有历史客户端要迁。

**这个端点不进口播稿。** 不是偷懒 —— `core/analysis/draft_engine.py` 的 docstring 里写清了：
V1 的引擎收下 `transcript` 却一个字都没读过，标题（`{topic}` / `{pain}` / `{solution}`
三个填空值的唯一来源）才是它对本地库的全部依赖。所以这里只取 `videos.title` 一个字段，
比 V1 少读一份稿子，而产出与 V1 相同。把"读了稿子"这件事演出来，就是 §1.3 禁的那种绿。

与 V1 的三条行为差异，都是"不假装"：

1. `custom_instructions` **非空即 422**。V1 收下它、传进引擎、然后从来不用 ——
   用户会以为自己的指示生效了。要么将来真接，要么现在如实拒绝。
2. `topic` 与 `video_id` **至少给一个**，否则 422。V1 在两者都空时把主题写成
   `"AI短视频二创"` —— 那是凭空发明一个主题，正是这类引擎最不该做的事。
3. 未知 `template_key` → 422（Pydantic `Literal`）。V1 的 `dict.get(默认)` 会把
   "我要工具实战演示"变成"给你教程型收藏闭环"，而响应里看不出来。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.core.analysis.draft_engine import (
    DraftMode,
    DraftScript,
    TemplateKey,
    generate_draft_script,
    template_keys,
)

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["analysis"])

__all__ = ["DraftScriptRequest", "router"]


class DraftScriptRequest(BaseModel):
    """`POST /api/generate-draft-script` 的 body。"""

    topic: str = Field(
        default="",
        max_length=200,
        description="要写的主题。空则取 `video_id` 那条作品的标题。",
    )
    video_id: int | None = Field(
        default=None,
        ge=1,
        description="只为拿标题而存在；这个端点不读那条作品的口播稿（见模块 docstring）。",
    )
    template_key: TemplateKey = Field(
        default="tutorial_save_loop",
        description=f"四张模板之一：{'、'.join(template_keys())}。",
    )
    mode: DraftMode = Field(
        default="SHORT",
        description="回执字段，不改变分镜数量；要更短的本子请换 `short_fast`。",
    )
    custom_instructions: str | None = Field(
        default=None,
        description="V1 有这个字段但从未生效。今天非空即 422，不装。",
    )


@router.post("/generate-draft-script", response_model=DraftScript)
async def create_draft_script(
    body: DraftScriptRequest,
    state: AppState = Depends(get_state),
) -> DraftScript:
    """按模板拼一份脚本。**纯本地拼装**：不联网、不调模型、不写库、不写文件。"""
    if (body.custom_instructions or "").strip():
        msg = (
            "custom_instructions 目前没有实现（V1 收下后从未使用，V2 不假装它生效）。"
            "要改内容请换 template_key，或直接编辑返回的 script_markdown。"
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=msg)

    topic = body.topic.strip()
    source_title = ""
    if body.video_id is not None:
        # 隐藏的作品照样能当来源：墓碑只管列表（§7.25），与拆解端点同一判据。
        source_title = (await state.storage.videos.get_or_raise(body.video_id)).title
    if not topic and not source_title.strip():
        msg = "topic 与 video_id 至少给一个 —— 不凭空发明主题"
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=msg)

    return generate_draft_script(
        topic=topic,
        template_key=body.template_key,
        mode=body.mode,
        source_title=source_title,
    )
