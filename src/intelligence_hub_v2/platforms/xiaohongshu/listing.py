"""小红书的页面 JS 与结果解析（主页 / 滚动补全）。

V1 源：`download_xiaohongshu_latest.py` 的 `PROFILE_PAGE_FUNCTION`（:539）、
`COLLECT_AFTER_SCROLL_FUNCTION`（:659）、`merge_note_cards`（:231）、
`parse_metric_number`（:141）、`note_sort_key` / `sort_notes`（:156-176）、
`PLACEHOLDER_NAMES`（:81）。笔记详情与站内搜索那两份在 `detail.py`，理由写在那文件顶上。

四条纪律在这里落地，前两条与抖音同源，后两条是小红书独有的：

1. **占位符不加引号**（V1 §2 契约三）。Python 侧注入的是 `json.dumps(value)`，
   它自己带引号；模板里再写 `"__X__"` 就拼成 `""5fb…""`，页面当场 SyntaxError，
   而报错在桥的那一头，Python 侧只看到一个"evaluate 失败"。
   `render_page_js()` 替换完会检查**没有残留占位符**，漏一个就抛 ——
   V1 的做法是在四份模板的调用点各手写一串 `.replace()`，忘了就得到一个带着
   `__WANTED__` 的 JS 打到页面上（变成 `ReferenceError`，方向被带到
   "小红书改版了 / 被风控了"上，那是最贵的一类误判）。
2. **只认页面自己给的身份**。主页 JS 先比对 `user_id`，不一致就整页丢弃
   （`user_id_mismatch`）。串页时**不许退回去用页面自报的 user_id**：那等于把 B 的笔记
   登记给 A，而库里已有的 A 那一行会被刷成 B 的昵称 —— 数据坏了还不报错。
3. **`userPageData.basicInfo` 才是博主，`user.userInfo` 是「当前登录的人」**
   （V1 :554 的注释）。这条要命的区分是实测换来的：拿 `userInfo` 当博主，昵称会变成
   你自己的号，而 uid 校验会把**每一个正常主页**都判成串号跳过 —— 全程绿灯、零条作品。
4. **DOM 卡片与 `__INITIAL_STATE__` 卡片各有一半信息，必须按字段互补合并**。
   页面上的 `<a>` 有标题/封面/点赞数；状态树里有 `type`（video/normal）与 token，
   而现网把 token 挂在外层包装上、`noteCard` 里只剩标题（V1 :624）。
   只翻一层的话 token 全丢，而**缺 token 的详情链接会被站内跳去风控页，
   等于一条笔记都取不到**。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import HttpUrl

from intelligence_hub_v2.errors import ListError, PlatformError
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.urls import absolute_http_url, parse_cn_count
from intelligence_hub_v2.platforms.xiaohongshu.urls import (
    NOTE_ID_PATTERN,
    build_note_url,
    published_at_from_note_id,
)

PLATFORM = "xiaohongshu"

__all__ = [
    "CARD_HELPERS",
    "COLLECT_AFTER_SCROLL_FUNCTION",
    "IMAGE_HELPERS",
    "NOTE_TYPE_IMAGE",
    "NOTE_TYPE_VIDEO",
    "PLACEHOLDER_NAMES",
    "PROFILE_PAGE_FUNCTION",
    "NoteCard",
    "PageBudget",
    "ParsedProfile",
    "card_to_video_meta",
    "decode_page_result",
    "find_placeholders",
    "is_image_note",
    "is_placeholder_nickname",
    "merge_note_cards",
    "parse_cards_payload",
    "parse_profile_payload",
    "render_page_js",
    "require_http_url",
    "sort_cards",
]

PLACEHOLDER_NAMES = frozenset({"", "小红书创作者", "小红书用户", "用户"})
"""页面没给出真昵称时的几种退化值（V1 的 `PLACEHOLDER_NAMES` 原样）。

V1 §7.24 的另一半：昵称会进目录名与入库作者字段，而**占位昵称不该覆盖库里已有的真名字**。
与抖音同一处置：只作为 `CreatorProfile.extra["nickname_is_placeholder"]` 交出去，
由入库层决定要不要覆盖。
"""

NOTE_TYPE_VIDEO = "video"
NOTE_TYPE_IMAGE = "normal"
"""`__INITIAL_STATE__` 里 `note.type` 的两个真实取值（V1 详情 JS `note.type || note.typeV2`）。

