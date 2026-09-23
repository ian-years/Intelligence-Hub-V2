"""B站 Adapter 契约测试。V1 §7.13 / §7.14 / §7.15 / §7.16 / §7.21 + 字幕路径。

**接口那半边用的是真响应**：`tests/fixtures/bilibili/view.json`、
`player_no_subtitle.json`、`card.json`、`ytdlp_412_stderr.txt` 都是 2026-09-22
在本机匿名打 `api.bilibili.com` 抓回来的（裁过字段、删了 `ip_info` 里的公网 IP）。
`subtitles[]` 的单条形状与 `--flat-playlist` 的条目**没取到真样本**
（前者要登录态，后者被 412 挡在门外），文件里的 `_comment` 各自写清了来源。
为什么值得这么绕：接口字段是"对面说了算"的东西，凭印象编一份 fixture
就等于把猜测钉成契约 —— 后来人对着它写代码，错了要等真跑那天才发现。

网络与子进程仍然是假的（`httpx.MockTransport` + `FakeYtDlpRunner`），
所以这一套还是能离线跑、且不会往 B站 发任何东西。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.contracts._doubles import FakeYtDlpRunner

from intelligence_hub_v2.errors import ListError, MediaDownloadError, PlatformError
from intelligence_hub_v2.infra import ytdlp as infra_ytdlp
from intelligence_hub_v2.infra.cookies import COOKIE_FILE_HEADER, CookieManager
from intelligence_hub_v2.infra.ytdlp import YtDlpResult, should_escalate_cookie_rung
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.media import SingleFileArtifact, VideoAudioPairArtifact
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.base import AdapterDeps, PlatformAdapter
from intelligence_hub_v2.platforms.bilibili import listing, subtitles
from intelligence_hub_v2.platforms.bilibili import media as bili_media
from intelligence_hub_v2.platforms.bilibili.adapter import BilibiliAdapter
from intelligence_hub_v2.platforms.bilibili.config import BilibiliConfig
from intelligence_hub_v2.platforms.bilibili.media import resolve_cookie_ladder
from intelligence_hub_v2.platforms.douyin.adapter import DouyinAdapter
from intelligence_hub_v2.platforms.douyin.config import DouyinConfig
from intelligence_hub_v2.platforms.registry import PLATFORMS
from intelligence_hub_v2.storage.files import FileStorage

MID = "486906719"
BVID = "BV1GJ411x7h7"
CID = 137649199
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "bilibili"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fast_config(**overrides: object) -> BilibiliConfig:
    payload: dict[str, object] = {
        "display_name": "B站",
        "rate_limit": {"per_minute": 100_000, "per_creator_seconds": 0.0},
        **overrides,
    }
    return BilibiliConfig.model_validate(payload)


def make_deps(
    tmp_path: Path,
    *,
    config: BilibiliConfig | None = None,
    http: httpx.AsyncClient | None = None,
) -> AdapterDeps:
    return AdapterDeps(
        config=config or fast_config(),
        storage=None,  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        http=http or default_client(),
        logger=get_logger("test.bilibili"),
        cookies=CookieManager(FileStorage(tmp_path / "data")),
        bridge=None,  # B站 不走桥（needs_browser=False）
    )


def default_client() -> httpx.AsyncClient:
    """默认客户端：答健康检查那句探活，**其余真请求一律炸**。

    探活是 healthcheck 唯一合法的出站请求（它回答的就是"到不到得了接口"），
    所以这里给它一个固定 200；其它路径落到 `_refuse` ——
    用例忘了配路由时得到的是"契约测试发出了真请求"，而不是意外通过。
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, text="ok")
                if str(request.url) == "https://api.bilibili.com/"
                else _refuse(request)
            )
        )
    )


def refusing_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_refuse))


def _refuse(request: httpx.Request) -> httpx.Response:
    msg = f"契约测试发出了真请求：{request.method} {request.url}"
    raise AssertionError(msg)


