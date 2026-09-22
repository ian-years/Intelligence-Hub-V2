"""平台注册表：把"有哪些平台实现了"这件事收成一处。

V1 的对应物是 `launcher_server.py` 里的 `TASK_DEFS` 字典 + 各采集器脚本名，
"哪个平台支持什么"散在三个地方（脚本、前端硬编码的平台列表、配置文件的 key）。
V2 的三份真相（`config/platforms.yaml` 的 key、`PLATFORM_CONFIG_SCHEMAS`、这里的 `PLATFORMS`）
**必须由代码互相校验**，不然加平台时会得到"配置里有、界面上能点、点了没实现"这种状态 ——
V1 §7.10 那句"顺手猜的路由不存在"就是同一类问题。

**显式登记，不靠导入魔法**：`PLATFORMS[name] = cls` 由 `@register("douyin")` 完成，
而装饰器会立刻检查名字在不在 `PLATFORM_CONFIG_SCHEMAS` 里。
一个平台"有配置 schema 但没适配器实现"或反之，都在导入时就抛，而不是等到有人点它。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from intelligence_hub_v2.errors import PlatformError, TaskRejected
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS
from intelligence_hub_v2.platforms.base import (
    AdapterDeps,
    Capabilities,
    PlatformAdapter,
    PlatformConfig,
)

logger = get_logger(__name__)

__all__ = ["PLATFORMS", "PlatformRegistry", "register"]


PLATFORMS: dict[str, type[PlatformAdapter]] = {}
"""`name → 适配器类`。

**V2.0 只放抖音与 B站**（ADR-0010）。小红书/YouTube 的配置 schema 已经有了
（V2.1 会用到），但这里空着就是"没实现" —— 注册表会让它们**不进** `all_platforms()`，
前端因此不会渲染出一个点了没反应的开关。

