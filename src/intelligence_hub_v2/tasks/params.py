"""六个 V2.0 任务的参数模型（`TaskDefinition.params_schema`）。

单独一个叶子模块：注册表要拿这些类填 `params_schema`，handler 又要在函数签名里
用它们 —— 如果每个 handler 各自定义自己的参数类，注册表就得反向 import 五个 handler
模块。收成一处后依赖是 `registry → params`、`handlers → params`，两边都不互相指。

`/api/tasks/{name}/schema` 直接把这里的 `model_json_schema()` 吐给前端渲染表单，
所以字段名与 `Field(description=...)` 是 API 契约的一部分（`AGENTS.md §6` 自检清单）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PreflightParams(BaseModel):
    """预检不需要参数。留一个空模型是为了 `params_schema` 这一栏统一都是 `type[BaseModel]`。"""

    model_config = ConfigDict(extra="ignore")


class AddCreatorParams(BaseModel):
    """收录一位博主（`add_creator`）。"""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="博主主页链接或分享短链（抖音会跟一次 302 拿 sec_uid）")
    platform: str | None = Field(
        default=None,
        description="留空则按链接域名自动判平台；填了则以填的为准（如裸 sec_uid 无从判域名时）",
    )
    tracking: bool = Field(default=True, description="是否加入持续跟踪（日更采集只收 tracking 的）")


class CollectParams(BaseModel):
    """单平台采集（`douyin_collect` / `bilibili_collect`）。"""

    model_config = ConfigDict(extra="ignore")

    creator_ids: list[int] = Field(
        default_factory=list,
        description="指定要采的博主主键；留空 = 该平台所有 is_tracking 的博主",
    )
    since: datetime | None = Field(
        default=None,
        description="发布时间早于此的跳过。**弱过滤**：抖音网格没有发布时间，判不了就放行；"
        "真正的增量靠 videos 表按 platform_video_id 查重",
    )
    limit: int | None = Field(
        default=None, ge=1, description="每位博主最多收几条；留空 = 平台配置的 videos_per_creator"
    )


class SingleLinkParams(BaseModel):
    """收一条作品（`single_link`）：解析链接 → 判平台 → 走对应适配器。"""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="作品链接（分享短链也可以）")


class PostprocessParams(BaseModel):
    """后处理（`postprocess`）：字幕优先，没有字幕轨的平台走本地 ASR（T1.2）。"""

    model_config = ConfigDict(extra="ignore")

    video_ids: list[int] = Field(
        default_factory=list, description="指定作品主键；留空 = 有媒体但没口播稿的作品"
    )
    platform: str | None = Field(default=None, description="只处理某平台；留空 = 全平台")
    limit: int = Field(default=50, ge=1, le=500, description="本轮最多处理多少条")


__all__ = [
    "AddCreatorParams",
    "CollectParams",
    "PostprocessParams",
    "PreflightParams",
    "SingleLinkParams",
]