**只有 `NOTE_TYPE_IMAGE` 这一个取值能判"图文"**：DOM 那条来路认不出类型时交回空串，
而空串既可能是图文也可能是"页面没渲染出来"。所以 `include_image_notes=False`
只丢明确标 `normal` 的那批 —— 见 `is_image_note()`。
"""

_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")
"""占位符的标记形状，与抖音一致（模板里**不带引号**，注入值自带引号）。

`PAGE_OWN_DUNDER_NAMES` 是这道检查唯一的豁免名单，理由不是风格：
小红书这四份模板都要读 `window.__INITIAL_STATE__`，而那个名字**长得就是一个
没替换的占位符**（`__` + 全大写 + `__`）。照抄抖音的正则会让每一份正常模板
都在渲染阶段误报，于是这道闸迟早被人关掉或改成 `if "INITIAL" not in name`
那种一次性特例 —— 名单化、点名、并在这里写清"我们从不注入它"才是稳的。
"""

PAGE_OWN_DUNDER_NAMES = frozenset({"__INITIAL_STATE__"})
"""页面上的东西，不是我们的占位符。加新条目之前先问：这个名字我们真的会注入吗？

`__INITIAL_STATE__` 长得就是一个没替换的占位符（`__` + 全大写 + `__`），
照抄抖音那份检查会让**每一份正常模板**都在渲染阶段误报，
于是这道闸迟早被人关掉，或改成 `if "INITIAL" not in name` 那种看不见理由的一次性特例。
名单化 + 在这里写明"我们从不注入它"才是稳的。
"""
_USER_ID_SHAPE = re.compile(r"^[0-9a-zA-Z]{8,40}$")
"""昵称位上出现"看起来就是个 user_id"也算占位名。

这条判据的形状与 V1 的 `extract_user_id` 最后那条兜底一致（"没有域名且 8–40 位
字母数字"就是裸 ID），所以复用同一个正则形状，不在这里另发明一套长度。
"""


@dataclass(frozen=True)
class PageBudget:
    """页面 JS 的轮询/滚动预算。

    放在一个 dataclass 里而不是散成模块常量，是因为**测试必须能改小它**：
    默认值是 V1 实测的（`MAX_SCROLL_ROUNDS=10` / `SCROLL_SETTLE_SECONDS=0.9` /
    搜索页 12×700ms），一份模板跑满就是七到十秒；用例里等十秒会把契约测试变成集成测试。
    """

    max_scroll_rounds: int = 10
    """主页边滚边收的最多轮数（V1 `MAX_SCROLL_ROUNDS`）。"""

    settle_ms: int = 900
    """每滚一屏之后给页面的加载时间（V1 `SCROLL_SETTLE_SECONDS` = 0.9 秒）。"""

    search_poll_rounds: int = 12
    """搜索页等候选出现的轮询次数（V1 `SEARCH_USER_PAGE_FUNCTION` 里写死的 12）。"""

    search_poll_ms: int = 700
    """搜索页每轮询一次的间隔（V1 同一处写死的 700）。"""

    detail_poll_rounds: int = 10
    """详情页等 `__INITIAL_STATE__.note.noteDetailMap` 出现的轮询次数。

    V1 没有这一轮：它假设 `navigate` 回来之后状态树已经挂好。桥那边确实等的是
    `domcontentloaded`，而 `__INITIAL_STATE__` 是服务端内联的，所以通常真的在 ——
    这一轮是**保险**，代价只在"页面确实没给"的那几种情况下才付（风控中间页）。
    """

    detail_poll_ms: int = 600
    """详情页每轮一次的间隔。"""

    def placeholder_values(
        self, *, user_id: str = "", note_id: str = "", wanted: int | None = None
    ) -> dict[str, str]:
        """占位符 → 注入文本。**四份模板共用这一张表**。

        `__EXPECTED_*__` 走 `json.dumps`（值带引号，模板里不带），其余是页面上当数字用的，
        注入十进制字面量。某份模板用不到的键在这里多 replace 一次是无害的；
        反过来（模板里有、这里没有）会被 `render_page_js()` 的残留检查当场抓住。
        """
        return {
            "__EXPECTED_USER_ID__": json.dumps(user_id),
            "__EXPECTED_NOTE_ID__": json.dumps(note_id),
            "__WANTED__": str(wanted if wanted is not None else 0),
            "__MAX_SCROLL_ROUNDS__": str(self.max_scroll_rounds),
            "__SETTLE_MS__": str(self.settle_ms),
            "__SEARCH_POLL_ROUNDS__": str(self.search_poll_rounds),
            "__SEARCH_POLL_MS__": str(self.search_poll_ms),
            "__DETAIL_POLL_ROUNDS__": str(self.detail_poll_rounds),
            "__DETAIL_POLL_MS__": str(self.detail_poll_ms),
        }


# 四份页面 JS 共用的那三个小工具。`unwrap()` 是这一族里唯一"只有小红书才需要"的东西：
# 小红书的前端是 Vue 3，`__INITIAL_STATE__` 上到处是 ref / reactive 包装
# （`_value` / `value` / `_rawValue`），不解开就什么都读不到 ——
# 而"解不开"的表现不是报错，是"这个博主一条笔记都没有"。
# 循环上限 5 层是 V1 实测够用的深度。
IMAGE_HELPERS = r"""
  const waitMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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