def routing_client(
    routes: Sequence[tuple[str, httpx.Response | Exception]],
) -> tuple[httpx.AsyncClient, list[str]]:
    """按 URL 子串路由。列表里放异常实例 = 这一次请求抛它。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        for needle, item in routes:
            if needle in url:
                if isinstance(item, Exception):
                    raise item
                return item
        msg = f"没有为 {url!r} 配路由；已配 {[n for n, _ in routes]}"
        raise AssertionError(msg)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def api_response(name: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=fixture(name))


def make_adapter(
    tmp_path: Path,
    *,
    config: BilibiliConfig | None = None,
    http: httpx.AsyncClient | None = None,
    runner: FakeYtDlpRunner | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> BilibiliAdapter:
    adapter = BilibiliAdapter(
        config or fast_config(), make_deps(tmp_path, config=config, http=http)
    )
    if runner is not None:
        assert monkeypatch is not None, "替换 runner 要 monkeypatch 夹具"
        monkeypatch.setattr(adapter, "_ytdlp_runner", lambda: runner)
    return adapter


def bili_ref() -> CreatorRef:
    return CreatorRef(
        platform="bilibili", platform_id=MID, profile_url=f"https://space.bilibili.com/{MID}"
    )


def bili_video(**updates: Any) -> VideoMeta:
    payload: dict[str, Any] = {
        "platform": "bilibili",
        "platform_video_id": BVID,
        "creator_ref": bili_ref(),
        "title": "【官方 MV】Never Gonna Give You Up - Rick Astley",
        "webpage_url": f"https://www.bilibili.com/video/{BVID}/",
        "duration_seconds": 213.0,
        "extra": {"cid": CID, "aid": 80433022, "pages": 1, "uploader_mid": MID},
    }
    payload.update(updates)
    return VideoMeta(**payload)


def reported_paths(stdout: str) -> tuple[Path, ...]:
    """按 yt-dlp 真实输出的 `[download] Destination:` 行算出它报了哪些文件。

    与 `YtDlpRunner` 生产路径同一套判据（它内部是 `_artifacts_from`）：
    用例只写"yt-dlp 会打出什么"，不手写第二个真相。
    """
    marker = "[download] Destination: "
    return tuple(
        Path(line[len(marker) :]) for line in stdout.splitlines() if line.startswith(marker)
    )


def ok_result(
    *,
    stdout: str = "",
    stderr: str = "",
    attempts: tuple[Any, ...] = (),
    artifacts: Sequence[Path] | None = None,
    variant: Any = None,
) -> YtDlpResult:
    """`artifacts` 要显式给。

    真的 `YtDlpRunner` 是从 stdout 的 `[download] Destination:` 行算出这批路径的
    （`_artifacts_from`，V1 §7.21 定下的"只认它自己报的路径"）。
    不传 `artifacts` 时从 `stdout` 里认（见 `reported_paths`）。
    """
    return YtDlpResult(
        ok=True,
        variant=variant,
        stdout=stdout,
        stderr=stderr,
        returncode=0,
        artifacts=tuple(artifacts) if artifacts is not None else reported_paths(stdout),
        attempts=attempts or (("带桥导出的登录 cookie", 0, ""),),
    )


def failed_result(stderr: str) -> YtDlpResult:
    return YtDlpResult(
        ok=False,
        variant=None,
        stdout="",
        stderr=stderr,
        returncode=1,
        attempts=(("带桥导出的登录 cookie", 1, stderr.strip()[:60]),),
    )


def write_cookie_file(path: Path, *, with_cookie: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        COOKIE_FILE_HEADER + ".bilibili.com\tTRUE\t/\tTRUE\t1893456000\tSESSDATA\tabc\n"
        if with_cookie
        else COOKIE_FILE_HEADER
    )
    path.write_text(body, encoding="utf-8")
    return path


def touch(path: Path, *, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


async def collect(
    adapter: BilibiliAdapter, *, limit: int = 30, since: Any = None
) -> list[VideoMeta]:
    out: list[VideoMeta] = []
    async for meta in adapter.list_creator_videos(bili_ref(), since=since, limit=limit):
        out.append(meta)
    return out


# =========================================================================== #
# 真样本自证：fixture 里那些标了"真样本"的东西，形状必须真的能被解析层吃下
# =========================================================================== #


class TestRealFixturesAreActuallyReal:
    """这几条不测适配器，测的是**我对接口形状的理解有没有落空**。

    它们红了意味着 B站 改了字段，或者当初那份"真样本"其实是编的。
    两种情况下都该有人立刻知道，而不是等某个适配器用例在三个月后变得看不懂。
    """

    def test_view_fixture_matches_the_fields_the_parser_reads(self) -> None:
        payload = fixture("view.json")
        assert payload["_comment"].startswith("真样本")
        assert payload["code"] == 0
        card = listing.view_to_card(BVID, payload["data"])
        assert card.bvid == BVID
        assert card.aid == 80433022
        assert card.cid == CID, "cid 是字幕接口必需的，它必须真的在 data 里"
        assert card.duration_seconds == 213
        assert card.published_at is not None and card.published_at.tzinfo is UTC
        assert card.uploader_mid == MID
        assert card.view_count == 106_018_501

    def test_card_fixture_carries_a_numeric_fans(self) -> None:
        """只断言"真接口给的是数字"，不断言具体值 —— 粉丝数每次抓都不一样。"""
        fans = fixture("card.json")["data"]["card"]["fans"]
        assert isinstance(fans, int) and fans > 10_000

    def test_the_412_stderr_is_the_text_the_escalation_table_knows(self) -> None:
        """V1 §7.15 的原文必须在 `infra.ytdlp` 那张表里 —— 不然退档根本不触发。"""
        text = fixture_text("ytdlp_412_stderr.txt")
        assert "Request is blocked by server (412)" in text
        assert should_escalate_cookie_rung(text) is True

    def test_player_fixture_anonymously_returns_no_tracks(self) -> None:
        """这条**记录的是一个事实而不是一个行为**：匿名请求确实拿不到字幕轨。"""
        assert listing_parse_tracks(fixture("player_no_subtitle.json")) == []


def listing_parse_tracks(payload: dict[str, Any]) -> Any:
    return subtitles.parse_subtitle_tracks(payload)


# =========================================================================== #
# §7.15 —— cookie 三档，枚举与下载两条路
# =========================================================================== #


class TestCookieLadder:
    def ladder(
        self, tmp_path: Path, config: BilibiliConfig, environ: dict[str, str] | None = None
    ) -> bili_media.CookieLadder:
        return resolve_cookie_ladder(
            BilibiliAdapter.capabilities.cookie_variants,
            config=config,
            cookies=CookieManager(FileStorage(tmp_path / "data")),
            environ=environ or {},
        )

    def test_three_rungs_in_the_v1_order(self, tmp_path: Path) -> None:
        """导出文件 > 浏览器 > 匿名。顺序的**唯一真源**是 capabilities（ADR-0011）。"""
        config = fast_config(cookies_file=write_cookie_file(tmp_path / "cfg" / "bilibili.com.txt"))
        rungs = self.ladder(tmp_path, config).variants
        assert [v.kind for v in rungs] == ["exported_file", "browser", "anonymous"]
        assert rungs[0].args[0] == "--cookies"
        assert rungs[1].args == ("--cookies-from-browser", "chrome")
        assert rungs[2].args == ()

    def test_no_config_field_carries_a_second_order(self) -> None:
        """`cookie_variant_order` 被删掉了：与 `capabilities` 是同一份顺序写两遍。"""
        for name in BilibiliConfig.model_fields:
            assert "order" not in name and "priority" not in name, name

    def test_header_only_file_is_not_a_login_rung(self, tmp_path: Path) -> None:
        """§7.15 的另一半：只有表头的文件传出去不报错，只是静默匿名。"""
        empty = write_cookie_file(tmp_path / "cfg" / "bilibili.com.txt", with_cookie=False)
        result = self.ladder(tmp_path, fast_config(cookies_file=empty))
        assert [v.kind for v in result.variants] == ["browser", "anonymous"]
        assert result.cookie_file is None
        assert "一条 cookie 都没有" in (result.note or "")
        assert result.logged_in is True, "浏览器档还在，只是多半读不出来"

    def test_env_var_overrides_config_and_says_so(self, tmp_path: Path) -> None:
        from_env = write_cookie_file(tmp_path / "env" / "bilibili.com.txt")
        config = fast_config(cookies_file=write_cookie_file(tmp_path / "cfg" / "bilibili.com.txt"))
        result = self.ladder(tmp_path, config, {bili_media.ENV_COOKIES_FILE: str(from_env)})
        assert result.cookie_file == from_env
        assert "环境变量" in (result.note or "")

    def test_blank_env_turns_the_browser_rung_off(self, tmp_path: Path) -> None:
        """`BILI_YTDLP_COOKIES_FROM_BROWSER=""` 是**显式关掉**，不能被配置默认值顶回去。"""
        config = fast_config(cookies_file=write_cookie_file(tmp_path / "cfg" / "bilibili.com.txt"))
        result = self.ladder(tmp_path, config, {bili_media.ENV_COOKIES_FROM_BROWSER: "  "})
        assert [v.kind for v in result.variants] == ["exported_file", "anonymous"]

    def test_a_healthy_ladder_says_nothing(self, tmp_path: Path) -> None:
        config = fast_config(cookies_file=write_cookie_file(tmp_path / "cfg" / "bilibili.com.txt"))
        assert self.ladder(tmp_path, fast_config(cookies_file=None)).note is not None
        assert self.ladder(tmp_path, config).note is None, "默认档位在默认配置下不该报告任何事"
        from_env = self.ladder(tmp_path, config, {bili_media.ENV_COOKIES_FROM_BROWSER: "edge"})
        assert "环境变量" in (from_env.note or ""), "env 覆盖了配置，这件事要说出来"

    async def test_the_enumeration_carries_the_same_cookie_rungs_as_download(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**V1 §7.15 的正身**：枚举与下载是两条路，两条都得带导出 cookie。

        老代码只在下那一趟带了 cookie，枚举裸奔 → 随机 352/412，
        "同一台机器上一条过一条不过"，而手动跑通一次又不能证明链路稳。
        """
        cookie = write_cookie_file(tmp_path / "cfg" / "bilibili.com.txt")
        config = fast_config(cookies_file=cookie, ytdlp_cookies_from_browser="chrome")
        runner = FakeYtDlpRunner(result=ok_result(stdout=fixture_text("flat_playlist.jsonl")))
        adapter = make_adapter(tmp_path, config=config, runner=runner, monkeypatch=monkeypatch)
        await collect(adapter, limit=3)
        listed = runner.calls[0]
        assert listed["kind"] == "flat_playlist"
        assert [v.kind for v in listed["variants"]] == ["exported_file", "browser", "anonymous"]
        assert listed["variants"][0].args == ("--cookies", str(cookie))
        assert listed["url"] == f"https://space.bilibili.com/{MID}/video"

    async def test_playlist_items_is_capped_at_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(result=ok_result(stdout=fixture_text("flat_playlist.jsonl")))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        await collect(adapter, limit=5)
        assert runner.calls[0]["playlist_items"] == 5


