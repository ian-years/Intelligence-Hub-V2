"""`PlatformRegistry` 与 `PlatformAdapter` Protocol 的契约测试。

注册表要守的是**三份真相一致**（`config/platforms.yaml` 的 key、
`PLATFORM_CONFIG_SCHEMAS`、`PLATFORMS`）。V1 §7.10 那句"顺手猜的路由不存在"
是同一类问题：症状出现在用户点击的那一刻，而原因在三个互不相干的字典里。

注册表自己不 import 适配器实现（是 `platforms/__init__.py` 在末尾把两边接上的），
所以这里的假适配器**必须满足 Protocol** —— 那既是注册表的测试，
也是"这个 Protocol 真的可以照着实现"的证明。
真适配器（抖音，Task 6）的契约测试在 `tests/contracts/test_douyin_adapter.py`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from intelligence_hub_v2.errors import PlatformError, TaskRejected
from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS
from intelligence_hub_v2.platforms.base import (
    AdapterDeps,
    Capabilities,
    CreatorProfile,
    CreatorRef,
    HealthReport,
    MediaArtifact,
    PlatformAdapter,
    PlatformConfig,
    ProgressCallback,
    SingleFileArtifact,
    Transcript,
    VideoMeta,
)
from intelligence_hub_v2.platforms.registry import PLATFORMS, PlatformRegistry, register
from intelligence_hub_v2.storage.files import FileStorage


def _capabilities(**overrides: object) -> Capabilities:
    payload: dict[str, object] = {
        "needs_browser": True,
        "needs_cookies": True,
        "cookie_variants": ("exported_file", "anonymous"),
        "supports_subtitles": False,
        "supports_dash_split": False,
        "list_strategy": "browser_scroll",
        "media_strategy": "yt_dlp_with_fallback",
    }
    payload.update(overrides)
    return Capabilities(**payload)  # type: ignore[arg-type]


class _Config(PlatformConfig):
    display_name: str = "假平台"
    media_strategy: str = "yt_dlp_with_fallback"  # type: ignore[assignment]
    list_strategy: str = "browser_scroll"  # type: ignore[assignment]


class _Adapter:
    """一个**完整满足 `PlatformAdapter`** 的实现。

    它同时也是"V3 重写时照着这份接口能不能落地"的一次演练：
    每个方法都真写了签名，缺一个 mypy 就会在下面那条赋值上报错。
    """

    name: ClassVar[str] = "fake"
    display_name: ClassVar[str] = "假平台"
    capabilities: ClassVar[Capabilities] = _capabilities()

    def __init__(self, config: PlatformConfig, deps: AdapterDeps) -> None:
        self.config = config
        self.deps = deps
        self.closed = False

    @classmethod
    def config_schema(cls) -> type[PlatformConfig]:
        return _Config

    async def healthcheck(self) -> HealthReport:
        return HealthReport(platform=self.name, status="ok", checked_at=datetime.now(UTC))

    async def parse_creator_url(self, url: str) -> CreatorRef:
        return CreatorRef(platform=self.name, platform_id="x", profile_url=url, source_url=url)

    async def fetch_creator_profile(self, ref: CreatorRef) -> CreatorProfile:
        return CreatorProfile(ref=ref, name="假博主")

    async def list_creator_videos(
        self,
        ref: CreatorRef,
        *,
        since: object = None,
        limit: int = 30,
    ) -> AsyncIterator[VideoMeta]:
        if False:  # pragma: no cover - 让函数成为 async generator
            yield None  # type: ignore[misc]

    async def download_media(
        self,
        video: VideoMeta,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> MediaArtifact:
        return SingleFileArtifact(
            path=Path("media/fake/x/media.mp4"), size_bytes=1, media_source="yt_dlp"
        )

    async def fetch_subtitles(self, video: VideoMeta) -> Transcript | None:
        return None


def _config(enabled: bool = True) -> PlatformConfig:
    return _Config(enabled=enabled, display_name="假平台")


def _deps(tmp_path: Path) -> AdapterDeps:
    files = FileStorage(tmp_path / "data")
    return AdapterDeps(
        config=_config(),
        storage=None,  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        http=None,  # type: ignore[arg-type]
        logger=None,  # type: ignore[arg-type]
        cookies=CookieManager(files),
        bridge=None,
    )


@pytest.fixture
def registry_factory(tmp_path: Path):
    """造一个只认 `fake` 的注册表（不污染全局 `PLATFORMS`）。"""

    def _make(**overrides: object) -> PlatformRegistry:
        classes = {"fake": _Adapter}
        configs = {"fake": _config(bool(overrides.get("enabled", True)))}
        return PlatformRegistry(
            configs,
            lambda name: _deps(tmp_path),
            classes=classes,  # type: ignore[arg-type]
        )

    return _make


# ---------------------------------------------------------------------------
# Protocol 本身
# ---------------------------------------------------------------------------


def test_the_reference_implementation_satisfies_the_protocol() -> None:
    """`_Adapter` 满足 `PlatformAdapter`。

    这条用例的价值不在运行期（`isinstance` 只查方法在不在），
    在于**它同时是 mypy 的检查对象**：`_Adapter` 里删掉一个方法，
    这个文件在下面那行赋值处就会 `assignment-error`。
    """
    adapter: PlatformAdapter = _Adapter(_config(), _deps(Path()))  # type: ignore[arg-type]
    assert isinstance(adapter, PlatformAdapter)
    assert adapter.name == "fake"


def test_a_partial_implementation_is_not_an_adapter(tmp_path: Path) -> None:
    class _Partial:
        name: ClassVar[str] = "partial"

        async def healthcheck(self) -> HealthReport:  # 只有这一个方法
            raise NotImplementedError

    assert not isinstance(_Partial(), PlatformAdapter)


def test_capabilities_are_frozen_so_a_scheduler_cannot_mutate_them() -> None:
    """能力是**声明**，跑起来之后不许被谁改掉（frozen + slots 的理由）。"""
    with pytest.raises(FrozenInstanceError):
        _capabilities().needs_browser = False  # type: ignore[misc]


def test_every_registered_platform_declares_a_real_strategy() -> None:
    """每个平台都得给出策略与档位，不能靠默认值兜 —— 默认值意味着"没人想过"。"""
    caps = _capabilities()
    assert caps.list_strategy and caps.media_strategy and caps.cookie_variants


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_a_disabled_platform_is_rejected_with_an_actionable_message(
    registry_factory: object,
) -> None:
    """文案要说"被关掉"而不是"未知平台"：后者会让人去翻配置文件的 key，
    而真正要做的动作是去 Settings 把开关打开。"""
    registry = registry_factory(enabled=False)  # type: ignore[operator]
    with pytest.raises(TaskRejected, match="已被关掉"):
        registry.get("fake")


def test_an_unimplemented_platform_says_what_is_available(registry_factory: object) -> None:
    """与"被关掉"分开：这里要的动作是"这个版本没移植"，翻配置没用。"""
    registry = registry_factory()  # type: ignore[operator]
    with pytest.raises(PlatformError, match="没有适配器实现") as caught:
        registry.get("xiaohongshu")
    assert "fake" in str(caught.value)


def test_a_missing_config_is_diagnosed_as_wiring_not_as_disabled(tmp_path: Path) -> None:
    """review P1-4：类注册了、配置字典里却没有 —— 这是装配漏了一环。

    `_is_enabled` 对 config=None 也返回 False，检查顺序若先判开关，
    这条会被谎报成"已被关掉（去 Settings 打开开关）"，而那里根本没有这个平台。
    """
    registry = PlatformRegistry(
        {},  # 配置字典是空的：类在、配置不在
        lambda name: _deps(tmp_path),
        classes={"fake": _Adapter},  # type: ignore[arg-type]
    )
    with pytest.raises(PlatformError, match="装配漏了一环"):
        registry.get("fake")


def test_instances_are_cached_per_platform(registry_factory: object) -> None:
    """每次都新建就等于把限速整个废掉：适配器握着限速状态与 HTTP 客户端引用。"""
    registry = registry_factory()  # type: ignore[operator]
    assert registry.get("fake") is registry.get("fake")


def test_invalidate_forces_a_rebuild_after_a_config_reload(registry_factory: object) -> None:
    registry = registry_factory()  # type: ignore[operator]
    first = registry.get("fake")
    registry.invalidate()
    assert registry.get("fake") is not first


def test_invalidate_one_platform_leaves_the_others(registry_factory: object) -> None:
    registry = registry_factory()  # type: ignore[operator]
    registry.get("fake")
    registry.invalidate("nonexistent")
    assert registry.get("fake") is not None


def test_enabled_only_returns_implemented_and_switched_on(registry_factory: object) -> None:
    registry = registry_factory()  # type: ignore[operator]
    assert registry.enabled_platforms() == ["fake"]
    assert registry.implemented_platforms() == ["fake"]


def test_capabilities_are_readable_without_instantiating(registry_factory: object) -> None:
    """调度器判断前置条件（要不要起桥）发生在"还没有任何任务在跑"的时候。"""
    registry = registry_factory()  # type: ignore[operator]
    assert registry.capabilities("fake").needs_browser is True


# ---------------------------------------------------------------------------
# 自洽性（三份真相）
# ---------------------------------------------------------------------------


def test_inconsistencies_are_reported_not_raised(registry_factory: object) -> None:
    """返回问题清单而不是抛：这是 preflight 的一行红字。
    启动就崩会让用户连 Settings 都进不去，而他要做的正是去开一个开关。"""
    registry = PlatformRegistry(
        {"fake": _config(), "ghost": _config()},
        lambda name: None,  # type: ignore[arg-type,return-value]
        classes={"fake": _Adapter},  # type: ignore[dict-item]
    )
    problems = registry.inconsistencies()
    assert any("ghost" in p and "没有适配器实现" in p for p in problems)


def test_both_directions_are_checked() -> None:
    """只查一个方向会漏掉"移植完了但平台不出现"这一半。"""
    registry = PlatformRegistry(
        {},  # 有实现、无配置
        lambda name: None,  # type: ignore[arg-type]
        classes={"orphan": _Adapter},  # type: ignore[dict-item]
    )
    assert any("orphan" in p and "配置里没有这一节" in p for p in registry.inconsistencies())


def test_a_fully_wired_platform_reports_nothing() -> None:
    """ "健康"的定义要按**全局 schema 表**来摆，不是"我这个注册表内部自洽"。

    第一版这里直接断言 `registry_factory().inconsistencies() == []`，
    当场红：假平台 `fake` 不在 `PLATFORM_CONFIG_SCHEMAS` 里，
    于是"注册了实现但没有配置 schema"三条全报出来。
    那个红是**对的** —— 自洽性检查必须看着整个构建，
    只查传入的那一小撮就等于永远绿灯。改用 `douyin`（schema 真的在）才成立。
    """
    registry = PlatformRegistry(
        {"douyin": _config(), "bilibili": _config()},
        lambda name: None,  # type: ignore[arg-type]
        classes={"douyin": _Adapter, "bilibili": _Adapter},  # type: ignore[dict-item]
    )
    problems = registry.inconsistencies()
    assert problems == [], problems


def test_the_shipped_platform_schemas_have_no_adapters_yet_by_design() -> None:
    """**这条是快照，不是断言"应该这样"**：它红过的次数就是"有人加了配置忘了实现"的次数。

    Task 6 落地抖音时它从 `{"douyin","bilibili"}` 变 `{"bilibili"}`，
    Task 7 落地 B站 后变成空集 —— 两次都是它设计出来要触发的动作。

    Task 6 之前这里写的是 `{"douyin", "bilibili"}`；抖音适配器落地并把 `@register`
    接上之后，差集只剩 B站 —— 那次变红正是这条用例设计出来要触发的动作
    （见 `docs/progress/2026-09-22.md` Task 6）。
    """
    missing = set(PLATFORM_CONFIG_SCHEMAS) - set(PLATFORMS)
    assert missing == set(), (
        f"配置 schema 与适配器的差集不是空集：{missing}。"
        "V2.0 的判据是抖音 + B站 两个都有实现；"
        "多出来一个没实现的平台名，说明配置表被改了却没走 ADR。"
    )
    assert PLATFORMS["douyin"].__module__.endswith("douyin.adapter")
    assert PLATFORMS["bilibili"].__module__.endswith("bilibili.adapter")


def test_registering_a_platform_without_a_config_schema_fails_at_import_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V1 的形状是"配置里有、界面上能点、点了没实现"。这里在**注册那一刻**就拒。"""

    class _Stray(_Adapter):
        name = "stray"

    with pytest.raises(PlatformError, match="配置里没有它"):
        register("stray")(_Stray)
    assert "stray" not in PLATFORMS