"""

# 主页卡片的两条来路：DOM（`<a>` + 卡片文本）与 `__INITIAL_STATE__.user.notes`（状态树）。
# `seen` + `put()` 就是纪律 4 的落点：同一条笔记按字段互补，缺的不覆盖已有的。
CARD_HELPERS = (
    IMAGE_HELPERS
    + r"""
  const seen = new Map();
  const put = (note) => {
    if (!note || !note.id) return;
    const existing = seen.get(note.id);
    if (!existing) { seen.set(note.id, note); return; }
    for (const field of Object.keys(note)) {
      if (!clean(existing[field]) && clean(note[field])) existing[field] = note[field];
    }
  };
  // 落地页的两条形状。`user/profile/<uid>/<note_id>` 必须排在前面并**吃掉 uid 那一段**，
  // 否则会把博主 ID 当成笔记 ID 认下来（`urls._NOTE_PATH_PATTERN` 同一条判据）。
  const NOTE_PATH_RES = [
    /xiaohongshu\.com\/user\/profile\/([0-9a-zA-Z]+)\/([0-9a-zA-Z]+)/,
    /xiaohongshu\.com\/(?:explore|item|discovery\/entry|search_result)\/([0-9a-zA-Z]+)/
  ];
  const cardId = (href) => {
    const text = String(href || "");
    for (const re of NOTE_PATH_RES) {
      const m = text.match(re);
      if (m) return m[m.length - 1];
    }
    return "";
  };
  const tokenOf = (href) => {
    const m = String(href || "").match(/[?&]xsec_token=([^&]+)/) || [];
    return decodeURIComponent(m[1] || "");
  };
  const ANCHOR_SELECTOR = "a[href*='/user/profile/'], a[href*='/explore/'], a[href*='/item/']";
  const VIDEO_MARK = "video, .play-icon, .video-tag-icon, .playoff-icon";
  const fromAnchor = (anchor) => {
    // 必须用 anchor.href 而不是 getAttribute("href")：页面里那个属性是相对路径
    // （/user/profile/xxx），带域名的正则在上面那条上一条都匹配不到 ——
    // 实测结果是"0 条卡片"，而 0 条卡片长得和"这个博主没发过笔记"一模一样。
    const href = anchor.href || anchor.getAttribute("href") || "";
    const id = cardId(href);
    if (!id) return null;
    const card = anchor.closest("section.note-item, .note-item, li") || anchor.parentElement;
    const image = card && card.querySelector("img");
    const titleNode = card
      && (card.querySelector(".footer .title") || card.querySelector(".title"));
    const likeNode = card && card.querySelector(".like-wrapper .count, .count");
    const isVideo = !!(card && card.querySelector(VIDEO_MARK));
    return {
      id: id,
      xsec_token: tokenOf(href),
      title: clean(titleNode && titleNode.textContent)
        || clean(image && (image.alt || image.getAttribute("aria-label"))),
      cover_url: clean(image && (image.currentSrc || image.src)),
      likes: clean(likeNode && likeNode.textContent),
      note_type: isVideo ? "video" : ""
    };
  };
  const harvestAnchors = () => {
    document.querySelectorAll(ANCHOR_SELECTOR).forEach((a) => put(fromAnchor(a)));
  };
  // 状态树那条来路只翻 `user.notes`（当前主页那位博主），不翻整个 __INITIAL_STATE__。
  // 整页扫会在侧栏「相关推荐」渲染出来之后把**别人**的笔记算到本博主头上，而且报成功。
  const walkNotes = (notes) => {
    const list = unwrap(notes);
    if (!Array.isArray(list)) return;
    list.forEach((group) => {
      const rows = Array.isArray(unwrap(group)) ? unwrap(group) : [group];
      rows.forEach((item) => {
        const node = unwrap(item) || {};
        const card = unwrap(node.noteCard) || unwrap(node.note_card) || node;
        const id = clean(card.note_id || card.id || card.noteId
          || node.note_id || node.id || node.noteId);
        // 现网把 xsecToken 挂在外层包装上、noteCard 里只剩标题和指标：
        // 只翻一层的话 token 会全丢，而缺 token 的详情链接会被站内跳去风控页。
        const token = clean(card.xsec_token || card.xsecToken || node.xsec_token || node.xsecToken);
        const type = clean(card.type || card.typeV2);
        if (!id || !(token || card.title || card.displayTitle)) return;
        const imageList = unwrap(card.imageList);
        const first = (Array.isArray(imageList) ? unwrap(imageList[0]) : null) || {};
        put({
          id: id,
          xsec_token: token,
          title: clean(card.title || card.displayTitle),
          cover_url: clean((unwrap(card.cover) || {}).url || first.urlDefault),
          likes: clean((unwrap(card.interactInfo) || {}).likedCount),
          note_type: type === "video" ? "video" : (type === "normal" ? "normal" : "")
        });
      });
    });
  };
  const harvestBoth = () => {
    const state = unwrap(window.__INITIAL_STATE__) || {};
    harvestAnchors();
    walkNotes((unwrap(state.user) || {}).notes);
  };
