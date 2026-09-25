"""YouTube Adapter 契约测试（V2.1 T2.2）。

**契约基类那份**：`TestYoutubeContract` 继承 `PlatformAdapterContractTests`，
实现 §4.2 那六个钩子就自动拿到整套通用契约（名字规范、能力快照、`VideoMeta` 形状、
产物说得出走了哪条路、不支持字幕时返回 None…）。按本仓库的约定，子类就写在
这个平台自己的文件里（`tests/contracts/test_platform_adapter.py` 是**别人的**契约面，
加平台不该去改它）。

网络与子进程仍然是假的：`httpx.MockTransport` + `FakeYtDlpRunner`，
所以这一整套离线可跑、且**不会**向 YouTube 发出任何请求。
本会话这台机器到 YouTube 不通，所以"真采一条"那一跑没做（见任务汇报的未验清单）。

YouTube 独有的深水区在本文件后半部分，每条都对应一个"两种判法后果不同"的地方：

- 时间窗：`since` 传与不传、窗外、**日期缺失**（V1 `--recent-days` 的语义）
- `-j` 那份契约常量：argv 与解析器共用（2026-09-24 的 P0）
- DASH 分片：声明 `supports_dash_split=False` 就必须**判失败**而不是挑一条交出去
  （V1 的 `glob("*.mp4")` 恰好会挑出纯视频轨）
- 字幕：确认没有才 None，问不出来要抛（吞成 None = 一次网络故障换一整轮白烧的 ASR）
- 不带 cookie：`cookie_variants=("none",)` 必须有真实 argv 路径，且 argv 里
  真的不出现 `--cookies`
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.contracts._doubles import FakeYtDlpRunner
from tests.contracts.test_platform_adapter import PlatformAdapterContractTests

from intelligence_hub_v2.errors import ListError, MediaDownloadError, PlatformError
from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.infra.ytdlp import YtDlpResult
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.base import AdapterDeps, Capabilities
from intelligence_hub_v2.platforms.bilibili import media as bili_media
from intelligence_hub_v2.platforms.youtube import adapter as adapter_module
from intelligence_hub_v2.platforms.youtube import listing, media
from intelligence_hub_v2.platforms.youtube.adapter import YouTubeAdapter
from intelligence_hub_v2.platforms.youtube.config import YouTubeConfig
from intelligence_hub_v2.storage.files import FileStorage

CHANNEL_ID = "UCKC8SZ0cdXPnnkKkQQLJmsQ"
VIDEO_ID = "aqzZKE2FJDg"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "youtube"
PROBE_URL = "https://www.youtube.com/"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fast_config(**overrides: object) -> YouTubeConfig:
    payload: dict[str, object] = {
        "display_name": "YouTube",
        "rate_limit": {"per_minute": 100_000, "per_creator_seconds": 0.0},
        **overrides,
    }
    return YouTubeConfig.model_validate(payload)


def ok_result(
    *, stdout: str = "", stderr: str = "", artifacts: Sequence[Path] = (), variant: Any = None
) -> YtDlpResult:
    return YtDlpResult(
        ok=True,
        variant=variant,
        stdout=stdout,
        stderr=stderr,
        returncode=0,
        artifacts=tuple(artifacts),
        attempts=(("none（本平台不需要登录态）", 0, ""),),
    )


def failed_result(stderr: str, *, returncode: int = 1) -> YtDlpResult:
    return YtDlpResult(
        ok=False,
        variant=None,
        stdout="",
        stderr=stderr,
        returncode=returncode,
        attempts=(("none（本平台不需要登录态）", returncode, stderr.strip()[:60]),),
    )


def default_client(*, reachable: bool = True) -> httpx.AsyncClient:
    """默认客户端：只答探活那一格，**其余真请求一律炸**。

    探活是 `healthcheck()` 唯一合法的出站请求（它回答的就是"到不到得了 YouTube"），
    所以给它固定 200 或固定的 `ConnectError`；其它路径落到 `_refuse` ——
    用例忘了配路由时得到的是"契约测试发出了真请求"，而不是意外通过。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PROBE_URL:
            if reachable:
                return httpx.Response(200, text="ok")
            raise httpx.ConnectError("Connection refused (fake) ", request=request)
        return _refuse(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _refuse(request: httpx.Request) -> httpx.Response:
    msg = f"契约测试发出了真请求：{request.method} {request.url}"
    raise AssertionError(msg)


class RecordingRunner(FakeYtDlpRunner):
    """`FakeYtDlpRunner` + "把该落的文件真的落下去"。

    字幕那一趟必须**真的**在目录里产出 `.vtt`：`find_subtitle_files` 判的是目录内容，
    不写文件的假件会让"没有字幕轨"这一档永远测不到（而它是 None/抛 那条分界）。
    """

    def __init__(self, *, files: dict[str, str] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.files = dict(files or {})

    async def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        variants: Sequence[Any] = (),
        file_template: str = "%(id)s.%(ext)s",
        on_line: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        if self.result is not None and self.result.ok:
            dest_dir.mkdir(parents=True, exist_ok=True)
            for name, body in self.files.items():
                (dest_dir / name).write_text(body, encoding="utf-8")
        return await super().download(
            url,
            dest_dir,
            variants=variants,
            file_template=file_template,
            on_line=on_line,
            timeout=timeout,
        )


def make_deps(
    tmp_path: Path,
    *,
    config: YouTubeConfig | None = None,
    http: httpx.AsyncClient | None = None,
) -> AdapterDeps:
    return AdapterDeps(
        config=config or fast_config(),
        storage=None,  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        http=http or default_client(),
        logger=get_logger("test.youtube"),
        cookies=CookieManager(FileStorage(tmp_path / "data")),
        bridge=None,  # YouTube 不走桥（needs_browser=False）
    )


def make_adapter(
    tmp_path: Path,
    *,
    config: YouTubeConfig | None = None,
    http: httpx.AsyncClient | None = None,
    runner: FakeYtDlpRunner | None = None,
    subtitle_runner: FakeYtDlpRunner | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> YouTubeAdapter:
    adapter = YouTubeAdapter(config or fast_config(), make_deps(tmp_path, config=config, http=http))
    adapter.subtitle_scratch_root = tmp_path / "scratch-root"
    if runner is not None or subtitle_runner is not None:
        assert monkeypatch is not None, "替换 runner 要 monkeypatch 夹具"
        if runner is not None:
            monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: runner)
        if subtitle_runner is not None:
            monkeypatch.setattr(adapter, "_subtitle_runner", lambda: subtitle_runner)
    return adapter


def yt_ref(**updates: Any) -> CreatorRef:
    payload: dict[str, Any] = {
        "platform": "youtube",
        "platform_id": CHANNEL_ID,
        "profile_url": f"https://www.youtube.com/channel/{CHANNEL_ID}",
    }
    payload.update(updates)
    return CreatorRef(**payload)


def yt_video(**updates: Any) -> VideoMeta:
    payload: dict[str, Any] = {
        "platform": "youtube",
        "platform_video_id": VIDEO_ID,
        "creator_ref": yt_ref(),
        "title": "对标频道的一期口播（合成样本）",
        "webpage_url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
        "duration_seconds": 612.0,
    }
    payload.update(updates)
    return VideoMeta(**payload)


def entry(video_id: str, **fields: Any) -> str:
    payload: dict[str, Any] = {
        "id": video_id,
        "type": "video",
        "title": f"作品 {video_id}",
        "url": f"/watch?v={video_id}",
        "channel": "合成频道",
        "channel_id": CHANNEL_ID,
    }
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def stdout_of(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def touch(path: Path, *, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def patched_which(monkeypatch: pytest.MonkeyPatch, *, has: tuple[str, ...]) -> None:
    """pretend PATH 上只有 `has` 里那些命令。"""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in has else None)


# =========================================================================== #
# 契约基类
# =========================================================================== #


class TestYoutubeContract(PlatformAdapterContractTests):
    def build(self, tmp_path: Path) -> YouTubeAdapter:
        return make_adapter(tmp_path)

    def expected_capabilities(self) -> Capabilities:
        return Capabilities(
            needs_browser=False,
            # YouTube 的三条路都不带登录态：V1 那 630 行里一个 cookie 参数都没有。
            cookie_variants=("none",),
            needs_cookies=False,
            # 声明 True 就必须真去取（`fetch_subtitles` 在 media.py 那一节）。
            supports_subtitles=True,
            # 拿到分片时**判失败**，绝不交出一对文件 —— 所以这条声明是 False 而不是 True。
            supports_dash_split=False,
            list_strategy="yt_dlp_flat",
            media_strategy="yt_dlp",
        )

    def resolvable_profile_url(self) -> str:
        return f"https://www.youtube.com/channel/{CHANNEL_ID}"

    def video_fixture(self) -> VideoMeta:
        return yt_video()

    def listing_adapter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> YouTubeAdapter:
        return make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(result=ok_result(stdout=fixture_text("flat_playlist.jsonl"))),
            monkeypatch=monkeypatch,
        )

    def downloadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[YouTubeAdapter, VideoMeta]:
        merged = touch(tmp_path / "media.mp4")
        runner = FakeYtDlpRunner(
            result=ok_result(stdout=f"[download] Destination: {merged}\n", artifacts=(merged,))
        )
        monkeypatch.setattr(adapter_module, "has_audio_stream", _always_true)
        return make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch), yt_video()


async def _always_true(_path: Path) -> bool:
    return True


# =========================================================================== #
# 能力声明的每一格都要有实现路径
# =========================================================================== #


class TestDeclarationHasAnImplementationPath:
    def test_the_single_declared_rung_is_the_one_that_runs(self, tmp_path: Path) -> None:
        """`cookie_variants` 里声明了就必须有真实路径 —— 这里验它落到 argv 上是"什么都不加"。

        `plan_cookie_variants(("none",))` 给出的是一个 args 为空的档位，
        所以"声明了一档"与"argv 里没有 cookie 参数"是同一件事的两面。
        """
        adapter = make_adapter(tmp_path)
        ladder = adapter._ladder()
        assert [variant.kind for variant in ladder] == list(adapter.capabilities.cookie_variants)
        assert all(variant.args == () for variant in ladder), "这一档不该往 argv 里加 cookie"

    async def test_no_cookie_flag_reaches_either_command_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """枚举与下载**两条路**都不带 `--cookies`（V1 §7.15 那一族的反面：这里是"都不带"）。"""
        runner = FakeYtDlpRunner(result=ok_result(stdout=stdout_of(entry(VIDEO_ID)), artifacts=()))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        await adapter._flat_playlist(
            "https://www.youtube.com/channel/x/videos", playlist_items=1, stage="list"
        )
        # 这一趟**故意**让它失败（artifacts 为空）：本条要看的是 argv，
        # 不是"下载成功"，而失败之前那条命令已经带着档位发出去了。
        with pytest.raises(MediaDownloadError, match="没有产出可读的文件"):
            await adapter.download_media(yt_video(), tmp_path)
        assert len(runner.calls) == 2, "两条路都要跑到，只跑一条就成了半断言"
        for call in runner.calls:
            assert all(variant.args == () for variant in call["variants"]), call
            assert not any("--cookies" in str(part) for part in call.get("args", ()))

    def test_needs_browser_false_means_bridge_is_not_required(self, tmp_path: Path) -> None:
        adapter = make_adapter(tmp_path)
        assert adapter.capabilities.needs_browser is False
        assert adapter._deps.bridge is None

    def test_the_config_mirrors_are_hidden_not_wired(self, tmp_path: Path) -> None:
        """`cookies_file` 这一格必须是**隐藏且没人读**的（ADR-0012 的反向判据）。

        如果哪天有人真的开始给它拼 `--cookies`，
        `test_config_fields_have_readers.py` 会要求去掉 `ui:hidden`，
        而那条红才是"实现路径变了"的正确信号。
        """
        schema = YouTubeAdapter.config_schema()
        field = schema.model_fields["cookies_file"]
        extra = field.json_schema_extra
        assert isinstance(extra, dict) and extra.get("ui:hidden") is True


# =========================================================================== #
# 健康检查：网络不通要红成 unreachable 并交出原文
# =========================================================================== #


class TestHealthcheck:
    async def test_unreachable_network_carries_the_httpx_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patched_which(monkeypatch, has=("yt-dlp", "ffmpeg", "node"))
        adapter = make_adapter(tmp_path, http=default_client(reachable=False))
        report = await adapter.healthcheck()
        assert report.status == "unreachable"
        assert report.components["network"] == "unreachable"
        assert report.detail is not None
        assert "Connection refused" in report.detail, "原文没带出来，红了也不知道为什么红"
        assert report.is_healthy is False

    async def test_missing_yt_dlp_is_unreachable_here_not_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """与抖音那格不同：YouTube 没有第二条媒体路，没装就是采不了。"""
        patched_which(monkeypatch, has=("ffmpeg", "node"))
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components["yt_dlp"] == "unreachable"
        assert "没有第二条路" in (report.detail or "")
        assert report.status == "unreachable"

    async def test_missing_ffmpeg_is_degraded_not_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """枚举与字幕还能跑，所以这一格只把整体压到 degraded（合并产物拿不到）。"""
        patched_which(monkeypatch, has=("yt-dlp", "node"))
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components["ffmpeg"] == "degraded"
        assert report.components["yt_dlp"] == "ok"
        assert report.status == "degraded"

    async def test_node_is_reported_only_when_it_is_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patched_which(monkeypatch, has=("yt-dlp", "ffmpeg"))
        required = await make_adapter(tmp_path).healthcheck()
        assert required.components["node"] == "degraded"
        assert "node" in (required.detail or "").lower()

        off = await make_adapter(
            tmp_path, config=fast_config(advanced={"require_node": False})
        ).healthcheck()
        # `ComponentStatus` 里没有 "unknown"：**没探的那一格不该出现在 components 里**
        assert "node" not in off.components
        assert off.status == "ok", off.detail

    async def test_a_fully_provisioned_box_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patched_which(monkeypatch, has=("yt-dlp", "ffmpeg", "node"))
        report = await make_adapter(tmp_path).healthcheck()
        assert report.status == "ok"
        assert report.detail is None
        assert set(report.components) == {"yt_dlp", "ffmpeg", "node", "network"}

    async def test_the_probe_follows_the_configured_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配了 proxy 还用直连去探活 = 一个**假**的"网络不通"。

        这一格钉的是：探活与 yt-dlp 用的是同一个 `proxy` 值。
        """
        seen: dict[str, Any] = {}

        class FakeClient:
            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            async def get(self, url: str) -> httpx.Response:
                seen["url"] = url
                return httpx.Response(200, text="ok")

        monkeypatch.setattr(adapter_module.httpx, "AsyncClient", FakeClient)
        called: list[str] = []

        class NeverGet:
            async def get(self, url: str, **_kw: Any) -> httpx.Response:
                called.append(str(url))
                raise AssertionError("配了 proxy 却还走了共享客户端")

        adapter = make_adapter(tmp_path, config=fast_config(proxy="http://127.0.0.1:7890"))
        adapter._deps.http = NeverGet()  # type: ignore[attr-defined]
        patched_which(monkeypatch, has=("yt-dlp", "ffmpeg", "node"))
        report = await adapter.healthcheck()
        assert seen["proxy"] == "http://127.0.0.1:7890"
        assert seen["url"] == PROBE_URL
        assert called == []
        assert report.components["network"] == "ok"

    async def test_cookies_is_not_a_component_anywhere(self, tmp_path: Path) -> None:
        """`needs_cookies=False` 就不该问 cookie —— 问它等于让用户去修一个不存在的前置。"""
        report = await make_adapter(tmp_path).healthcheck()
        assert not any("cookie" in key for key in report.components)


# =========================================================================== #
# 身份解析
# =========================================================================== #


class TestParseCreatorUrl:
    async def test_a_channel_url_is_resolved_without_touching_yt_dlp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boom = FakeYtDlpRunner(raises=AssertionError("不该发请求"))
        adapter = make_adapter(tmp_path, runner=boom, monkeypatch=monkeypatch)
        ref = await adapter.parse_creator_url(f"https://www.youtube.com/channel/{CHANNEL_ID}")
        assert ref.platform_id == CHANNEL_ID
        assert str(ref.profile_url) == f"https://www.youtube.com/channel/{CHANNEL_ID}"
        assert boom.calls == []

    async def test_a_handle_is_resolved_by_asking_yt_dlp_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ok_result(stdout=stdout_of(entry(VIDEO_ID))))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        ref = await adapter.parse_creator_url("https://www.youtube.com/@synthetic_channel")
        assert ref.platform_id == CHANNEL_ID
        assert len(runner.calls) == 1
        assert runner.calls[0]["url"] == "https://www.youtube.com/@synthetic_channel/videos"

    async def test_a_video_link_resolves_its_own_channel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ok_result(stdout=stdout_of(entry(VIDEO_ID))))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        ref = await adapter.parse_creator_url(f"https://youtu.be/{VIDEO_ID}")
        assert ref.platform_id == CHANNEL_ID
        assert runner.calls[0]["url"] == f"https://www.youtube.com/watch?v={VIDEO_ID}"

    async def test_an_ambiguous_channel_answer_is_refused_not_guessed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跨频道播放列表里"挑第一个"= 把 A 的作品登记成 B 频道。"""
        mixed = stdout_of(
            entry(VIDEO_ID), entry("bQw4w9WgXc1", channel_id="UCzzzzzzzzzzzzzzzzzzzzzz")
        )
        adapter = make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(result=ok_result(stdout=mixed)),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(PlatformError, match="认不出频道身份"):
            await adapter.parse_creator_url("https://www.youtube.com/@synthetic_channel")

    async def test_a_failed_lookup_keeps_the_original_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(result=failed_result("ERROR: no such URL (fake)")),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(PlatformError) as caught:
            await adapter.parse_creator_url("https://www.youtube.com/@synthetic_channel")
        assert "no such URL (fake)" in str(caught.value)

    async def test_an_unrecognized_string_raises_parse_url(self, tmp_path: Path) -> None:
        adapter = make_adapter(tmp_path)
        with pytest.raises(PlatformError, match=r"认不出 YouTube 频道身份|应该是一个 http"):
            await adapter.parse_creator_url("随便一句话")
        with pytest.raises(PlatformError, match="链接是空的"):
            await adapter.parse_creator_url("   ")


# =========================================================================== #
# 博主资料
# =========================================================================== #


class TestFetchCreatorProfile:
    async def test_the_name_comes_from_the_entries_and_the_blanks_stay_blank(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ok_result(stdout=stdout_of(entry(VIDEO_ID))))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        profile = await adapter.fetch_creator_profile(yt_ref())
        assert profile.name == "合成频道"
        assert profile.ref.platform_id == CHANNEL_ID
        # 头像与订阅数**留 None**：flat 条目里的 thumbnail 是作品的封面，
        # 把它当头像存是"看起来有数据、越看越不对"。
        assert profile.avatar_url is None
        assert profile.follower_count is None
        assert "不猜" in str(profile.extra["note"])

    async def test_a_channel_that_reports_another_identity_is_a_hard_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = "UCaaaaaaaaaaaaaaaaaaaaaa"
        runner = FakeYtDlpRunner(
            result=ok_result(stdout=stdout_of(entry(VIDEO_ID, channel_id=other)))
        )
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(PlatformError, match="身份不一致"):
            await adapter.fetch_creator_profile(yt_ref())

    async def test_a_hard_failure_does_not_come_back_as_an_empty_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(result=failed_result("ERROR: sign in to confirm (fake)")),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(PlatformError, match="sign in to confirm"):
            await adapter.fetch_creator_profile(yt_ref())


# =========================================================================== #
# 枚举 + 时间窗（V1 `--recent-days`）
# =========================================================================== #


async def collect(
    adapter: YouTubeAdapter, *, since: Any = None, limit: int = 30
) -> list[VideoMeta]:
    out: list[VideoMeta] = []
    async for meta in adapter.list_creator_videos(yt_ref(), since=since, limit=limit):
        out.append(meta)
    return out


NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
RECENT = int(NOW.timestamp())
OLD = RECENT - 400 * 86_400


class TestListingAndTimeWindow:
    def runner_for(self, *video_ids: str, timestamps: dict[str, int | None] | None = None):
        stamps = timestamps or {}
        lines = []
        for video_id in video_ids:
            stamp = stamps.get(video_id, "absent")
            fields: dict[str, Any] = {}
            if stamp != "absent":
                fields = {} if stamp is None else {"timestamp": stamp}
            lines.append(entry(video_id, **fields))
        return FakeYtDlpRunner(result=ok_result(stdout=stdout_of(*lines)))

    async def test_since_is_a_real_filter_here(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = self.runner_for("aQw4w9WgXc0", "bQw4w9WgXc1", timestamps={"bQw4w9WgXc1": OLD})
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        got = await collect(adapter, since=NOW - timedelta(days=7))
        assert [m.platform_video_id for m in got] == ["aQw4w9WgXc0"]

    async def test_undated_entries_survive_the_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = self.runner_for("aQw4w9WgXc0", "bQw4w9WgXc1", timestamps={"bQw4w9WgXc1": None})
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        got = await collect(adapter, since=NOW - timedelta(days=7))
        assert {m.platform_video_id for m in got} == {"aQw4w9WgXc0", "bQw4w9WgXc1"}
        assert [m for m in got if m.extra["has_date"] is False], "缺失要能被下游看出来"

    async def test_the_window_widens_the_scan_and_the_plain_limit_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        filtered = self.runner_for("aQw4w9WgXc0")
        adapter = make_adapter(tmp_path, runner=filtered, monkeypatch=monkeypatch)
        await collect(adapter, since=NOW - timedelta(days=7), limit=5)
        assert filtered.calls[0]["playlist_items"] == listing.scan_budget(5, filtered=True)

        plain = self.runner_for("aQw4w9WgXc0")
        adapter2 = make_adapter(tmp_path, runner=plain, monkeypatch=monkeypatch)
        await collect(adapter2, limit=5)
        assert plain.calls[0]["playlist_items"] == 5

    async def test_limit_is_a_ceiling_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = self.runner_for(*[f"{i:011d}" for i in range(10)])
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        got = await collect(adapter, limit=3)
        assert len(got) == 3

    async def test_the_flat_playlist_url_is_the_videos_tab(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = self.runner_for("aQw4w9WgXc0")
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        await collect(adapter)
        assert runner.calls[0]["url"] == f"https://www.youtube.com/channel/{CHANNEL_ID}/videos"

    async def test_a_hard_failure_keeps_the_yt_dlp_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(result=failed_result("ERROR: unable to download page (fake)")),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ListError, match="unable to download page"):
            await collect(adapter)

    async def test_zero_entries_names_the_rejected_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2026-09-24 那条 P0 的看护形状：退出码 0 而一条都没抽出来时，
        报错必须把**被丢的 id**交出去，否则"抽取器对不上了"这件事没人看得见。"""
        runner = FakeYtDlpRunner(result=ok_result(stdout=stdout_of(entry("PLnotavideoatall"))))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(ListError) as caught:
            await collect(adapter)
        assert "PLnotavideoatall" in str(caught.value)

    async def test_missing_yt_dlp_is_a_list_error_that_names_the_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(raises=LookupError("找不到 'yt-dlp'")),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ListError, match="没有第二条路"):
            await collect(adapter)

    async def test_limit_zero_yields_nothing_without_calling_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boom = FakeYtDlpRunner(raises=AssertionError("不该发请求"))
        adapter = make_adapter(tmp_path, runner=boom, monkeypatch=monkeypatch)
        assert await collect(adapter, limit=0) == []
        assert boom.calls == []


# =========================================================================== #
# 媒体
# =========================================================================== #


class TestDownloadMedia:
    async def test_a_merged_file_comes_back_as_a_single_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        merged = touch(tmp_path / "media.mp4", size=7000)
        runner = FakeYtDlpRunner(
            result=ok_result(stdout=f"[download] Destination: {merged}\n", artifacts=(merged,))
        )
        monkeypatch.setattr(adapter_module, "has_audio_stream", _always_true)
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        artifact = await adapter.download_media(yt_video(), tmp_path)
        assert isinstance(artifact, SingleFileArtifact)
        assert artifact.media_source == "yt_dlp"
        assert artifact.yt_dlp_error is None
        assert artifact.cookie_rung == "none（本平台不需要登录态）"
        assert artifact.size_bytes == 7000

    async def test_a_dash_pair_is_a_failure_not_a_silent_single_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`supports_dash_split=False` 的兑现：判失败并点名 ffmpeg。

        V1 那份是 `glob("*.mp4")` 取第一个 —— 分片情形下恰好交出**纯视频轨**，
        于是 §7.21 那句 `Output file does not contain any stream`
        长得像"ffmpeg 没装"而方向完全不同。这里不许重演。
        """
        video_track = touch(tmp_path / "media.f137.mp4", size=9000)
        audio_track = touch(tmp_path / "media.f140.m4a", size=900)
        runner = FakeYtDlpRunner(
            result=ok_result(
                artifacts=(video_track, audio_track),
                stdout=f"[download] Destination: {video_track}\n",
            )
        )
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(MediaDownloadError) as caught:
            await adapter.download_media(yt_video(), tmp_path)
        text = str(caught.value)
        assert "ffmpeg" in text and "f137" in text
        monkeypatch.setattr(adapter_module, "has_audio_stream", _always_true)
        assert "supports_dash_split=False" in text

    async def test_reporting_paths_that_are_not_there_is_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ghost = tmp_path / "media.mp4"
        runner = FakeYtDlpRunner(
            result=ok_result(stdout=f"[download] Destination: {ghost}\n", artifacts=(ghost,))
        )
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(MediaDownloadError, match="没有产出可读的文件"):
            await adapter.download_media(yt_video(), tmp_path)

    async def test_failure_text_keeps_every_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(result=failed_result("ERROR: HTTP Error 403: Forbidden")),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(MediaDownloadError) as caught:
            await adapter.download_media(yt_video(), tmp_path)
        assert "403: Forbidden" in str(caught.value)
        assert "exit 1" in str(caught.value), "档位轨迹（attempts_note）不能丢"

    async def test_missing_binary_is_reported_as_no_second_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter(
            tmp_path,
            runner=FakeYtDlpRunner(raises=LookupError("找不到 'yt-dlp'")),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(MediaDownloadError, match="没有第二条路"):
            await adapter.download_media(yt_video(), tmp_path)

    async def test_progress_lines_become_a_fraction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        merged = touch(tmp_path / "media.mp4")
        runner = RecordingRunner(
            result=ok_result(stdout=f"[download] Destination: {merged}\n", artifacts=(merged,))
        )
        runner.emit_lines = ["[download]  42.0% of 10.0MiB", "[info] nothing to see"]
        monkeypatch.setattr(adapter_module, "has_audio_stream", _always_true)
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        seen: list[float] = []
        await adapter.download_media(yt_video(), tmp_path, on_progress=seen.append)
        assert seen == [pytest.approx(0.42)], "认不出的行不该变成 0%"


# --------------------------------------------------------------------------- #
# argv 的形状（配置字段 -> 命令行）
# --------------------------------------------------------------------------- #


class TestArgvShape:
    def test_format_preference_reaches_the_command_line(self) -> None:
        args = media.ytdlp_extra_args(fast_config())
        assert "-f" in args
        assert args[args.index("-f") + 1] == "bv*[height<=1080]+ba/b[height<=1080]"
        assert "mp4" in args

    def test_a_custom_format_is_passed_through_untouched(self) -> None:
        """V2 不解释 yt-dlp 的 format 语法：写错时它的报错原文才是能看懂的说法。"""
        args = media.ytdlp_extra_args(fast_config(advanced={"format_preference": "best"}))
        assert args[args.index("-f") + 1] == "best"

    def test_proxy_appears_in_both_command_lines(self) -> None:
        config = fast_config(proxy="http://127.0.0.1:7890")
        for args in (media.ytdlp_extra_args(config), media.subtitle_extra_args(config)):
            assert args[args.index("--proxy") + 1] == "http://127.0.0.1:7890"
        plain = media.ytdlp_extra_args(fast_config())
        assert "--proxy" not in plain, "没配代理时不能凭空加一个参数"

    def test_the_node_flag_follows_the_probe_not_the_requirement(self) -> None:
        config = fast_config()
        assert _pairs(media.ytdlp_extra_args(config, node=True)) >= {("--js-runtimes", "node")}
        assert "--js-runtimes" not in media.ytdlp_extra_args(config, node=False)
        assert "--js-runtimes" not in media.ytdlp_extra_args(
            fast_config(advanced={"node_as_js_runtime": False}), node=True
        )
        # require_node 只管健康检查那一格的颜色，不改 argv（两条判据不同）
        assert "--js-runtimes" in media.ytdlp_extra_args(
            fast_config(advanced={"require_node": False}), node=True
        )

    def test_subtitle_argv_asks_for_subs_and_nothing_else(self) -> None:
        args = media.subtitle_extra_args(fast_config())
        assert "--skip-download" in args
        assert "--write-subs" in args and "--write-auto-subs" in args
        assert args[args.index("--sub-langs") + 1] == "zh-Hans,zh,en"
        assert args[args.index("--sub-format") + 1] == "vtt"
        assert "-f" not in args, "字幕那一趟不该挑媒体格式"


def _pairs(args: Sequence[str]) -> set[tuple[str, str]]:
    return {(args[i], args[i + 1]) for i in range(len(args) - 1)}


# =========================================================================== #
# 字幕（supports_subtitles=True 的兑现）
# =========================================================================== #


class TestFetchSubtitles:
    VTT = fixture_text("subtitles_en.vtt")

    async def test_a_real_track_becomes_a_transcript(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = RecordingRunner(
            files={"subtitles.en.vtt": self.VTT},
            result=ok_result(stdout="[info] ok\n"),
        )
        adapter = make_adapter(tmp_path, subtitle_runner=runner, monkeypatch=monkeypatch)
        transcript = await adapter.fetch_subtitles(yt_video())
        assert transcript is not None
        assert transcript.engine == "youtube_subtitle"
        assert transcript.language == "en"
        assert transcript.sentence_count == len(transcript.segments) >= 1
        assert transcript.char_count == len(transcript.text)
        # 内联标签与 cue settings 都不许进正文
        assert "<c>" not in transcript.text and "align:start" not in transcript.text
        # 滚动重复行被收敿成一条
        assert transcript.text.count("大家好") == 1

    async def test_no_track_is_none_but_a_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        absent = make_adapter(
            tmp_path,
            subtitle_runner=RecordingRunner(result=ok_result(stdout="[info] ok\n")),
            monkeypatch=monkeypatch,
        )
        assert await absent.fetch_subtitles(yt_video()) is None

        broken = make_adapter(
            tmp_path,
            subtitle_runner=RecordingRunner(
                result=failed_result("ERROR: Unable to download API page (fake)")
            ),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(PlatformError, match="Unable to download API page"):
            await broken.fetch_subtitles(yt_video())

    async def test_an_empty_track_is_none_not_a_zero_length_transcript(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = RecordingRunner(
            files={"subtitles.en.vtt": "WEBVTT\n\n"}, result=ok_result(stdout="")
        )
        adapter = make_adapter(tmp_path, subtitle_runner=runner, monkeypatch=monkeypatch)
        assert await adapter.fetch_subtitles(yt_video()) is None

    async def test_the_scratch_directory_is_cleaned_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = RecordingRunner(files={"subtitles.en.vtt": self.VTT}, result=ok_result(stdout=""))
        adapter = make_adapter(tmp_path, subtitle_runner=runner, monkeypatch=monkeypatch)
        await adapter.fetch_subtitles(yt_video())
        leftovers = list((tmp_path / "scratch-root").iterdir())
        assert leftovers == [], f"字幕中间产物留下来了：{leftovers}"

    async def test_missing_binary_is_a_platform_error_not_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter(
            tmp_path,
            subtitle_runner=RecordingRunner(raises=LookupError("找不到 'yt-dlp'")),
            monkeypatch=monkeypatch,
        )
        with pytest.raises(PlatformError, match="未安装 yt-dlp"):
            await adapter.fetch_subtitles(yt_video())

    def test_the_preferred_language_wins_and_an_unlisted_one_still_counts(self) -> None:
        files = [Path("subtitles.zh-Hans.vtt"), Path("subtitles.en.vtt")]
        assert media.choose_subtitle_file(files, ("zh-Hans", "en")) == files[0]
        assert media.choose_subtitle_file(files, ("en",)) == files[1]
        # 前缀匹配：配 `zh` 要能挑中 `zh-Hans`
        assert media.choose_subtitle_file(files, ("zh",)) == files[0]
        assert media.choose_subtitle_file([], ("en",)) is None
        # 一条都没匹配上时退回"有内容的一条"而不是 None：语言不对也比没有强
        assert media.choose_subtitle_file(files, ("de",)) in files

    def test_the_subtitle_runner_is_a_different_command_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两趟 runner 不能共用 argv：媒体那趟带 `-f`，字幕那趟带 `--skip-download`。"""
        adapter = make_adapter(tmp_path)
        assert "--skip-download" in media.subtitle_extra_args(adapter._config)
        assert "--skip-download" not in media.ytdlp_extra_args(adapter._config)


class TestVttParsing:
    def test_cue_settings_and_inline_tags_are_gone(self) -> None:
        segments = media.parse_vtt(fixture_text("subtitles_en.vtt"))
        assert segments
        assert all("align:" not in s.text and "<c>" not in s.text for s in segments)
        assert all("<" not in s.text for s in segments)

    def test_the_clock_without_a_tz_still_parses_and_multi_line_cues_join(self) -> None:
        segments = media.parse_vtt(fixture_text("subtitles_en.vtt"))
        first = segments[0]
        assert first.start_seconds == pytest.approx(0.479)
        joined = [s for s in segments if "第二行" in s.text]
        assert len(joined) == 1, "一条 cue 的多行正文必须并成一句"

    def test_an_unparseable_clock_drops_the_cue_instead_of_filling_zero(self) -> None:
        """`00:99:99.000` 这种越界时刻：**整条丢**，不折成 6039 秒、也不填 0。

        折成秒的坏数据会长得像一条正常片段（位置合理、时间错 100 倍），
        而填 0 会让前端"点这句跳到 0:00" —— 两种都比"这句没有"难查。
        """
        segments = media.parse_vtt(fixture_text("subtitles_en.vtt"))
        assert all("时间轴读不出来" not in s.text for s in segments)
        assert all(s.start_seconds >= 0 and s.end_seconds >= s.start_seconds for s in segments)
        assert [s for s in segments if s.start_seconds == 0.0 and s.end_seconds == 0.0] == []
        assert media._clock_to_seconds("00:99:99.000") is None
        assert media._clock_to_seconds("01:02:03.500") == pytest.approx(3723.5)
        assert media._clock_to_seconds("不是时刻") is None

    def test_repeated_rolling_lines_collapse_but_different_text_never_does(self) -> None:
        vtt = (
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n同一句\n\n"
            "00:00:02.000 --> 00:00:03.000\n同一句\n\n"
            "00:00:03.000 --> 00:00:04.000\n另一句\n"
        )
        collapsed = media.parse_vtt(vtt)
        assert [s.text for s in collapsed] == ["同一句", "另一句"]
        assert collapsed[0].end_seconds == pytest.approx(3.0), "去重要把时间轴接上"
        kept = media.parse_vtt(vtt, collapse_repeats=False)
        assert len(kept) == 3


# =========================================================================== #
# 反漂移：与 B站 共用判据的那两份实现必须还一致
# =========================================================================== #


class TestSharedJudgementsDoNotDrift:
    @pytest.mark.parametrize(
        "result",
        [
            failed_result("ERROR: Request is blocked by server (412)\nsecond line"),
            failed_result(""),
            YtDlpResult(
                ok=False,
                variant=None,
                stdout="",
                stderr="",
                returncode=7,
                attempts=(("a", 7, "x"), ("b", 1, "y")),
            ),
        ],
    )
    def test_the_two_failure_notes_still_agree(self, result: YtDlpResult) -> None:
        """`youtube.media.ytdlp_failure_reason` 与 B站 那份是同一判据的两份实现。

        正解是提到 `infra/ytdlp.py`（它才是那份判据该住的地方），本格不顺手做。
        这一条红 = 有人只改了一份。
        """
        assert media.ytdlp_failure_reason(result) == bili_media.ytdlp_failure_reason(result)


class TestReadings:
    """读数补抓：这一家**故意不做**，理由不是没时间，是那条路更贵且与采集重复。

    单条 `-J` 确实能回 view/like，但那是为"补一个数"再跑一次几秒到几十秒的完整解析，
    而同一次采集的 flat-playlist 已经带回这些计数并记成 publish 快照。
    所以这里看护的是"如实抛 + 消息点名该跑哪一条"，与抖音那两条同一形状。
    """

    async def test_metrics_refuse_and_point_at_the_task_that_can_answer(
        self, tmp_path: Path
    ) -> None:
        adapter = make_adapter(tmp_path)
        assert adapter.capabilities.supports_comments is False
        with pytest.raises(PlatformError) as caught:
            await adapter.fetch_metrics(yt_video())
        message = str(caught.value)
        assert caught.value.stage == "metrics"
        assert "youtube_collect" in message, message

    async def test_the_refusal_costs_no_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "做不到"必须是**当场说的**，不是先跑一次 yt-dlp 再说。

        少了这一条，一个"先试 `-J` 再放弃"的实现也满足上一条，而补读数一轮 50 条
        就会变成 50 次白跑的子进程 —— 那才是这条决定真正的代价所在。
        """
        runner = RecordingRunner(result=ok_result(stdout=""))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(PlatformError):
            await adapter.fetch_metrics(yt_video())
        assert runner.calls == []
