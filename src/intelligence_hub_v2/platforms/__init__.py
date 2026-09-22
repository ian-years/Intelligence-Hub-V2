"""平台注册表。

V2 的纪律：**显式注册表**，不搞 import 时自动发现的魔法（ADR-0004）。
`PLATFORM_CONFIG_SCHEMAS` 的 key 必须与三处一致：
1. `PlatformAdapter.name`（Task 5 补）
2. `config/platforms.yaml` 的顶层 key
3. 前端平台开关的 id

V2.0 只注册 douyin + bilibili。xiaohongshu / youtube 在 V2.1 进来
（ADR-0010 ROADMAP），目录已经建好但**不注册** —— 注册了却没有实现
等于对前端撒谎。
"""

from __future__ import annotations

from pydantic_core import PydanticUndefined

from intelligence_hub_v2.platforms.base import (
    Capabilities,
    CookieVariant,
    ListStrategy,
    MediaStrategy,
    PlatformConfig,
    RateLimitConfig,
)
from intelligence_hub_v2.platforms.bilibili.config import BilibiliAdvanced, BilibiliConfig
from intelligence_hub_v2.platforms.douyin.config import DouyinAdvanced, DouyinConfig

PLATFORM_CONFIG_SCHEMAS: dict[str, type[PlatformConfig]] = {
    "douyin": DouyinConfig,
    "bilibili": BilibiliConfig,
}
"""平台名 → 配置模型类。`/api/platforms/{name}/schema` 就是查这张表。"""


def supported_platforms() -> tuple[str, ...]:
    """本构建支持的平台名，按注册顺序。"""
    return tuple(PLATFORM_CONFIG_SCHEMAS)


def config_schema_for(platform: str) -> type[PlatformConfig]:
    """取平台的配置模型类。未注册抛 KeyError（不是静默返回基类）。"""
    return PLATFORM_CONFIG_SCHEMAS[platform]


def default_display_name(platform: str) -> str | None:
    """平台配置类里 `display_name` 的默认值，没有默认值返回 None。

    用途：`platforms.yaml` 只写了 `enabled: false` 时，
    `display_name` 从注册表补，而不是让校验失败。
    **单一真源**：默认值只写在配置类里，这里只是读出来。
    """
    schema = config_schema_for(platform)
    default = schema.model_fields["display_name"].default
    return None if default is PydanticUndefined else str(default)


def platform_defaults(platform: str) -> dict[str, object]:
    """该平台配置的兜底字段（当前只有 display_name）。"""
    out: dict[str, object] = {}
    name = default_display_name(platform)
    if name is not None:
        out["display_name"] = name
    return out


__all__ = [
    "PLATFORM_CONFIG_SCHEMAS",
    "BilibiliAdvanced",
    "BilibiliConfig",
    "Capabilities",
    "CookieVariant",
    "DouyinAdapter",
    "DouyinAdvanced",
    "DouyinConfig",
    "ListStrategy",
    "MediaStrategy",
    "PlatformConfig",
    "RateLimitConfig",
    "config_schema_for",
    "default_display_name",
    "platform_defaults",
    "supported_platforms",
]


# --------------------------------------------------------------------------- #
# 适配器实现（**必须放在 PLATFORM_CONFIG_SCHEMAS 之后**）
# --------------------------------------------------------------------------- #
#
# `@register(name)` 在**导入期**就要查 `PLATFORM_CONFIG_SCHEMAS`（名字不在配置表里
# 就直接抛，这是 registry.py 的设计）。而 `registry.py` 自己又要 import 本包拿那张表 ——
# 于是这一句只能放在表定义之后：放前面会拿到"包还没执行完"的半成品，
# 症状是一句看不出所以然的 ImportError。这不是风格问题，是这条依赖边的形状：
# 配置 schema 表（下层）不能知道适配器（上层），所以由本包在末尾把两边接起来。
#
# 放在这里而不是 `main.py` 的装配点，为的是让"这个构建实现了哪些平台"
# **在导入期就固定**。注册表快照用例（`tests/unit/platforms/test_registry.py`）
# 靠这一点才会在"加了适配器却忘了更新快照"时主动变红。
#
# bilibili 的适配器在 Task 7 补，届时这里加一行。
from intelligence_hub_v2.platforms.douyin.adapter import DouyinAdapter  # noqa: E402
