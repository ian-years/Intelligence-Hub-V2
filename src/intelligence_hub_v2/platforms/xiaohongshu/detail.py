"""小红书的**笔记详情**与**站内搜索**：另两份页面 JS + 它们的解析与编排。

为什么单开这一个文件而不是塞进 `listing.py`（抖音那份是 `media.py` 里带着详情 JS 的）：

1. 抖音的详情 JS 只回答一个问题——"播放直链在哪"，所以它天然属于 `media.py`。
   小红书的详情一次带回 标题 / 正文 / 作者 / 指标 / **原图列表** / **视频直链** /
   发布时间 / 笔记类型，前半是元数据（`list_creator_videos` 要用）、后半是媒体地址
   （`download_media` 要用）。**两边都要用它就不属于任何一边。**
2. 站内搜索（V1 `--creator-name`）与"枚举一位博主的作品"是两件事：
   它的产出是**候选博主**，不是作品卡片。放 `listing.py` 会让那个文件的标题说谎。
3. 生命周期也不同：`listing.py` 的两份 JS 一位博主跑一次，这一份**每条笔记跑一次**。
   把它放在这里，"每条作品多一次桥导航"这件事在文件名字上就看得见。

V1 源：`download_xiaohongshu_latest.py` 的 `NOTE_DETAIL_PAGE_FUNCTION`（:767）、
`fetch_note_detail`（:959）、`SEARCH_USER_PAGE_FUNCTION`（:704）、
`search_creators_by_name`（:884）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.infra.cdp_bridge import BridgeClient
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.platforms.urls import absolute_http_url, parse_cn_count
from intelligence_hub_v2.platforms.xiaohongshu.listing import (
    PageBudget,
    decode_page_result,
    render_page_js,
    require_http_url,
)
from intelligence_hub_v2.platforms.xiaohongshu.urls import (
    build_note_url,
    build_profile_url,
    parse_publish_time,
    published_at_from_note_id,
)

PLATFORM = "xiaohongshu"

__all__ = [
    "GENERAL_SEARCH_TYPE",
    "NOTE_DETAIL_PAGE_FUNCTION",
    "SEARCH_USER_PAGE_FUNCTION",
    "USER_SEARCH_TYPE",
    "CreatorCandidate",
    "NoteDetail",
    "fetch_note_detail",
    "fetch_search_candidates",
    "parse_detail_payload",
    "parse_search_payload",
    "render_detail_js",
    "render_search_js",
    "search_creators_by_name",
]

USER_SEARCH_TYPE = "51"
"""小红书搜索页的 `type=51` = 「用户」这一栏。V1 同一取值。"""

GENERAL_SEARCH_TYPE = "1"
"""`type=1` = 综合搜索。V1 的顺序是"用户页拿不到就退化到综合页"，照搬。"""

_SEARCH_PAGE = "https://www.xiaohongshu.com/search_result/"


# --------------------------------------------------------------------------- #
# 页面 JS：笔记详情
# --------------------------------------------------------------------------- #

# 一条笔记的正文、封面、指标、原图列表与视频直链都在这一步取。
# `__EXPECTED_NOTE_ID__` 走 json.dumps 注入，模板里**不带引号**（V1 §2 契约三）。
NOTE_DETAIL_PAGE_FUNCTION = r"""
async () => {
  const expected = __EXPECTED_NOTE_ID__;
  const rounds = __DETAIL_POLL_ROUNDS__;
  const settle = __DETAIL_POLL_MS__;
  const clean = (v) => (v === undefined || v === null ? "" : String(v).trim());
  const unwrap = (node) => {
    let out = node;
    for (let i = 0; i < 5 && out && typeof out === "object" && !Array.isArray(out); i++) {
      const next = (out._value !== undefined) ? out._value
        : (out.value !== undefined ? out.value
        : (out._rawValue !== undefined ? out._rawValue : null));
      if (next === null || next === undefined || next === out) break;
      out = next;
    }
    return (typeof out === "object") ? out : null;
  };
  const one = (sel) => clean((document.querySelector(sel) || {}).textContent);

  // noteDetailMap 是异步挂上去的：V1 假设 navigate 回来就一定有，于是
  // "页面还没渲染完" 与 "这条笔记真的没有内容" 都会走到 empty_note 那一条红。
  // 这里先轮询到点再判空 —— 判空仍然是判空，只是不再把竞态算进去。
  let detailMap = {};
  for (let i = 0; i < rounds; i++) {
    const state = unwrap(window.__INITIAL_STATE__) || {};
    const note = unwrap(state.note) || {};
    detailMap = unwrap(note.noteDetailMap) || {};
    if (Object.keys(detailMap).length) break;
    await new Promise((resolve) => setTimeout(resolve, settle));
  }
  const keys = Object.keys(detailMap);
  // 优先拿 expected 那一条：详情页会把"上一条/下一条"也塞进同一张 map，
  // 直接取 keys[0] 就是**串号**的来源。
  const key = (expected && detailMap[expected]) ? expected : (keys[0] || "");
  const entry = unwrap(detailMap[key]) || {};
  const note = unwrap(entry.note) || unwrap(entry.noteCard) || {};
  const author = unwrap(note.user) || {};
  const interact = unwrap(note.interactInfo) || unwrap(note.interact) || {};
  const rawImages = unwrap(note.imageList);
  const images = (Array.isArray(rawImages) ? rawImages : []).map((item) => {
    const node = unwrap(item) || {};
    return clean(node.urlDefault || node.url || node.traceId);
  }).filter(Boolean);
  const video = unwrap(note.video) || {};
  const stream = unwrap((unwrap(video.media) || {}).stream) || {};
  const h264 = Array.isArray(unwrap(stream.h264)) ? unwrap(stream.h264) : [];
  const h265 = Array.isArray(unwrap(stream.h265)) ? unwrap(stream.h265) : [];
  const pick = (list) => {
    // masterUrl 是"页面自己已经在放的那一条"：它带签名、几小时后失效，
    // 所以只用来落盘，绝不入库（与抖音那条播放直链同一判据）。
    for (const item of list) {
      const node = unwrap(item) || {};
      const url = clean(node.masterUrl || node.master_url || node.backupUrl);
      if (url) return { url: url, duration: node.duration || node.capa_duration || "" };
    }
    return { url: "", duration: "" };
  };
  const chosen = pick(h264.length ? h264 : h265);
  const body = document.body ? document.body.innerText : "";
  const rawTags = unwrap(note.tagList);
  const ogImage = document.querySelector('meta[property="og:image"]') || {};

  const result = {
    ok: true,
    note_id: clean(note.noteId || note.id || key || expected),
    title: clean(note.title || note.displayTitle)
      || one("#noteContent .title") || one(".note-content .title"),
    content: clean(note.desc || note.description)
      || one("#detail-desc") || one("#noteContent .desc"),
    author: clean(author.nickname || author.nickName)
      || one(".author-wrapper .username") || one("#author .username"),
    author_id: clean(author.userId || author.user_id),
    cover_url: clean(images[0] || (unwrap(note.cover) || {}).url) || clean(ogImage.content),
    images: images,
    video_url: chosen.url,
    duration_seconds: clean(chosen.duration || (unwrap(video.capa) || {}).duration
      || note.duration),
    published_at: clean(note.time || note.lastUpdateTime || note.publish_time),
    note_type: clean(note.type || note.typeV2),
    likes: clean(interact.likedCount || interact.like_count) || one(".like-wrapper .count"),
    collects: clean(interact.collectedCount || interact.collect_count)
      || one(".collect-wrapper .count"),
    comments: clean(interact.commentCount) || one(".chat-wrapper .count"),
    tags: (Array.isArray(rawTags) ? rawTags : [])
      .map((item) => clean((unwrap(item) || {}).name)).filter(Boolean),
    page_url: location.href
  };
  const BLOCKED_RE = /当前笔记暂时无法浏览|去小红书App操作|笔记不存在|扫码登录/;
  const blocked = BLOCKED_RE.test(body);
  if (!result.title && !result.content) {
    // 这一句区分的是"要去登录"与"这条笔记本身取不到"，两者的修法完全不同。
    return JSON.stringify({
      ok: false, error: blocked ? "login_wall_or_removed" : "empty_note",
      note_id: result.note_id, page_url: location.href
    });
  }
  if (expected && result.note_id !== expected) {
    return JSON.stringify({
      ok: false, error: "note_id_mismatch", expected: expected, actual: result.note_id
    });
  }
  return JSON.stringify(result);
}
"""


# --------------------------------------------------------------------------- #
# 页面 JS：按名搜主页（V1 `--creator-name` 那条兜底）
# --------------------------------------------------------------------------- #

SEARCH_USER_PAGE_FUNCTION = r"""
async () => {
  const rounds = __SEARCH_POLL_ROUNDS__;
  const settle = __SEARCH_POLL_MS__;
  const clean = (v) => (v === undefined || v === null ? "" : String(v).trim());
  const waitMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const PROFILE_RE = /xiaohongshu\.com\/user\/profile\/([0-9a-zA-Z]+)/;
  const candidates = new Map();
  const collect = () => {
    // 只认带 pc_search 的链接：搜索结果是异步挂上来的，而页头「我」的头像链接一进页就在，
    // 用「有没有 /user/profile/ 链接」当就绪信号会立刻通过、然后收下 0 个候选。
    const anchors = Array.from(document.querySelectorAll("a[href*='/user/profile/']"))
      .filter((a) => /xsec_source=pc_search/.test(a.href || ""));
    anchors.forEach((anchor) => {
      // 必须用 anchor.href：页面里的 href 属性是相对路径（/user/profile/xxx），
      // 带域名的正则在那条上一条都匹配不到，于是「搜到 0 个主页」。
      const href = anchor.href || anchor.getAttribute("href") || "";
      const match = href.match(PROFILE_RE);
      if (!match) return;
      const userId = match[1];
      if (candidates.has(userId)) return;
      const card = anchor.closest(".user-card, .search-user-card, .onebox, li, .user-info")
        || anchor.parentElement;
      const cardText = clean(anchor.innerText) || clean(card && card.innerText);
      const nameNode = card && card.querySelector(".title, .name, .user-name");
      const fansNode = card && card.querySelector(".fans, .user-fans, .count");
      const image = card && card.querySelector("img");
      // 搜索卡片是一整块文本：「影视飓风 / 2天前更新 / 小红书号：550242123 /
      // 粉丝・217.5万 / 笔记・5」，第一行才是昵称，后面几行都得切掉，
      // 否则按名认领永远对不上。
      let name = clean(nameNode && nameNode.textContent) || clean(cardText.split("\n")[0]);
      name = name.split("小红书号")[0].split("粉丝")[0].split("获赞")[0].split("天前")[0].trim();
      const fansMatch = cardText.match(/粉丝[・:：]*\s*([\d.,]+[万亿]?)/) || [];
      candidates.set(userId, {
        user_id: userId,
        name: name,
        follower_count: clean(fansNode && fansNode.textContent) || clean(fansMatch[1]),
        avatar_url: clean(image && (image.currentSrc || image.src)),
        homepage_url: href
      });
    });
  };
  for (let round = 0; round < rounds; round++) {
    collect();
    if (candidates.size) break;
    await waitMs(settle);
  }
  if (!candidates.size) {
    // 万一以后搜索链接不再带 xsec_source，退回旧的宽松口径：
    // 宁可多收候选，也别直接空手而归。（宽松那一侧只少两个字段，不会认错人。）
    document.querySelectorAll("a[href*='/user/profile/']").forEach((anchor) => {
      const href = anchor.href || anchor.getAttribute("href") || "";
      const match = href.match(PROFILE_RE);
      if (match && !candidates.has(match[1])) {
        candidates.set(match[1], {
          user_id: match[1],
          name: clean(anchor.innerText).split("\n")[0],
          homepage_url: href
        });
      }
    });
  }
  const BLOCKED_RE = /登录后查看搜索结果|登录后即可查看|扫码登录|安全检测/;
  const body = document.body ? document.body.innerText : "";
  return JSON.stringify({
    candidates: Array.from(candidates.values()),
    title: clean(document.title),
    login_wall: BLOCKED_RE.test(body) && candidates.size === 0,
    page_url: location.href
  });
}
"""


def render_detail_js(*, note_id: str, budget: PageBudget) -> str:
    """详情 JS 占位符注入：值走 `json.dumps`，模板里不能带引号。

    与 `listing.render_page_js()` 共用同一个渲染器，所以**残留检查也共用** ——
    漏替换的 `__EXPECTED_NOTE_ID__` 会以 `ReferenceError` 出现在桥那一头，
    而 Python 侧只看到"evaluate 失败"，排查的人会先怀疑"小红书改版了"。
    """
    return render_page_js(NOTE_DETAIL_PAGE_FUNCTION, budget=budget, note_id=note_id)


def render_search_js(*, budget: PageBudget) -> str:
    """搜索页 JS 注入。没有身份占位符，只有轮询预算那两位。"""
    return render_page_js(SEARCH_USER_PAGE_FUNCTION, budget=budget)


@dataclass(frozen=True)
class NoteDetail:
    """一条笔记的详情页产出。**`xsec_token` 不在这里** —— 它是入参，不是页面给的东西。

    字段可空性照 ADR-0016：`published_at` 要么 None 要么 aware datetime；
    指标是 `int | None`（None = 页面没写，不是 0）。
    """

    note_id: str
    title: str = ""
    content: str = ""
    author: str = ""
    author_id: str = ""
    cover_url: str = ""
    images: tuple[str, ...] = ()
    video_url: str = ""
    duration_seconds: float | None = None
    published_at: datetime | None = None
    note_type: str = ""
    likes: str = ""
    collects: str = ""
    comments: str = ""
    tags: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        """页面给了可直连的视频地址。**判据是 `video_url`，不是 `note_type`**：
        `type` 在现网有 `video` / `normal` 之外的取值（改版过渡期见过 `""`），
        而"能不能下"只取决于有没有那条 masterUrl。
        """
        return bool(self.video_url)

    @property
    def is_image_note(self) -> bool:
        """图文笔记 = 没有视频直链但有原图。**不是失败**（ADR-0019）。"""
        return not self.has_video and bool(self.images)

    @property
    def like_count(self) -> int | None:
        return parse_cn_count(self.likes)

    @property
    def collect_count(self) -> int | None:
        return parse_cn_count(self.collects)

    @property
    def comment_count(self) -> int | None:
        return parse_cn_count(self.comments)


def _number_or_none(value: object) -> float | None:
    """页面上的时长（"59" / "59.9"）→ float。认不出返回 None，不返回 0。"""
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _string_tuple(value: object) -> tuple[str, ...]:
    """页面给的字符串数组 → 去掉空串的元组。形状不对就当没有（不是当空数组报错）。

    这里不抛是有理由的：`images` 与 `tags` 一个是媒体来源、一个是装饰字段，
    形状漂了（改版把它包了一层对象）应该让**媒体那一步**去如实失败，
    而不是让"读详情"这一步在一条本来有正文有标题的笔记上整体红掉。
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(text for item in value if (text := str(item or "").strip()))


