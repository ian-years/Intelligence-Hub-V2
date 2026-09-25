"""B站的列表枚举与元数据。

**三条路，按可靠性排序**，走哪条由 `capabilities.list_strategy` 与配置决定：

1. `yt_dlp_flat`（默认）：`yt-dlp --flat-playlist --dump-json <space>/video`。
   V1 一直跑通的就是这条。**必须带导出 cookie**：
   2026-09-22 在本机匿名复现过一次，回的是
   `ERROR: [BilibiliSpaceVideo] 486906719: Request is blocked by server (412)` ——
   那句 412 就是 V1 §7.15 写的那件事，也正是 `should_escalate_cookie_rung()`
   要认的原文（不认的话"换一档 cookie"根本不会发生）。
2. `external_manifest`：吃 `.agents/skills/bilibili-download` 那份浏览器清单
   （V1 §7.13：技能脚本物理上有两份会漂，所以只认**仓库内**那一份产出的文件）。
   命中清单的博主跳过内置枚举，没命中的**照旧回落**到第 1 条。
3. ~~公开 web-interface 做列表枚举~~：**设计文档里写的这条实测不成立**。
   `x/space/wbi/arc/search` 不带 WBI 签名时回的是一个 HTML 风控页而不是 JSON
   （2026-09-22 现场验的），而签名那份等于把 yt-dlp 已经实现的抽取器重写一遍。
   所以接口还在用，只是角色换成**逐条作品的元数据**（下面 `fetch_view`，
   实测匿名 `code:0`）。这条收口记在 `docs/adr/0011` 的 Task 7 追记。

Cookie 阶梯的顺序不在这里（`capabilities.cookie_variants`，ADR-0011）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from intelligence_hub_v2.errors import ListError
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.engagement import MetricReadings
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.bilibili.urls import (
    as_http_url,
    canonical_video_url,
    extract_bvid,
    media_comment_url,
    require_model_url,
    space_video_url,
    view_api_url,
)

PLATFORM = "bilibili"

__all__ = [
    "BiliCard",
    "card_to_video_meta",
    "fetch_view",
    "load_external_manifest",
    "manifest_cards_for",
    "parse_dump_json_lines",
    "view_to_card",
]

API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
"""api.bilibili.com 按 UA 分客户端。空 UA 与非浏览器 UA 会被降到"匿名受限"档，
症状就是 `code:-352` 那一类"风控但没说为什么"。"""


def api_headers(*, referer: str) -> dict[str, str]:
    """接口要求的两个头。`Referer` 不是可选的：
    `x/web-interface/view` 缺它时一部分响应会退化成 `-400 请求错误`。"""
    return {"User-Agent": API_USER_AGENT, "Referer": referer, "Accept": "application/json"}


@dataclass(frozen=True)
class BiliCard:
    """一条作品的最小 Known（列表来源不管是谁，都先收成这个形状）。"""

    bvid: str
    title: str = ""
    url: str = ""
    cid: int | None = None
    aid: int | None = None
    duration_seconds: float | None = None
    published_at: datetime | None = None
    cover_url: str | None = None
    uploader_mid: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    pages: int = 1

    def __post_init__(self) -> None:
        if not self.url:
            object.__setattr__(self, "url", canonical_video_url(self.bvid))


def _as_int(value: object) -> int | None:
    """收成 int，收不出返回 None（**不是 0**）。

    0 是合法值（新号 0 播放），把"读不到"写成 0 就是让看板上多一排假数据 ——
    这是本仓库对 `None` 与 `0` 的统一口径，见 `douyin/urls.parse_cn_count`。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = str(value or "").strip().replace(",", "")
    if text.isdigit():
        return int(text)
    return None


def _as_aware(value: object) -> datetime | None:
    """把 unix 秒（B站的 `pubdate` / `timestamp`）转成带时区的 datetime。

    没有 `tzinfo` 的时间进库就是"每台机器读出一个不同结果"，
    而 `data-model.md §2.3` 的 `published_at` 要能被前端排序 —— 这里补上 UTC。
    """
    stamp = _as_int(value)
    if stamp is None or stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp, tz=UTC)


# --------------------------------------------------------------------------- #
# 第 1 条路：yt-dlp --flat-playlist 的输出
# --------------------------------------------------------------------------- #