"""
)

# 在博主主页执行：昵称 / 头像 / 粉丝数 + 首屏笔记卡片。
# `__EXPECTED_USER_ID__` 由 render_page_js() 用 json.dumps(user_id) 替换（模板里不带引号）。
PROFILE_PAGE_FUNCTION = (
    """
async () => {
  const expected = __EXPECTED_USER_ID__;
"""
    + CARD_HELPERS
    + r"""
  const state = unwrap(window.__INITIAL_STATE__) || {};
  const user = unwrap(state.user) || {};
  // 博主身份只能从页面本身认：userPageData.basicInfo 是主页主人，
  // user.userInfo 是**当前登录的人**。拿错那一份的后果：昵称变成自己的号，
  // 而 uid 校验会把每一个正常主页都判成串号跳过 —— 全程绿灯、零条作品。
  const page = unwrap(user.userPageData) || {};
  const basic = unwrap(page.basicInfo) || {};
  const OG_URL_RE = /\/user\/profile\/([0-9a-zA-Z]+)/;
  const ogMeta = document.querySelector('meta[property="og:url"]') || {};
  const ogUrl = clean(ogMeta.content) || location.pathname;
  const userId = clean(basic.userId || basic.user_id)
    || clean((ogUrl.match(OG_URL_RE) || [])[1]);
  if (expected && userId && userId !== expected) {
    return JSON.stringify({
      ok: false, error: "user_id_mismatch", expected: expected, actual: userId
    });
  }

  const textOf = (sel) => {
    const node = document.querySelector(sel);
    return clean(node && node.textContent);
  };
  const nickname = clean(basic.nickname)
    || textOf("#user-info .user-name")
    || clean(document.title).split("的小红书")[0].split(" - ")[0];
  const avatar = clean(basic.images || basic.image || basic.avatar
    || (unwrap(basic.imageb) || {}).url);
  let fans = "";
  const interactions = unwrap(page.interactions);
  for (const item of (Array.isArray(interactions) ? interactions : [])) {
    const node = unwrap(item) || {};
    if (clean(node.type) === "fans") { fans = clean(node.count); break; }
  }
  if (!fans) {
    // 状态树没给（或被改版挪走）时的 DOM 兜底：那一排是「label + 数值」两列，
    // 只能按 label 文本找到"粉丝"那一格，不能按位置取（位置会随平台加 tab 而漂）。
    const rows = document.querySelectorAll("#user-info .user-interactions .total-count");
    const labels = document.querySelectorAll("#user-info .user-interactions .tab-header");
    for (let i = 0; i < rows.length; i++) {
      const label = clean(labels[i] && labels[i].textContent);
      if (label.indexOf("粉丝") >= 0) { fans = clean(rows[i].textContent); break; }
    }
  }

  harvestBoth();

  const body = document.body ? document.body.innerText : "";
  const EMPTY = seen.size === 0;
  const LOGIN_WALL_RE = /登录后享用更多|扫码登录|安全检测|当前笔记暂时无法浏览/;
  const NOT_FOUND_RE = /用户不存在|该用户已注销|暂时无法访问/;
  const blocked = LOGIN_WALL_RE.test(body);
  // 「登录墙」与「查无此人」必须分开：前者要人去桥里扫码，后者要人改链接。
  // 合成一句"没采到作品"就是 V1 §1.3 点名的形状 —— 看起来在跑，其实是两件不同的事。
  return JSON.stringify({
    ok: true,
    user_id: userId || expected,
    nickname: nickname,
    avatar_url: avatar,
    follower_count: clean(fans).replace(/[+,]/g, ""),
    notes: Array.from(seen.values()),
    login_wall: EMPTY && blocked,
    user_not_found: EMPTY && !blocked && NOT_FOUND_RE.test(body),
    page_url: location.href
  });
}
"""
)

# 主页只渲染视口内的卡片，边滚边收才凑得够 videos_per_creator / 爆款回溯要的样本量。
# 卡片来路与主页那份完全一样（DOM + 状态树），所以共用 CARD_HELPERS。
COLLECT_AFTER_SCROLL_FUNCTION = (
    """