# =========================================================================== #
# 枚举：flat-playlist 输出 / 外部清单 / 搜索兜底
# =========================================================================== #


class TestEnumeration:
    def _runner(self) -> FakeYtDlpRunner:
        return FakeYtDlpRunner(result=ok_result(stdout=fixture_text("flat_playlist.jsonl")))

    async def test_only_real_bvids_survive_and_duplicates_collapse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fixture 里摆了重复行与一条 `av114514`（认不出 BV 就该丢）。"""
        adapter = make_adapter(tmp_path, runner=self._runner(), monkeypatch=monkeypatch)
        videos = await collect(adapter, limit=10)
        assert [v.platform_video_id for v in videos] == [BVID, "BV1xx411c7mD"]
        assert all(v.platform == "bilibili" for v in videos)
        assert all(str(v.webpage_url).endswith("/") for v in videos), "作品页地址带尾斜杠"

    async def test_debug_noise_lines_are_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--dump-json` 的 stdout 里混着 `[debug]` 与 `[BilibiliSpaceVideo]` 行。

        整段 `json.loads` 会在第一行就炸，报出来的错长得像"这个平台不支持"。
        """
        assert fixture_text("flat_playlist.jsonl").startswith("[debug]")
        adapter = make_adapter(tmp_path, runner=self._runner(), monkeypatch=monkeypatch)
        assert len(await collect(adapter, limit=10)) == 2

    async def test_412_on_the_first_rung_becomes_a_list_error_with_the_original_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """本机实测的那句原文必须整段出现在异常里。"""
        text = fixture_text("ytdlp_412_stderr.txt")
        runner = FakeYtDlpRunner(result=failed_result(text))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(ListError) as caught:
            await collect(adapter, limit=3)
        assert "Request is blocked by server (412)" in str(caught.value)
        assert caught.value.stage == "list"
        assert "cookie 阶梯" in str(caught.value), "还要说清这次走的是哪几档"

    async def test_missing_yt_dlp_fails_the_enumeration_honestly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B站 的枚举没有第二条路（抖音还有页面直链）。缺二进制要红，不许静默空手。"""
        runner = FakeYtDlpRunner(raises=LookupError("找不到可执行文件 'yt-dlp'"))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(ListError, match="没有第二条路"):
            await collect(adapter, limit=3)

    async def test_exit_zero_with_nothing_parsed_is_still_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """退出码 0 + 一条没抽出来 = 抽取器与页面对不上了，不能报"这个号没发过作品"。"""
        runner = FakeYtDlpRunner(result=ok_result(stdout="[debug] nothing here\n"))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(ListError, match="一条作品都没抽出来"):
            await collect(adapter, limit=3)

    async def test_external_manifest_hit_skips_the_builtin_collector(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(fixture("external_manifest.json")), encoding="utf-8")
        runner = FakeYtDlpRunner(result=ok_result(stdout=fixture_text("flat_playlist.jsonl")))
        adapter = make_adapter(
            tmp_path,
            config=fast_config(external_browser_manifest_path=manifest),
            runner=runner,
            monkeypatch=monkeypatch,
        )
        videos = await collect(adapter, limit=5)
        assert [v.platform_video_id for v in videos] == [BVID, "BV1xx411c7mD"]
        assert videos[0].title == "清单里的第一条"
        assert runner.calls == [], "命中清单就不该再跑 yt-dlp"

    async def test_manifest_rows_without_bvid_are_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """清单里那条"没有 bvid 的脏条目"被跳过，而不是让整位博主失败。"""
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps(fixture("external_manifest.json")), encoding="utf-8")
        make_adapter(
            tmp_path,
            config=fast_config(external_browser_manifest_path=manifest),
            monkeypatch=monkeypatch,
        )
        cards = listing.manifest_cards_for(
            listing.load_external_manifest(manifest), mid=MID, limit=10
        )
        assert len(cards) == 2

    async def test_manifest_for_another_creator_does_not_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps(fixture("external_manifest.json")), encoding="utf-8")
        cards = listing.manifest_cards_for(
            listing.load_external_manifest(manifest), mid=MID, limit=10
        )
        assert "BV1otherother" not in [c.bvid for c in cards]

    async def test_a_manifest_the_builtin_collector_misses_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text('{"parsed": []}', encoding="utf-8")
        runner = FakeYtDlpRunner(result=ok_result(stdout=fixture_text("flat_playlist.jsonl")))
        adapter = make_adapter(
            tmp_path,
            config=fast_config(external_browser_manifest_path=manifest),
            runner=runner,
            monkeypatch=monkeypatch,
        )
        assert len(await collect(adapter, limit=5)) == 2
        assert runner.calls, "清单没命中 → 回落到内置枚举"

    def test_an_unreadable_manifest_raises_naming_the_producer(self, tmp_path: Path) -> None:
        """§7.13/§7.14：清单形状不对要**当场红**，并点名生产者。

        静默返回空列表 = "看起来在跑"（V1 那个 SkipTest 形状的坑）。
        """
        bad = tmp_path / "bad.json"
        bad.write_text('{"something_else": 1}', encoding="utf-8")
        with pytest.raises(ListError, match="仓库内") as caught:
            listing.load_external_manifest(bad)
        assert "parsed/creators/results" in str(caught.value)

    def test_a_missing_manifest_file_is_reported_with_its_path(self, tmp_path: Path) -> None:
        with pytest.raises(ListError, match="读外部浏览器清单失败"):
            listing.load_external_manifest(tmp_path / "nope.json")

    async def test_search_fallback_is_refused_rather_than_silently_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§7.16：那条路依赖链很脏（Node 版 playwright + NODE_PATH），V2 还没实现。

        配置勾了它而枚举又空手时，必须红 —— 静默跳过就是"看起来在跑"。
        """
        runner = FakeYtDlpRunner(result=ok_result(stdout=""))
        adapter = make_adapter(
            tmp_path,
            config=fast_config(
                advanced={"search_fallback_node_playwright": True},
            ),
            runner=runner,
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ListError, match="静默跳过就是臆造成功"):
            await collect(adapter, limit=3)

    async def test_a_hard_failure_keeps_its_text_even_with_the_fallback_switched_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """勾了搜索兜底也不许把真失败换成"未实现"。

        曾经写成 `except ListError: cards = []`，于是那句真 412
        在开了兜底的配置下会被替换成另一句话 —— 排查的人就看不到风控原文了。
        "没抽到结果"与"跑失败了"是两种红，只有前者该进兜底分支。
        """
        text = fixture_text("ytdlp_412_stderr.txt")
        runner = FakeYtDlpRunner(result=failed_result(text))
        adapter = make_adapter(
            tmp_path,
            config=fast_config(advanced={"search_fallback_node_playwright": True}),
            runner=runner,
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ListError, match="Request is blocked by server") as caught:
            await collect(adapter, limit=3)
        assert "未实现" not in str(caught.value)

    def test_the_flat_playlist_entry_key_order_follows_v1(self) -> None:
        """`id` 优先于 `webpage_url_basename`，`url` 优先于 `webpage_url`。"""
        with_id = listing.entry_to_card({"id": BVID, "webpage_url_basename": "BV0000000000"})
        only_basename = listing.entry_to_card({"webpage_url_basename": "BV1xx411c7mD"})
        assert with_id is not None and with_id.bvid == BVID
        assert only_basename is not None and only_basename.bvid == "BV1xx411c7mD"
        assert listing.entry_to_card({"id": "av123"}) is None


