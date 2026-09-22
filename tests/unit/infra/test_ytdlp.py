"""yt-dlp 的 argv 装配与档位阶梯。

**这里不碰网络、不装 yt-dlp**：mock 掉 `run_subprocess`，只验
"发了什么 argv / 哪一档退到下一档 / 失败原文留没留"。
理由与 V1 一样（AGENTS.md §5：契约类测试 mock 掉真实网络与子进程，只验 argv、
清单字段、落盘结构）—— 真机那一档在 `@pytest.mark.real_network` 里（Task 6/7）。

阶梯这块是 V1 §7.15 的契约化，三条都要钉：
1. 顺序**照声明**，不许被"哪个更可能成功"这类猜测重排（重排过一次就没人知道画质为什么变差）；
2. 执行不了的档位**跳过**而不是硬传（`--cookies <不存在的路径>` 报的是"打不开文件"，看着像权限）；
3. 退档判据认得全原文 —— V1 就栽在少认了一句 `Could not copy Chrome cookie database`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.errors import MediaDownloadError
from intelligence_hub_v2.infra import ytdlp as ytdlp_module
from intelligence_hub_v2.infra.subprocess import SubprocessResult
from intelligence_hub_v2.infra.ytdlp import (
    YtDlpResult,
    YtDlpRunner,
    looks_like_cookie_failure,
    plan_cookie_variants,
)


def _result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    argv: tuple[str, ...] = ("yt-dlp",),
) -> SubprocessResult:
    return SubprocessResult(
        argv=argv, returncode=returncode, stdout=stdout, stderr=stderr, duration_seconds=0.1
    )


class _Recorder:
    """替身：按脚本依次返回结果，并记下每次的 argv。"""

    def __init__(self, results: list[SubprocessResult]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], **kwargs: Any) -> SubprocessResult:
        index = len(self.calls)
        self.calls.append(list(argv))
        return self.results[min(index, len(self.results) - 1)]


@pytest.fixture
def cookie_file(tmp_path: Path) -> Path:
    path = tmp_path / "douyin.com.txt"
    path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 判据表（V1 §7.3 + §7.15）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Could not copy Chrome cookie database",
        "Failed to decrypt with DPAPI",
        "ERROR: [douyin] could not open cookie file",
        "Cookie file is not accessible",
        "Operation not permitted while reading browser cookie",
    ],
)
def test_the_cookie_failure_table_recognises_every_known_wording(text: str) -> None:
    """V1 的原始事故：新那句 `Could not copy` **不在表里**，
    于是退档不触发、整条判死。每加一种写法就要往这里加一条（也要往实现里加）。"""
    assert looks_like_cookie_failure(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "HTTP Error 404: Not Found",
        "ERROR: Unsupported URL",
        "video is private",
    ],
)
def test_non_cookie_failures_are_not_squeezed_in(text: str) -> None:
    """反过来也要钉：把什么都认成 cookie 问题 = 每条死链都白试三档。"""
    assert looks_like_cookie_failure(text) is False


def test_matching_is_case_insensitive() -> None:
    assert looks_like_cookie_failure("could not COPY chrome COOKIE database")


# ---------------------------------------------------------------------------
# 阶梯装配
# ---------------------------------------------------------------------------


def test_ladder_order_follows_the_declaration_not_what_looks_likely(
    cookie_file: Path,
) -> None:
    kinds = [
        v.kind
        for v in plan_cookie_variants(
            ("exported_file", "browser", "anonymous"),
            cookies_file=cookie_file,
            browser="chrome",
        )
    ]
    assert kinds == ["exported_file", "browser", "anonymous"]


def test_missing_cookie_file_is_skipped_not_passed_through(tmp_path: Path) -> None:
    """传一个不存在的路径给 `--cookies`，yt-dlp 报"打不开文件"，看起来像权限问题。"""
    ladder = plan_cookie_variants(
        ("exported_file", "anonymous"), cookies_file=tmp_path / "nope.txt"
    )
    assert [v.kind for v in ladder] == ["anonymous"]


def test_browser_rung_is_skipped_without_a_browser_name(cookie_file: Path) -> None:
    """Windows 上这一档基本永远读不出来（V1 §7.3），所以抖音的阶梯通常根本不列它；
    列了但没浏览器名就是白等一次子进程。"""
    ladder = plan_cookie_variants(("browser", "anonymous"), cookies_file=cookie_file, browser=None)
    assert [v.kind for v in ladder] == ["anonymous"]


def test_anonymous_is_always_available() -> None:
    assert [v.kind for v in plan_cookie_variants(("anonymous",))] == ["anonymous"]
    assert plan_cookie_variants(("anonymous",))[0].args == ()


def test_every_rung_carries_a_human_label(cookie_file: Path) -> None:
    """档位差别是**画质**不是"能不能下"，所以清单 note 必须写清走了哪一档
    （V1 §7.15：登录档 1772p vs 匿名 886p）。"""
    ladder = plan_cookie_variants(
        ("exported_file", "anonymous"), cookies_file=cookie_file, browser="chrome"
    )
    assert all(v.label for v in ladder)
    assert "登录" in ladder[0].label and "匿名" in ladder[1].label


def test_unknown_rung_is_a_hard_error() -> None:
    """`CookieVariant` 是 Literal，运行期真收到别的值只能是绕过了类型 —— 抛，别静默跳。"""
    with pytest.raises(ValueError, match="未知的 cookie 档位"):
        plan_cookie_variants(["whatever"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 下载：退档逻辑
# ---------------------------------------------------------------------------


async def test_download_stops_at_the_first_working_rung(
    monkeypatch: pytest.MonkeyPatch, cookie_file: Path
) -> None:
    ladder = plan_cookie_variants(
        ("exported_file", "browser", "anonymous"), cookies_file=cookie_file, browser="chrome"
    )
    recorder = _Recorder([_result(returncode=1, stderr="Could not copy Chrome cookie database")])
    monkeypatch.setattr(ytdlp_module, "run_subprocess", recorder)

    result = await YtDlpRunner().download("https://example.com/v", Path(), variants=ladder)

    assert result.ok is False
    assert len(recorder.calls) == 3  # 三档 cookie 形状的错 → 全试完
    assert "--cookies" in recorder.calls[0]
    assert "--cookies-from-browser" in recorder.calls[1]
    assert "--cookies" not in recorder.calls[2]


async def test_a_genuine_failure_does_not_burn_the_whole_ladder(
    monkeypatch: pytest.MonkeyPatch, cookie_file: Path
) -> None:
    """这是对 V1 的一处有意改动：三档盲试让一条真·已删除的视频要等三次超时才判死。

    403/412/352 这类**风控**仍算 cookie 形状（见下一个用例），所以退档没错杀。
    """
    ladder = plan_cookie_variants(("exported_file", "anonymous"), cookies_file=cookie_file)
    recorder = _Recorder([_result(returncode=1, stderr="HTTP Error 404: Not Found")])
    monkeypatch.setattr(ytdlp_module, "run_subprocess", recorder)

    result = await YtDlpRunner().download("https://example.com/gone", Path(), variants=ladder)

    assert result.ok is False
    assert len(recorder.calls) == 1


@pytest.mark.parametrize(
    ("stderr", "tries_again"),
    [
        ("Request is blocked by server (412)", True),
        ("Request is rejected by server (352)", True),
        ("Fresh cookies (not necessarily logged in) are needed", True),
        ("Could not copy Chrome cookie database", True),
        ("ERROR: Unsupported URL", False),
    ],
)
async def test_the_escalation_boundary(
    monkeypatch: pytest.MonkeyPatch, stderr: str, tries_again: bool, cookie_file: Path
) -> None:
    """退不退档的分界。**判错方向的代价不对称**：
    该退不退 = 整轮判死（V1 真发生）；不该退一直退 = 每条死链多等两次超时。
    """
    ladder = plan_cookie_variants(("exported_file", "anonymous"), cookies_file=cookie_file)
    recorder = _Recorder([_result(returncode=1, stderr=stderr)])
    monkeypatch.setattr(ytdlp_module, "run_subprocess", recorder)
    await YtDlpRunner().download("https://example.com/x", Path(), variants=ladder)
    assert (len(recorder.calls) == 2) is tries_again


async def test_every_attempt_keeps_its_own_stderr_tail(
    monkeypatch: pytest.MonkeyPatch, cookie_file: Path
) -> None:
    """V1 §7.2 的教训：兜底成功后把 yt-dlp 的失败原文丢掉，日志只剩"未拿到媒体"，
    等于没法判断该修什么。"""
    ladder = plan_cookie_variants(("exported_file", "anonymous"), cookies_file=cookie_file)
    recorder = _Recorder(
        [
            _result(returncode=1, stderr="第一档挂了：Could not copy Chrome cookie database"),
            _result(returncode=0, stdout="[download] Destination: media.mp4"),
        ]
    )
    monkeypatch.setattr(ytdlp_module, "run_subprocess", recorder)

    result = await YtDlpRunner().download("https://example.com/x", Path(), variants=ladder)

    assert result.ok
    assert result.variant is not None and result.variant.kind == "anonymous"
    assert len(result.attempts) == 2
    assert "Could not copy" in result.attempts[0][2]
    assert "带导出" in result.attempts[0][0]
    assert "exit 1" in result.attempts_note() and "exit 0" in result.attempts_note()


async def test_download_with_an_empty_ladder_refuses_to_invent_success(
    tmp_path: Path,
) -> None:
    """阶梯被声明了却一档都执行不了 = 配置/装配错误，不能"那就匿名试一下"。"""
    with pytest.raises(MediaDownloadError, match="没有可用的 cookie 档位"):
        await YtDlpRunner().download(
            "https://example.com/x",
            tmp_path,
            variants=plan_cookie_variants(("exported_file",), cookies_file=tmp_path / "nope.txt"),
        )


async def test_binary_missing_propagates_as_lookuperror_not_a_download_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "yt-dlp 没装"与"这个视频下不下来"要的动作不同（装包 vs 换 cookie），
    糊成一个类型就分不出文案。"""

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise LookupError("找不到可执行文件 'yt-dlp'")

    monkeypatch.setattr(ytdlp_module, "run_subprocess", boom)
    with pytest.raises(LookupError, match="yt-dlp"):
        await YtDlpRunner().download("https://example.com/x", Path(), variants=(_ANON,))


