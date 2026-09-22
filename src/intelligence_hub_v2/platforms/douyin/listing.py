"""抖音主页的页面 JS 与结果解析。

两条纪律是从 V1 带过来的，都在这里落地：

1. **占位符不加引号**（V1 §2 契约三）。Python 侧注入的是 `json.dumps(value)`，
   它自己带引号；模板里再写 `"__X__"` 就拼成 `""MS4w…""`，页面当场 SyntaxError，
   而报错在桥的那一头，Python 侧只看到一个"evaluate 失败"。
   `render_page_js()` 在替换完会检查**没有残留占位符**，漏一个就抛 ——
   V1 的做法是在调用点手写一串 `.replace()`，忘了就得到一个带着
   `__WANTED__` 的 JS 打到页面上（变成 ReferenceError，方向被带到"页面结构变了"上）。
2. **只认作品网格里的链接**。整页扫 `a[href*="/video/"]` 会在网格没渲染时
   把**别人**的作品算到本博主头上，而且报成功 —— V1 实测踩过：
   一条练字帖广告、一条财经播报被登记成某位博主的作品。采集口播稿的库，
   宁可这一轮空手，也不能脏。所以网格为空是 `ok:false` + 原文，不是空列表。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from intelligence_hub_v2.errors import ListError, PlatformError
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.douyin.urls import (
    SEC_UID_PREFIX,
    as_http_url,
    canonical_video_url,
    is_http_url,
    parse_cn_count,
    require_http_url,
)

__all__ = [
    "COLLECT_AFTER_SCROLL_FUNCTION",
    "PROFILE_PAGE_FUNCTION",
    "PageBudget",
    "VideoCard",
    "card_to_video_meta",
    "decode_page_result",
    "is_placeholder_nickname",
    "parse_cards_payload",
    "parse_profile_payload",
    "render_page_js",
]

PLATFORM = "douyin"

PLACEHOLDER_NAMES = frozenset({"", "抖音创作者", "抖音用户", "用户"})
"""页面没给出真昵称时的几种退化值（V1 的 `PLACEHOLDER_NAMES` 原样）。

V1 §7.24 的另一半：昵称会进目录名与入库作者字段，而**占位昵称不该覆盖用户手起的名字**。
V2 里没有"手起的名字"这一层（库里只有采集来的），所以这里只把它作为
`CreatorProfile.extra["nickname_is_placeholder"]` 交出去，由入库层决定要不要覆盖。
"""

_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


@dataclass(frozen=True)
class PageBudget:
    """页面 JS 的轮询/滚动预算。

    放在一个 dataclass 里而不是散成模块常量，是因为**测试必须能改小它**：
    默认值合计约 12 秒（15×800ms 等网格 + 12 轮滚动），
    用例里等 12 秒会把"契约测试"变成"集成测试"。
    """

    grid_poll_rounds: int = 15
    """等作品网格出现的轮询次数。抖音主页卡片是异步水合的，慢的时候十秒上下才出现
    （验证码中间页、SPA 慢水合），轮询到点才允许判空。"""

    grid_poll_ms: int = 800
    max_scroll_rounds: int = 12
    """首屏只渲染视口内的卡片，边滚边收才能凑够 `videos_per_creator` 条。"""

    settle_ms: int = 800
    """每滚一屏之后给页面的加载时间。"""

    def placeholder_values(self, *, sec_uid: str, wanted: int | None) -> dict[str, str]:
        """占位符 → 注入文本。

        `__EXPECTED_SEC_UID__` 走 `json.dumps`（值带引号，模板里不带），
        其余是页面上当数字用的，注入十进制字面量。
        """
        values: dict[str, str] = {
            "__EXPECTED_SEC_UID__": json.dumps(sec_uid),
            "__GRID_POLL_ROUNDS__": str(self.grid_poll_rounds),
            "__GRID_POLL_MS__": str(self.grid_poll_ms),
            "__MAX_SCROLL_ROUNDS__": str(self.max_scroll_rounds),
            "__SETTLE_MS__": str(self.settle_ms),
            "__WANTED__": str(wanted if wanted is not None else 0),
        }
        return values


# 在抖音主页执行，返回昵称/头像/粉丝数与首屏作品卡片。
# __EXPECTED_SEC_UID__ 由 render_page_js() 用 json.dumps(sec_uid) 替换（占位符本身
# 不能带引号，否则替换完就是 ""xxx"" 的非法 JS），用来识别登录态串页
# （打开 A 主页被重定向到 B）并直接判定失败。
GRID_HELPERS = """
  const waitMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const clean = (v) => (v === undefined || v === null ? "" : String(v).trim());
  const pickId = (href) => {
    const m = String(href || "").match(/\\/video\\/(\\d+)/);
    return m ? m[1] : "";
  };
  const countLikes = (card) => {
    const node = card && card.querySelector('[data-e2e="user-post-item-like-count"]');
    if (node) return clean(node.textContent);
    const text = clean(card && card.innerText).replace(/^置顶\\s*/, "");
    const m = text.match(/^([\\d.,]+\\s*[万亿]?)/);
    return m ? m[1].replace(/\\s+/g, "") : "";
  };
  const cardTitle = (card, anchor) => {
    const caption = card && card.querySelector("p");
    const image = card && card.querySelector("img");
    return clean(caption && caption.innerText)
      || clean(image && (image.alt || image.getAttribute("aria-label")))
      || clean(anchor.getAttribute("aria-label"));
  };
  const readCard = (anchor) => {
    const card = anchor.closest("li")
      || anchor.closest('[data-e2e="user-post-item"]')
      || anchor.parentElement;
    return card;
  };
  const harvestGrid = (root) => {
    const items = [];
    root.querySelectorAll('a[href*="/video/"]').forEach((anchor) => {
      const id = pickId(anchor.getAttribute("href"));
      if (!id || items.some((item) => item.id === id)) return;
      const card = readCard(anchor);
      items.push({
        id: id,
        url: "https://www.douyin.com/video/" + id,
        title: cardTitle(card, anchor),
        likes: countLikes(card)
      });
    });
    return items;
  };
  const gridReady = () => {
    const root = document.querySelector('[data-e2e="user-post-list"]');
    return root && root.querySelectorAll('a[href*="/video/"]').length > 0 ? root : null;
  };
  // 网格偶尔要 10 秒上下才出现（验证码中间页、SPA 慢水合），轮询到点再判空。
  const waitForGrid = async () => {
    for (let i = 0; i < __GRID_POLL_ROUNDS__; i++) {
      const root = gridReady();
      if (root) return root;
      await waitMs(__GRID_POLL_MS__);
    }
    return null;
  };