def parse_dump_json_lines(stdout: str) -> list[dict[str, Any]]:
    """`--dump-json` 是**一行一个 JSON**，不是数组。

    为什么要逐行 `startswith("{")` 筛：yt-dlp 会把 `[debug]` / `[BilibiliSpaceVideo] ...`
    混进 stdout（尤其退档那几轮），整段 `json.loads` 会在第一行就炸，
    而报出来的错长得像"这个平台不支持"。
    解析单行失败时**跳过那一行**而不是放弃整批：一条卡片坏了不该让整位博主空手。
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


def entry_to_card(entry: object) -> BiliCard | None:
    """flat-playlist 的一条 entry → `BiliCard`。

    取值顺序照 V1：`id` → `webpage_url_basename`，`url` → `webpage_url`。
    **`--flat-playlist` 只保证 id/url/title**，时长与发布时间常常是缺的 ——
    缺就是 None，不去猜（`since` 那一半逻辑见适配器 docstring）。

    本会话没能取到真样本（匿名请求被 412 挡在门外，见模块 docstring），
    所以这里只认 V1 用过的那几个键，不做任何"顺手多读几个字段"的发挥。

    开头那个 `isinstance` 看着多余（`parse_dump_json_lines` 已经筛过一遍），
    但 `entries_to_cards` 是公开出口、也会被外部清单那条路喂 ——
    一条 `"not a dict"` 在那里会变成
    `AttributeError: 'str' object has no attribute 'get'`，
    而那句完全指不到"第 3 行长得不像对象"。这条是被用例抓出来的。
    """
    if not isinstance(entry, Mapping):
        return None
    bvid = extract_bvid(entry.get("id") or entry.get("webpage_url_basename") or entry.get("url"))
    if not bvid:
        return None
    return BiliCard(
        bvid=bvid,
        title=str(entry.get("title") or "").strip(),
        url=str(entry.get("url") or entry.get("webpage_url") or "").strip()
        or canonical_video_url(bvid),
        duration_seconds=_as_float(entry.get("duration")),
        view_count=_as_int(entry.get("view_count")),
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    try:
        text = str(value or "").strip()
        return float(text) if text and text.replace(".", "", 1).isdigit() else None
    except ValueError:
        return None


def entries_to_cards(entries: Sequence[Mapping[str, Any]], *, limit: int) -> list[BiliCard]:
    """去重 + 截断。同一位博主的空间页里 `BV` 可能被抽到两次（分区页与投稿页重复）。"""
    out: list[BiliCard] = []
    seen: set[str] = set()
    for entry in entries:
        card = entry_to_card(entry)
        if card is None or card.bvid in seen:
            continue
        seen.add(card.bvid)
        out.append(card)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 第 3 条路（元数据那半条）：公开 web-interface
# --------------------------------------------------------------------------- #


class ViewApiError(ListError):
    """`x/web-interface/view` 没给出可用的 `data`。"""


async def fetch_view(
    http: httpx.AsyncClient, bvid: str, *, budget_seconds: float = 30.0
) -> dict[str, Any]:
    """取一条作品的公开元数据（标题 / 时长 / 发布时间 / cid / 分P 数）。

    B站的接口把"业务失败"编在 200 里：`{"code": -404, "message": "啥都木有"}`。
    **只看 HTTP 状态码会把"视频不存在"当成成功**，然后拿着一个空 dict 往下走。
    所以这里必须判 `code`，且把 `code` 与 `message` 原文一起交出去。
    """
    try:
        response = await http.get(
            view_api_url(bvid),
            headers=api_headers(referer=media_comment_url(bvid)),
            timeout=budget_seconds,
        )
    except httpx.HTTPError as exc:
        msg = f"取 {bvid} 的元数据失败：{type(exc).__name__}: {exc}"
        raise ViewApiError(PLATFORM, "list", msg) from exc

    try:
        payload: Any = response.json()
    except ValueError:
        # 风控时这里回的是一整页 HTML（空间列表那条路实测如此）。
        # 把 HTML 的头 200 字带进异常，才能一眼看出"是被风控了"而不是"接口坏了"。
        snippet = response.text[:200]
        msg = f"{bvid} 的元数据接口回的不是 JSON（HTTP {response.status_code}）：{snippet!r}"
        raise ViewApiError(PLATFORM, "list", msg) from None

    if not isinstance(payload, dict):
        msg = f"{bvid} 的元数据接口形状不对：拿到 {type(payload).__name__}"
        raise ViewApiError(PLATFORM, "list", msg)
    code = payload.get("code")
    if code != 0:
        msg = f"B站 view 接口回 code={code} message={payload.get('message')!r}（{bvid}）"
        raise ViewApiError(PLATFORM, "list", msg)
    data = payload.get("data")
    if not isinstance(data, dict):
        msg = f"B站 view 接口 code=0 但没有 data（{bvid}）"
        raise ViewApiError(PLATFORM, "list", msg)
    return data


def view_to_card(bvid: str, data: Mapping[str, Any]) -> BiliCard:
    """`view` 接口的 `data` → `BiliCard`（字段名取自 2026-09-22 的真响应）。

    `pages[0].cid` 是给字幕接口用的 —— **多 P 视频 cid 是分 P 的**，
    所以这里取第一 P 的 cid 只是"有就比没有强"，真按 P 转写是 V2.1 的事。
    """
    owner: Mapping[str, Any] = _mapping(data.get("owner"))
    pages: list[Mapping[str, Any]] = [
        item for item in (data.get("pages") or []) if isinstance(item, Mapping)
    ]
    first_page: Mapping[str, Any] = pages[0] if pages else {}
    stat: Mapping[str, Any] = _mapping(data.get("stat"))
    title = str(data.get("title") or "").strip()
    pic = data.get("pic")
    return BiliCard(
        bvid=str(data.get("bvid") or bvid),
        title=title or bvid,
        url=canonical_video_url(str(data.get("bvid") or bvid)),
        cid=_as_int(first_page.get("cid") or data.get("cid")),
        aid=_as_int(data.get("aid")),
        duration_seconds=_as_float(first_page.get("duration") or data.get("duration")),
        published_at=_as_aware(data.get("pubdate")),
        cover_url=str(pic) if isinstance(pic, str) and pic else None,
        uploader_mid=str(owner.get("mid")) if owner.get("mid") is not None else None,
        view_count=_as_int(stat.get("view")),
        like_count=_as_int(stat.get("like")),
        pages=len(pages) or 1,
    )


def stat_to_readings(data: Mapping[str, Any]) -> MetricReadings:
    """`view` 接口里的 `stat` → 一次读数（ADR-0020 决定二的 `fetch_metrics`）。

    读的是 `stat` 那一段而不是顶层：B站 把计数全嵌在 `stat` 里，顶层读 `view_count`
    会永远拿到 None，于是"补抓一次"静默变成"抓了个空的"—— 而空读数会被仓库层
    当成"这个窗口已经抓过了"，从此不再补抓（见 `MetricSnapshotRepository.put`）。

    `favorite`（收藏）不当成 `share_count`：V1 也没有把两者混过，但这一格最容易被人
    "反正都是个位数级互动"地合并 —— 收藏与转发回答的是两个问题。
    认不出来的项留 NULL（`_as_int` 的既有口径），不补 0。
    """
    stat = _mapping(data.get("stat"))
    return MetricReadings(
        view_count=_as_int(stat.get("view")),
        like_count=_as_int(stat.get("like")),
        comment_count=_as_int(stat.get("reply")),
        share_count=_as_int(stat.get("share")),
        metadata_json=json.dumps(
            {"coin": _as_int(stat.get("coin")), "favorite": _as_int(stat.get("favorite"))},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def view_to_profile_fields(data: Mapping[str, Any]) -> dict[str, str]:
    """`view` 里能反推博主身份的那几个字段（`parse_creator_url` 粘的是作品链接时用）。"""
    owner = _mapping(data.get("owner"))
    mid = owner.get("mid")
    return {"mid": str(mid) if mid is not None else ""}


def _mapping(value: object) -> Mapping[str, Any]:
    """是 mapping 就当 mapping，不是就当空。

    写成一个小函数而不是三处 `x if isinstance(x, Mapping) else {}`：
    后者让 mypy 收不了窄（它会去 narrow 那个 `isinstance` 的**参数**，
    而参数与返回值是两个表达式），于是每个调用点都多一条 union-attr 错。
    """
    return value if isinstance(value, Mapping) else {}


# --------------------------------------------------------------------------- #
# 第 2 条路：外部浏览器清单（V1 `bilibili-download` 技能）
# --------------------------------------------------------------------------- #


def load_external_manifest(path: Path) -> list[object]:
    """读清单文件，返回顶层条目列表。

    键名兼容 `parsed` / `creators` / `results` 三种（V1 的技能脚本换过两版输出格式，
    存量文件里三种都在）。一个都不认识时抛，**不返回空列表** ——
    返回空的后果是"清单看起来是空的"，而真原因是文件被改过格式，
    那属于 V1 §7.13 说的"两份脚本漂移"，必须当场说破。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"读外部浏览器清单失败（{path}）：{exc}"
        raise ListError(PLATFORM, "list", msg) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"外部浏览器清单不是合法 JSON（{path}）：{exc}"
        raise ListError(PLATFORM, "list", msg) from exc

    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict):
        for key in ("parsed", "creators", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    msg = (
        f"外部浏览器清单里找不到 parsed/creators/results 列表（{path}）。"
        f"生产者必须是**仓库内**那份技能脚本（V1 §7.13：用户级副本会漂）"
    )
    raise ListError(PLATFORM, "list", msg)


def manifest_cards_for(
    entries: Sequence[Any], *, mid: str, name: str = "", limit: int = 30
) -> list[BiliCard]:
    """在清单里找这一位博主的作品。**只按 mid 认人**（其次才按昵称）。

    V1 那边是一堆 `compact_match_key`（昵称、mid、space_url 各归一化一遍再互相匹配），
    属于 §7.11"同一个东西三种叫法"的家族。V2 的身份就是 `CreatorRef.platform_id`
    = mid，昵称只做**清单里根本没有 mid** 时的兜底 —— 少一层模糊匹配就少一处漂移。

    找不到这一位时返回空列表（不是抛）：调用方会回落到内置枚举，
    这正是这条策略的语义（"命中清单的博主跳过内置枚举"）。
    """
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        creator = entry.get("creator") or entry.get("creator_config") or entry
        if not isinstance(creator, dict):
            creator = {}
        entry_mid = str(creator.get("mid") or entry.get("mid") or "").strip()
        entry_name = str(creator.get("name") or entry.get("name") or "").strip()
        if entry_mid:
            if entry_mid != mid:
                continue
        elif not name or entry_name != name:
            continue
        return _cards_from_video_list(entry, limit=limit)
    return []


def _cards_from_video_list(entry: Mapping[str, Any], *, limit: int) -> list[BiliCard]:
    for key in ("selected", "videos", "candidates"):
        videos = entry.get(key)
        if isinstance(videos, dict):
            videos = [videos]
        if not isinstance(videos, list):
            continue
        cards: list[BiliCard] = []
        for item in videos:
            card = _normalize_manifest_video(item)
            if card is not None:
                cards.append(card)
            if len(cards) >= limit:
                break
        if cards:
            return cards
    return []


def _normalize_manifest_video(item: object) -> BiliCard | None:
    """清单里的一条作品 → `BiliCard`。

    V1 这里遇到没有 bvid 的条目是直接 raise 的。V2 改成跳过 + 计数：
    一份几百条的清单里混进一条脏记录（浏览器插件抓到广告位）
    不该让整位博主的采集失败 —— 但**也不能静默**，所以数量由调用方记日志。
    """
    if not isinstance(item, dict):
        return None
    bvid = extract_bvid(
        item.get("bvid")
        or item.get("platform_video_id")
        or item.get("url")
        or item.get("video_url")
    )
    if not bvid:
        return None
    url = str(item.get("url") or item.get("video_url") or "").strip()
    return BiliCard(
        bvid=bvid,
        title=str(item.get("title") or "").strip(),
        url=url if url.startswith(("http://", "https://")) else canonical_video_url(bvid),
        cid=_as_int(item.get("cid")),
        duration_seconds=_as_float(item.get("duration") or item.get("duration_seconds")),
    )


def card_to_video_meta(card: BiliCard, *, ref: CreatorRef) -> VideoMeta:
    """`BiliCard` → 契约里的 `VideoMeta`。"""
    # 封面认不出就当"没有"：它不是必填字段，不该让一条作品因为一张图消失。
    cover = as_http_url(card.cover_url) if card.cover_url else None
    return VideoMeta(
        platform=PLATFORM,
        platform_video_id=card.bvid,
        creator_ref=ref,
        title=card.title or card.bvid,
        published_at=card.published_at,
        duration_seconds=card.duration_seconds,
        view_count=card.view_count,
        like_count=card.like_count,
        cover_url=cover,
        webpage_url=require_model_url(
            canonical_video_url(card.bvid), context=f"{card.bvid} 的作品页"
        ),
        extra={
            "cid": card.cid,
            "aid": card.aid,
            "pages": card.pages,
            "uploader_mid": card.uploader_mid,
        },
    )


def space_url_for(ref: CreatorRef) -> str:
    """枚举用的地址：作品页而不是空间首页（后者抽不出投稿）。"""
    return space_video_url(ref.platform_id)
