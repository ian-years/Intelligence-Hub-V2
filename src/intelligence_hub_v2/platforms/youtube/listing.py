"""YouTube 的作品枚举：`yt-dlp --flat-playlist -j` 的输出解析 + **时间窗**。

V1 对应物是 `download_youtube_latest.py` 里那三个纯函数：`to_timestamp`、
`select_flat_entries`、`flat_entry_as_video`。搬进 V2 时改了两处**方向**，
其余判据一字未动：

1. **时间从"本地时区的展示串"变成 UTC 的 `datetime`**。
   V1 的 `published_text()` 产出 `"%Y-%m-%d %H:%M:%S"`（终点是飞书表格里的单元格），
   V2 的 `videos.published_at` 是可排序的列（`data-model.md §2.3`），
   所以一律 `tzinfo=UTC`。后果写在 `timestamp_to_utc` 的 docstring 里 ——
   `upload_date`（`YYYYMMDD`，无时分）在 V1 是**本地零点**、在 V2 是 **UTC 零点**，
   同一位博主同一批作品，窗外窗内可能差一条（东八区差 8 小时）。
2. **`--recent-days` 变成契约里的 `since`**。V1 那个 CLI 参数在 V2 是
   `CollectParams.since`（`tasks/collect.py` 原样传给 `list_creator_videos`），
   于是"天数"这一格只存在于调用方，适配器只看一个时刻。
   **V1 的语义保留完整**：窗外丢弃、窗内保留、**日期缺失的条目保留**并计数。

日期缺失那一档为什么"保留"而不是"丢弃"（V1 的原话是"不编造时间，按原顺序取"）：
`--flat-playlist` 只保证 `id/url/title`，`timestamp` 是**看标签页给不给**。
判"缺失即丢弃"的后果是**整位博主空手**（频道页偶发不给日期时，
一次采集什么收成都没有，而退出码是 0）。判"保留"的后果是多收几条旧的，
而 `videos` 表按 `platform_video_id` 查重会把它们挡在库外。
两边不对称，所以 V1 选保留、V2 照做，并把这条钉成用例。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import HttpUrl

from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.urls import absolute_http_url
from intelligence_hub_v2.platforms.youtube.urls import (
    canonical_video_url,
    is_channel_id,
    is_video_id,
    require_model_url,
)

__all__ = [
    "FlatVideo",
    "WindowOutcome",
    "entry_to_video",
    "parse_dump_json_lines",
    "scan_budget",
    "timestamp_to_utc",
    "to_videos",
    "video_meta_for",
    "within_window",
]

PLATFORM = "youtube"

_SCAN_MULTIPLIER = 3
"""只按时间窗筛选时多枚举一些候选（V1 `RECENT_SCAN_MULTIPLIER` 同值）。
窗口边界的私密/短片/社区帖会把"最新 N 条"里的名额挤掉，多扫几倍才凑得满。"""

_SCAN_CEILING = 50
"""V1 `MAX_RECENT_SCAN`。再多就是拿一次几十秒的枚举换几条可能用不上的候选。"""

_UPLOAD_DATE_RE = re.compile(r"^\d{8}$")
_EPOCH_SECONDS_RE = re.compile(r"^\d{9,11}$")
"""V1 的两种数字形状，**区间原样搬**。

