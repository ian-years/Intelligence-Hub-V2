"""L2 适配器契约测试抽象基类（`docs/specs/contract-tests.md §4`）。

**这是 V3 重写的验收门禁**：任何一个平台适配器（新加的，或被重写成别的语言的）只要
提供 §4.2 那六个钩子，继承本类就自动拿到 §4.1 那整套通用契约。基类一字不改。

通用契约收的是"跨平台都成立"的那部分，各条都对应 V1 §7 的一处陷阱：
- 名字规范 / `isinstance(..., PlatformAdapter)`（§7.10 结构：实现必须满足 Protocol）
- `capabilities` 等于子类声明的快照，且与配置里那几个镜像字段一致（§7.3 顺序的唯一真源）
- `config_schema()` 是 `PlatformConfig` 子类（`/api/platforms/{name}/schema` 的前提）
- `healthcheck()` 回结构化报告，且 **`is_healthy` 只认显式 ok**（§7.20"测不到≠正常"）
- `parse_creator_url` 交回的不是 URL（§7.1 短链/落地页不含身份）
- `list_creator_videos` 流式产出的 `VideoMeta` 形状与 `limit` 上限
- `download_media` 的产物**说得出走了哪条路**，兜底那一路必须带 yt-dlp 原文（§7.2）
- 不支持字幕的平台 `fetch_subtitles` **返回 None 而不是抛**（`platforms/base.py` 契约）

平台**特有**的深水区（cookie 阶梯 argv 长什么样、DASH 怎么配对、桥 503 自愈…）留在各自的
`test_<platform>_adapter.py` 里，本基类不重复。

异步用例靠 `asyncio_mode=auto` 被 pytest 自动跑，不需要手动包 loop。
"""

from __future__ import annotations

import abc
import re
from typing import TYPE_CHECKING

import pytest
from tests.contracts import test_bilibili_adapter as _bili
from tests.contracts import test_douyin_adapter as _dy
from tests.contracts import test_xiaohongshu_adapter as _xhs

