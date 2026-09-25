"""`enrich_metrics`：给已有作品补一次读数（快照），顺手把评论抓进另一张表（T4.2 / ADR-0020）。

为什么单独一个任务，不塞进 `collect`：采集的产物是"新作品 + 媒体"，
而"这一条现在多少赞"是**同一批旧作品**的第 N 次读数 —— 两者的挑活方式不同
（前者看"库里有没有这条"，后者看"这条缺不缺某个窗口的快照"），
超时与限速也不同（补读数一条一个 GET，可以跑很久）。

两条窗口口径写在这里而不是散在各适配器里：

1. **窗口由这一趟跑的时刻算**（`window_for`）：适配器只交读数，不交窗口。
   同一篇稿子早上跑与晚上跑会落到不同窗口，那是事实（采集时刻决定的），
   让适配器猜就等于把同一个判断放两处。
2. **`published_at` 为 NULL 时算 `manual`，不算 `publish`**：不知道发布时间
   就声称"这是发布那一刻的数"是编的。这类"NULL 不当 0、也不当最近"的口径
   与 ADR-0020 对快照读数的判据同源。

一份 V1 遗留的**同名词表**要注意：`core/identity.py` 里那组
`CHECKPOINT_INITIAL="初始"` / `CHECKPOINT_T3="T+3"` / `CHECKPOINT_DAYS` 是 T4.3
照搬 V1 的词汇，与本仓库契约里那组（`models/engagement.MetricCheckpoint`：
`publish`/`24h`/`72h`/`7d`/`manual`，DB 的 CHECK 认的是它）**不是同一套**。
这里用后者；前者今天没有任何调用方，谁去接 T4.3 那条链要先把它对齐 ——
`identity.checkpoint_target()` 传中文档名会抛，不会静默给一个错时刻。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from intelligence_hub_v2.errors import IntelligenceHubError
from intelligence_hub_v2.models.engagement import (
    MetricCheckpoint,
    MetricSnapshotDraft,
)
from intelligence_hub_v2.models.task import FailureRecord, TaskResult
from intelligence_hub_v2.models.video import Video, VideoMeta
from intelligence_hub_v2.tasks.params import EnrichMetricsParams

if TYPE_CHECKING:
    from intelligence_hub_v2.platforms.base import PlatformAdapter
    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["run_enrich_metrics", "window_for"]

#: 已发布多久算落在哪个窗口：`(窗口, 该窗口开始于发布后多少小时)`，**必须按小时升序**。
#: 写成表而不是三个 if：加一档（比如 `14d`）时只改一处，且顺序由这张表自己保证。
_WINDOWS: tuple[tuple[MetricCheckpoint, int], ...] = (
    ("24h", 24),
    ("72h", 72),
    ("7d", 168),
)

_LATEST_WINDOW: MetricCheckpoint = "7d"
"""超过所有窗口的老作品落在最后一档：那条读数**是**"7 天以后还在长"，
把它叫 `manual` 会把"补抓一条老作品"与"人为指定重抓"混成一件事。"""


def window_for(published_at: datetime | None, taken_at: datetime) -> MetricCheckpoint:
    """这次读数属于哪个窗口。

    - 还没到第一个窗口（<24h）→ `publish`：第一次抓到就算，V1 同口径。
    - 过了几个窗口的起点 → 取**最靠后那个已经开始的**：一篇发了 3 天的稿子，
      今天这次抓的数就是 `72h` 那一格的内容（不是 `24h`，那格早过了）。
    - `published_at` 为 NULL → `manual`，见模块 docstring 第 2 条。
    """
    if published_at is None:
        return "manual"
    age_hours = (taken_at - published_at).total_seconds() / 3600
    if age_hours < 0:
        # 平台给的发布时间在将来（时区没换算对的典型症状）：不能算 publish，
        # 因为"刚发布"与"时钟不对"会是同一个标签 —— 标 manual 让人去查那条时间。
        return "manual"
    chosen: MetricCheckpoint = "publish"
    for checkpoint, start_hours in _WINDOWS:
        if age_hours >= start_hours:
            chosen = checkpoint
        else:
            break
    if age_hours >= _WINDOWS[-1][1] * 2:
        # 远超最后一档（>14 天）仍记 `_LATEST_WINDOW`：这张表回答的是
        # "发布后第 N 天长什么样"，老稿子的补抓读数就该归到最后一格。
        return _LATEST_WINDOW
    return chosen


async def run_enrich_metrics(
    ctx: TaskContext, params: EnrichMetricsParams
) -> TaskResult:  # pragma: no branch - 由 core/task_registry 的 runner 调
    """一趟补读数：挑活 → 逐条问适配器 → 落快照（可选再抓评论）→ 汇总。"""
    ctx.check_cancelled()
    videos = await _resolve_targets(ctx, params)
    now = datetime.now(UTC)
    tally = _EnrichTally()
    total = max(1, len(videos))

    for index, video in enumerate(videos):
        ctx.check_cancelled()
        await _enrich_one(ctx, tally, video=video, now=now, params=params)
        await ctx.progress((index + 1) / total, stage="enrich", current_item=video.title[:40])

    return tally.to_result(params.platform)


async def _resolve_targets(ctx: TaskContext, params: EnrichMetricsParams) -> list[Video]:
    """给了 id 就按位补；没给就挑"一条快照都没有"的那些（`video_ids_missing` 的判据）。

    刻意**不**按"缺某个固定窗口"挑活：一条三年前的稿子永远等不到它的 `24h`，
    按窗口挑会让它每轮被重挑、每轮什么都没抓到（`metrics.video_ids_missing` 的 docstring
    记的就是这件事，V1 踩过）。真要按窗口补抓，那是调用方显式给 id 的场景。
    """
    if params.video_ids:
        return [await ctx.storage.videos.get_or_raise(vid) for vid in params.video_ids]
    missing = await ctx.storage.metrics.video_ids_missing(
        platform=params.platform, limit=params.limit
    )
    resolved: list[Video] = []
    for vid in missing:
        video = await ctx.storage.videos.get(vid)
        if video is not None:
            resolved.append(video)
    return resolved


async def _enrich_one(
    ctx: TaskContext,
    tally: _EnrichTally,
    *,
    video: Video,
    now: datetime,
    params: EnrichMetricsParams,
) -> None:
    """一条作品：读数（必做）+ 评论（声明了能力且给了条数才做）。

    两次抓取各自包住：读数失败不该顺手把评论也标成失败（清单按 stage 分组，
    排查"评论接口被风控"与"stat 读不出来"要做的动作不同）。

    适配器**取不到**也算一次 `metrics` 失败而不是让整轮崩：`video_ids_missing` 是全平台的，
    而"库里有一条 B站 作品、这一家的适配器今天没装配（平台被关掉 / 配置坏了）"是常态 ——
    让它抛出去会让一条孤儿记录掀掉 50 条的批次，且清单上只剩一个 traceback。
    """
    try:
        adapter = ctx.adapters.get(video.platform)
    except IntelligenceHubError as exc:
        tally.record_failure(video=video, stage="metrics", error=str(exc))
        return

    try:
        readings = await adapter.fetch_metrics(_as_meta(video))
    except IntelligenceHubError as exc:
        tally.record_failure(video=video, stage="metrics", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - 适配器里任何异常都不能掀掉整轮，但必须留原文
        tally.record_failure(video=video, stage="metrics", error=f"{type(exc).__name__}: {exc}")
        return

    if readings.is_empty():
        # 契约上适配器不该交空读数（`base.py::fetch_metrics`），但那是"应该"：
        # 这里再挡一次，是因为空快照会让这条作品从此不再被补抓（`put` 的拒收理由同一条）。
        tally.record_failure(video=video, stage="metrics", error="适配器交的读数四项全空")
        return

    draft = MetricSnapshotDraft(
        checkpoint=window_for(video.published_at, now),
        view_count=readings.view_count,
        like_count=readings.like_count,
        comment_count=readings.comment_count,
        share_count=readings.share_count,
        metadata_json=readings.metadata_json,
    )
    try:
        row = await ctx.storage.metrics.put(video.id, draft)
    except IntelligenceHubError as exc:
        tally.record_failure(video=video, stage="store", error=str(exc))
        return
    tally.record(video.platform, row.checkpoint)

    if params.comments_limit <= 0:
        return
    await _fetch_comments(ctx, tally, video=video, adapter=adapter, limit=params.comments_limit)


async def _fetch_comments(
    ctx: TaskContext,
    tally: _EnrichTally,
    *,
    video: Video,
    adapter: PlatformAdapter,
    limit: int,
) -> None:
    """评论只在**能力位为真**的平台上问。

    先问能力再决定要不要为它写一段分支（而不是 try/except 里猜），
    是 ADR-0020 决定二的落地方式：`supports_comments=False` 的平台连问都不该问 ——
    它的 `fetch_comments` 按契约返回 None，问了只是白跑一趟。
    """
    if not ctx.adapters.capabilities(video.platform).supports_comments:
        tally.comments_unsupported += 1
        return
    try:
        drafts = await adapter.fetch_comments(_as_meta(video), limit=limit)
    except IntelligenceHubError as exc:
        tally.record_failure(video=video, stage="comments", error=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - 同上：留原文，不掀掉整轮
        tally.record_failure(video=video, stage="comments", error=f"{type(exc).__name__}: {exc}")
        return
    if drafts is None:
        # 能力位说会、实现却交 None：这是声明与实现不一致，不能记成"这条没评论"。
        tally.record_failure(
            video=video,
            stage="comments",
            error=f"{video.platform} 声明 supports_comments=True 但 fetch_comments 返回了 None",
        )
        return
    inserted, updated = await ctx.storage.video_comments.upsert_many(video.id, list(drafts))
    tally.record_comments(video.platform, inserted=inserted, updated=updated)


def _as_meta(video: Video) -> VideoMeta:
    """DB 行 → 适配器认的 `VideoMeta`（契约的入参是后者）。

    与 `tasks/collect.py` 那边同方向但不同构造：那边手上就有 `VideoMeta`，这里只有行。
    `creator_ref` 是用行里那两个 id **凑**出来的 —— 评论与读数都不要作者身份，
    只要平台侧作品 id。凑的是一个不存在的主页 URL，所以它不许被任何下游当链接去访问
    （`extra.from_video_row=True` 就是给下游用来识别"这份 meta 不是采集来的"的旗标）。
    """
    platform_id = str(video.platform_video_id)
    return VideoMeta.model_validate(
        {
            "platform": video.platform,
            "platform_video_id": platform_id,
            "creator_ref": {
                "platform": video.platform,
                "platform_id": platform_id,
                "profile_url": f"https://{video.platform}/video/{platform_id}",
            },
            "title": video.title,
            "webpage_url": f"https://{video.platform}/video/{platform_id}",
            "extra": {"from_video_row": True},
        }
    )


class _EnrichTally:
    """计数器 + 失败明细。与 `postprocess._TranscribeTally` 同一形状（各数各的，不合并）。"""

    def __init__(self) -> None:
        self.snapshots: dict[str, int] = {}
        self.comments_new = 0
        self.comments_seen = 0
        self.comments_unsupported = 0
        self.failed = 0
        self.failures: list[FailureRecord] = []

    def record(self, platform: str, checkpoint: MetricCheckpoint) -> None:
        key = f"{platform}/{checkpoint}"
        self.snapshots[key] = self.snapshots.get(key, 0) + 1

    def record_comments(self, platform: str, *, inserted: int, updated: int) -> None:
        del platform  # 平台名进不了这两个数：一批评论只可能来自一条作品
        self.comments_new += inserted
        self.comments_seen += updated

    def record_failure(
        self, *, video: Video, stage: Literal["metrics", "comments", "store"], error: str
    ) -> None:
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=video.platform, stage=stage, video_id=video.platform_video_id, error=error
            )
        )

    def to_result(self, platform: str | None) -> TaskResult:
        written = sum(self.snapshots.values())
        status: Literal["success", "partial", "failed"]
        if written == 0 and self.failed > 0:
            status = "failed"
        elif self.failed > 0:
            status = "partial"
        else:
            status = "success"
        summary: dict[str, int | str] = {
            "platform": platform or "全部",
            "snapshots": written,
            "by_window": ", ".join(f"{k}={v}" for k, v in sorted(self.snapshots.items()))
            or "（无）",
            "comments_new": self.comments_new,
            "comments_updated": self.comments_seen,
            # 这一栏存在的理由：只给"抓到 0 条"分不清"这家不支持"与"接口挂了"。
            # 补一个数比省一行注释便宜（评论那一路今天是 B站 专属）。
            "comments_unsupported_platforms": self.comments_unsupported,
            "failed": self.failed,
        }
        return TaskResult(status=status, summary=summary, failures=self.failures)