"""

PROFILE_PAGE_FUNCTION = (
    """
async () => {
  const expected = __EXPECTED_SEC_UID__;
  const textOf = (sel) => {
    const node = document.querySelector(sel);
    return clean(node && node.textContent);
  };
"""
    + GRID_HELPERS
    + """
  let userInfo = null;
  try {
    const router = window._ROUTER_DATA || {};
    const loader = router.loaderData || {};
    for (const key of Object.keys(loader)) {
      const node = loader[key] || {};
      const candidate = node.userInfo || (node.userPageInfo && node.userPageInfo.userInfo);
      if (candidate && candidate.sec_uid) { userInfo = candidate; break; }
    }
  } catch (err) { /* 结构随抖音前端版本变化，取不到就走 DOM 兜底 */ }

  const nickname = clean(userInfo && (userInfo.nickname || userInfo.unique_id))
    || textOf('[data-e2e="user-info"] [data-e2e="user-name"]')
    || textOf('[data-e2e="user-info"] h1')
    || clean(document.title).split("的关注")[0].split(" - ")[0];
  const avatar = clean(userInfo && (userInfo.avatar_larger && userInfo.avatar_larger.url_list
    && userInfo.avatar_larger.url_list[0]));
  const fansBlock = document.querySelector('[data-e2e="user-info-fans"]');
  const fansText = fansBlock ? clean(fansBlock.innerText).replace(/^粉丝\\s*/, "") : "";
  const fansRaw = (userInfo && userInfo.follower_count !== undefined)
    ? userInfo.follower_count
    : (textOf('[data-e2e="user-fans-count"]') || fansText);
  const actualSecUid = clean(userInfo && userInfo.sec_uid);
  if (expected && actualSecUid && actualSecUid !== expected) {
    return JSON.stringify({
      ok: false,
      error: "sec_uid_mismatch",
      expected: expected,
      actual: actualSecUid,
    });
  }

  const grid = await waitForGrid();
  if (!grid) {
    return JSON.stringify({
      ok: false,
      error: "作品网格没有渲染（首页被降级成推荐流或卡在验证页），本轮不采集以免混入他人作品",
      nickname: nickname,
      page_url: location.href
    });
  }

  return JSON.stringify({
    ok: true,
    sec_uid: actualSecUid || expected,
    nickname: nickname,
    avatar_url: avatar,
    follower_count: clean(fansRaw).replace(/[+,]/g, ""),
    videos: harvestGrid(grid)
  });
}
"""
)

# 主页列表只渲染视口内的卡片，边滚边收才能凑够 videos_per_creator 条。
COLLECT_AFTER_SCROLL_FUNCTION = (
    """
