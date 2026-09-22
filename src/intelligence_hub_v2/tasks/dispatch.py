"""按链接认平台（`add_creator` 与 `single_link` 共用）。

V1 §7.10「路由靠记忆」的结构性解法在采集侧的对应物：过去"这是抖音还是 B站"
散在脚本里靠人记，V2 收成一处**按主机名**判，且只认已经在注册表里实现了、
并且当前 `enabled` 的平台 —— 认出一个"配置里有但没移植"的平台会明确报错，
而不是硬塞给一个不存在的适配器。

主机名匹配用后缀而不是 `in`：`"douyin.com" in "evil-douyin.com.attacker.net"`
是 True，会把别人的链接当成抖音。所以拆成标签逐段比。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from intelligence_hub_v2.errors import PlatformError

if TYPE_CHECKING:
    from intelligence_hub_v2.platforms.registry import PlatformRegistry

__all__ = ["PLATFORM_HOST_SUFFIXES", "canonical_video_url", "detect_platform"]

PLATFORM_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "bilibili": ("bilibili.com", "b23.tv"),
}
"""平台 → 允许的主机名后缀。与 `config/platforms.yaml` 的 key、注册表 name 同一批名字。

V2.0 只有这两个（`ADR-0010`）。小红书 / YouTube 进来时在这里加一行即可，
`detect_platform` 不用动 —— 它按这张表 + 注册表的 enabled 集合求交。
"""

_VIDEO_URL_TEMPLATE: dict[str, str] = {
    "bilibili": "https://www.bilibili.com/video/{id}",
    "douyin": "https://www.douyin.com/video/{id}",
}


def canonical_video_url(platform: str, video_id: str) -> str:
    """从平台内唯一 id 反推作品主页 URL。

    `postprocess` 拿到的是库里的 `platform_video_id`，而 `fetch_subtitles` 要一个
    `webpage_url` —— 库行不存原始 URL（存了也是会过期的分享链），所以按 id 现拼规范主页。
    """
    template = _VIDEO_URL_TEMPLATE.get(platform)
    if template is None:
        msg = f"平台 {platform!r} 不认识，拼不出作品主页 URL"
        raise PlatformError(platform, "parse_url", msg)
    return template.format(id=video_id)


def _host_of(url: str) -> str:
    """取出小写主机名，取不出返回空串。

    不用 `httpx.URL` 是因为它对没写 scheme 的串（`www.bilibili.com/video/BV…`）
    会把第一段当 scheme。这里手工去 scheme / 端口 / 路径，稳。
    """
    text = str(url or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    text = text.split("?", 1)[0]
    text = text.split(":", 1)[0]
    return text.strip(".").lower()


def detect_platform(url: str, registry: PlatformRegistry) -> str:
    """这条链接属于哪个**当前可采**的平台。认不出或不可采都抛 `PlatformError`。"""
    host = _host_of(url)
    if not host:
        msg = f"链接里认不出主机名，判断不了平台：{url!r}"
        raise PlatformError("unknown", "parse_url", msg)

    matched = next(
        (name for name, suf in PLATFORM_HOST_SUFFIXES.items() if _ends_with(host, suf)), None
    )
    if matched is None:
        known = ", ".join(f"{n}（{'/'.join(s)}）" for n, s in PLATFORM_HOST_SUFFIXES.items())
        msg = f"主机 {host!r} 不在已知平台里。已知：{known}"
        raise PlatformError("unknown", "parse_url", msg)

    if matched not in registry.enabled_platforms():
        available = ", ".join(registry.enabled_platforms()) or "（当前没有启用的平台）"
        msg = f"链接指向平台 {matched!r}，但它没启用或本构建没实现。可采的平台：{available}"
        raise PlatformError(matched, "parse_url", msg)
    return matched


def _ends_with(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == s or host.endswith(f".{s}") for s in suffixes)