def parse_detail_payload(payload: Mapping[str, Any], *, expected_note_id: str) -> NoteDetail:
    """详情那份 JS 的产出 → `NoteDetail`。

    **两种"这条不要"必须分开，因为动作不同**：

    - `note_id_mismatch` —— 被重定向到了**别的笔记**（V1 :971 那条"串号就整条丢弃"）。
      绝不能退回去用页面自报的 ID 继续：那会把别人的正文与图片记成这条笔记的，
      而库里两条 ID 相同的作品会互相覆盖。
    - `login_wall_or_removed` / `empty_note` —— 前者是登录态或风控（去扫码），
      后者是这条笔记本身没内容（被删 / 审核中 / 只有一张图且没渲染出来）。

    `published_at` 两条来路：页面给的（毫秒时间戳或裸日期串）优先，
    给不出再退到 `note_id` 前 8 位十六进制 —— 与 V1 `fetch_note_detail` 同一顺序。
    """
    if payload.get("ok") is not True:
        error = str(payload.get("error") or "unknown")
        if error == "note_id_mismatch":
            msg = (
                f"笔记串号（预期 {expected_note_id}，实际 {payload.get('actual')}），"
                f"已整条丢弃：被重定向到了别的笔记"
            )
            raise PlatformError(PLATFORM, "media", msg)
        page_url = str(payload.get("page_url") or "")
        # 「去扫码」与「这条笔记本身取不到」的修法不同，原文必须分得开。
        hint = "（可能是缺 xsec_token 被站内跳去风控页，或登录态过期）" * (
            error == "login_wall_or_removed"
        )
        stopped = f"（停在 {page_url}）" if page_url else ""
        msg = f"笔记详情失败（{expected_note_id}）: {error}{hint}{stopped}"
        raise PlatformError(PLATFORM, "media", msg)

    claimed = str(payload.get("note_id") or "").strip()
    if claimed and claimed != expected_note_id:
        # 页函数自己已经挡了这一层；走到这里说明页面绕过了那道判断。
        msg = f"笔记串号（预期 {expected_note_id}，页面自报 {claimed}），已整条丢弃"
        raise PlatformError(PLATFORM, "media", msg)

    images = payload.get("images")
    tags = payload.get("tags")
    return NoteDetail(
        note_id=expected_note_id,
        title=str(payload.get("title") or "").strip(),
        content=str(payload.get("content") or "").strip(),
        author=str(payload.get("author") or "").strip(),
        author_id=str(payload.get("author_id") or "").strip(),
        cover_url=str(payload.get("cover_url") or "").strip(),
        images=tuple(str(item).strip() for item in images if str(item).strip())
        if isinstance(images, Sequence) and not isinstance(images, str)
        else (),
        video_url=str(payload.get("video_url") or "").strip(),
        duration_seconds=_number_or_none(payload.get("duration_seconds")),
        published_at=parse_publish_time(payload.get("published_at"))
        or published_at_from_note_id(expected_note_id),
        note_type=str(payload.get("note_type") or "").strip(),
        likes=str(payload.get("likes") or "").strip(),
        collects=str(payload.get("collects") or "").strip(),
        comments=str(payload.get("comments") or "").strip(),
        tags=tuple(str(item).strip() for item in tags if str(item).strip())
        if isinstance(tags, Sequence) and not isinstance(tags, str)
        else (),
        raw=payload,
    )