# =========================================================================== #
# §7.21 —— DASH 未合并分片
# =========================================================================== #


class TestDashSplit:
    def test_merged_file_wins_over_the_parts_it_came_from(self, tmp_path: Path) -> None:
        """合并成功后目录里可能还留着分片；那时候**报单文件是对的**。"""
        merged = touch(tmp_path / "media.mp4", size=10_000)
        part = touch(tmp_path / "media.f30064.mp4", size=500)
        parts = infra_ytdlp.classify_artifacts([part, merged])
        assert parts.kind == "single" and parts.main == merged
        assert parts.extras == (part,)

    def test_unmerged_pair_is_detected_by_filename_not_by_glob(self, tmp_path: Path) -> None:
        video = touch(tmp_path / "media.f30064.mp4", size=34_600_000)
        audio = touch(tmp_path / "media.f30280.m4a", size=2_500_000)
        parts = infra_ytdlp.classify_artifacts([video, audio])
        assert parts.kind == "pair" and parts.video == video and parts.audio == audio
        assert "未合并 DASH" in parts.description

    def test_two_webm_tracks_are_told_apart_by_size(self, tmp_path: Path) -> None:
        """webm 既可能是视频轨也可能是音频轨 —— 扩展名分不出，只能看字节数。"""
        big = touch(tmp_path / "media.f100025.webm", size=8000)
        small = touch(tmp_path / "media.f30280.webm", size=800)
        parts = infra_ytdlp.classify_artifacts([big, small])
        assert parts.kind == "pair" and parts.video == big and parts.audio == small

    def test_a_lone_part_is_reported_as_single_not_pair(self, tmp_path: Path) -> None:
        only_video = touch(tmp_path / "media.f30064.mp4", size=9000)
        parts = infra_ytdlp.classify_artifacts([only_video])
        assert parts.kind == "single" and parts.main == only_video

    def test_unreadable_paths_are_dropped(self) -> None:
        assert infra_ytdlp.classify_artifacts([Path("nope/media.mp4")]).kind == "empty"

    def test_only_yt_dlp_reported_paths_are_considered(self, tmp_path: Path) -> None:
        """V1 §7.21 的原话：**不许扫目录**。这里用一个"藏在 audio/ 子目录里"的文件验。"""
        real = touch(tmp_path / "media.mp4", size=1000)
        hidden = touch(tmp_path / "audio" / "part-001.m4a", size=999_999)
        parts = infra_ytdlp.classify_artifacts([real])
        assert parts.kind == "single" and parts.main == real
        assert hidden not in parts.extras

    async def test_pair_reaches_the_caller_as_a_video_audio_pair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        video = touch(tmp_path / "media.f30064.mp4", size=4000)
        audio = touch(tmp_path / "media.f30280.m4a", size=900)
        runner = FakeYtDlpRunner(
            result=ok_result(
                stdout=f"[download] Destination: {video}\n[download] Destination: {audio}\n"
            )
        )
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        artifact = await adapter.download_media(bili_video(), tmp_path)
        assert isinstance(artifact, VideoAudioPairArtifact)
        assert artifact.media_source == "dash_split"
        assert artifact.cookie_rung == "未知档"
        assert artifact.video_path == video and artifact.audio_path == audio
        assert artifact.video_size_bytes == 4000 and artifact.audio_size_bytes == 900
        assert "§7.21" in (artifact.yt_dlp_error or ""), "为什么是两条也要能说清"

    async def test_merge_mode_refuses_a_pair_instead_of_downgrading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置说"我只要一个文件"，就别给两个文件还报成功。"""
        video = touch(tmp_path / "media.f30064.mp4", size=4000)
        audio = touch(tmp_path / "media.f30280.m4a", size=900)
        runner = FakeYtDlpRunner(
            result=ok_result(
                stdout=f"[download] Destination: {video}\n[download] Destination: {audio}\n"
            )
        )
        adapter = make_adapter(
            tmp_path,
            config=fast_config(advanced={"dash_split_handling": "merge"}),
            runner=runner,
            monkeypatch=monkeypatch,
        )
        with pytest.raises(MediaDownloadError, match="要求单文件"):
            await adapter.download_media(bili_video(), tmp_path)

    async def test_merged_download_is_reported_as_yt_dlp_with_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        merged = touch(tmp_path / "media.mp4", size=7000)
        runner = FakeYtDlpRunner(result=ok_result(stdout=f"[download] Destination: {merged}\n"))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        artifact = await adapter.download_media(bili_video(), tmp_path)
        assert isinstance(artifact, SingleFileArtifact)
        assert artifact.media_source == "yt_dlp"
        assert artifact.size_bytes == 7000
        assert artifact.yt_dlp_error is None, "阶梯没问题时别编一句"

    async def test_every_rung_failing_reports_the_ladder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = fixture_text("ytdlp_412_stderr.txt")
        runner = FakeYtDlpRunner(result=failed_result(text))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(MediaDownloadError) as caught:
            await adapter.download_media(bili_video(), tmp_path)
        message = str(caught.value)
        assert "Request is blocked by server (412)" in message
        assert "阶梯" in message

    async def test_missing_yt_dlp_is_fatal_for_media_here(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(raises=LookupError("找不到可执行文件 'yt-dlp'"))
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(MediaDownloadError, match="没有第二条路"):
            await adapter.download_media(bili_video(), tmp_path)

    async def test_exit_zero_without_readable_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = FakeYtDlpRunner(
            result=ok_result(stdout=f"[download] Destination: {tmp_path / 'gone.mp4'}\n")
        )
        adapter = make_adapter(tmp_path, runner=runner, monkeypatch=monkeypatch)
        with pytest.raises(MediaDownloadError, match="没有产出可读的文件"):
            await adapter.download_media(bili_video(), tmp_path)


# =========================================================================== #
# 公开 web-interface（元数据 / 资料）
# =========================================================================== #


class TestWebApi:
    async def test_view_is_requested_with_a_referer_and_ua(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        http, seen = routing_client([(f"view?bvid={BVID}", api_response("view.json"))])
        adapter = make_adapter(tmp_path, http=http)
        await adapter._view(BVID)
        assert len(seen) == 1
        assert seen[0].startswith("https://api.bilibili.com/x/web-interface/view")

    async def test_business_error_inside_http_200_is_a_failure(self, tmp_path: Path) -> None:
        """B站 把"啥都木有"编在 200 里。只看状态码会把它当成功，然后拿着空 dict 往下走。"""
        http, _ = routing_client(
            [("view?bvid=", httpx.Response(200, json=fixture("view_not_found.json")))]
        )
        adapter = make_adapter(tmp_path, http=http)
        with pytest.raises(ListError) as caught:
            await adapter._view(BVID)
        message = str(caught.value)
        assert "code=-400" in message and "请求错误" in message

    async def test_an_html_risk_control_page_is_reported_with_a_snippet(
        self, tmp_path: Path
    ) -> None:
        """空间列表那条路实测就是这个形状（HTML 而不是 JSON）。"""
        http, _ = routing_client(
            [("view?bvid=", httpx.Response(200, text="<!DOCTYPE html>\n<html>风控</html>"))]
        )
        adapter = make_adapter(tmp_path, http=http)
        with pytest.raises(ListError, match="不是 JSON") as caught:
            await adapter._view(BVID)
        assert "DOCTYPE" in str(caught.value)

    async def test_a_transport_error_keeps_its_type(self, tmp_path: Path) -> None:
        http, _ = routing_client([("view?bvid=", httpx.ConnectError("no route to host"))])
        adapter = make_adapter(tmp_path, http=http)
        with pytest.raises(PlatformError, match="ConnectError"):
            await adapter._view(BVID)

    async def test_creator_profile_comes_from_the_real_card_response(self, tmp_path: Path) -> None:
        http, _ = routing_client([(f"card?mid={MID}", api_response("card.json"))])
        profile = await make_adapter(tmp_path, http=http).fetch_creator_profile(bili_ref())
        assert profile.name == "索尼音乐中国"
        assert profile.follower_count == fixture("card.json")["data"]["card"]["fans"] > 10_000
        assert str(profile.avatar_url).startswith("https://i2.hdslb.com")
        assert profile.bio and "一键三连" in profile.bio
        assert profile.ref.platform_id == MID

    async def test_a_card_response_without_the_card_field_is_red(self, tmp_path: Path) -> None:
        http, _ = routing_client([("card?mid=", httpx.Response(200, json={"code": 0, "data": {}}))])
        with pytest.raises(PlatformError, match="没有 card 字段"):
            await make_adapter(tmp_path, http=http).fetch_creator_profile(bili_ref())


# =========================================================================== #
# since：什么时候才值得逐条补元数据
# =========================================================================== #


class TestSinceAndEnrichment:
    def _runner(self) -> FakeYtDlpRunner:
        return FakeYtDlpRunner(result=ok_result(stdout=fixture_text("flat_playlist.jsonl")))

    async def test_no_since_means_no_per_video_requests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没要求按时间过滤时，不为"把字段填满"多发 N 个请求。

        一位博主 30 条 × 20 位 = 600 个请求，按 `per_minute=60` 要跑 10 分钟。
        后处理本来就会逐条补，所以晚一点到手不影响正确性。
        """
        http, seen = routing_client([])
        adapter = make_adapter(tmp_path, http=http, runner=self._runner(), monkeypatch=monkeypatch)
        videos = await collect(adapter, limit=10)
        assert len(videos) == 2
        assert seen == [], "一次接口都不该发"
        assert all(v.published_at is None for v in videos)

    async def test_since_enriches_and_actually_filters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B站 的 `since` 是**实过滤**（与抖音相反：那边详情接口要页面上下文，问不出时间）。"""
        routes = [
            (f"view?bvid={BVID}", api_response("view.json")),
            ("view?bvid=BV1xx411c7mD", api_response("view.json")),
        ]
        http, seen = routing_client(routes)
        adapter = make_adapter(tmp_path, http=http, runner=self._runner(), monkeypatch=monkeypatch)
        future = datetime(2999, 1, 1, tzinfo=UTC)
        assert await collect(adapter, limit=2, since=future) == []
        assert len([url for url in seen if "view?bvid=" in url]) == 2, "两条都问过了才判得掉"

    async def test_enrichment_keeps_the_video_when_view_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单条补全失败只少几个字段，**不该把这条作品丢掉**。"""
        routes = [
            (f"view?bvid={BVID}", httpx.Response(200, json=fixture("view_not_found.json"))),
            ("view?bvid=BV1xx411c7mD", api_response("view.json")),
        ]
        http, _ = routing_client(routes)
        adapter = make_adapter(tmp_path, http=http, runner=self._runner(), monkeypatch=monkeypatch)
        past = datetime(2000, 1, 1, tzinfo=UTC)
        videos = await collect(adapter, limit=2, since=past)
        assert len(videos) == 2
        first = videos[0]
        assert first.platform_video_id == BVID and first.published_at is None

    async def test_enriched_fields_reach_the_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        http, _ = routing_client([("view?bvid=", api_response("view.json"))])
        adapter = make_adapter(tmp_path, http=http, runner=self._runner(), monkeypatch=monkeypatch)
        videos = await collect(adapter, limit=1, since=datetime(2000, 1, 1, tzinfo=UTC))
        assert videos[0].extra["cid"] == CID
        assert videos[0].duration_seconds == 213
        assert videos[0].view_count == 106_018_501