13 位（毫秒）**不认**，与 V1 一致：认错的代价比漏掉大 —— 把 `1700000000000`
当秒用会得出公元 55841 年，于是"这条在窗内"永远成立，时间窗整个失效。
漏掉的后果只是这一条变成"日期缺失"，走上面那条"保留并计数"的路。
"""

_VIDEO_ENTRY_TYPE = "video"
"""`--flat-playlist` 在频道标签页上还会给出别的条目类型（`playlist`、`url`）。
V1 的判据是 `str(item.get("type") or "video") == "video"` —— 缺 `type` 当视频，
因为**单条作品**的 dump 根本没有这个字段。判紧会把整批作品丢掉。"""


@dataclass(frozen=True)
class FlatVideo:
    """一条 `-j` 输出里"可以当作品收"的那几样。

    刻意不叫 `VideoMeta`：flat 模式下大量字段是缺的，
    把它们填成 0 / 空串就是撒谎（`platforms/urls.py::parse_cn_count` 同一条纪律）。
    """

    video_id: str
    title: str
    webpage_url: str
    duration_seconds: float | None = None
    published_at: datetime | None = None
    view_count: int | None = None
    channel_id: str = ""
    channel: str = ""

    @property
    def has_date(self) -> bool:
        return self.published_at is not None


def parse_dump_json_lines(stdout: str) -> list[dict[str, Any]]:
    """`--dump-json`（`-j`）是**一行一个 JSON**，不是数组。

    与 `platforms/bilibili/listing.py` 里那份**同名同判据**。这是已知的重复，
    而且是有人看着的那种：`tests/unit/platforms/test_youtube_listing.py` 里有一条
    用例把同一批脏样本喂给两份实现并要求结果相等，所以"只改一份"会立刻红。
    正解是提到 `infra/ytdlp.py`（它才是命令行契约的所在），那一次要连着改
    B站 的引用与它的用例 —— 不在本格里顺手做，理由记在 `docs/progress/`。

    逐行 `startswith("{")` 筛掉的是 yt-dlp 混进 stdout 的 `[debug]` / `[info]` 行：
    整段 `json.loads` 会在第一行就炸，而报出来的错长得像"这个平台不支持"。
    单行解析失败**跳过那一行**而不是放弃整批：一条坏了不该让整位博主空手。
    """
    entries: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def timestamp_to_utc(value: object) -> datetime | None:
    """yt-dlp 可能给出的多种时间形态 → UTC 的 `datetime`；认不出返回 **None**。

    四种形状与 V1 的 `to_timestamp` 一一对应：unix 秒（int/float）、
    `YYYYMMDD`（`upload_date`，**按 UTC 零点**解释）、9~11 位数字串（秒）、
    `"%Y-%m-%d %H:%M:%S"`（无时区 → 按 UTC 解释）。

    返回 None 而不是"现在"或"1970"：编一个时间会让一条老作品看起来在窗内
    （或窗外），而 `--recent-days` 的全部意义就是这一判。
    `0` 也归 None（与 B站 的 `_as_aware` 同一口径）：抽取器把"没有值"落成 0 是常态，
    而 0 秒 = 1970 年 = **永远在窗外**，那会把一条真作品静默丢掉。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = str(value).strip()
    if not text:
        return None
    if _UPLOAD_DATE_RE.fullmatch(text):
        return _from_date(text, "%Y%m%d")
    if _EPOCH_SECONDS_RE.fullmatch(text):
        return _from_epoch(float(text))
    return _from_date(text, "%Y-%m-%d %H:%M:%S")


def _from_epoch(seconds: float) -> datetime | None:
    """unix 秒 → UTC 时刻。0 / 负数 / 超出范围都算"没给时间"。"""
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # 13 位毫秒当秒用会得出公元五万年，在这里变成 None（= 日期缺失，保留并计数）。
        return None


