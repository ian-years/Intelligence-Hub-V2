"""`single_link` handler：收一条作品链接（V2.0 最小实现）。

思路：认平台 → 从链接里认出 `platform_video_id` → 拼一个**最小 `VideoMeta`**
（只喂 `download_media` 真正读的那几样：`platform_video_id` / `webpage_url`）→ 下载 →
入库（`creator_id=None`，因为一条作品链接不含博主身份，硬凑一个假博主就是脏数据）。

**分享短链先跟一次 302**：抖音的 `v.douyin.com/<码>` 落地页里才带 aweme_id，
认平台与认 id 都要在展开之后做（V1 §7.1 的同类跳转，只是对象从博主换成作品）。

两处"够用但欠打磨"，都写在 V2.1 待办里：
- **标题是占位的**（`<id>`）。链接里不含标题，而 `PlatformAdapter` 契约没有"按作品
  URL 拉详情"这一手（加它要走 ADR）。V2.1 的 postprocess 抓详情时会用真标题覆盖。
- id 抽取是任务层的保守正则。往适配器上加 `resolve_video` 方法会为这一个 V2.0
  最小功能撑开 Locked 契约，不值当；"从字符串认 BV / 数字 id"本就是纯字符串处理。
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.task import TaskResult
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.xiaohongshu.urls import extract_note_id
from intelligence_hub_v2.platforms.youtube.urls import extract_video_id
from intelligence_hub_v2.tasks.collect import build_video_draft
from intelligence_hub_v2.tasks.dispatch import canonical_video_url, detect_platform
from intelligence_hub_v2.tasks.params import SingleLinkParams

if TYPE_CHECKING:
    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["run_single_link"]

_BVID = re.compile(r"(BV[0-9A-Za-z]{10})")
_AWEME = re.compile(
    r"(?:video|share/video)/(?P<id>\d+)|[?&](?:modal_id|obj_id|aweme_id)=(?P<q>\d+)"
)


async def run_single_link(ctx: TaskContext, params: SingleLinkParams) -> TaskResult:
    ctx.check_cancelled()
    url = await _expand_if_short(ctx, params.url)
    platform = detect_platform(url, ctx.adapters)
    video_id = _extract_video_id(platform, url)
    canonical = canonical_video_url(platform, video_id)

    meta = VideoMeta.model_validate(
        {
            "platform": platform,
            "platform_video_id": video_id,
            "creator_ref": _synthetic_ref(platform, video_id),
            "title": video_id,  # 占位标题，V2.1 抓详情时覆盖（见模块 docstring）
            "webpage_url": canonical,
        }
    )

    # 与 collect 同一条纪律：**先查重再下载**（review P1-10）。原来是无脑下载完才
    # insert_or_get —— 同一条链接点两次 = 两次完整下载 + 第二次覆盖第一次的产物
    # （带宽与风控都是成本），summary 却报 `created=0` 仍 `success`。
    existing = await ctx.storage.videos.find_by_platform_id(platform, video_id)
    if existing is not None:
        await ctx.progress(1.0)
        return TaskResult(
            status="success",
            summary={
                "video_id": existing.id,
                "platform": platform,
                "platform_video_id": video_id,
                "media_source": existing.media_source or "",
                "created": 0,
                "skipped": 1,
            },
        )

    await ctx.progress(0.2, stage="download")
    dest = ctx.files.media_dir(platform, "single-link", video_id, video_id)
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
    artifact = await ctx.adapters.get(platform).download_media(meta, dest)

    draft = build_video_draft(meta, creator_id=None, artifact=artifact, ctx=ctx)
    row, created = await ctx.storage.videos.insert_or_get(draft)
    if created:
        await ctx.publish(
            EventType.VIDEO_ADDED,
            {
                "video_id": row.id,
                "platform": row.platform,
                "platform_video_id": row.platform_video_id,
                "title": row.title,
                "creator_id": row.creator_id,
            },
        )
    await ctx.progress(1.0)
    summary: dict[str, int | str] = {
        "video_id": row.id,
        "platform": platform,
        "platform_video_id": video_id,
        "media_source": row.media_source or "",
        "created": int(created),
    }
    return TaskResult(status="success", summary=summary)


async def _expand_if_short(ctx: TaskContext, url: str) -> str:
    """抖音 / B站 短链展开；带 BV 或数字 id 的长链接直接返回。"""
    if "b23.tv" in url or "v.douyin.com" in url:
        try:
            resp = await ctx.deps.http.get(url, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001 - 展开失败按原链接继续，认不出 id 会在下一步如实响
            ctx.logger.warning(
                "single_link.expand_failed", url=url, error=f"{type(exc).__name__}: {exc}"
            )
            return url
        return str(resp.url or url)
    return url


def _extract_video_id(platform: str, url: str) -> str:
    if platform == "bilibili":
        match = _BVID.search(url)
        if match is not None:
            return match.group(1)
    elif platform == "xiaohongshu":
        # **不在这里再抄一份 note_id 的形状**：`{18,}` 那条规则住在
        # `platforms/xiaohongshu/urls.py`，两处各写一遍迟早会漂成"任务认得出而适配器认不出"。
        found = extract_note_id(url)
        if found:
            return found
    elif platform == "douyin":
        match = _AWEME.search(url)
        if match is not None:
            found = match.group("id") or match.group("q")
            if found:
                return found
    elif platform == "youtube":
        # 同小红书那条纪律：**不在这里再抄一份 11 位的形状**。
        # `watch?v=` / `youtu.be/` / `/shorts/` / `/live/` / `/embed/` 五种落地页
        # 的判据住在 `platforms/youtube/urls.py`，两处各写一遍迟早漂成
        # "任务认得出而适配器认不出"。
        found = extract_video_id(url)
        if found:
            return found
    msg = f"从 {platform} 链接里认不出作品 id：{url}（平台改了 URL 形状？）"
    raise PlatformError(platform, "parse_url", msg)


def _synthetic_ref(platform: str, video_id: str) -> CreatorRef:
    """一条作品链接不含博主身份，给一个可识别的占位 ref 只为满足 `VideoMeta` 必填字段。"""
    return CreatorRef.model_validate(
        {
            "platform": platform,
            "platform_id": f"single-link:{video_id}",
            "profile_url": canonical_video_url(platform, video_id),
        }
    )