async def fetch_note_detail(
    bridge: BridgeClient, *, note_id: str, xsec_token: str = "", budget: PageBudget
) -> NoteDetail:
    """打开详情页取回一条笔记的全部内容字段。失败抛 `PlatformError(stage="media")`。

    **桥的调用形状**：`BridgeClient.evaluate(js)` 只发表达式，所以 V1 那套
    `/evaluate {url, expression}` 必须拆成 `navigate()` → `evaluate()` 两步
    （`douyin/adapter.py` 是现成例子）。少那一次 navigate 会得到什么：
    表达式在**上一次停留的那个页面**里执行 —— 于是上一条笔记的 `noteDetailMap`
    还在，`keys[0]` 拿到的就是别人的笔记，而页面自报的 `note_id` 恰好也对得上那条。
    这是本文件最贵的一种错，因为它产出的每一行数据都"看起来自洽"。

    `xsec_token` 只在这一步用来拼详情链接，**不进任何返回值、不入库**（ADR-0016）。
    """
    if not note_id:
        msg = "卡片缺少 note_id，问不了详情"
        raise PlatformError(PLATFORM, "media", msg)
    await bridge.navigate(build_note_url(note_id, xsec_token))
    payload = decode_page_result(
        await bridge.evaluate(render_detail_js(note_id=note_id, budget=budget)), stage="media"
    )
    return parse_detail_payload(payload, expected_note_id=note_id)


