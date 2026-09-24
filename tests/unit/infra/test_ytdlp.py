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

import json
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
from intelligence_hub_v2.platforms.bilibili import listing


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
    assert "--flat-playlist" in argv
    # 命令行 flag 与解析器共用同一份契约常量（2026-09-24 review P0-1：
    # 这里曾经写死 `-J`，与 `parse_dump_json_lines` 吃的逐行形状对不上，B站 枚举恒空，
    # 而 FakeYtDlpRunner 喂的 fixture 恰好是 `-j` 形状，测试全绿但真机必挂）。
    assert ytdlp_module.FLAT_PLAYLIST_DUMP_FLAG in argv
    assert "-J" not in argv
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


def test_merged_output_is_recognized_not_judged_empty(tmp_path: Path) -> None:
    """合并成功的常规路径（review P0-2）：`-f bv*+ba/b --merge-output-format mp4` 下
    stdout 只有两个**已被删除的**分片 `Destination:` 行 + 一行
    `[Merger] Merging formats into "media.mp4"`。认不到 Merger 行的话，
    `classify_artifacts` 只见两个读不到的路径 → `empty` → 媒体明明躺在磁盘上
    却被抛"没有产出可读的文件"（B站 100% 判失败、抖音关掉兜底）。"""
    media = tmp_path / "media.mp4"
    media.write_bytes(b"merged!")
    stdout = (
        "[download] Destination: media.f30064.mp4\n"
        "[download] Destination: media.f30280.m4a\n"
        '[Merger] Merging formats into "media.mp4"\n'
    )
    found = ytdlp_module._artifacts_from(stdout, tmp_path)
    parts = ytdlp_module.classify_artifacts(found)
    assert parts.kind == "single"
    assert parts.main == media


def test_merge_line_with_spaces_in_filename(tmp_path: Path) -> None:
    """文件名含空格：用引号定界而不是按空白切。"""
    stdout = '[Merger] Merging formats into "my video final.mp4"\n'
    found = ytdlp_module._artifacts_from(stdout, tmp_path)
    assert [p.name for p in found] == ["my video final.mp4"]


def test_merge_line_with_trailing_junk_is_rejected(tmp_path: Path) -> None:
    """不认长得像但尾巴有东西的行 —— 宁可漏认也不要把垃圾当路径。"""
    stdout = '[Merger] Merging formats into "x.mp4" and then some\n'
    assert ytdlp_module._artifacts_from(stdout, tmp_path) == ()


def test_a_result_without_attempts_says_so_instead_of_looking_clean() -> None:
    assert (
        YtDlpResult(ok=False, variant=None, stdout="", stderr="", returncode=1)
        .attempts_note()
        .startswith("yt-dlp 没有产生任何尝试记录")
    )


# ---------------------------------------------------------------------------
# 同一份契约：命令行 flag ↔ 解析器 ↔ fixture（2026-09-24 review P0-1 的教训）
# ---------------------------------------------------------------------------


def test_flat_playlist_flag_and_parser_agree_on_the_same_contract() -> None:
    """argv 构造（`infra/ytdlp.py`）与 stdout 解析器（`bilibili/listing.py`）
    必须认**同一个** dump 形状。

    P0-1 的形状：两处各写各的 —— 命令行 `-J`（单对象）、解析器与 fixture `-j`（逐行），
    `FakeYtDlpRunner` 从不执行真子进程，于是 1301 全绿而真机枚举恒空。
    这条用例把三件事钉在一起：flag 常量、`-J` 不在 argv、以及 **`-J` 的真实输出形状
    喂给解析器必须一条都抽不出来**（真对象形状与逐行解析互斥，谁漂谁红）。
    """
    # 1) flag 常量就是逐行档
    assert ytdlp_module.FLAT_PLAYLIST_DUMP_FLAG == "-j"

    # 2) `-J` 的真实形状（整个 playlist 一个对象、一行，见 yt-dlp YoutubeDL.to_stdout）
    #    走完**与 adapter.py 相同的调用链**必须一条卡片都抽不出来 —— playlist 本体
    #    会被当"一条 entry"解析（它以 `{` 开头），但它的 `id` 是 mid（纯数字），
    #    `entry_to_card` 认不出 BV → 空。这就是 P0-1 真机上的症状，钉在这里。
    single_json = json.dumps(
        {
            "_type": "playlist",
            "id": "486906719",  # playlist 本体的 id 是 mid（纯数字），不是 BV
            "entries": [
                {
                    "id": "BV1GJ411x7h7",
                    "title": "第一条",
                    "url": "https://www.bilibili.com/video/BV1GJ411x7h7/",
                }
            ],
        }
    )
    cards_from_single = listing.entries_to_cards(
        listing.parse_dump_json_lines(single_json), limit=5
    )
    assert cards_from_single == []  # 单对象形状 ≠ 逐行形状，逐行解析器救不了它

    # 3) 逐行形状（真 fixture 的形状）能抽出 entry —— 解析器没有被改坏
    fixture_line = json.dumps({"id": "BV1GJ411x7h7", "title": "第一条"})
    cards = listing.entries_to_cards(listing.parse_dump_json_lines(fixture_line), limit=5)
    assert [c.bvid for c in cards] == ["BV1GJ411x7h7"]
