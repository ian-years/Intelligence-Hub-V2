"""`douyin_collect` / `bilibili_collect` 的共同实现（工厂按平台绑定）。

两个平台的采集流程逐步骤相同（枚举 → 查重 → 下载 → 入库），差别全在适配器里，
所以这里**只写一份** `make_collect_handler(platform)`，注册表用平台名把它实例化两次。
写成 `douyin_collect.py` + `bilibili_collect.py` 会是四十行几乎一样的复制，
正是"两处真相漂一次"的形状（V1 的 cookie 阶梯就是这么漂过的）。

本轮记账（`docs/progress/2026-09-22.md` 的"下一步"）在这里全部落地：

- ① 跨博主间隔 `per_creator_seconds` **在这一层做**（适配器故意不做，见抖音
  `_pace` 的注释）；用可取消的 `ctx.sleep`。
- ② `since` 在抖音是弱过滤、在 B站 是实过滤 —— 两种语义都不靠它做增量游标。
- ③ **真正的增量是 `videos.find_by_platform_id` 查重**：已收过就 `skipped`，不重下。
- ④ 清单里带上 `cookie_rung`（档位差别是画质不是能不能下，V1 §7.15）：
  每条写进 `videos.metadata_json`，并按档位汇总进 `summary["cookie_rungs"]`。
- ⑤ 平台配置层的 env source 还没接线（`config-schema.md §6` 的收口项）—— 不在本层。

失败语义（V1 §1.3）：**逐条 item 失败**记进 `failures[]` 并继续跑，最后按
"有没有下载成功"定 success/partial/failed；**整位博主枚举失败**记一条 list 失败后
换下一位，不让一位坏博主掀掉整轮。**取消例外**：`TaskCancelled` 原样往上抛，
不能当成"这位博主采集失败"被记进 failures。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.creator import Creator, CreatorRef
from intelligence_hub_v2.models.engagement import MetricSnapshotDraft
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.media import (
    MediaArtifact,
    VideoAudioPairArtifact,
    audio_path_of,
    total_size_bytes,
)
from intelligence_hub_v2.models.task import ArtifactRef, FailureRecord, TaskResult
from intelligence_hub_v2.models.video import VideoDraft, VideoMeta
from intelligence_hub_v2.platforms.base import PlatformAdapter
from intelligence_hub_v2.tasks.params import CollectParams

if TYPE_CHECKING:
    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["build_video_draft", "make_collect_handler"]


def make_collect_handler(
    platform: str,
) -> Callable[[TaskContext, CollectParams], Awaitable[TaskResult]]:
    """造一个"只采某个平台"的 runner。闭包只吃一个 `platform`，别无状态。"""

    async def handler(ctx: TaskContext, params: CollectParams) -> TaskResult:
        return await _collect(ctx, params, platform)

    return handler


async def _collect(ctx: TaskContext, params: CollectParams, platform: str) -> TaskResult:
    ctx.check_cancelled()
    adapter = ctx.adapters.get(platform)
    config = ctx.adapters.config_for(platform)
    creators = await _resolve_targets(ctx, platform, params)
    if not creators:
        return TaskResult(
            status="success",
            summary={"platform": platform, "creators": 0, "downloaded": 0, "skipped_existing": 0},
        )

    total_creators = len(creators)
    gap = config.rate_limit.per_creator_seconds
    limit = params.limit or config.videos_per_creator
    tally = _Tally()

    for index, creator in enumerate(creators):
        if index > 0:
            await ctx.sleep(gap)  # 记账 ①：跨博主节流，可取消
        ctx.check_cancelled()
        await _collect_one_creator(
            ctx,
            tally,
            adapter=adapter,
            creator=creator,
            platform=platform,
            limit=limit,
            since=params.since,
            metrics_only=params.metrics_only,
            progress_base=index,
            progress_total=total_creators,
        )
        await ctx.progress((index + 1) / total_creators, stage="collect", current_item=creator.name)

    return tally.to_result(platform)


async def _resolve_targets(ctx: TaskContext, platform: str, params: CollectParams) -> list[Creator]:
    """`creator_ids` 给定就按位取，否则取该平台所有跟踪中的博主（V1 §7.22 的分工）。"""
    if params.creator_ids:
        return [await ctx.storage.creators.get_or_raise(cid) for cid in params.creator_ids]
    return await ctx.storage.creators.list_tracked(platform=platform)


async def _collect_one_creator(
    ctx: TaskContext,
    tally: _Tally,
    *,
    adapter: PlatformAdapter,
    creator: Creator,
    platform: str,
    limit: int,
    since: datetime | None,
    metrics_only: bool,
    progress_base: int,
    progress_total: int,
) -> None:
    """一位博主一趟。整趟枚举失败只记一条 list 失败，不影响别的博主。"""
    ref = _to_ref(creator)
    seen = 0
    try:
        async for meta in adapter.list_creator_videos(ref, since=since, limit=limit):
            ctx.check_cancelled()
            seen += 1
            await _collect_one_video(
                ctx, tally, adapter=adapter, meta=meta, creator=creator, metrics_only=metrics_only
            )
            frac = min(0.95, seen / max(1, limit))
            await ctx.progress((progress_base + frac) / progress_total, stage="collect")
    except TaskCancelled:
        raise  # 取消不是"这位博主失败"，交给 runner 落 cancelled 终态
    except Exception as exc:  # noqa: BLE001 - 一位坏博主不该掀掉整轮，但失败必须留原文
        tally.record_list_failure(creator=creator, platform=platform, error=str(exc))


async def _collect_one_video(
    ctx: TaskContext,
    tally: _Tally,
    *,
    adapter: PlatformAdapter,
    meta: VideoMeta,
    creator: Creator,
    metrics_only: bool = False,
) -> None:
    """一条作品：查重 → 下载 → 入库。查重命中直接 `skipped`（记账 ③）。

    查重与入库各自包住（review P1-5）：库写不进去不是"这位博主枚举失败"，
    记 `stage="store"`；漏包的话这些异常会落进 `_collect_one_creator` 的
    list 兜底，排查方向被带偏，且已下载成功的媒体不进 `artifacts`。

    `metrics_only` 那条分支**连媒体目录都不建**：V1 的"仅采集数据"要的就是"不动下载配额、
    不占磁盘，先把读数与历史立起来"。建一个空目录会留下一个"下载失败了"的形状，
    而这里根本没有发生过一次下载尝试 —— 两件事必须能在磁盘上被区分开。
    """
    try:
        existing = await ctx.storage.videos.find_by_platform_id(
            meta.platform, meta.platform_video_id
        )
    except TaskCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - 库问不出来也要留原文，并按下一条继续
        tally.record_store_failure(platform=meta.platform, meta=meta, error=str(exc))
        return
    if existing is not None:
        tally.skipped += 1
        return

    artifact: MediaArtifact | None = None
    if not metrics_only:
        dest = ctx.files.media_dir(meta.platform, creator.name, meta.platform_video_id, meta.title)
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
        try:
            artifact = await adapter.download_media(meta, dest)
        except TaskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条下载失败：留原文进 failures，继续下一条
            tally.record_download_failure(platform=meta.platform, meta=meta, error=str(exc))
            return

    try:
        draft = build_video_draft(meta, creator_id=creator.id, artifact=artifact, ctx=ctx)
        row, created = await ctx.storage.videos.insert_or_get(draft)
    except TaskCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - 同上：入库失败记 store，媒体已落盘不回收
        tally.record_store_failure(platform=meta.platform, meta=meta, error=str(exc))
        return
    if not created:
        tally.skipped += 1  # 与并发的另一轮抢到了同一条
        return

    # 新作品顺手记一条 `publish` 快照（ADR-0020：这条读数是"刚抓到时长什么样"的唯一一份）。
    # 放在这里而不是 `enrich_metrics` 里：采集当场就拿得到 meta 的计数，
    # 让 enrich 去补就等于要求"每条新作品都要再跑一轮任务"才能留下第一格。
    # 失败**不**掀掉这一条（作品已经进库了），但要在清单里看得见。
    await _record_publish_snapshot(ctx, tally, row_id=row.id, meta=meta)

    if artifact is None:
        # 判据看的是"有没有字节"，不是那个开关：开关与产物对不上时（将来多一条
        # "下得来但不落盘"的路），跟着事实走才不会往清单里塞一个不存在的路径。
        tally.record_registered()
    else:
        tally.record_success(
            artifact=artifact, meta=meta, row_id=row.id, media_rel=draft.media_path or ""
        )
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


def _to_ref(creator: Creator) -> CreatorRef:
    """库里的博主行 → 适配器认的 `CreatorRef`。

    用 `model_validate` 而不是直接构造：`profile_url` 存的是字符串，而 `CreatorRef` 要
    `HttpUrl` —— 交给 pydantic 校验（入库时已是规范主页，这里只是类型还原），
    比手工 `HttpUrl(...)` 构造在 mypy 下更干净。
    """
    return CreatorRef.model_validate(
        {
            "platform": creator.platform,
            "platform_id": creator.platform_id,
            "profile_url": creator.profile_url,
        }
    )


def build_video_draft(
    meta: VideoMeta,
    *,
    creator_id: int | None,
    artifact: MediaArtifact | None,
    ctx: TaskContext,
) -> VideoDraft:
    """把 `VideoMeta` + `MediaArtifact` 拼成入库草稿（collect 与 single_link 共用）。

    路径归一化（绝对 → 相对 `data/`）**在这里做**，不在适配器里：`AdapterDeps` 没有
    `FileStorage`，只有 handler 知道这条作品最终归在谁名下（`platform-adapter.md §2.4` 修订）。

    `artifact=None` = 这一条只登记读数（`CollectParams.metrics_only`）。媒体三列留 NULL，
    `metadata_json` 里写明 `media_downloaded: false` —— 下游（postprocess / ASR）据此
    跳过，而不是"读不出音频所以失败"。
    """
    if artifact is None:
        return _metrics_only_draft(meta, creator_id=creator_id)
    main_rel, aux_paths, source, size, duration, rung, ytdlp_err = _artifact_fields(artifact, ctx)
    meta_json = json.dumps(
        {
            "media_source": source,
            "cookie_rung": rung,
            "yt_dlp_error": ytdlp_err,
            "size_bytes": size,
            "has_audio": _has_audio(artifact),
            # ADR-0019 造了这一位就是给"要不要挂 `<video>`"用的（一张 jpg 躺在
            # `media_path` 里是真的存在，喂给播放器只会得到一块**不报错的黑屏**）。
            # 不落进 metadata 就等于字段只活在产物上：库里那一行读不回来，
            # 前端只能按"media_path 有没有"猜 —— 而那个判据对图文笔记恰好是错的。
            "has_video": _has_video(artifact),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return VideoDraft(
        platform=meta.platform,
        platform_video_id=meta.platform_video_id,
        creator_id=creator_id,
        title=meta.title,
        description=meta.description,
        published_at=meta.published_at,
        duration_seconds=duration if duration is not None else meta.duration_seconds,
        view_count=meta.view_count,
        like_count=meta.like_count,
        comment_count=meta.comment_count,
        share_count=meta.share_count,
        media_path=main_rel,
        media_source=source,
        media_aux_paths_json=json.dumps(aux_paths, ensure_ascii=False),
        metadata_json=meta_json,
    )


def _metrics_only_draft(meta: VideoMeta, *, creator_id: int | None) -> VideoDraft:
    """只登记读数、不带媒体的草稿。

    `has_audio: False` 与 `media_downloaded: False` 都要写：前者让后处理那一步
    干净地跳过（ADR-0019 定的那个闸），后者让"这一条从来没有过媒体"在库里可读 ——
    只留 NULL 的话，它与"下载失败但没记上原因"完全同形。
    """
    return VideoDraft(
        platform=meta.platform,
        platform_video_id=meta.platform_video_id,
        creator_id=creator_id,
        title=meta.title,
        description=meta.description,
        published_at=meta.published_at,
        duration_seconds=meta.duration_seconds,
        view_count=meta.view_count,
        like_count=meta.like_count,
        comment_count=meta.comment_count,
        share_count=meta.share_count,
        media_path=None,
        media_source=None,
        media_aux_paths_json="[]",
        metadata_json=json.dumps(
            {
                "media_downloaded": False,
                "media_source": None,
                "has_audio": False,
                "has_video": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


async def _record_publish_snapshot(
    ctx: TaskContext, tally: _Tally, *, row_id: int, meta: VideoMeta
) -> None:
    """新作品入库时顺手记一条 `publish` 读数（ADR-0020 决定二里"collect 的这一半"）。

    取的是 `meta` 上那四个计数，**不再问一次适配器**：列表枚举刚读回来的就是这一刻的数，
    再问一趟等于同一个 GET 做两遍 —— 而抖音/小红书那一趟要走 CDP 桥。
    这也是为什么它不需要 `capabilities` 参与：用的数据这一条链路已经拿到了。

    四项全空直接跳过，不问库：`metrics.put()` 拒收空草稿（一条全 NULL 的快照会冒充
    "这个窗口抓过了"，让 `enrich_metrics` 从此不再看这条作品）。跳过是**预期**而不是失败，
    所以不进 `failures`，但 `summary` 里那一栏会如实少一。
    """
    draft = MetricSnapshotDraft(
        checkpoint="publish",
        view_count=meta.view_count,
        like_count=meta.like_count,
        comment_count=meta.comment_count,
        share_count=meta.share_count,
    )
    if draft.is_empty():
        return
    try:
        await ctx.storage.metrics.put(row_id, draft)
    except Exception as exc:  # noqa: BLE001 - 作品已经在库里，不该被一次附带的写入抹掉
        tally.record_snapshot_failure(platform=meta.platform, meta=meta, error=str(exc))
        return
    tally.record_snapshot()


def _artifact_fields(
    artifact: MediaArtifact, ctx: TaskContext
) -> tuple[str, list[str], str, int, float | None, str | None, str | None]:
    """把三种产物形状摊平成同一组入库字段。

    返回：主路径rel / 其他产物rel / 来源 / 体积 / 时长 / 档位 / 兜底原文。

    `media_aux_paths_json` 那一列装的是"**同一条作品的其他产物文件**"（ADR-0019）：
    DASH 分片在那儿放音频轨，小红书图文在那儿放第 2~N 张原图。
    **读的一侧不许按"那一定是音频"解释它** —— 转写走 `audio_path_of()`，
    那里由 `has_audio` 把关（`_has_audio` 就是这条边界的落点）。
    """
    if isinstance(artifact, VideoAudioPairArtifact):
        return (
            ctx.files.rel(artifact.video_path),
            [ctx.files.rel(artifact.audio_path)],
            artifact.media_source,
            total_size_bytes(artifact),
            artifact.duration_seconds,
            artifact.cookie_rung,
            artifact.yt_dlp_error,
        )
    return (
        ctx.files.rel(artifact.path),
        [ctx.files.rel(path) for path in artifact.extra_paths],
        artifact.media_source,
        artifact.size_bytes,
        artifact.duration_seconds,
        artifact.cookie_rung,
        artifact.yt_dlp_error,
    )


def _has_video(artifact: MediaArtifact) -> bool:
    """这条产物**能不能当视频播**。图文笔记的主文件是一张原图 → False（ADR-0019）。

    判据取适配器交回来的那一位，不在这里按后缀猜：猜会得到第二套真相，
    而 ADR-0019 当初立这一位，就是因为 V1 靠 `glob("*.mp4")` 那类猜法踩过方向相反的错
    （§7.21 把音频轨当视频喂给 ffmpeg，报出的错长得像"ffmpeg 没装"）。

    `VideoAudioPairArtifact` 恒为 True：那一型就是"视频轨与音频轨各自一份"，
    有分片必然有视频轨（没有视频轨的那一型是图文，走单文件 + `has_video=False`）。
    """
    if isinstance(artifact, VideoAudioPairArtifact):
        return True
    return artifact.has_video


def _has_audio(artifact: MediaArtifact) -> bool:
    """这条产物有没有可转写的音频轨（V1 §7.21：dash_split 的主文件是纯视频轨）。

    `audio_path_of` 是唯一的可信答案 —— 单文件没音频时它会抛，这里把它转成 False，
    好让 metadata 如实记"这条不能转写"，而不是让后处理层再去猜一次。
    """
    try:
        audio_path_of(artifact)
    except ValueError:
        return False
    return True


class _Tally:
    """一趟采集的计数器 + 失败明细 + 产物引用。收成一处是为了让 `to_result` 的
    success/partial/failed 判定只有一个地方读得到计数。"""

    def __init__(self) -> None:
        self.downloaded = 0
        self.registered = 0
        self.skipped = 0
        self.snapshots = 0
        self.failed = 0
        self.rungs: dict[str, int] = {}
        self.failures: list[FailureRecord] = []
        self.artifacts: list[ArtifactRef] = []

    def record_snapshot(self) -> None:
        """这一条留下了 `publish` 读数。与 `registered` 分栏：后者数的是"进了库"，
        前者数的是"库里能看见它当时是多少" —— 计数全 NULL 的作品进得了库但没有第一格。"""
        self.snapshots += 1

    def record_snapshot_failure(self, *, platform: str, meta: VideoMeta, error: str) -> None:
        """快照写不进去。**不**记成 `store`：那一格说的是"作品本身没进库"，
        而这里作品已经在库里了 —— 混在一起会让"整批没有第一格"看起来像"入库在挂"。"""
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=platform,
                stage="metrics",
                video_id=meta.platform_video_id,
                error=error,
                error_kind="MetricSnapshot",
            )
        )

    def record_registered(self) -> None:
        """只登记了读数（`metrics_only`）：**不产 ArtifactRef**。

        清单里挂一个不存在的路径，等于让"产物核验"那一步去 stat 一个空目录 ——
        那一栏会红，而红的原因（这一条压根没打算下载）是假的。
        哪几条进来了库里查得到（`media_path IS NULL` 配上 `metadata_json` 里那个
        `media_downloaded: false`），所以这里只需要一个数。
        """
        self.registered += 1

    def record_success(
        self, *, artifact: MediaArtifact, meta: VideoMeta, row_id: int, media_rel: str
    ) -> None:
        self.downloaded += 1
        key = artifact.cookie_rung or "（未标注档位）"
        self.rungs[key] = self.rungs.get(key, 0) + 1
        self.artifacts.append(
            ArtifactRef(
                kind="media",
                # 清单里的产物路径是**相对 `data/`** 的（`_video_draft` 已归一化）。
                path=Path(media_rel),
                platform=meta.platform,
                video_id=str(row_id),
                size_bytes=total_size_bytes(artifact),
            )
        )

    def record_download_failure(self, *, platform: str, meta: VideoMeta, error: str) -> None:
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=platform,
                stage="download",
                video_id=meta.platform_video_id,
                error=error,
                error_kind="MediaDownload",
            )
        )

    def record_list_failure(self, *, creator: Creator, platform: str, error: str) -> None:
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=platform,
                stage="list",
                creator_id=str(creator.id),
                error=error,
                error_kind="List",
            )
        )

    def record_store_failure(self, *, platform: str, meta: VideoMeta, error: str) -> None:
        """查重/入库挂了（review P1-5）。**不是**"这位博主枚举失败"——记成 list
        会把排查的人带去查平台的枚举接口，而真实原因是库打不开/写不进去。
        下载已经成功的媒体留在磁盘上：下一轮 `find_by_platform_id` 命中就跳过，
        不会重复下载。"""
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=platform,
                stage="store",
                video_id=meta.platform_video_id,
                error=error,
                error_kind="Storage",
            )
        )

    def to_result(self, platform: str) -> TaskResult:
        status: Literal["success", "partial", "failed"]
        if self.downloaded + self.registered == 0 and self.failed > 0:
            status = "failed"
        elif self.failed > 0:
            status = "partial"
        else:
            status = "success"
        summary: dict[str, int | str] = {
            "platform": platform,
            "downloaded": self.downloaded,
            # 与 `downloaded` 分栏：一轮"只采指标"里两条计数不会互相冒充，
            # 而 `downloaded=0` 在 metrics_only 那一站是**预期**，不是失败。
            "registered_metrics_only": self.registered,
            "skipped_existing": self.skipped,
            # ADR-0020：`downloaded` 与 `registered` 都不回答"这条的第一格读数进了没"。
            # 少了这一栏，"平台没给计数"（预期）与"快照写挂了"（要查）在清单里同形。
            "publish_snapshots": self.snapshots,
            "failed": self.failed,
            # 记账 ④：把档位分布抬进清单，"这批为什么糊"才有地方可查。
            "cookie_rungs": ", ".join(f"{k}={v}" for k, v in sorted(self.rungs.items()))
            or "（无）",
        }
        return TaskResult(
            status=status, summary=summary, artifacts=self.artifacts, failures=self.failures
        )