def _from_date(text: str, fmt: str) -> datetime | None:
    """按 `fmt` 解析并**贴上 UTC**（V1 那些串都是无时区的，见模块 docstring 的第 1 条）。"""
    try:
        return datetime.strptime(text, fmt).replace(tzinfo=UTC)
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _as_int(value: object) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def entry_to_video(entry: object) -> FlatVideo | None:
    """一条 `-j` 的 entry → `FlatVideo`；不是作品就返回 None。

    丢掉的是四种：不是 dict、`type` 明确不是 `video`（频道页的播放列表条目）、
    没有 id、**id 不是 11 位作品形状**。第四种是 2026-09-24 那条 B站 P0 的镜像：
    `-J`（整个 playlist 一个对象）喂给逐行解析器会给出**一条**条目，
    它的 `id` 是频道/列表的 ID（`UC…`，24 位）—— 没有这道形状闸就会被当成一条作品收进去，
    于是"枚举恒空"变成"库里多了一条永远下不动的孤儿"。
    代价写在 `urls._VIDEO_ID_RE` 的注释里：YouTube 真要改 id 长度时这一批会全被丢，
    但那不是静默的 —— `to_videos` 的第二个返回值会把被丢的 id 交进 `ListError` 原文。

    **没有第五种**：`listing` 里曾经按"标题为空"丢条目，
    那会把一条只有 ID 的新作品整个吞掉，而它的媒体与字幕都只依赖 ID。
    """
    if not isinstance(entry, Mapping):
        return None
    if str(entry.get("type") or _VIDEO_ENTRY_TYPE) != _VIDEO_ENTRY_TYPE:
        return None
    video_id = str(entry.get("id") or "").strip()
    if not is_video_id(video_id):
        return None
    raw_url = entry.get("url") or entry.get("webpage_url")
    as_url = absolute_http_url(raw_url) if isinstance(raw_url, str) else None
    return FlatVideo(
        video_id=video_id,
        # 标题缺失时**用 id 顶上**，不填空串：`VideoMeta.title` 是必填且前端要显示，
        # 而"一串不像标题的字符"比"什么都不显示"更容易被认出是数据缺了（V1 同做法）。
        title=str(entry.get("title") or video_id).strip() or video_id,
        webpage_url=str(as_url) if as_url is not None else canonical_video_url(video_id),
        duration_seconds=_as_float(entry.get("duration")),
        published_at=timestamp_to_utc(entry.get("timestamp") or entry.get("upload_date")),
        view_count=_as_int(entry.get("view_count")),
        channel_id=str(entry.get("channel_id") or "").strip(),
        channel=str(entry.get("channel") or entry.get("uploader") or "").strip(),
    )


def to_videos(entries: Sequence[object]) -> tuple[list[FlatVideo], list[str]]:
    """entries → (作品, 被丢条目的**说明**)。

    第二个返回值不是装饰：`--flat-playlist` 的抽取器与 YouTube 页面结构是两条
    会各自演化的东西，"退出码 0 但一条都没抽出来"必须能自己讲完故事
    （2026-09-24 那条 B站 枚举恒空的 P0 就是这么被发现的 —— 差别只是当时没人看得见）。
    """
    kept: list[FlatVideo] = []
    seen: set[str] = set()
    rejected: list[str] = []
    for index, entry in enumerate(entries):
        video = entry_to_video(entry)
        if video is None:
            rejected.append(f"#{index}: {describe_rejection(entry)}")
            continue
        if video.video_id in seen:
            rejected.append(f"#{index}: 重复的 {video.video_id}")
            continue
        seen.add(video.video_id)
        kept.append(video)
    return kept, rejected


def describe_rejection(entry: object) -> str:
    """为什么这一条没被当成作品 —— 一句话，带它自己的关键字段。"""
    if not isinstance(entry, Mapping):
        return f"不是对象（{type(entry).__name__}）"
    kind = str(entry.get("type") or _VIDEO_ENTRY_TYPE)
    ident = str(entry.get("id") or "").strip()
    if kind != _VIDEO_ENTRY_TYPE:
        return f"类型是 {kind!r}，不是作品"
    if not ident:
        return "没有 id"
    return f"id {ident!r} 不是 11 位的作品 ID（YouTube 改了 ID 形状，还是喂进来了整份 playlist？）"


@dataclass(frozen=True)
class WindowOutcome:
    """时间窗判完的结果。三个数各答一问，不许合并。"""

    selected: tuple[FlatVideo, ...]
    undated: int
    """**没有发布时间因而无法判定新旧**的条数（V1 那句 warn 的就是这个数）。"""

    older: int
    """有日期但在窗外、被丢掉的条数。"""

    @property
    def nothing_new(self) -> bool:
        """窗内一条都没有，且**不是因为整批都没日期**。"""
        return not self.selected and self.older > 0


