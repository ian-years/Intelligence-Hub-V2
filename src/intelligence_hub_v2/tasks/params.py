"""任务参数模型（`TaskDefinition.params_schema`）—— 一个任务一份，形状不同就不合并。

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


class BackfillParams(BaseModel):
    """爆款回溯（`backfill`）：**点名一位博主**，扫她的近况、按读数挑前几条收进来。

    参数是 `creator_url` 而不是 `creator_id`（V1 §7.22 事故被契约化成了这个形状，
    见 `docs/specs/task-runner.md §457`）：链接里带着平台身份，适配器用
    `parse_creator_url` 直接定位到那一位，不需要"先取一份博主名单"——而"取名单"这一步
    在 V1 就是按跟踪开关筛全库，开关关着时拿到 0 条。

    **`scan` 与 `top` 是两件事，故意不给同一个默认值**：`scan` 是"往回看多少条"
    （爆款可能藏在第 20 条的位置上），`top` 是"这次收几条进来"。两者合并成一栏的话，
    清单里就分不出"只看了最新 5 条"与"看了 50 条挑了 5 条"——那正是这个功能有没有干活的区别。
    """

    model_config = ConfigDict(extra="ignore")

    creator_url: str = Field(
        description="博主主页链接或分享短链（抖音跟一次 302 拿 sec_uid）。"
        "**库里没有这位博主时会如实失败**并让你先跑「收录博主」，不会退化成扫全库"
    )
    platform: str | None = Field(
        default=None,
        description="留空则按链接域名判平台；填了则以填的为准（裸 sec_uid 那种无从判域名的链接）",
    )
    top: int = Field(default=5, ge=1, le=50, description="这次收进来几条代表作（按点赞数排）")
    scan: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="往回扫多少条再挑；留空 = 平台 `videos_per_creator` 的 4 倍（下限 20）",
    )


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
    metrics_only: bool = Field(
        default=False,
        description="只登记读数、不下媒体（V1 `--collection-strategy 仅采集数据`）。"
        "真的是跳过 `download_media`：库里那一行 `media_path` 留 NULL，"
        "转写那一步会因为没音频而自然跳过，不是假装成功",
    )


class EnrichMetricsParams(BaseModel):
    """补读数（`enrich_metrics`）。挑活方式与 `collect` 不同，所以参数也不同一份。"""

    model_config = ConfigDict(extra="ignore")

    video_ids: list[int] = Field(
        default_factory=list,
        description="指定要补的作品主键；留空 = 库里**一条快照都没有**的作品",
    )
    platform: str | None = Field(default=None, description="只补某平台；留空 = 全平台")
    limit: int = Field(default=50, ge=1, le=500, description="本轮最多补几条（挑活时用）")
    comments_limit: int = Field(
        default=0,
        ge=0,
        le=200,
        description="顺手抓多少条顶层评论；**默认 0=不抓**。评论只有声明了 "
        "`supports_comments` 的平台能问，且它比读数贵（一次翻页可能好几个请求）",
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
    "BackfillParams",
    "CollectParams",
    "EnrichMetricsParams",
    "PostprocessParams",
    "PreflightParams",
    "SingleLinkParams",
]