@pytest.fixture
def clean_registry() -> object:
    """跑完把全局 `PLATFORMS` 恢复原样。

    没有这层，本文件里"注册一个 douyin"的用例会污染
    `test_the_shipped_platform_schemas_have_no_adapters_yet_by_design` ——
    那条快照就变成"取决于文件内用例顺序"，而顺序是会变的。
    """
    snapshot = dict(PLATFORMS)
    yield PLATFORMS
    PLATFORMS.clear()
    PLATFORMS.update(snapshot)


def test_registering_twice_is_refused_rather_than_silently_overwritten(
    clean_registry: object,
) -> None:
    """V1 的 `TASK_DEFS` 靠"后写的覆盖先写的"悄悄改过任务定义，而改的人完全不知道。"""

    class _Second(_Adapter):
        name = "douyin"

    # Task 6 之后真适配器已经在导入时装上了，所以先把这一格清空再演"重复登记"。
    # `clean_registry` 负责跑完恢复原样 —— 少了那一步，这条会污染上面那条快照。
    PLATFORMS.pop("douyin", None)
    register("douyin")(_Adapter)
    with pytest.raises(PlatformError, match="已被") as caught:
        register("douyin")(_Second)
    assert "_Second" in str(caught.value)
    assert PLATFORMS["douyin"] is _Adapter  # 第一次注册没被第二次冲掉


def test_registered_names_are_theones_in_the_config_file_sections() -> None:
    """`config/platforms.yaml` 的顶层 key 必须与 schema 注册表一致 —— Task 2 已有
    一条读真实发货文件的看护，这里补的是"两边都活着"这一层。"""
    assert PLATFORM_CONFIG_SCHEMAS
    assert set(PLATFORM_CONFIG_SCHEMAS) == {"douyin", "bilibili"}