from intelligence_hub_v2.models.media import MediaArtifact, VideoAudioPairArtifact
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.base import (
    Capabilities,
    PlatformAdapter,
    PlatformConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.contracts.test_bilibili_adapter import BilibiliAdapter
    from tests.contracts.test_douyin_adapter import DouyinAdapter
    from tests.contracts.test_xiaohongshu_adapter import XiaohongshuAdapter

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_HEALTHY_STATUSES = {"ok", "degraded", "unreachable", "unknown"}
_COMPONENT_STATUSES = {"ok", "degraded", "unreachable"}
_MEDIA_SOURCES = {"yt_dlp", "page_play_url", "dash_merged", "dash_split"}


def _artifact_paths(artifact: MediaArtifact) -> tuple[Path, ...]:
    """产物落在磁盘上的文件。**不许 `rglob` 猜**（V1 §7.21 的教训就是扫目录扫出来的）。"""
    if isinstance(artifact, VideoAudioPairArtifact):
        return (artifact.video_path, artifact.audio_path)
    return (artifact.path,)


class PlatformAdapterContractTests(abc.ABC):
    """所有平台适配器共享的契约用例。子类实现 §4.2 那六个钩子。"""

    @abc.abstractmethod
    def build(self, tmp_path: Path) -> PlatformAdapter:
        """用 mock 依赖造一个被测适配器实例（不碰网络 / 浏览器 / 真二进制）。"""

    @abc.abstractmethod
    def expected_capabilities(self) -> Capabilities:
        """这个平台**应当**声明的能力快照（V1 §7.3 / §7.15 那条阶梯的顺序就在这里）。

        写第二份 `Capabilities` 不是"第二处真相"：真源仍然只有适配器类上那一份，
        这里是它的**快照断言**（同 `docs/specs/openapi-snapshot.json` 的位置）。
        改阶梯顺序的人必须同时改这里，否则红 —— 而那件事的代价是"画质掉了但没人知道为什么"。
        """

    @abc.abstractmethod
    def resolvable_profile_url(self) -> str:
        """一个**能离线解析**（不需跟 302）的博主主页链接，喂给 parse_creator_url。"""

    @abc.abstractmethod
    def video_fixture(self) -> VideoMeta:
        """一条该平台的 `VideoMeta`（`platform_video_id` 与 `creator_ref` 都自洽）。"""

    @abc.abstractmethod
    def listing_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlatformAdapter:
        """已经 primed 到"枚举 `resolvable_profile_url()` 能出至少一条"的适配器。

        返回 0 条 = 钩子没 primed，用例按失败处理（**不许** `pytest.skip`：
        V1 §7.14 的教训就是"绿色的 skip 会让看护静默消失"）。
        """

    @abc.abstractmethod
    def downloadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[PlatformAdapter, VideoMeta]:
        """一对能离线走完一次 `download_media()` 的 (适配器, 视频)。

        声明 `media_strategy='yt_dlp_with_fallback'` 的平台必须 primed 成
        **yt-dlp 失败 → 走兜底**，因为"原文有没有留下来"正是那条用例要看的东西。
        """

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

    def test_capabilities_match_expected(self, tmp_path: Path) -> None:
        """声明与快照相等 —— 顺序、要不要桥、支持不支持字幕，一次改动都跑不掉。"""
        assert self.build(tmp_path).capabilities == self.expected_capabilities()

    def test_capabilities_agree_with_the_config_mirrors(self, tmp_path: Path) -> None:
        """`capabilities` 与配置默认值那三个镜像字段必须一致。

        那三个字段（`use_cdp_bridge` / `media_strategy` / `list_strategy`）现在标着
        `ui:hidden`、**没有读取路径**（ADR-0012）：它们唯一的价值就是把声明回显给
        看 yaml 的人。回显错了比不回显更糟 —— 一个人照着 yaml 以为"这平台不走桥"。
        """
        caps = self.build(tmp_path).capabilities
        defaults = self.build(tmp_path).config_schema()()
        assert caps.list_strategy == defaults.list_strategy
        assert caps.media_strategy == defaults.media_strategy
        assert caps.needs_browser == defaults.use_cdp_bridge

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

    async def test_list_creator_videos_streams_well_formed_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """流式枚举的产出形状是契约：身份非空、平台名对得上、`limit` 是上限不是建议。

        各平台自己的"页面脏行要不要丢 / 日期怎么解析 / `since` 能不能过滤"那类
        深水区在 `test_<platform>_adapter.py`，这里只钉跨平台都成立的那几条。
        """
        adapter = self.listing_adapter(tmp_path, monkeypatch)
        ref = await adapter.parse_creator_url(self.resolvable_profile_url())
        seen: list[VideoMeta] = []
        async for meta in adapter.list_creator_videos(ref, limit=2):
            seen.append(meta)
            assert meta.platform == adapter.name
            assert meta.platform_video_id, "没有平台原生 ID 的记录进库就是孤儿"
            assert meta.title
            assert str(meta.webpage_url).startswith("http")
            assert meta.creator_ref.platform_id == ref.platform_id, (
                "枚举出来的作品必须挂在请求的那个 ref 下"
            )
        assert seen, "listing_adapter() 没 primed 到能出东西（这不是「平台没视频」，别 skip）"
        assert len(seen) <= 2, "`limit` 被超发：调度器的取消与配额全建立在它是上限这件事上"

    async def test_download_media_reports_how_it_got_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """产物必须说清走了哪条路；兜底那一路必须留着 yt-dlp 的原文（V1 §7.2）。

        丢原文的后果不是"日志难看"，是**永远判断不出该修什么** —— V1 那句
        "看起来在跑"就是这么来的。只有声明了兜底的平台能触发后半句，所以后半句
        按 `capabilities.media_strategy` 分档，而不是拿一个平台的形状去要求所有平台。
        """
        adapter, video = self.downloadable(tmp_path, monkeypatch)
        artifact = await adapter.download_media(video, dest=tmp_path)

        assert artifact.media_source in _MEDIA_SOURCES
        for path in _artifact_paths(artifact):
            assert path.is_file(), f"{artifact.media_source} 声称拿到了 {path}，但文件不在"
            assert path.stat().st_size > 0, "零字节文件比失败更难查"

        fell_back = artifact.media_source != "yt_dlp"
        if adapter.capabilities.media_strategy == "yt_dlp_with_fallback" and fell_back:
            assert artifact.yt_dlp_error, "兜底成功却把 yt-dlp 的失败原文丢了（V1 §7.2）"

    async def test_unsupported_subtitles_returns_none_not_raise(self, tmp_path: Path) -> None:
        """`supports_subtitles=False` 的平台 `fetch_subtitles` 直接回 None（契约）。

        抛异常会让"每条作品都先失败一次"变成常态，调度器要的是"没字幕，去走 ASR"。
        支持字幕的平台这条不约束（交给各自的具体测试验字幕解析）。
        """
        adapter = self.build(tmp_path)
        if adapter.capabilities.supports_subtitles:
            return
        assert await adapter.fetch_subtitles(self.video_fixture()) is None


class TestDouyinContract(PlatformAdapterContractTests):
    def build(self, tmp_path: Path) -> DouyinAdapter:
        return _dy.make_adapter(tmp_path)

    def expected_capabilities(self) -> Capabilities:
        return Capabilities(
            needs_browser=True,
            needs_cookies=True,
            # 阶梯顺序是 ADR-0011 定的唯一真源；改这一行等于改契约。
            cookie_variants=("exported_file", "browser", "none"),
            supports_subtitles=False,
            supports_dash_split=False,
            list_strategy="browser_scroll",
            media_strategy="yt_dlp_with_fallback",
        )

    def resolvable_profile_url(self) -> str:
        return str(_dy.profile_ref().profile_url)

    def video_fixture(self) -> VideoMeta:
        return _dy.make_video()

    def listing_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlatformAdapter:
        del monkeypatch  # 抖音这条路只经桥，不经 subprocess。
        return _dy.make_adapter(
            tmp_path,
            bridge=_dy.FakeBridge(
                script=[
                    _dy.page_payload("profile_page.json"),
                    _dy.page_payload("scroll_page.json"),
                ]
            ),
        )

    def downloadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[PlatformAdapter, VideoMeta]:
        """primed 成"yt-dlp 被风控挡下 → 走页面播放直链"，也就是 V1 §7.2 的常态。"""
        harness = _dy.media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            runner=_dy.FakeYtDlpRunner(result=_dy.ytdlp_blocked()),
        )
        return harness.adapter, _dy.make_video()


