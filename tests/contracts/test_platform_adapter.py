"""L2 适配器契约测试抽象基类（`docs/specs/contract-tests.md §4`）。

**这是 V3 重写的验收门禁**：任何一个平台适配器（新加的，或被重写成别的语言的）只要
提供一个 `build(tmp_path)` 造出实例，继承本类就自动拿到整套通用契约。基类一字不改。

通用契约收的是"跨平台都成立"的那部分，各条都对应 V1 §7 的一处陷阱：
- 名字规范 / `isinstance(..., PlatformAdapter)`（§7.10 结构：实现必须满足 Protocol）
- `capabilities` 与类级声明一致（§7.3 顺序的唯一真源）
- `config_schema()` 是 `PlatformConfig` 子类（`/api/platforms/{name}/schema` 的前提）
- `healthcheck()` 回结构化报告，且 **`is_healthy` 只认显式 ok**（§7.20"测不到≠正常"）
- `parse_creator_url` 交回的不是 URL（§7.1 短链/落地页不含身份）
- 不支持字幕的平台 `fetch_subtitles` **返回 None 而不是抛**（`platforms/base.py` 契约）

平台**特有**的深水区（cookie 阶梯 argv、DASH 分片配对、桥 503 自愈…）留在各自的
`test_<platform>_adapter.py` 里，本基类不重复。

异步用例靠 `asyncio_mode=auto` 被 pytest 自动跑，不需要手动包 loop。
"""

from __future__ import annotations

import abc
import re
from typing import TYPE_CHECKING

from tests.contracts import test_bilibili_adapter as _bili
from tests.contracts import test_douyin_adapter as _dy

from intelligence_hub_v2.platforms.base import (
    Capabilities,
    PlatformAdapter,
    PlatformConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.contracts.test_bilibili_adapter import BilibiliAdapter
    from tests.contracts.test_douyin_adapter import DouyinAdapter

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_HEALTHY_STATUSES = {"ok", "degraded", "unreachable", "unknown"}
_COMPONENT_STATUSES = {"ok", "degraded", "unreachable"}


class PlatformAdapterContractTests(abc.ABC):
    """所有平台适配器共享的契约用例。子类实现 `build` / 三个声明钩子。"""

    @abc.abstractmethod
    def build(self, tmp_path: Path) -> PlatformAdapter:
        """用 mock 依赖造一个被测适配器实例（不碰网络 / 浏览器 / 真二进制）。"""

    @abc.abstractmethod
    def resolvable_profile_url(self) -> str:
        """一个**能离线解析**（不需跟 302）的博主主页链接，喂给 parse_creator_url。"""

    # ---- 通用契约 ----

    def test_platform_name_is_a_valid_token(self, tmp_path: Path) -> None:
        adapter = self.build(tmp_path)
        assert _NAME_RE.match(adapter.name), f"平台名不合规: {adapter.name!r}"

    def test_is_registered_as_platform_adapter(self, tmp_path: Path) -> None:
        # runtime_checkable Protocol 的 isinstance 只查方法在不在（签名靠 mypy 那一层）。
        assert isinstance(self.build(tmp_path), PlatformAdapter)

    def test_capabilities_are_frozen_declaration(self, tmp_path: Path) -> None:
        adapter = self.build(tmp_path)
        caps = adapter.capabilities
        assert isinstance(caps, Capabilities)
        assert isinstance(caps.cookie_variants, tuple), "阶梯顺序必须是不可变 tuple"
        # 声明式：能力是 ClassVar 级、跑起来不许被改。改一个字段要能抛。
        try:
            caps.needs_browser = not caps.needs_browser  # type: ignore[misc]
        except (AttributeError, TypeError, NotImplementedError):
            pass
        else:
            raise AssertionError("Capabilities 必须是 frozen dataclass，居然被改写了")

    def test_config_schema_is_platform_config_subclass(self, tmp_path: Path) -> None:
        adapter = self.build(tmp_path)
        assert issubclass(adapter.config_schema(), PlatformConfig)

    async def test_healthcheck_returns_structured_report(self, tmp_path: Path) -> None:
        adapter = self.build(tmp_path)
        report = await adapter.healthcheck()
        assert report.platform == adapter.name
        assert report.status in _HEALTHY_STATUSES, f"非法健康值: {report.status}"
        assert report.checked_at.tzinfo is not None
        for value in report.components.values():
            assert value in _COMPONENT_STATUSES
        # V1 §7.20：只有显式 ok 算健康。测不到（unknown / None detail 的降级）不许冒绿。
        assert report.is_healthy == (report.status == "ok")

    async def test_parse_creator_url_yields_non_url_platform_id(self, tmp_path: Path) -> None:
        # V1 §7.1：platform_id 是平台原生 ID，不是 URL 里的东西。
        adapter = self.build(tmp_path)
        ref = await adapter.parse_creator_url(self.resolvable_profile_url())
        assert ref.platform == adapter.name
        assert ref.platform_id
        assert not str(ref.platform_id).startswith("http")
        assert str(ref.profile_url).startswith("http")

    async def test_unsupported_subtitles_returns_none_not_raise(self, tmp_path: Path) -> None:
        """`supports_subtitles=False` 的平台 `fetch_subtitles` 直接回 None（契约）。

        抛异常会让"每条作品都先失败一次"变成常态，调度器要的是"没字幕，去走 ASR"。
        支持字幕的平台这条不约束（交给各自的具体测试验字幕解析）。
        """
        adapter = self.build(tmp_path)
        if adapter.capabilities.supports_subtitles:
            return
        video = await self._one_video(adapter)
        assert video is not None
        assert await adapter.fetch_subtitles(video) is None

    async def _one_video(self, adapter: PlatformAdapter):
        raise NotImplementedError


class TestDouyinContract(PlatformAdapterContractTests):
    def build(self, tmp_path: Path) -> DouyinAdapter:
        return _dy.make_adapter(tmp_path)

    def resolvable_profile_url(self) -> str:
        return str(_dy.profile_ref().profile_url)

    async def _one_video(self, adapter):
        return _dy.make_video()


class TestBilibiliContract(PlatformAdapterContractTests):
    def build(self, tmp_path: Path) -> BilibiliAdapter:
        return _bili.make_adapter(tmp_path)

    def resolvable_profile_url(self) -> str:
        return "https://space.bilibili.com/12345678"
