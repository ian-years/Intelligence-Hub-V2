"""B站字幕轨 → `Transcript`（字幕优先路径，省掉一整轮 ASR）。

契约位置：`PlatformAdapter.fetch_subtitles()`。抖音那边永远返回 None（没有字幕轨），
B站这边**有就有、没有就没有** —— 但"没有"和"问不出来"必须分开：

| 情况 | 返回 | 为什么 |
|---|---|---|
| 接口通了、`subtitles` 是空数组 | `None` | 确凿没有。调度器去跑 ASR，正确 |
| 需要登录才有 AI 字幕（匿名请求回空） | `None` | 同上，但 note 要提一句"带登录 cookie 可能有" |
| 412/352 风控、连不上、返回 HTML | **抛 `PlatformError`** | 这不是"没有字幕"，是"没问到" |

吞成 `None` 的代价：一次网络故障静默变成一整轮昂贵的 ASR，
而症状只是"转写特别慢"，日志干净 —— 最难归因的那一类。

协议 docstring 写的是"拿不到返回 None"，这里把它读成"确认没有才返回 None"。
理由：让 ASR 替一次风控买单，症状是"转写特别慢"而日志干净，属于最难归因的那一类。

数据形状来自 `x/player/v2`（2026-09-22 实测：BV1GJ411x7h7 / cid=137649199 匿名可访问，
`data.subtitle = {allow_submit, lan, lan_doc, subtitles: [], subtitle_position, font_size_type}`）。
**`subtitles[]` 里单个条目的形状本会话没取到真样本**（这条视频没有字幕，
而 UGC 字幕多数要登录态），按公开口径解析并且每一步都 `isinstance` 兜底：
读不出来的字段一律当"没有这条轨"，绝不猜。
字幕正文 `{"body": [{"from": 0.0, "to": 2.4, "content": "…"}]}` 同理。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.models.transcript import Transcript, TranscriptSegment
from intelligence_hub_v2.platforms.bilibili.listing import api_headers
from intelligence_hub_v2.platforms.bilibili.urls import (
    media_comment_url,
    player_api_url,
    require_http_url,
)

PLATFORM = "bilibili"

__all__ = [
    "SubtitleTrack",
    "build_transcript",
    "choose_track",
    "fetch_subtitle_tracks",
    "fetch_transcript",
    "parse_subtitle_body",
    "parse_subtitle_tracks",
]

_PREFERRED_LANGS: tuple[str, ...] = ("zh-cn", "zh", "cn")
"""挑选顺序。中文站的主流情形是 `zh-CN`；带 `en-US` 的翻译轨排后面。"""


@dataclass(frozen=True)
class SubtitleTrack:
    """一条字幕轨。`url` 已经补成绝对地址（B站给的是 `//ai...` 协议相对形式）。"""

    lan: str
    lan_doc: str
    url: str
    ai_type: int | None = None

    @property
    def is_ai(self) -> bool:
        """`ai_type` 非 0 视为机翻轨。

        认不出来（字段缺失/形状不对）时**当作 False**：宁可用一条可能是机翻的字幕，
        也不要因此跑去重跑 ASR —— ASR 的代价是几分钟 GPU，而机翻字幕的
        可用性在 V1 的实践中是够格的。
        """
        return bool(self.ai_type)


def parse_subtitle_tracks(payload: Mapping[str, Any]) -> list[SubtitleTrack]:
    """`x/player/v2` 的整份响应 → 可用的字幕轨列表。

    每一处都是"形状不对就当没有"，因为这些字段随 B站前端版本变。
    一条轨缺 `subtitle_url` 就直接丢掉它（没有地址的轨是不可用的，
    留着会在下一步变成一句更难懂的 HTTP 错）。
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    subtitle = data.get("subtitle")
    if not isinstance(subtitle, Mapping):
        return []
    items = subtitle.get("subtitles")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []

    tracks: list[SubtitleTrack] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        raw_url = item.get("subtitle_url_v2") or item.get("subtitle_url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        try:
            url = require_http_url(raw_url.strip(), context="字幕轨地址")
        except PlatformError:
            continue
        lan = str(item.get("lan") or "").strip()
        tracks.append(
            SubtitleTrack(
                lan=lan,
                lan_doc=str(item.get("lan_doc") or "").strip(),
                url=url,
                ai_type=_as_optional_int(item.get("ai_type")),
            )
        )
    return tracks


def _as_optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def choose_track(
    tracks: Sequence[SubtitleTrack], *, preferred_langs: Sequence[str] = _PREFERRED_LANGS
) -> SubtitleTrack | None:
    """挑一条最能用的轨：先语言，再"人翻优先于机翻"，最后按给定顺序。

    语言匹配用 `startswith` 而不是相等：B站回过 `zh-CN`、`zh-Hans`、`中文（中国）`
    好几种写法（`lan` 与 `lan_doc` 一个稳定一个不稳定），严格相等会因为一个
    大小写就把人翻轨判成"没有中文轨"，然后白跑一轮 ASR。
    """
    if not tracks:
        return None
    lowered = [lang.lower() for lang in preferred_langs]

    def rank(indexed: tuple[int, SubtitleTrack]) -> tuple[int, int, int]:
        index, track = indexed
        lan = track.lan.lower()
        lang_hit = next((i for i, want in enumerate(lowered) if lan.startswith(want)), len(lowered))
        return (lang_hit, 1 if track.is_ai else 0, index)

    # `index` 进排序键是为了**稳定**：两条轨排名并列时必须每次挑同一条，
    # 否则同一批视频跑两遍会一批用 A 轨一批用 B 轨，字幕变更就看不出是不是一回事。
    return min(enumerate(tracks), key=rank)[1]


def parse_subtitle_body(payload: object) -> list[TranscriptSegment]:
    """字幕正文 `{body: [{from, to, content}]}` → 带时间戳的片段。

    `from`/`to` 缺一个或不是数字时**丢掉那一句**而不是把时间戳填 0：
    填 0 会让前端"点击句子跳到 0:00"，而跳到 0:00 与"这句没有时间戳"
    在界面上完全同形（V1 §1.3 的"看起来在跑"）。
    """
    body = payload.get("body") if isinstance(payload, Mapping) else None
    if not isinstance(body, Sequence) or isinstance(body, (str, bytes)):
        return []
    segments: list[TranscriptSegment] = []
    for item in body:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("content") or "").strip()
        start = _as_time(item.get("from"))
        end = _as_time(item.get("to"))
        if not text or start is None or end is None or end < start:
            continue
        segments.append(TranscriptSegment(start_seconds=start, end_seconds=end, text=text))
    return segments