async () => {
  const seen = new Map();
  const rounds = __MAX_SCROLL_ROUNDS__;
  const settle = __SETTLE_MS__;
"""
    + GRID_HELPERS
    + """
  const harvest = () => {
    const root = gridReady();
    if (!root) return;
    harvestGrid(root).forEach((item) => { if (!seen.has(item.id)) seen.set(item.id, item); });
  };
  const wanted = __WANTED__;
  for (let i = 0; i < rounds && seen.size < wanted; i++) {
    harvest();
    window.scrollBy(0, window.innerHeight * 0.9);
    await new Promise((resolve) => setTimeout(resolve, settle));
  }
  harvest();
  return JSON.stringify({ videos: Array.from(seen.values()) });
}
"""
)


def render_page_js(
    template: str,
    *,
    sec_uid: str,
    budget: PageBudget,
    wanted: int | None = None,
) -> str:
    """把页面 JS 模板里的占位符换成实值，并**确认一个都没剩**。

    残留检查是这整个函数存在的理由：V1 在调用点手写一串 `.replace()`，
    漏掉的那个占位符会原样进页面 —— 症状是页面 JS 抛
    `ReferenceError: __WANTED__ is not defined`，而排查的人会先怀疑
    "抖音改版了 / 风控"，那是最贵的一类误判。
    """
    rendered = template
    for key, value in budget.placeholder_values(sec_uid=sec_uid, wanted=wanted).items():
        rendered = rendered.replace(key, value)
    leftover = sorted(set(_PLACEHOLDER.findall(rendered)))
    if leftover:
        msg = f"页面 JS 里还剩未替换的占位符：{', '.join(leftover)}（模板加了新占位符？）"
        raise ValueError(msg)
    return rendered


def decode_page_result(result: object, *, stage: str) -> dict[str, Any]:
    """把 `/evaluate` 的返回收成 dict。

    页函数返回的是 `JSON.stringify(...)`，所以桥给回来的通常是**字符串**；
    但桥自己会把已经是 JSON 的东西解一层，某些路径下直接给 dict。
    两种都认，认不出就把原文截一段交出去 —— "页面未返回可解析结果"后面
    没有原文，等于让人去重放一遍才能看到到底回了什么。
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
    """空名、平台默认名、裸 sec_uid 和整条 URL 都算占位名。

    最后一项是 V1 §7.1 的形状：历史脏行把分享短链写进了 `name`/`platform_id`。
    昵称位上出现 `http…` 一定不是人起的名字，让它覆盖真昵称就是把脏数据固化。
    """
    text = str(name or "").strip()
    if not text or text in PLACEHOLDER_NAMES:
        return True
    return text.startswith(SEC_UID_PREFIX) or is_http_url(text)


def _profile_error(stage: str, message: str) -> PlatformError:
    """`ListError` 与 `PlatformError` 是同一族，只差"这次失败发生在枚举里"。

    分开是因为上层的处置不同：枚举里的一位博主失败要**跳过继续下一位**，
    而"给这位补资料"失败是单条任务的红。
    """
    cls = ListError if stage == "list" else PlatformError
    return cls(PLATFORM, stage, message)


@dataclass(frozen=True)
class VideoCard:
    """作品网格里的一张卡片（页面 JS 的最小产出）。"""

    aweme_id: str
    title: str = ""
    likes: str = ""
    """页面上的**原样文本**（"1.2万" / "3456"）。转 int 在 `card_to_video_meta()` 做，
    这里不转是为了让"页面到底写了什么"在失败时还有据可查。"""

    @property
    def url(self) -> str:
        return canonical_video_url(self.aweme_id)


@dataclass(frozen=True)
class ParsedProfile:
    """一次主页解析的产出（资料 + 首屏卡片，V1 是一次进页同时拿回两样）。"""

    sec_uid: str
    nickname: str
    avatar_url: str | None
    follower_count: int | None
    cards: tuple[VideoCard, ...] = ()
    nickname_is_placeholder: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_profile(self, ref: CreatorRef) -> CreatorProfile:
        name = self.nickname or ref.platform_id
        return CreatorProfile(
            ref=ref,
            name=name,
            # 页面上的头像常常没有协议头（`//p3.douyinpic.com/…`）。`as_http_url()`
            # 把它收成 None 而不是让 Pydantic 在采集链路里抛 ValidationError。
            avatar_url=as_http_url(self.avatar_url),
            follower_count=self.follower_count,
            extra={
                "nickname": self.nickname,
                "nickname_is_placeholder": self.nickname_is_placeholder,
            },
        )