# =========================================================================== #
# 字幕
# =========================================================================== #


class TestSubtitles:
    async def test_anonymously_absent_subtitles_return_none_without_raising(
        self, tmp_path: Path
    ) -> None:
        http, _ = routing_client([("player/v2", api_response("player_no_subtitle.json"))])
        assert await make_adapter(tmp_path, http=http).fetch_subtitles(bili_video()) is None

    async def test_a_present_track_becomes_a_transcript(self, tmp_path: Path) -> None:
        body = fixture("subtitle_body.json")
        http, seen = routing_client(
            [
                ("player/v2", api_response("player_with_subtitle.json")),
                ("aisubtitle.hdslb.com", httpx.Response(200, json=body)),
            ]
        )
        transcript = await make_adapter(tmp_path, http=http).fetch_subtitles(bili_video())
        assert transcript is not None
        assert transcript.engine == "bilibili_subtitle"
        assert transcript.language == "zh-CN", "中文轨优先于 en-US 的机翻轨"
        assert transcript.sentence_count == 3
        assert transcript.char_count == len(transcript.text)
        assert "Never gonna give you up" in transcript.text
        assert any("aisubtitle" in url for url in seen)
        assert seen[1].startswith("https://"), "协议相对地址必须被补成绝对"

    def test_the_preferred_language_wins_and_ai_tracks_lose(self) -> None:
        tracks = listing_parse_tracks(fixture("player_with_subtitle.json"))
        assert [t.lan for t in tracks] == ["zh-CN", "en-US"]
        assert choose_lan(tracks) == "zh-CN"
        only_ai = [t for t in tracks if t.lan == "en-US"]
        assert choose_lan(only_ai) == "en-US"
        assert tracks[1].is_ai is True and tracks[0].is_ai is False

    def test_segments_without_timestamps_are_dropped_not_zeroed(self) -> None:
        """填 0 会让"点击句子跳到 0:00"与"这句没有时间戳"在界面上完全同形。"""
        parsed = subtitles.parse_subtitle_body(fixture("subtitle_body.json"))
        assert len(parsed) == 3
        assert all(segment.start_seconds >= 0 for segment in parsed)

    def test_malformed_track_items_are_skipped(self) -> None:
        payload = {
            "data": {
                "subtitle": {
                    "subtitles": [
                        {"lan": "zh-CN"},  # 没有 url
                        {"lan": "x", "subtitle_url": "javascript:alert(1)"},
                        "not even a dict",
                        {"lan": "zh-CN", "subtitle_url": "//ok.example/x.json"},
                    ]
                }
            }
        }
        tracks = subtitles.parse_subtitle_tracks(payload)
        assert [t.url for t in tracks] == ["https://ok.example/x.json"]

    async def test_a_risk_controlled_player_call_raises_instead_of_claiming_absence(
        self, tmp_path: Path
    ) -> None:
        """**"没问到"不等于"没有"**：吞成 None 会让一次网络故障静默变成一整轮 ASR。"""
        http, _ = routing_client(
            [("player/v2", httpx.Response(200, json={"code": -352, "message": "风控"}))]
        )
        with pytest.raises(PlatformError, match="code=-352"):
            await make_adapter(tmp_path, http=http).fetch_subtitles(bili_video())

    async def test_a_broken_subtitle_body_falls_back_to_none(self, tmp_path: Path) -> None:
        """轨列表是权威给的，正文那条 CDN 挂了 → 这条轨拿不到，值得去跑 ASR。"""
        http, _ = routing_client(
            [
                ("player/v2", api_response("player_with_subtitle.json")),
                ("aisubtitle.hdslb.com", httpx.Response(200, text="<html>挂了</html>")),
            ]
        )
        assert await make_adapter(tmp_path, http=http).fetch_subtitles(bili_video()) is None

    async def test_missing_cid_is_looked_up_once(self, tmp_path: Path) -> None:
        http, seen = routing_client(
            [
                (f"view?bvid={BVID}", api_response("view.json")),
                ("player/v2", api_response("player_no_subtitle.json")),
            ]
        )
        video = bili_video(extra={"pages": 1})
        assert await make_adapter(tmp_path, http=http).fetch_subtitles(video) is None
        assert any("web-interface/view" in url for url in seen)
        assert any("player/v2" in url and f"cid={CID}" in url for url in seen), (
            "cid 该从 view 里拿到"
        )

    async def test_no_cid_at_all_returns_none(self, tmp_path: Path) -> None:
        http, _ = routing_client(
            [("view?bvid=", httpx.Response(200, json={"code": 0, "data": {"bvid": BVID}}))]
        )
        assert await make_adapter(tmp_path, http=http).fetch_subtitles(bili_video(extra={})) is None