def _as_time(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    try:
        text = str(value).strip()
        return float(text) if text else None
    except ValueError:
        return None


def build_transcript(
    segments: Sequence[TranscriptSegment], *, lan: str | None = None
) -> Transcript:
    """片段 → `Transcript`。

    `sentence_count` 就是片段数：B站字幕天然一句一条，
    与 ASR 那条路（SenseVoice 不产标点，要按静音切句补 `。`，V1 §7.9）
    的"句子"口径一致，前端可以同一套渲染。
    """
    text = "\n".join(segment.text for segment in segments)
    return Transcript(
        engine="bilibili_subtitle",
        language=lan or None,
        text=text,
        char_count=len(text),
        sentence_count=len(segments),
        segments=list(segments),
    )


async def fetch_subtitle_tracks(
    http: httpx.AsyncClient, bvid: str, cid: int | str, *, budget_seconds: float = 30.0
) -> list[SubtitleTrack]:
    """问一次 `x/player/v2` 拿字幕轨。业务码不是 0 时抛（见模块 docstring 那张表）。"""
    referer = media_comment_url(bvid)
    try:
        response = await http.get(
            player_api_url(bvid, cid),
            headers=api_headers(referer=referer),
            timeout=budget_seconds,
        )
        payload = response.json()
    except httpx.HTTPError as exc:
        msg = f"问 {bvid} 的字幕轨失败：{type(exc).__name__}: {exc}"
        raise PlatformError(PLATFORM, "subtitle", msg) from exc
    except ValueError:
        msg = f"{bvid} 的字幕接口回的不是 JSON（HTTP {response.status_code}）"
        raise PlatformError(PLATFORM, "subtitle", msg) from None
    if not isinstance(payload, Mapping):
        msg = f"{bvid} 的字幕接口形状不对：{type(payload).__name__}"
        raise PlatformError(PLATFORM, "subtitle", msg)
    if payload.get("code") != 0:
        msg = (
            f"B站 player 接口回 code={payload.get('code')} "
            f"message={payload.get('message')!r}（{bvid}）"
        )
        raise PlatformError(PLATFORM, "subtitle", msg)
    return parse_subtitle_tracks(payload)


async def fetch_transcript(
    http: httpx.AsyncClient, bvid: str, cid: int | str, *, budget_seconds: float = 30.0
) -> Transcript | None:
    """一整趟：问轨 → 挑一条 → 取正文 → 收成 `Transcript`。

    返回 None 的**唯一**条件是"接口确认没有可用字幕"。
    取正文失败也返回 None（轨列表是权威接口给的，正文那条 CDN 挂了
    属于"这一条轨拿不到"，值得走 ASR 而不是把整条作品判红）。
    """
    track = choose_track(
        await fetch_subtitle_tracks(http, bvid, cid, budget_seconds=budget_seconds)
    )
    if track is None:
        return None
    try:
        response = await http.get(
            track.url,
            headers=api_headers(referer=media_comment_url(bvid)),
            timeout=budget_seconds,
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    segments = parse_subtitle_body(payload)
    if not segments:
        return None
    return build_transcript(segments, lan=track.lan)