def _parse_cards(items: object) -> tuple[VideoCard, ...]:
    if not isinstance(items, list):
        return ()
    cards: list[VideoCard] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("id") or "").strip()
        if not aweme_id.isdigit() or aweme_id in seen:
            # 不是纯数字就不是 aweme_id（脏行、广告位链接），宁可不收
            continue
        seen.add(aweme_id)
        cards.append(
            VideoCard(
                aweme_id=aweme_id,
                title=str(item.get("title") or "").strip(),
                likes=str(item.get("likes") or "").strip(),
            )
        )
    return tuple(cards)


def parse_cards_payload(payload: Mapping[str, Any]) -> tuple[VideoCard, ...]:
    """滚动补全那份 JS 的产出（只有 `videos`，没有资料字段）。"""
    return _parse_cards(payload.get("videos"))


def parse_profile_payload(
    payload: Mapping[str, Any], *, expected_sec_uid: str, stage: str = "profile"
) -> ParsedProfile:
    """主页那份 JS 的产出 → `ParsedProfile`。

    `stage` 决定抛哪一档异常：`list_creator_videos` 传 `"list"`（要 `ListError`，
    调度器按它判断"这一位跳过"），`fetch_creator_profile` 用默认的 `"profile"`。
    两种红要的动作不同，所以文案与类型都得跟着分。

    两种"这一轮不采"必须分开，因为要做的动作不同：

    - `error: "sec_uid_mismatch"` —— 打开 A 主页被重定向到了 B（登录态串页）。
      整条丢弃，**不能退回去用页面自报的 sec_uid**：那等于把 B 的作品登记给 A，
      而库里已经有的 A 的行会被刷成 B 的昵称。
    - 网格没渲染（`ok: false` + 中文说明）—— 这一位跳过，换下一位。
      原文必须进异常消息，因为它区分的是"卡在验证页"和"首页被降级成推荐流"，
      前者要人工过验证，后者是页面结构变了。
    """
    if payload.get("ok") is not True:
        error = str(payload.get("error") or "unknown")
        if error == "sec_uid_mismatch":
            actual = str(payload.get("actual") or "")
            msg = (
                f"页面 sec_uid 与预期不一致（预期 {expected_sec_uid}，实际 {actual}），"
                f"已跳过避免串号（桥的登录态串页？）"
            )
            raise _profile_error(stage, msg)
        page_url = str(payload.get("page_url") or "")
        msg = f"主页解析失败: {error}" + (f"（停在 {page_url}）" if page_url else "")
        raise _profile_error(stage, msg)

    claimed = str(payload.get("sec_uid") or "").strip()
    if expected_sec_uid and claimed and claimed != expected_sec_uid:
        # 页函数自己已经挡了这一层（走 sec_uid_mismatch 分支）；
        # 走到这里说明桥/页面绕过了那道判断，宁可现在抛，也不要"采了别人家作品"。
        msg = f"页面自报 sec_uid={claimed} 与目标 {expected_sec_uid} 不一致，已跳过避免串号"
        raise _profile_error(stage, msg)

    nickname = str(payload.get("nickname") or "").strip()
    avatar = str(payload.get("avatar_url") or "").strip()
    return ParsedProfile(
        sec_uid=claimed or expected_sec_uid,
        nickname=nickname,
        avatar_url=avatar if is_http_url(avatar) else None,
        follower_count=parse_cn_count(payload.get("follower_count")),
        cards=_parse_cards(payload.get("videos")),
        nickname_is_placeholder=is_placeholder_nickname(nickname),
        raw=payload,
    )


def card_to_video_meta(card: VideoCard, *, ref: CreatorRef) -> VideoMeta:
    """卡片 → `VideoMeta`。

    `published_at` / `duration_seconds` 是 **None，不是 0**：
    作品网格里的那张卡片只有 id / 标题 / 点赞数（DOM 上确实没有别的），
    所以 `list_creator_videos(since=...)` 在抖音这一侧是个**弱过滤** ——
    见 `adapter.py` 的 docstring 与 `docs/lessons.md`。
    填 0 会让"since 之后没有新作"被误判成"全是旧作"，那是把未知当结论。
    """
    return VideoMeta(
        platform=PLATFORM,
        platform_video_id=card.aweme_id,
        creator_ref=ref,
        title=card.title or card.aweme_id,
        published_at=None,
        duration_seconds=None,
        like_count=parse_cn_count(card.likes),
        # 必填字段用 require_http_url()：值由 canonical_video_url() 拼出来，
        # 真要是不成，那说明拼接那一侧被谁改坏了，要的是带上下文的红而不是字段列表。
        webpage_url=require_http_url(card.url, context=f"抖音作品 {card.aweme_id} 的播放页"),
        extra={"aweme_id": card.aweme_id, "likes_text": card.likes},
    )
