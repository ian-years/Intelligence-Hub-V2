"""`backfill` handler：爆款回溯 —— 点名一位博主，扫她的近况，按点赞挑前几条收进来。

**这一格存在的理由是 V1 §7.22 那次事故**，而事故的形状是"点名"与"取名单"混用：
按钮标题写着某一位博主，argv 里却只有 `--videos-per-creator 5`，采集器于是按跟踪开关
筛全库；库里两位 B站 博主的开关都关着时拿到 0 条，`RuntimeError` 一路甩成 traceback，
清单停在没有 `status` / `ended_at` 的初稿。所以本模块有三条不许商量的纪律：

1. **只走 `creators.find()`，一次都不走 `list_tracked()` / `list_all()`。** 点名任务不需要
   名单；存储层那两道的分工已经写在 `repositories/creators.py` 的 docstring 里。
   看护：`test_a_backfill_of_an_untracked_creator_still_finds_her`。
2. **认不出身份就如实失败，不退化成扫全库。** `parse_creator_url` 交回什么就查什么；
   库里没有这一行 → `stage="store"`… 不，是 `stage="task"` 的一条失败（这是任务级，
   不是某个 item 的流水线挂了），文案给出下一步（先跑「收录博主」）。
3. **提前失败也要有终态清单。** 这一条不需要本模块做什么：`manifest_writer` 是上下文
   管理器，五条退出路径都落终态（V2.0 的结构性消除）。所以这里只测"红得说实话"。

逐条下载**复用 `collect._collect_one_video`**：查重 → 下载 → 入库 → publish 快照，
一整套失败语义（`TaskCancelled` 原样上抛、单条失败留原文、媒体已落盘不回收）都在那一份里。
再写一份就是 V1 §7.11 那一族"同一件事写两遍然后漂一次"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.task import FailureRecord, TaskResult
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.tasks.collect import _collect_one_video, _Tally, _to_ref
from intelligence_hub_v2.tasks.dispatch import detect_platform
from intelligence_hub_v2.tasks.params import BackfillParams

if TYPE_CHECKING:
    from intelligence_hub_v2.models.creator import Creator
    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["run_backfill"]

#: `scan` 留空时，扫描窗口 = 平台配置的 `videos_per_creator` × 这个倍数（再夹一个下限）。
#: 4 倍不是算出来的，是"爆款往往不在最新那几条里"这个经验的数字化；下限 20 是为了
#: 把 `videos_per_creator=1` 这种配置下的窗口撑到还能挑的程度。
_SCAN_MULTIPLIER = 4
_SCAN_FLOOR = 20


async def run_backfill(ctx: TaskContext, params: BackfillParams) -> TaskResult:
    ctx.check_cancelled()
    platform = params.platform or detect_platform(params.creator_url, ctx.adapters)
    adapter = ctx.adapters.get(platform)

    await ctx.progress(0.05, stage="parse_url", current_item=params.creator_url[:60])
    ref = await adapter.parse_creator_url(params.creator_url)

    # ↓ 唯一的博主查询：按身份键，**不过跟踪开关**（纪律 1）
    creator = await ctx.storage.creators.find(platform, ref.platform_id)
    if creator is None:
        return TaskResult(
            status="failed",
            summary={
                "platform": platform,
                "platform_id": ref.platform_id,
                "scanned": 0,
                "top": params.top,
                "downloaded": 0,
                "failed": 1,
            },
            failures=[
                FailureRecord(
                    platform=platform,
                    stage="task",
                    error=(
                        f"库里没有这位博主（{platform} / {ref.platform_id}），回溯不扫全库。"
                        "先跑「收录博主」（add_creator）把这条链接收成一行，再回溯。"
                    ),
                    error_kind="CreatorNotFound",
                )
            ],
        )

    config = ctx.adapters.config_for(platform)
    window = (config.videos_per_creator or _SCAN_FLOOR) * _SCAN_MULTIPLIER
    scan = params.scan or max(_SCAN_FLOOR, window)

    await ctx.progress(0.12, stage="scan", current_item=creator.name)
    metas: list[VideoMeta] = []
    try:
        async for meta in adapter.list_creator_videos(_to_ref(creator), limit=scan):
            ctx.check_cancelled()
            metas.append(meta)
            await ctx.progress(0.12 + 0.38 * min(1.0, len(metas) / scan), stage="scan")
    except TaskCancelled:
        raise  # 与 collect 同一条纪律：取消不是"这位博主枚举失败"，交给 runner 落 cancelled 终态
    except Exception as exc:  # noqa: BLE001 - 枚举挂了就是挂了，留原文；不 catch 会甩成 traceback（V1 那样）
        tally = _Tally()
        tally.record_list_failure(creator=creator, platform=platform, error=str(exc))
        result = tally.to_result(platform)
        return _merge(result, creator=creator, scanned=0, top=params.top, unknown=0, picked=[])

    ranked, unknown = _rank_by_likes(metas)
    picked = ranked[: params.top]

    tally = _Tally()
    for index, meta in enumerate(picked):
        ctx.check_cancelled()
        await _collect_one_video(ctx, tally, adapter=adapter, meta=meta, creator=creator)
        await ctx.progress(0.5 + 0.5 * (index + 1) / max(1, len(picked)), stage="collect")

    result = tally.to_result(platform)
    return _merge(
        result,
        creator=creator,
        scanned=len(metas),
        top=params.top,
        unknown=unknown,
        picked=[f"{m.platform_video_id}(like={m.like_count})" for m in picked],
    )


def _rank_by_likes(metas: list[VideoMeta]) -> tuple[list[VideoMeta], int]:
    """按点赞降序排，返回 `(排好的, 读数缺失的条数)`。

    **`like_count=None` 一律排在最后**，而不是当 0 参与比较：一条"平台没给点赞数"的作品
    与一条"点赞真的是 0"的作品不是一回事，前者冒充爆款被收进来，这份清单就在说谎；
    而它排在最后也不等于被判了死刑 —— 只有全部读数都缺时它才会因为排序稳定而按枚举顺序入选，
    那种情况下 `unknown_metrics` 会把"这次其实没按热度挑"这件事说出口。
    """
    unknown = sum(1 for m in metas if m.like_count is None)
    ordered = sorted(metas, key=lambda m: (m.like_count is None, -(m.like_count or 0)))
    return ordered, unknown


def _merge(
    result: TaskResult,
    *,
    creator: Creator,
    scanned: int,
    top: int,
    unknown: int,
    picked: list[str],
) -> TaskResult:
    """把"扫了多少 / 挑了哪几条 / 有多少读数缺失"抬进清单。

    这些必须和 collect 那几栏（downloaded / skipped_existing / failed …）**并排**而不是
    替换：`scanned=40, top=5, downloaded=0, skipped_existing=5` 讲的是"挑出来的 5 条早就在库里"，
    这与 `scanned=0`（枚举挂了）或 `downloaded=0, failed=5`（五条都下不动）是三种不同的红，
    合成一栏就分不出来了。
    """
    summary = dict(result.summary)
    summary.update(
        {
            "creator_id": creator.id,
            "creator_name": creator.name,
            # 这一栏就是 §7.22 的反证：点名任务**允许**打在 is_tracking=False 的博主身上，
            # 但它必须说出来，好让人分清"没跟踪所以本来就不会日更"与"回溯把它捞进来了"。
            "creator_was_tracked": int(creator.is_tracking),
            "scanned": scanned,
            "top": top,
            "unknown_metrics": unknown,
            "picked": ", ".join(picked) or "（无）",
        }
    )
    return TaskResult(
        status=result.status, summary=summary, artifacts=result.artifacts, failures=result.failures
    )
