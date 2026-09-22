"""`add_creator` handler：把一个链接收成博主库里的一行。

链路：认平台 → `adapter.parse_creator_url`（抖音短链跟 302，V1 §7.1）→
`fetch_creator_profile` → 幂等写库 → 发 `creator.added`。

**profile 拉失败就整个任务红**（不在这里 catch）：拿平台 ID 当昵称入库，就是
V1 §7.24 那一类"把数据刷坏而且刷完看不出来"。宁可让用户重点一次。

已有这位博主时走 `update_fields` 刷新资料，但**占位昵称不覆盖真昵称**
（`profile.extra["nickname_is_placeholder"]` 是适配器给入库层的信号，见抖音
`fetch_creator_profile` docstring）。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.task import TaskResult
from intelligence_hub_v2.tasks.dispatch import detect_platform
from intelligence_hub_v2.tasks.params import AddCreatorParams

if TYPE_CHECKING:
    from intelligence_hub_v2.models.creator import Creator, CreatorProfile, CreatorRef
    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["run_add_creator"]


async def run_add_creator(ctx: TaskContext, params: AddCreatorParams) -> TaskResult:
    ctx.check_cancelled()
    platform = params.platform or detect_platform(params.url, ctx.adapters)
    adapter = ctx.adapters.get(platform)

    await ctx.progress(0.15, stage="parse_url", current_item=params.url[:60])
    ref = await adapter.parse_creator_url(params.url)
    await ctx.progress(0.55, stage="store")
    profile = await adapter.fetch_creator_profile(ref)

    creator, created = await _upsert(ctx, ref, profile, tracking=params.tracking)

    if created:
        await ctx.publish(
            EventType.CREATOR_ADDED,
            {
                "creator_id": creator.id,
                "platform": creator.platform,
                "platform_id": creator.platform_id,
                "name": creator.name,
            },
        )
    else:
        await ctx.publish(
            EventType.CREATOR_UPDATED,
            {"creator_id": creator.id, "changed_fields": ["name", "avatar_url", "follower_count"]},
        )

    await ctx.progress(1.0)
    summary: dict[str, int | str] = {
        "creator_id": creator.id,
        "platform": creator.platform,
        "created": int(created),
        "name": creator.name,
    }
    return TaskResult(status="success", summary=summary)


async def _upsert(
    ctx: TaskContext,
    ref: CreatorRef,
    profile: CreatorProfile,
    *,
    tracking: bool,
) -> tuple[Creator, bool]:
    """幂等入库。新建走 `insert_or_get`，已存在只 `update_fields` 能改的那几样。

    返回 `(行, 是否新建)`。注意 `insert_or_get` 的 `created` 才是"这次真的插了一条"，
    而 `tracking` 只在**新建**时决定初值 —— 把一个已存在、被用户特意关掉跟踪的博主
    重新打开，不该由"再收一次同一条链接"触发。
    """
    metadata = _metadata(profile)
    draft = CreatorDraft(
        platform=ref.platform,
        platform_id=ref.platform_id,
        name=profile.name,
        avatar_url=str(profile.avatar_url) if profile.avatar_url is not None else None,
        follower_count=profile.follower_count,
        profile_url=str(ref.profile_url),
        is_tracking=tracking,
        metadata_json=metadata,
    )
    creator, created = await ctx.storage.creators.insert_or_get(draft)
    if created:
        return creator, True

    fields: dict[str, Any] = {"profile_url": str(ref.profile_url), "metadata_json": metadata}
    if not profile.extra.get("nickname_is_placeholder"):
        fields["name"] = profile.name
    if profile.avatar_url is not None:
        fields["avatar_url"] = str(profile.avatar_url)
    if profile.follower_count is not None:
        fields["follower_count"] = profile.follower_count
    updated = await ctx.storage.creators.update_fields(creator.id, **fields)
    return updated, False


def _metadata(profile: CreatorProfile) -> str:
    """`profile.extra` 落成一列 JSON。缺依赖 / 风控标记等杂项都住在 `extra` 里。

    `default=str`：extra 可能塞进 datetime、Path 这类非原生可序列化对象，
    为这个把整次收录搞挂不值得；转不成 JSON 的东西按字符串存住，原文不丢。
    """
    return json.dumps(profile.extra, ensure_ascii=False, default=str, sort_keys=True)