def within_window(
    videos: Sequence[FlatVideo],
    *,
    since: datetime | None,
    limit: int,
) -> WindowOutcome:
    """V1 `select_flat_entries` 的时间窗，语义原样。

    - `since is None` → 不过滤，按顺序取前 `limit` 条（`undated`/`older` 都是 0：
      没判过，报告这两个数就是撒谎）。
    - 有日期且 `< since` → 丢，计入 `older`。
    - **没有日期 → 留**，计入 `undated`（模块 docstring 里那条不对称判断）。
    - 凑满 `limit` 就停：列表顺序即"最新发布在前"（yt-dlp 给的），
      再扫下去只是拿一次几十秒换一批已经用不上的候选。

    `since` 必须是带时区的：拿一个 naive 时刻与 UTC 比较会当场抛
    `TypeError: can't compare offset-naive and offset-aware datetimes`，
    而那异常发生在枚举**之后**，看起来像"适配器坏了"。这里显式挡住并说清。
    """
    budget = max(0, int(limit))
    if since is None:
        return WindowOutcome(tuple(videos[:budget]), 0, 0)
    if since.tzinfo is None or since.tzinfo.utcoffset(since) is None:
        msg = f"since 必须带时区，收到 {since.isoformat()}（naive 时刻与 UTC 无法比较）"
        raise ValueError(msg)

    selected: list[FlatVideo] = []
    undated = 0
    older = 0
    for video in videos:
        moment = video.published_at
        if moment is None:
            undated += 1
            selected.append(video)
        elif moment >= since:
            selected.append(video)
        else:
            older += 1
            continue
        if len(selected) >= budget:
            break
    return WindowOutcome(tuple(selected[:budget]), undated, older)


def scan_budget(limit: int, *, filtered: bool) -> int:
    """该向 yt-dlp 要几条候选（`--playlist-items 1:N` 里那个 N）。

    只有开了时间窗才多要（V1 同一个条件）：不限时间时"最新 N 条"就是答案，
    多扫 3 倍只是把一次几秒的枚举变成几十秒。
    """
    base = max(1, int(limit))
    if not filtered:
        return base
    return min(_SCAN_CEILING, max(base, base * _SCAN_MULTIPLIER))


def video_meta_for(video: FlatVideo, *, ref: CreatorRef) -> VideoMeta:
    """`FlatVideo` → 契约的 `VideoMeta`。

    缺的字段保持缺（`None`），不用 0 或空串填：`duration_seconds=None` 在前端是
    "不知道"，`0.0` 是"这条作品 0 秒"，两回事。

    `channel_id` 与请求的 ref 不一致时**照收**，但把它记进 `extra`：
    频道改手柄、被合并、播放列表跨频道都会造成不一致，
    而"这条作品不属于这位博主"要由 `ref` 说了算（V1 §7.1 那一族：
    入库时把身份换成页面自报的那个，会造成同一条作品挂到两个博主下）。
    """
    url: HttpUrl = absolute_http_url(video.webpage_url) or require_model_url(
        canonical_video_url(video.video_id), context=f"作品 {video.video_id} 的主页"
    )
    reported = video.channel_id
    return VideoMeta(
        platform=PLATFORM,
        platform_video_id=video.video_id,
        creator_ref=ref,
        title=video.title,
        published_at=video.published_at,
        duration_seconds=video.duration_seconds,
        view_count=video.view_count,
        webpage_url=url,
        extra={
            "channel_id": reported,
            "channel": video.channel,
            "channel_matches_creator": bool(reported and reported == ref.platform_id),
            "has_date": video.has_date,
        },
    )


def channel_id_from_entries(entries: Sequence[object]) -> str:
    """从一批 flat 条目里取出**唯一一个**自报的频道 ID；没有或不止一个返回空串。

    用途是 `parse_creator_url` 里把 `@手柄` 解析成 `UC…`（V1 做不了这一步，
    它把主页链接原样交给 yt-dlp）。"不止一个就返回空"是有意的：
    跨频道播放列表里挑第一个当身份，就是把 A 频道的作品挂到 B 频道名下。
    """
    found = {
        channel
        for entry in entries
        if isinstance(entry, Mapping) and (channel := str(entry.get("channel_id") or "").strip())
    }
    if len(found) != 1:
        return ""
    only = next(iter(found))
    return only if is_channel_id(only) else ""


def days_to_since(days: int, *, now: datetime | None = None) -> datetime:
    """把"最近 N 天"换成 `since`（V1 `--recent-days` 的那一步算术，留在调用方）。

    放在这里是为了让**它**可离线测：窗口边界只由这一个减法决定。
    """
    if days < 1:
        msg = f"days 必须 >= 1，收到 {days}（0 天的窗口是空集，那不是'不过滤'）"
        raise ValueError(msg)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        msg = f"now 必须带时区，收到 {moment.isoformat()}"
        raise ValueError(msg)
    return moment - timedelta(days=days)
