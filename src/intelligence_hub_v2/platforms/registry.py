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
    PlatformAvailability,
    PlatformConfig,
    resolve_platform_availability,
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

    `master_enabled` 是**闭包而不是 bool**（ADR-0025）：总闸在运行期会被设置页翻，
    传进来的那份快照在翻转之后就成旧真相 —— 症状是"设置页显示总闸已开，任务列表还是空的"，
    要重启才恢复。所以这里要的是"每次问现读一次"。它没有默认值：默认等于
    "忘记传的新调用点会静默忽略总闸"，那正是这道闸门最坏的失效方式。
    """

    def __init__(
        self,
        configs: Mapping[str, PlatformConfig],
        deps_factory: Callable[[str], AdapterDeps],
        *,
        master_enabled: Callable[[], bool],
        classes: Mapping[str, type[PlatformAdapter]] | None = None,
    ) -> None:
        self._configs = dict(configs)
        self._deps_factory = deps_factory
        self._classes = dict(classes) if classes is not None else dict(PLATFORMS)
        self._instances: dict[str, PlatformAdapter] = {}
        self._master_enabled = master_enabled

    # ---- 名字 ----

    def implemented_platforms(self) -> list[str]:
        """有适配器实现的平台（不管开关）。按名字排序，前端列表才不跳。"""
        return sorted(self._classes)

    def availability(self, name: str) -> PlatformAvailability:
        """这一家此刻能不能用，不能用的话是**哪一道闸**挡着。

        判据不在本文件重写：`ConfigManager.platform_availability`、`/api/tasks` 的过滤
        与跑前那道门调的是同一个 `resolve_platform_availability`。分叉成两份的成本
        是"按钮在、点下去被拒"或反之，而两种都有人每天看见。
        """
        return resolve_platform_availability(
            self._configs.get(name), master_enabled=self._master_enabled()
        )

    def enabled_platforms(self) -> list[str]:
        """既实现了、又**可用**（总闸 AND 自身开关）的平台。日更采集与定时任务走这条。

        注意与 V1 §7.22 的分工：「抓取某一位博主的爆款」这类**按位任务不走这里**，
        它按链接认博主、不看开关。开关只管"整库/定时"。
        """
        return [
            name for name in self.implemented_platforms() if self.availability(name) == "available"
        ]

    # ---- 取实例 ----

    def get(self, name: str) -> PlatformAdapter:
        """拿适配器。不可用的时候有**三种**红法，文案各不相同，因为它们要的动作完全不同：

        - 没实现 → 这个平台在当前构建里不支持（V2.0 只有抖音/B站）。
        - 没有配置对象 → 装配漏了一环，翻设置没用。
        - 被关了 → 再去分是谁关的：**自己那段** `enabled: false`，还是**总闸**压着
          （ADR-0025）。这两种在 `platforms.yaml` 里长得一模一样（那里还是 `enabled: true`），
          所以把第二种说成第一种是把人支去改一个本来就开着的字段。

        把前两种合并成"未知平台"会让人去翻配置，而问题是这个版本压根没移植。
        """
        cls = self._classes.get(name)
        if cls is None:
            available = ", ".join(self.implemented_platforms()) or "（无）"
            msg = f"平台 {name!r} 在当前构建里没有适配器实现。可用的：{available}"
            raise PlatformError(name, "task", msg)

        # "没有配置对象"必须**先于**"已被关掉"判（review P1-4）：`availability` 对
        # config=None 给的是 `absent`，先判开关会把装配错误谎报成"去 Settings 打开
        # 开关"—— 而那里根本没有这个平台。原来写在下面的同款检查因此不可达。
        state = self.availability(name)
        if state == "absent":
            msg = f"平台 {name!r} 注册了但没有配置对象 —— ConfigManager 的装配漏了一环"
            raise PlatformError(name, "task", msg)

        if state != "available":
            raise TaskRejected(self._unavailable_message(name, state))

        cached = self._instances.get(name)
        if cached is not None:
            return cached

        instance = cls(self.config_for(name), self._deps_factory(name))
        self._instances[name] = instance
        return instance

    def _unavailable_message(self, name: str, state: PlatformAvailability) -> str:
        """`master_off` / `own_off` 两句分开写，因为**动作相反**。

        总闸那句必须顺带说清"它自己那一段没被关" —— 否则第一个动作仍然是去翻
        `platforms.yaml`，而那里看见的是 `enabled: true`，什么也查不出来。
        两道闸都拉着时两句都说：只报总闸会让人开完总闸再吃一次同样的红。
        """
        if state == "master_off":
            config = self._configs.get(name)
            if config is not None and not config.enabled:
                tail = "而且它自己在 config/platforms.yaml 里也是关着的，开完总闸还要再开这一家。"
            else:
                tail = (
                    "它自己那一段并没有被关（platforms.yaml 里还是 enabled: true），挡着的是总闸。"
                )
            return (
                f"平台 {name!r} 被总闸关着（app.yaml 的 platform_control.enabled: false）："
                f"关掉时四家一律不可用。{tail}"
            )
        return f"平台 {name!r} 已被关掉（config/platforms.yaml 的 enabled: false）"

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

    def update_config(self, name: str, config: PlatformConfig) -> None:
        """热重载后把某平台的新配置换进来，并丢掉它的缓存实例。

        注册表握的是一份 `configs` 快照（`AGENTS.md §2`：它是运行期读配置的入口）。
        `ConfigManager.reload_platform()` 只更新 manager 自己那份内存，不会自动回流到这里 ——
        所以"改完开关立刻生效"这一步必须显式做，否则 `enabled_platforms()` 还看着旧值，
        刚被关掉的平台照样能被采集。与 `invalidate` 合在一起：换配置 + 换实例一个动作。
        """
        self._configs[name] = config
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