@dataclass(frozen=True)
class CreatorCandidate:
    """站内搜索的一个候选主页。"""

    user_id: str
    name: str = ""
    follower_count: int | None = None
    avatar_url: str | None = None
    homepage_url: str = ""


def parse_search_payload(payload: Mapping[str, Any]) -> tuple[CreatorCandidate, ...]:
    """搜索页那份 JS 的产出 → 候选列表，按 user_id 去重、保持页面顺序。

    **登录墙与"查无此人"必须可区分**（V1 :902 那段注释是这条判据的原文）：
    搜索页要求登录时返回的是**空结果**而不是错误，不区分会把"没登录"报成
    "查无此人"，让人去改博主名字，而真正要做的只有去桥里扫码。
    所以这里不抛，只把两件事分开交出去（`login_wall` / 空元组），由
    `search_creators_by_name()` 决定红成什么样。
    """
    items = payload.get("candidates")
    if not isinstance(items, list):
        return ()
    seen: set[str] = set()
    out: list[CreatorCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        out.append(
            CreatorCandidate(
                user_id=user_id,
                name=str(item.get("name") or "").strip(),
                follower_count=parse_cn_count(item.get("follower_count")),
                avatar_url=str(item.get("avatar_url") or "").strip() or None,
                homepage_url=str(item.get("homepage_url") or "").strip(),
            )
        )
    return tuple(out)


def _login_wall(payload: Mapping[str, Any]) -> bool:
    return payload.get("login_wall") is True


async def fetch_search_candidates(
    bridge: BridgeClient, *, keyword: str, budget: PageBudget
) -> tuple[tuple[CreatorCandidate, ...], bool, str | None]:
    """跑一次站内搜索。返回 `(候选, 撞没撞登录墙, 这一趟的失败原文)`。

    与 V1 同一顺序：先「用户」栏（`type=51`），空则退化到综合搜索页（`type=1`）。
    **单栏失败不掀整趟**：综合页值得一试，用户页炸了不该让按名搜直接放弃 ——
    但那一趟的原文要留着，两栏都没结果时它就是报错的正文。
    """

    js = render_search_js(budget=budget)
    login_wall = False
    last_error: str | None = None
    for search_type in (USER_SEARCH_TYPE, GENERAL_SEARCH_TYPE):
        url = f"https://www.xiaohongshu.com/search_result/?keyword={quote(keyword)}&type={search_type}"
        try:
            await bridge.navigate(url)
            payload = decode_page_result(await bridge.evaluate(js), stage="profile")
        except (PlatformError, ValueError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        login_wall = login_wall or _login_wall(payload)
        candidates = parse_search_payload(payload)
        if candidates:
            return candidates, login_wall, None
    return (), login_wall, last_error


async def search_creators_by_name(
    bridge: BridgeClient, *, keyword: str, budget: PageBudget
) -> list[CreatorProfile]:
    """按昵称搜主页（V1 `--creator-name` 那条兜底），返回排好序的候选博主资料。

    三种结果各自的形状，全在报错文案里：

    - 有候选 → 交回 `CreatorProfile` 列表（页面顺序 = 小红书自己的相关性顺序，
      **我们不在这里重排**：拿粉丝数排会把"同名但不同人"的号顶到第一位，
      而调用方要的是"最像的那一个"）。
    - 空 + 登录墙 → 红，正文写"先在桥里扫码"，并给出另一条路（直接传主页链接）。
    - 空 + 没登录墙 → 红，正文写"查无此人"。这不是"没采到"，是**名字不对**。
    """
    text = str(keyword or "").strip()
    if not text:
        msg = "按名搜索要的是昵称，给的是空串"
        raise PlatformError(PLATFORM, "profile", msg)
    candidates, login_wall, last_error = await fetch_search_candidates(
        bridge, keyword=text, budget=budget
    )
    if not candidates:
        if login_wall:
            msg = (
                f"小红书搜索页返回登录墙，「{text}」这一趟没有可比对的结果："
                f"先在桥里扫码登录一次（{bridge.base_url} 起的那个 Chrome），"
                f"或直接给主页链接"
            )
            raise PlatformError(PLATFORM, "profile", msg)
        if last_error is not None:
            msg = f"按名搜索主页失败（{text}）：{last_error}"
            raise PlatformError(PLATFORM, "profile", msg)
        msg = (
            f"按名搜索没有任何候选（{text}）：这个名字在小红书上搜不到 —— "
            f"这与「未登录」是两件事，扫码解决不了"
        )
        raise PlatformError(PLATFORM, "profile", msg)

    out: list[CreatorProfile] = []
    for candidate in candidates:
        ref = CreatorRef(
            platform=PLATFORM,
            platform_id=candidate.user_id,
            # 入库的一律是 `build_profile_url(user_id)`。搜索页那条 `homepage_url`
            # 带着 `xsec_token` 与 `xsec_source=pc_search`，那是门票不是身份
            # （ADR-0016）：存进去会让同一位博主被"搜索页链接"与"主页链接"认成两个人
            # ——V1 §7.1 在抖音上踩过的那一条，只是换了个域名。
            profile_url=require_http_url(
                build_profile_url(candidate.user_id),
                context=f"博主 {candidate.user_id} 的主页",
            ),
        )
        out.append(
            CreatorProfile(
                ref=ref,
                name=candidate.name or candidate.user_id,
                avatar_url=absolute_http_url(candidate.avatar_url),
                follower_count=candidate.follower_count,
                extra={
                    "source": "name_search",
                    "nickname_is_placeholder": not candidate.name,
                },
            )
        )
    return out