`dict` 而不是 list/set：查找要按名字，而名字是三份真相的公共键。
"""


def register(name: str) -> Callable[[type[PlatformAdapter]], type[PlatformAdapter]]:
    """登记一个平台适配器。

    **导入时就检查名字**：`platforms.yaml` 的 key、`PLATFORM_CONFIG_SCHEMAS`、
    这里的 key 三处必须一致，而"某个平台只有两处有"是最难查的那种 bug
    —— 症状是 Settings 页有个开关，点下去报"未知平台"。

    重复注册同一个名字直接抛：V1 的 `TASK_DEFS` 就是靠"后写的覆盖先写的"
    悄悄改过任务定义，改的人完全不知道。
    """
    if name not in PLATFORM_CONFIG_SCHEMAS:
        known = ", ".join(sorted(PLATFORM_CONFIG_SCHEMAS)) or "（还没有任何平台）"
        msg = (
            f"注册了平台 {name!r} 的配置里没有它。已注册配置的平台：{known}。"
            f"新平台要同时加 config/platforms.yaml 的一节、"
            f"platforms/<name>/config.py 与 PLATFORM_CONFIG_SCHEMAS 的条目。"
        )
        raise PlatformError(name, "task", msg)

    def wrapper(cls: type[PlatformAdapter]) -> type[PlatformAdapter]:
        existing = PLATFORMS.get(name)
        if existing is not None and existing is not cls:
            msg = (
                f"平台 {name!r} 已被 {existing.__qualname__} 注册，"
                f"现在又要被 {cls.__qualname__} 覆盖 —— 静默覆盖等于"
                f"谁最后导入谁说话，而导入顺序在别处看不见。"
            )
            raise PlatformError(name, "task", msg)
        PLATFORMS[name] = cls
        logger.debug("platform.registered", platform=name, cls=cls.__qualname__)
        return cls

    return wrapper


class PlatformRegistry:
    """运行期入口：按名字拿适配器实例，按配置过滤掉关掉的平台。

    实例按平台缓存（`get()` 第二次拿到同一个对象）：适配器握着 HTTP 客户端引用与
    限速状态，每拿一次就新建一个等于把限速整个废掉。
    配置热重载之后要 `invalidate()` —— 否则新 `videos_per_creator` 之类要等到重启才生效。

    `deps_factory` 而不是 `deps`：依赖袋里有 `bridge=None` 这种情况
    （声明 `needs_browser=True` 却拿不到桥是**装配错误**，V1 §7.20 的教训），
    装配哪些东西是平台的 `capabilities` 决定的，所以必须按名字现造。
    """

    def __init__(
        self,
        configs: Mapping[str, PlatformConfig],
        deps_factory: Callable[[str], AdapterDeps],
        *,
        classes: Mapping[str, type[PlatformAdapter]] | None = None,
    ) -> None:
        self._configs = dict(configs)
        self._deps_factory = deps_factory
        self._classes = dict(classes) if classes is not None else dict(PLATFORMS)
        self._instances: dict[str, PlatformAdapter] = {}

    # ---- 名字 ----

    def implemented_platforms(self) -> list[str]:
        """有适配器实现的平台（不管开关）。按名字排序，前端列表才不跳。"""
        return sorted(self._classes)

    def enabled_platforms(self) -> list[str]:
        """既实现了、配置里 `enabled=True` 的平台。日更采集与定时任务走这条。

        注意与 V1 §7.22 的分工：「抓取某一位博主的爆款」这类**按位任务不走这里**，
        它按链接认博主、不看开关。开关只管"整库/定时"。
        """
        return [name for name in self.implemented_platforms() if self._is_enabled(name)]

    def _is_enabled(self, name: str) -> bool:
        config = self._configs.get(name)
        return bool(getattr(config, "enabled", False))

    # ---- 取实例 ----

    def get(self, name: str) -> PlatformAdapter:
        """拿适配器。三种红法各有不同文案，因为它们要的动作完全不同：

        - 没实现 → 这个平台在当前构建里不支持（V2.0 只有抖音/B站）。
        - 实现了但被关掉 → 去 Settings 打开开关。
        - 两者都过 → 正常返回。

        把前两种合并成"未知平台"会让人去翻配置，而问题是这个版本压根没移植。
        """
        cls = self._classes.get(name)
        if cls is None:
            available = ", ".join(self.implemented_platforms()) or "（无）"
            msg = f"平台 {name!r} 在当前构建里没有适配器实现。可用的：{available}"
            raise PlatformError(name, "task", msg)

        if not self._is_enabled(name):
            msg = f"平台 {name!r} 已被关掉（config/platforms.yaml 的 enabled: false）"
            raise TaskRejected(msg)

        cached = self._instances.get(name)
        if cached is not None:
            return cached

        config = self._configs.get(name)
        if config is None:
            msg = f"平台 {name!r} 注册了但没有配置对象 —— ConfigManager 的装配漏了一环"
            raise PlatformError(name, "task", msg)

        instance = cls(config, self._deps_factory(name))
        self._instances[name] = instance
        return instance

    def config_for(self, name: str) -> PlatformConfig:
        """这个平台的已校验配置。`inconsistencies()` 之外不该有别人绕过它读 YAML。"""
        config = self._configs.get(name)
        if config is None:
            msg = f"平台 {name!r} 没有配置对象"
            raise PlatformError(name, "task", msg)
        return config

    def capabilities(self, name: str) -> Capabilities:
        cls = self._classes.get(name)
        if cls is None:
            msg = f"平台 {name!r} 在当前构建里没有适配器实现，问不到它的能力"
            raise PlatformError(name, "task", msg)
        return cls.capabilities

    def invalidate(self, name: str | None = None) -> None:
        """丢掉缓存实例。配置热重载之后必须调，否则新配置要等到重启才生效。"""
        if name is None:
            self._instances.clear()
        else:
            self._instances.pop(name, None)

    # ---- 自洽性 ----

    def inconsistencies(self) -> list[str]:
        """三份真相的差集。**返回问题而不是抛**：这是 preflight 要显示的一行红字，
        启动就崩会让用户连 Settings 都进不去，而他要做的正是去勾一个开关。

        两个方向都要查，因为成因不同：
        - 有配置、无实现 → 界面上有个点了没反应的开关（V1 §7.10 的形状）。
        - 有实现、无配置 → 平台永远不出现在列表里，移植白做了还没人知道。
        """
        problems: list[str] = []
        configured = set(self._configs)
        implemented = set(self._classes)
        schema_backed = set(PLATFORM_CONFIG_SCHEMAS)

        for name in sorted(implemented - configured):
            problems.append(f"{name}: 有适配器实现但配置里没有这一节")
        for name in sorted(configured - implemented):
            problems.append(f"{name}: 配置里有这一节但当前构建没有适配器实现")
        for name in sorted(schema_backed - implemented):
            problems.append(f"{name}: 注册了配置 schema 但没有适配器实现")
        for name in sorted(implemented - schema_backed):
            problems.append(f"{name}: 有适配器实现但没有配置 schema")
        return problems

    def __repr__(self) -> str:
        return (
            f"PlatformRegistry(enabled={self.enabled_platforms()}, "
            f"implemented={self.implemented_platforms()})"
        )