async () => {
  const wanted = __WANTED__;
  const rounds = __MAX_SCROLL_ROUNDS__;
  const settle = __SETTLE_MS__;
"""
    + CARD_HELPERS
    + """
  for (let i = 0; i < rounds && seen.size < wanted; i++) {
    harvestBoth();
    window.scrollBy(0, window.innerHeight * 0.9);
    await waitMs(settle);
  }
  harvestBoth();
  window.scrollTo(0, 0);
  return JSON.stringify({ notes: Array.from(seen.values()) });
}
"""
)


def render_page_js(
    template: str,
    *,
    budget: PageBudget,
    user_id: str = "",
    note_id: str = "",
    wanted: int | None = None,
) -> str:
    """把页面 JS 模板里的占位符换成实值，并**确认一个都没剩**。

    残留检查是这整个函数存在的理由：V1 在四份模板的调用点各手写一串 `.replace()`，
    漏掉的那个占位符会原样进页面 —— 症状是页面 JS 抛
    `ReferenceError: __WANTED__ is not defined`，而排查的人会先怀疑
    "小红书改版了 / 被风控了"。真机上 Python 侧唯一能看到的一行是"evaluate 失败"，
    两边之间没有任何线索，所以这一道闸必须扣在**渲染**这一步。
    """
    rendered = template
    values = budget.placeholder_values(user_id=user_id, note_id=note_id, wanted=wanted)
    for key, value in values.items():
        rendered = rendered.replace(key, value)
    leftover = sorted(find_placeholders(rendered))
    if leftover:
        msg = f"页面 JS 里还剩未替换的占位符：{', '.join(leftover)}（模板加了新占位符？）"
        raise ValueError(msg)
    return rendered


def decode_page_result(result: object, *, stage: str) -> dict[str, Any]:
    """把桥的 `evaluate` 返回收成 dict。

    页函数返回的是 `JSON.stringify(...)`，所以桥给回来的通常是**字符串**；
    但桥自己会把已经是 JSON 的东西解一层，某些路径下直接给 dict。
    两种都认，认不出就把原文截一段交出去 —— "页面未返回可解析结果"后面
    没有原文，等于让人去重放一遍才能看到到底回了什么。

    这一份与 `douyin/listing.py::decode_page_result` 是**两份**，不是没提上公共层：
    ADR-0016 那条"第三个平台进来就该提"的判据在这里还不成立 ——
    四平台里只有抖音与小红书走桥（B站 是公开接口、YouTube 是纯 yt-dlp），
    所以两份就是终态。等第五个走桥的平台进来再提。
    """
    payload: Any = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            msg = f"页面返回的不是合法 JSON（{exc}）：{payload[:200]!r}"
            raise ListError(PLATFORM, stage, msg) from exc
    if not isinstance(payload, dict):
        msg = f"页面未返回可解析结果（{type(payload).__name__}）：{str(payload)[:200]!r}"
        raise ListError(PLATFORM, stage, msg)
    return payload


def is_placeholder_nickname(name: object) -> bool:
    """空名、平台默认名、裸 user_id、整条 URL 都算占位名。

    后两条是 V1 §7.1 的形状：历史脏行把分享短链或 ID 写进了昵称位。
    昵称位上出现 `http…` 一定不是人起的名字，让它覆盖真昵称就是把脏数据固化。
    """
    text = str(name or "").strip()
    if not text or text in PLACEHOLDER_NAMES:
        return True
    if bool(_USER_ID_SHAPE.match(text)):
        return True
    return text.lower().startswith(("http://", "https://"))


def is_image_note(note_type: object) -> bool:
    """页面**明确说**这是图文笔记才算。空串（DOM 那条来路认不出类型）返回 False。

    这个不对称是 `include_image_notes=False` 唯一安全的读法：
    把"判不出"当成"图文"会整批丢掉视频笔记，当成"不是图文"只会多收几条图文 ——
    两者都静默，但后者的代价是磁盘，前者的代价是内容。
    """
    return str(note_type or "").strip() == NOTE_TYPE_IMAGE


def _page_error(stage: str, message: str) -> PlatformError:
    """`ListError` 与 `PlatformError` 是同一族，只差"这次失败发生在枚举里"。

    分开是因为上层的处置不同：枚举里的一位博主失败要**跳过继续下一位**，
    而"给这位补资料"失败是单条任务的红。
    """
    cls = ListError if stage == "list" else PlatformError
    return cls(PLATFORM, stage, message)


@dataclass(frozen=True)
class NoteCard:
    """主页 / 滚动那份 JS 交出的一张笔记卡片（页面形状的最小投影）。"""

    note_id: str
    title: str = ""
    likes: str = ""
    """页面上的**原样文本**（"1.2万" / "3456"）。转 int 在 `card_to_video_meta()` 做，
    这里不转是为了让"页面到底写了什么"在失败时还有据可查。"""

    cover_url: str = ""
    xsec_token: str = ""
    """取详情的门票。**不是身份、不入库**（ADR-0016）：它只活在
    `VideoMeta.extra["xsec_token"]` 与拼详情 URL 那一刻 —— `VideoMeta` 本身不落库，
    落库的那份 `VideoDraft` 里没有能装它的字段（看护在契约测试里）。"""

    note_type: str = ""
    """`video` / `normal` / `""`（认不出）。见 `is_image_note()` 那条不对称。"""

    @property
    def url(self) -> str:
        """带 token 的详情链接。没有 token 时也给裸链接 —— 能不能过风控由页面说了算，
        判"取不到"的权力留给详情那一步（那里看得到 `login_wall_or_removed` 原文）。
        """
        return build_note_url(self.note_id, self.xsec_token)


@dataclass(frozen=True)
class ParsedProfile:
    """一次主页解析的产出（资料 + 首屏卡片，V1 是一次进页同时拿回两样）。"""

    user_id: str
    nickname: str
    avatar_url: str | None
    follower_count: int | None
    cards: tuple[NoteCard, ...] = ()
    nickname_is_placeholder: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_profile(self, ref: CreatorRef) -> CreatorProfile:
        name = self.nickname or ref.platform_id
        return CreatorProfile(
            ref=ref,
            name=name,
            # 页面上的头像/封面经常没有协议头（`//sns-webpic-qc.xhscdn.com/…`）。
            # `absolute_http_url()` 补成 `https:`，真不像 URL 的收成 None，
            # 而不是让 Pydantic 在采集链路里抛 ValidationError。
            avatar_url=absolute_http_url(self.avatar_url),
            follower_count=self.follower_count,
            extra={
                "nickname": self.nickname,
                "nickname_is_placeholder": self.nickname_is_placeholder,
            },
        )


def _clean(value: object) -> str:
    return str(value if value is not None else "").strip()


def _parse_cards(items: object) -> tuple[NoteCard, ...]:
    if not isinstance(items, list):
        return ()
    cards: list[NoteCard] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        note_id = _clean(item.get("id") or item.get("note_id"))
        if not NOTE_ID_PATTERN.fullmatch(note_id) or note_id in seen:
            # 不是笔记 ID 的形状就不是笔记（推荐位、@ 链接、广告位），宁可不收。
            # `{18,}` 与"异步水合会扫到两次"两条都在 `urls.NOTE_ID_PATTERN` 与调用方钉着。
            continue
        seen.add(note_id)
        cards.append(
            NoteCard(
                note_id=note_id,
                title=_clean(item.get("title")),
                likes=_clean(item.get("likes")),
                cover_url=_clean(item.get("cover_url")),
                xsec_token=_clean(item.get("xsec_token")),
                note_type=_clean(item.get("note_type")),
            )
        )
    return tuple(cards)


def parse_cards_payload(payload: Mapping[str, Any]) -> tuple[NoteCard, ...]:
    """滚动补全那份 JS 的产出（只有 `notes`，没有资料字段）。"""
    return _parse_cards(payload.get("notes"))


def merge_note_cards(
    primary: Iterable[NoteCard], extra: Iterable[NoteCard]
) -> tuple[NoteCard, ...]:
    """把滚动补采到的卡片并进来：同一条笔记**按字段互补**，缺的不覆盖已有的（V1 :231）。

    为什么不是"后到的整条覆盖"，也不是"先到先得就丢掉后面那条"：
    两条来路各有一半信息 —— DOM 那份有标题 / 封面 / 点赞，状态树那份有 token 与
    `note_type`。覆盖会丢类型（图文被判成视频 → 走 yt-dlp → 白失败一场），
    丢弃会丢 token（详情跳风控页 → 一条笔记都取不到）。

    顺序保持 primary 在前：页面的原始顺序是"新在最前"，那是日更采集要的。
    """
    merged: dict[str, NoteCard] = {}
    for card in [*primary, *extra]:
        current = merged.get(card.note_id)
        if current is None:
            merged[card.note_id] = card
            continue
        updates = {
            key: value
            for key, value in (
                ("title", card.title),
                ("likes", card.likes),
                ("cover_url", card.cover_url),
                ("xsec_token", card.xsec_token),
                ("note_type", card.note_type),
            )
            if not _clean(getattr(current, key)) and _clean(value)
        }
        if updates:
            merged[card.note_id] = replace(current, **updates)
    return tuple(merged.values())


def sort_cards(cards: Sequence[NoteCard], sort_by: str) -> tuple[NoteCard, ...]:
    """按指标重排（V1 `sort_notes` / `note_sort_key`）。`page_order` 原样返回。

    两条从 V1 带来的判据，都不是风格问题：

    1. **缺指标的排最后，但不会被丢掉**（V1 的键是 `(1, value)` / `(0, 0.0)` 两级）。
       直接 `int(likes)` 会让一条页面写成"赞"的卡片抛异常，而"排序抛了"在采集链路里
       通常被兜成"这一位博主整轮失败"。
    2. **稳定排序**：点赞数相同的笔记保持页面原序。反过来说，"同为 0 赞"那批互相之间
       的顺序不该由我们发明 —— 页面给的是发布时间倒序，那是有意义的信息。
    """
    if sort_by != "likes":
        return tuple(cards)
    return tuple(sorted(cards, key=_like_rank, reverse=True))


def _like_rank(card: NoteCard) -> tuple[int, float]:
    """V1 `note_sort_key` 原样：`(有没有指标, 数值)`，缺指标的那一档排在后面。"""
    value = parse_cn_count(card.likes)
    return (1, float(value)) if value is not None else (0, 0.0)


def parse_profile_payload(
    payload: Mapping[str, Any], *, expected_user_id: str, stage: str = "profile"
) -> ParsedProfile:
    """主页那份 JS 的产出 → `ParsedProfile`。

    `stage` 决定抛哪一档异常：`list_creator_videos` 传 `"list"`（要 `ListError`，
    调度器按它判断"这一位跳过"），`fetch_creator_profile` 用默认的 `"profile"`。

    **三种"这一轮不采"分开，因为要做的动作不同**：

    - `user_id_mismatch` —— 打开 A 主页被重定向到了 B（登录态串页）。整条丢弃，
      **不许退回去用页面自报的 user_id**（纪律 2）。
    - `login_wall` —— 桥里那份 profile 的登录态过期了。动作是"去扫码"，
      "改天再跑"与"换个名字"都是错的（V1 :934 同一句）。
    - `user_not_found` —— 这个 user_id 不存在（用户填错 / 被注销）。
      与上一条的区分成本是一句文案，混了的代价是人去改一个本来没错的链接。

    卡片为空**不是**错误：小红书确实有零笔记的账号。抖音那边可以用
    "`data-e2e=\"user-post-list\"` 在不在"区分"网格没渲染"与"真的没有作品"，
    小红书这边 DOM 与状态树两条来路都空才叫空，没有中间态可以甩锅 ——
    所以那一族"宁可空手也不能脏"的判据在这里落成了 `login_wall` / `user_not_found`
    两个显式字段，而不是"空列表 = 失败"。
    """
    if payload.get("ok") is not True:
        error = str(payload.get("error") or "unknown")
        if error == "user_id_mismatch":
            actual = str(payload.get("actual") or "")
            msg = (
                f"页面 user_id 与预期不一致（预期 {expected_user_id}，实际 {actual}），"
                f"已跳过避免串号（桥的登录态串页？）"
            )
            raise _page_error(stage, msg)
        page_url = str(payload.get("page_url") or "")
        msg = f"主页解析失败: {error}" + (f"（停在 {page_url}）" if page_url else "")
        raise _page_error(stage, msg)

    claimed = _clean(payload.get("user_id"))
    if expected_user_id and claimed and claimed != expected_user_id:
        # 页函数自己已经挡了这一层（走 user_id_mismatch 分支）；走到这里说明
        # 桥 / 页面绕过了那道判断，宁可现在抛，也不要"采了别人家笔记"。
        msg = f"页面自报 user_id={claimed} 与目标 {expected_user_id} 不一致，已跳过避免串号"
        raise _page_error(stage, msg)

    page_url = str(payload.get("page_url") or "")
    if payload.get("login_wall") is True:
        msg = (
            "小红书主页返回登录墙，桥里那份登录态可能已过期"
            f"（停在 {page_url or '小红书主页'}）：先在桥里扫码登录一次再跑"
        )
        raise _page_error(stage, msg)
    if payload.get("user_not_found") is True:
        msg = (
            f"查无此人：user_id {expected_user_id} 在小红书上不存在或已注销"
            f"（停在 {page_url or '小红书主页'}）—— 这与「未登录」是两件事，扫码解决不了"
        )
        raise _page_error(stage, msg)

    nickname = _clean(payload.get("nickname"))
    return ParsedProfile(
        user_id=claimed or expected_user_id,
        nickname=nickname,
        avatar_url=_clean(payload.get("avatar_url")) or None,
        follower_count=parse_cn_count(payload.get("follower_count")),
        cards=_parse_cards(payload.get("notes")),
        nickname_is_placeholder=is_placeholder_nickname(nickname),
        raw=payload,
    )


def card_to_video_meta(card: NoteCard, *, ref: CreatorRef) -> VideoMeta:
    """卡片 → `VideoMeta`。

    与抖音那份 `card_to_video_meta` 的**一处真实差别**：`published_at` 不是 None。
    小红书的 `note_id` 前 8 位十六进制就是发布时间戳（V1 的 `infer_published_at`，
    ADR-0016 把它定成"页面给不出时间时的第二手"），所以这里的 `since`
    是**判得动就真判**的过滤，不像抖音那一侧只能是弱过滤。

    `xsec_token` 进的是 `extra`（不落库）而不是任何模型字段 —— 见
    `NoteCard.xsec_token` 那段与契约测试里"token 不出现在入库草稿里"那条。
    """
    return VideoMeta(
        platform=PLATFORM,
        platform_video_id=card.note_id,
        creator_ref=ref,
        title=card.title or card.note_id,
        published_at=published_at_from_note_id(card.note_id),
        like_count=parse_cn_count(card.likes),
        # 封面是页面上的缩略图，可能没有协议头；认不出就当没有（这一列可空）。
        cover_url=absolute_http_url(card.cover_url),
        webpage_url=require_http_url(card.url, context=f"笔记 {card.note_id} 的详情页"),
        extra={
            "note_id": card.note_id,
            "xsec_token": card.xsec_token,
            "likes_text": card.likes,
            "note_type": card.note_type,
            "published_at_source": "note_id",
        },
    )


def find_placeholders(*templates: str) -> frozenset[str]:
    """模板里出现的**我们注入的**占位符（`__INITIAL_STATE__` 那一类页面名已排除）。

    存在的理由是让"模板加了占位符、`placeholder_values()` 得跟着加"这条看护
    能在**渲染之前**问一句。只靠 `render_page_js()` 的残留检查不够：
    那条要跑到渲染才生效，而用例改一份模板不一定会去渲染它。
    """
    found: set[str] = set()
    for template in templates:
        found.update(_PLACEHOLDER.findall(template))
    return frozenset(found - PAGE_OWN_DUNDER_NAMES)


def require_http_url(value: object, *, context: str) -> HttpUrl:
    """`absolute_http_url()` 的"这里不给 None"版本。

    校验本身走公共层（ADR-0016），这一层只负责把它翻成带 platform/stage 的
    `PlatformError` —— 那两位元数据是平台自己的，所以没法上提
    （上提等于让 `platforms/urls.py` 认识异常类型）。
    失败意味着**拼接那一侧被谁改坏了**，要的是带上下文的红，不是 Pydantic 的字段列表。
    """
    url = absolute_http_url(value)
    if url is None:
        msg = f"{context} 里应该是一个 http(s) URL，实际是 {str(value)[:120]!r}"
        raise PlatformError(PLATFORM, "parse", msg)
    return url