_ANON = plan_cookie_variants(("anonymous",))[0]


# ---------------------------------------------------------------------------
# 枚举（flat playlist）与产物识别
# ---------------------------------------------------------------------------


async def test_flat_playlist_also_carries_the_cookie_rung(
    monkeypatch: pytest.MonkeyPatch, cookie_file: Path
) -> None:
    """V1 §7.15：枚举与下载是**两条路，两条都得带导出 cookie**。
    只给下载带 cookie 的话，枚举会随机 412/352，同一台机器上一条过一条不过。"""
    ladder = plan_cookie_variants(("exported_file", "anonymous"), cookies_file=cookie_file)
    recorder = _Recorder([_result(stdout='{"entries": []}')])
    monkeypatch.setattr(ytdlp_module, "run_subprocess", recorder)

    await YtDlpRunner().flat_playlist("https://space.bilibili.com/1/video", variants=ladder)

    argv = recorder.calls[0]
    assert "--flat-playlist" in argv and "-J" in argv
    assert "--cookies" in argv


async def test_flat_playlist_honours_the_items_budget(
    monkeypatch: pytest.MonkeyPatch, cookie_file: Path
) -> None:
    ladder = plan_cookie_variants(("exported_file",), cookies_file=cookie_file)
    recorder = _Recorder([_result(stdout="{}")])
    monkeypatch.setattr(ytdlp_module, "run_subprocess", recorder)

    await YtDlpRunner().flat_playlist(
        "https://space.bilibili.com/1/video", variants=ladder, playlist_items=5
    )
    argv = recorder.calls[0]
    assert "--playlist-items" in argv
    assert argv[argv.index("--playlist-items") + 1] == "1:5"


def test_artifacts_are_taken_from_yt_dlp_reported_paths_only(tmp_path: Path) -> None:
    """不 glob 目录：V1 §7.21 扫 `*.mp4` 会把自己产出的 `audio/part-001.m4a`
    也认成源媒体，一条作品转两遍。"""
    stdout = (
        "[download] Destination: media.f137.mp4\n"
        "[download] Destination: media.f140.m4a\n"
        "[download] media.mp4 has already downloaded\n"
        "[info] whatever\n"
    )
    result = _result(stdout=stdout)
    found = ytdlp_module._artifacts_from(result.stdout, tmp_path)
    assert [p.name for p in found] == ["media.f137.mp4", "media.f140.m4a", "media.mp4"]
    assert all(p.parent == tmp_path for p in found)


def test_a_result_without_attempts_says_so_instead_of_looking_clean() -> None:
    assert (
        YtDlpResult(ok=False, variant=None, stdout="", stderr="", returncode=1)
        .attempts_note()
        .startswith("yt-dlp 没有产生任何尝试记录")
    )