def choose_lan(tracks: Sequence[Any]) -> str:
    chosen = subtitles.choose_track(tracks)
    return "" if chosen is None else chosen.lan


# =========================================================================== #
# parse_creator_url
# =========================================================================== #


class TestParseCreatorUrl:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://space.bilibili.com/{MID}",
            f"https://space.bilibili.com/{MID}/video?spm_id_from=333.1007.top_right_bar_window_history.content.click",
            f"https://m.bilibili.com/space/{MID}",
            MID,
        ],
    )
    async def test_creator_links_parse_offline(self, tmp_path: Path, url: str) -> None:
        http, seen = routing_client([])
        ref = await make_adapter(tmp_path, http=http).parse_creator_url(url)
        assert ref.platform_id == MID
        assert str(ref.profile_url) == f"https://space.bilibili.com/{MID}"
        assert seen == [], "B站 的 mid 就在链接里，不该联网"

    async def test_a_video_link_resolves_to_its_uploader(self, tmp_path: Path) -> None:
        """用户从"这个 UP 主发过一条这样的"进来时粘的就是作品链接。"""
        http, seen = routing_client([(f"view?bvid={BVID}", api_response("view.json"))])
        ref = await make_adapter(tmp_path, http=http).parse_creator_url(
            f"https://www.bilibili.com/video/{BVID}/?spm_id_from=333.788"
        )
        assert ref.platform_id == MID, "mid 来自 view 的 owner.mid"
        assert len(seen) == 1

    async def test_short_link_follows_the_redirect(self, tmp_path: Path) -> None:
        final = f"https://space.bilibili.com/{MID}"
        http, seen = routing_client(
            [
                (
                    "b23.tv",
                    httpx.Response(
                        302,
                        headers={"location": final},
                        request=httpx.Request("GET", "https://b23.tv/abc"),
                    ),
                ),
                ("space.bilibili.com", httpx.Response(200, request=httpx.Request("GET", final))),
            ]
        )
        ref = await make_adapter(tmp_path, http=http).parse_creator_url("https://b23.tv/abc")
        assert ref.platform_id == MID
        assert str(ref.source_url) == "https://b23.tv/abc"
        assert len(seen) == 2

    @pytest.mark.parametrize("junk", ["", "  ", "https://example.com/user/1", "BV短", "B站"])
    async def test_unrecognisable_input_is_red_with_the_original_text(
        self, tmp_path: Path, junk: str
    ) -> None:
        with pytest.raises(PlatformError) as caught:
            await make_adapter(tmp_path).parse_creator_url(junk)
        assert caught.value.stage == "parse_url"
        if junk.strip():
            assert junk in str(caught.value)

    async def test_a_non_numeric_mid_cannot_slip_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`extract_mid` 只回数字，这道再判一次防的是以后有人放宽它。"""
        monkeypatch.setattr(
            "intelligence_hub_v2.platforms.bilibili.adapter.extract_mid", lambda _x: "abc"
        )
        with pytest.raises(PlatformError, match="不是纯数字"):
            await make_adapter(tmp_path).parse_creator_url("https://space.bilibili.com/1")


# =========================================================================== #
# 健康检查
# =========================================================================== #


class TestHealthcheck:
    async def test_missing_yt_dlp_is_unreachable_here_not_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """与抖音正好相反的那一格（抖音有页面直链兜底，B站 没有第二条路）。

        报 degraded 会让人以为"跟抖音一样能跑，只是画质差一点" —— 那是把人往错方向支。
        """
        monkeypatch.setattr(
            "intelligence_hub_v2.platforms.bilibili.adapter.shutil.which", lambda _n: None
        )
        douyin = await make_douyin_without_ytdlp(tmp_path, monkeypatch).healthcheck()
        bili = await make_adapter(tmp_path).healthcheck()
        assert douyin.components["yt_dlp"] == "degraded"
        assert bili.components["yt_dlp"] == "unreachable"
        assert bili.status == "unreachable"

    async def test_no_exported_cookie_is_degraded_not_unreachable(self, tmp_path: Path) -> None:
        """匿名档确实能下（只是 4K/高帧率不可用），而且浏览器档在 Linux 上还能用。"""
        report = await make_adapter(tmp_path).healthcheck()
        assert report.components["cookies"] == "degraded"
        assert "登录档画质不可用" in (report.detail or "")

    async def test_network_failure_is_reported_as_unreachable_with_the_error(
        self, tmp_path: Path
    ) -> None:
        http, _ = routing_client([("api.bilibili.com", httpx.ConnectError("no route"))])
        report = await make_adapter(tmp_path, http=http).healthcheck()
        assert report.components["network"] == "unreachable"
        assert "api.bilibili.com" in (report.detail or "")

    async def test_any_http_answer_counts_as_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """探活只回答"到不到得了接口"，所以 404 也算通。"""
        monkeypatch.setattr(
            "intelligence_hub_v2.platforms.bilibili.adapter.shutil.which", lambda _n: "/x/yt-dlp"
        )
        http, _ = routing_client([("api.bilibili.com", httpx.Response(404, text="nope"))])
        files = CookieManager(FileStorage(tmp_path / "data"))
        files.write_netscape(
            "bilibili.com",
            [{"domain": ".bilibili.com", "name": "SESSDATA", "value": "x", "expires": 1893456000}],
        )
        report = await make_adapter(tmp_path, http=http).healthcheck()
        assert report.components == {"yt_dlp": "ok", "cookies": "ok", "network": "ok"}
        assert report.status == "ok" and report.detail is None

    async def test_checked_at_is_timezone_aware(self, tmp_path: Path) -> None:
        report = await make_adapter(tmp_path).healthcheck()
        assert report.checked_at.tzinfo is not None


def make_douyin_without_ytdlp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DouyinAdapter:
    """一个"PATH 里没有 yt-dlp"的抖音适配器。

    放在这里只为了一件事：**同一个组件在两个平台该亮不同的灯**。
    对照实验要两份都在，否则"为什么 B站 这里红而抖音那里黄"没法自证。
    """
    monkeypatch.setattr(
        "intelligence_hub_v2.platforms.douyin.adapter.shutil.which", lambda _n: None
    )
    deps = AdapterDeps(
        config=DouyinConfig(display_name="抖音"),
        storage=None,  # type: ignore[arg-type]
        events=object(),  # type: ignore[arg-type]
        http=default_client(),
        logger=get_logger("test.douyin"),
        cookies=CookieManager(FileStorage(tmp_path / "data")),
        bridge=None,
    )
    return DouyinAdapter(DouyinConfig(display_name="抖音"), deps)


# =========================================================================== #
# 契约形状
# =========================================================================== #


class TestContractShape:
    def test_registered_under_the_platform_name(self) -> None:
        assert PLATFORMS["bilibili"] is BilibiliAdapter
        assert set(PLATFORMS) == {"douyin", "bilibili"}

    def test_satisfies_the_protocol(self, tmp_path: Path) -> None:
        adapter: PlatformAdapter = make_adapter(tmp_path)
        assert isinstance(adapter, PlatformAdapter)

    # 能力声明的**快照**与"与配置默认值一致"两条上提到契约基类了
    # （`test_platform_adapter.py::test_capabilities_match_expected`，值在
    # `TestBilibiliContract.expected_capabilities()`；以及
    # `...::test_capabilities_agree_with_the_config_mirrors`）。

    def test_the_subtitle_preference_mirrors_the_declaration(self) -> None:
        """`prefer_subtitles` 必须与 `supports_subtitles` 一致。

        这条留在平台侧而不是上提：那个字段是 B站 独有的（基类没有），而且它今天
        `ui:hidden`、没人读（ADR-0012）—— 唯一还成立的作用就是"别和声明打架"。
        """
        assert BilibiliAdapter.capabilities.supports_subtitles == BilibiliConfig().prefer_subtitles

    def test_config_schema_is_the_bilibili_model(self) -> None:
        assert BilibiliAdapter.config_schema() is BilibiliConfig

    def test_wrong_config_type_is_an_assembly_error(self, tmp_path: Path) -> None:
        with pytest.raises(PlatformError, match="装配错误"):
            BilibiliAdapter(DouyinConfig(display_name="抖音"), make_deps(tmp_path))

    async def test_healthcheck_never_opens_the_bridge(self, tmp_path: Path) -> None:
        """`needs_browser=False` 的承诺：B站 的采集不碰 CDP 桥（桥只绑回环那条硬约束的边界）。"""
        assert BilibiliAdapter.capabilities.needs_browser is False
        assert make_adapter(tmp_path)._deps.bridge is None
        report = await make_adapter(tmp_path).healthcheck()
        assert "bridge" not in report.components