class TestBilibiliContract(PlatformAdapterContractTests):
    def build(self, tmp_path: Path) -> BilibiliAdapter:
        return _bili.make_adapter(tmp_path)

    def expected_capabilities(self) -> Capabilities:
        return Capabilities(
            needs_browser=False,
            needs_cookies=True,
            # 三档顺序是 V1 §7.15 用画质差换来的（登录档 1772p vs 匿名 886p）。
            cookie_variants=("exported_file", "browser", "anonymous"),
            supports_subtitles=True,
            supports_dash_split=True,
            list_strategy="yt_dlp_flat",
            media_strategy="yt_dlp",
        )

    def resolvable_profile_url(self) -> str:
        return "https://space.bilibili.com/12345678"

    def video_fixture(self) -> VideoMeta:
        return _bili.bili_video()

    def listing_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlatformAdapter:
        return _bili.make_adapter(
            tmp_path,
            runner=_bili.FakeYtDlpRunner(
                result=_bili.ok_result(stdout=_bili.fixture_text("flat_playlist.jsonl"))
            ),
            monkeypatch=monkeypatch,
        )

    def downloadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[PlatformAdapter, VideoMeta]:
        """合并成功的那一趟（`media_strategy='yt_dlp'`，没有兜底这一步）。

        未合并 DASH 分片怎么报、`merge` 档怎么失败，是 B站 独有的深水区，
        留在 `test_bilibili_adapter.py`。
        """
        merged = _bili.touch(tmp_path / "media.mp4", size=7000)
        runner = _bili.FakeYtDlpRunner(
            result=_bili.ok_result(stdout=f"[download] Destination: {merged}\n")
        )
        return (
            _bili.make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch),
            _bili.bili_video(),
        )


class TestXiaohongshuContract(PlatformAdapterContractTests):
    """小红书：第三个平台，也是第二个"只能在已登录浏览器里跑"的平台。

    钩子全部转调 `test_xiaohongshu_adapter.py` 里那套假件 —— 那里才是深水区，
    这一层只钉跨平台都成立的那几条。
    """

    def build(self, tmp_path: Path) -> XiaohongshuAdapter:
        return _xhs.make_adapter(tmp_path)

    def expected_capabilities(self) -> Capabilities:
        return Capabilities(
            needs_browser=True,
            needs_cookies=True,
            # 阶梯顺序是 ADR-0011 的第二份快照：改它等于改契约，两家必须一起红。
            cookie_variants=("exported_file", "browser", "none"),
            supports_subtitles=False,
            supports_dash_split=False,
            list_strategy="browser_scroll",
            media_strategy="yt_dlp_with_fallback",
        )

    def resolvable_profile_url(self) -> str:
        return str(_xhs.profile_ref().profile_url)

    def video_fixture(self) -> VideoMeta:
        return _xhs.make_video()

    def listing_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlatformAdapter:
        del monkeypatch  # 小红书只经桥，不经 subprocess。
        return _xhs.make_adapter(
            tmp_path,
            bridge=_xhs.FakeBridge(
                script=[
                    _xhs.page_payload("profile_page.json"),
                    _xhs.page_payload("scroll_page.json"),
                ]
            ),
        )

    def downloadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[PlatformAdapter, VideoMeta]:
        """primed 成"yt-dlp 失败 → 退页面 masterUrl"，也就是 §7.2 那条常态。"""
        harness = _xhs.media_harness(
            tmp_path,
            monkeypatch=monkeypatch,
            runner=_xhs.FakeYtDlpRunner(result=_xhs.ytdlp_blocked()),
        )
        return harness.adapter, _xhs.make_video()
