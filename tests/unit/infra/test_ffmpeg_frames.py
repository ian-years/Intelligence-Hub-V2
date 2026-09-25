"""`infra/ffmpeg` 的截帧那一族（工坊「截图包」的数据源）。

这台机器的 PATH 里**没有 ffmpeg**（2026-09-25 实测 `where ffmpeg` 为空），
所以这里一帧都不靠真二进制：全部走 `frame_argv` 纯函数 + mock 掉的 `run_subprocess`。
"真机能不能截出图"这件事在本文里**未验证**，不许被这批绿冒充。

三条判据值得单独钉：

- **`-ss` 必须在 `-i` 前面**。放后面是"从 0 开始解到那一秒"，截第 600 秒要先把
  600 秒解完 —— 那样"同步返回"这个设计整个失效，而功能表面上还在。
- **缺二进制必须往上抛**（对比 `probe_streams()` 就地消化）。这里是在产出用户点着
  要的东西，"这台机器没装 ffmpeg"不能变成一次看起来像代码 bug 的失败，更不能变成空列表。
- **退出码 0 却没产出文件不算成功**。ffmpeg 对"`-ss` 落在片长之外"就是回 0 且不写文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.infra import ffmpeg as ffmpeg_module
from intelligence_hub_v2.infra.ffmpeg import (
    FrameTarget,
    extract_frame,
    extract_frames,
    ffmpeg_binary,
    ffmpeg_version,
    frame_argv,
)
from intelligence_hub_v2.infra.subprocess import SubprocessResult

#: 一张最小可辨识的 JPEG（SOI + 尾巴），够让"产出了字节"这件事是真的。
FAKE_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x00" * 32 + b"ffd9"


def _result(
    returncode: int = 0,
    stdout: str = "ffmpeg version FAKE-6.1 Copyright (c) 2000-2024",
    stderr: str = "",
    *,
    argv: tuple[str, ...] = ("ffmpeg",),
) -> SubprocessResult:
    return SubprocessResult(
        argv=argv,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
    )


class Seam:
    """假的 `run_subprocess`：记下每一条 argv，按需产出字节。"""

    def __init__(self, *, body: bytes = FAKE_JPEG, fail: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.body = body
        self.fail = fail or set()
        self.raise_: BaseException | None = None

    async def __call__(self, argv: Any, **_kwargs: Any) -> SubprocessResult:
        args = [str(part) for part in argv]
        self.calls.append(args)
        if self.raise_ is not None:
            raise self.raise_
        if "-version" in args:
            return _result(argv=tuple(args))
        target = Path(args[-1])
        broken = any(token in target.name for token in self.fail)
        if broken:
            return _result(returncode=1, stdout="", stderr="Invalid data found", argv=tuple(args))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.body)
        return _result(argv=tuple(args))

    def seek_requests(self) -> list[str]:
        """每条截帧命令里 `-ss` 后面那个值。"""
        out: list[str] = []
        for args in self.calls:
            if "-ss" in args and "-version" not in args:
                out.append(args[args.index("-ss") + 1])
        return out


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> Seam:
    fake = Seam()
    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    return fake


# ---------------------------------------------------------------------------
# argv 构造（纯函数，不碰子进程）
# ---------------------------------------------------------------------------


def test_the_seek_happens_before_the_input(tmp_path: Path) -> None:
    """`-ss` 在 `-i` **前面**：这是"秒级返回"的全部凭据。

    关系断言而不是逐字比一条命令：命令里还有 `-hide_banner` 那类装饰，
    真正会坏掉的是这一对的相对位置。
    """
    argv = frame_argv(tmp_path / "media.mp4", tmp_path / "shots" / "shot-600.jpg", 600.0)
    assert argv.index("-ss") < argv.index("-i"), "顺序反了就是先把 600 秒解完"
    assert argv[argv.index("-ss") + 1] == "600.000"
    assert argv[-1] == str(tmp_path / "shots" / "shot-600.jpg")


def test_exactly_one_frame_is_taken_and_audio_is_dropped(tmp_path: Path) -> None:
    """`-frames:v 1` 与 `-an`：没有前者就是"从这一秒开始把剩下的全编出来"。"""
    argv = frame_argv(tmp_path / "media.mp4", tmp_path / "shot-0.jpg", 0.0)
    assert argv[argv.index("-frames:v") + 1] == "1"
    assert "-an" in argv
    assert "-y" in argv, "同一秒重截要覆盖（shot_file 的命名规则靠这条才成立）"


def test_paths_with_spaces_and_quotes_stay_single_arguments(tmp_path: Path) -> None:
    """标题里带空格与引号是**常态**（昵称/标题都是外部输入）。

    断的是"每个路径各自占一个 argv 位置"：真把它们拼成一条 shell 字符串，
    这一条会立刻红，而症状在真机上只是"某些作品截不出图"。
    """
    media = tmp_path / '某 UP 的"片子" [测试] / media.mp4'
    out = tmp_path / "shots" / "shot-8.5.jpg"
    argv = frame_argv(media, out, 8.5)
    assert str(media) in argv
    assert str(out) in argv
    assert " ".join(argv).count(str(media)) == 1


def test_the_configured_binary_wins_and_the_default_is_a_bare_name(tmp_path: Path) -> None:
    """`config.paths.ffmpeg` 指着一个**真文件**时用它，否则回落裸名字走 PATH。"""
    assert ffmpeg_binary(None) == "ffmpeg"
    assert ffmpeg_binary("") == "ffmpeg"
    missing = tmp_path / "nope" / "ffmpeg.exe"
    assert ffmpeg_binary(missing) == "ffmpeg"
    real = tmp_path / "ffmpeg.exe"
    real.write_bytes(b"")
    assert ffmpeg_binary(real) == str(real)
    assert frame_argv(Path("a.mp4"), Path("b.jpg"), 1.0, binary=str(real))[0] == str(real)


# ---------------------------------------------------------------------------
# extract_frame
# ---------------------------------------------------------------------------


async def test_an_existing_frame_is_reused_without_spawning_anything(
    tmp_path: Path, seam: Seam
) -> None:
    """已有一帧且非空 → 不调 ffmpeg，返回 False（= 响应里那句 `cached` 的来源）。"""
    out = tmp_path / "shots" / "shot-8.jpg"
    out.parent.mkdir(parents=True)
    out.write_bytes(FAKE_JPEG)
    produced = await extract_frame(tmp_path / "media.mp4", out, at_seconds=8.0)
    assert produced is False
    assert seam.calls == []


async def test_a_zero_byte_leftover_is_not_treated_as_a_frame(tmp_path: Path, seam: Seam) -> None:
    """0 字节的残留（上次被 kill 掉）必须重截。

    写成"存在就跳过"的话，一次中断会让那一帧**永远**是空的，
    而界面上是一个坏掉的缩略图，重点多少次都不会修好。
    """
    out = tmp_path / "shots" / "shot-9.jpg"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"")
    assert await extract_frame(tmp_path / "media.mp4", out, at_seconds=9.0) is True
    assert len(seam.calls) == 1


async def test_a_missing_binary_is_reported_as_such_and_not_swallowed(
    tmp_path: Path, seam: Seam
) -> None:
    """缺 ffmpeg 必须**红**，且原文里点得出是哪个可执行文件。"""
    seam.raise_ = LookupError("找不到可执行文件 'ffmpeg'（命令：ffmpeg -hide_banner …）")
    with pytest.raises(LookupError, match="ffmpeg"):
        await extract_frame(tmp_path / "media.mp4", tmp_path / "shot-0.jpg", at_seconds=0.0)


async def test_ffmpeg_failure_keeps_the_stderr_text(tmp_path: Path, seam: Seam) -> None:
    """退出码非 0 时，ffmpeg 的原文要留在异常消息里（V1 §1.3：失败原因不许丢）。"""
    seam.fail = {"shot-3.jpg"}
    with pytest.raises(RuntimeError, match="Invalid data found"):
        await extract_frame(tmp_path / "media.mp4", tmp_path / "shot-3.jpg", at_seconds=3.0)


async def test_exit_zero_without_a_file_is_not_success(tmp_path: Path, monkeypatch) -> None:
    """退出码 0 但**没写出文件**：这是"点越界了"的真实形状，不能算成功。"""

    async def silent(argv: Any, **_kwargs: Any) -> SubprocessResult:
        args = [str(part) for part in argv]
        return _result(argv=tuple(args))

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", silent)
    out = tmp_path / "shots" / "shot-9999.jpg"
    with pytest.raises(RuntimeError, match="没产出"):
        await extract_frame(Path("media.mp4"), out, at_seconds=9999.0)


# ---------------------------------------------------------------------------
# extract_frames
# ---------------------------------------------------------------------------


def _targets(tmp_path: Path, times: list[float]) -> list[FrameTarget]:
    return [
        FrameTarget(at_seconds=value, output_path=tmp_path / "shots" / f"shot-{value:g}.jpg")
        for value in times
    ]


async def test_one_broken_second_does_not_take_the_others_down(tmp_path: Path, seam: Seam) -> None:
    """坏的是**那一秒**，不是这一批：失败的进 failures，剩下的照样交出去。"""
    times = [1.0, 2.0, 3.0]
    targets = _targets(tmp_path, times)
    targets[1] = FrameTarget(at_seconds=2.0, output_path=tmp_path / "shots" / "shot-broken.jpg")
    seam.fail = {"broken"}
    result = await extract_frames(tmp_path / "media.mp4", targets)
    assert [frame.at_seconds for frame in result.frames] == [1.0, 3.0]
    assert [item.at_seconds for item in result.failures] == [2.0]
    assert result.all_reused is False
    assert "Invalid data" in result.failures[0].reason


async def test_a_missing_binary_stops_the_whole_batch_after_one_attempt(
    tmp_path: Path, seam: Seam
) -> None:
    """缺二进制时**不重试到第 12 次**：那是全局状态，多试只会多 11 条同样的错，
    并把一次 HTTP 请求拖成十几次启动失败。"""
    seam.raise_ = LookupError("找不到可执行文件 'ffmpeg'")
    with pytest.raises(LookupError):
        await extract_frames(tmp_path / "media.mp4", _targets(tmp_path, [1.0, 2.0, 3.0]))
    assert len(seam.calls) == 1


async def test_every_frame_reused_reads_as_all_reused(tmp_path: Path, seam: Seam) -> None:
    """全部命中已有文件 → `all_reused` 为真且一次进程都不起。"""
    targets = _targets(tmp_path, [1.0, 2.0])
    for target in targets:
        target.output_path.parent.mkdir(parents=True, exist_ok=True)
        target.output_path.write_bytes(FAKE_JPEG)
    result = await extract_frames(tmp_path / "media.mp4", targets)
    assert len(result.frames) == 2
    assert result.all_reused is True
    assert seam.calls == []


async def test_frames_come_out_as_real_bytes_under_the_requested_names(
    tmp_path: Path, seam: Seam
) -> None:
    """假 seam 产出的东西**在磁盘上**、非空、且时间点各自一份。

    （这一条不证明"能解出画面"，只证明这条链路的文件语义成立。真画面未验证。）
    """
    result = await extract_frames(tmp_path / "media.mp4", _targets(tmp_path, [0.0, 8.5, 65.0]))
    assert seam.seek_requests() == ["0.000", "8.500", "65.000"]
    paths = [frame.path for frame in result.frames]
    assert len(set(paths)) == 3
    assert all(path.is_file() and path.stat().st_size > 0 for path in paths)
    assert all(path.parent == tmp_path / "shots" for path in paths)


# ---------------------------------------------------------------------------
# ffmpeg_version（附注，不许把成功搞成失败）
# ---------------------------------------------------------------------------


async def test_the_version_is_the_first_line(seam: Seam) -> None:
    assert await ffmpeg_version() == "ffmpeg version FAKE-6.1 Copyright (c) 2000-2024"


async def test_a_missing_version_is_none_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到版本只是少了附注：为此把已经截好的帧报成失败是本末倒置。"""

    async def boom(argv: Any, **_kwargs: Any) -> SubprocessResult:
        raise LookupError("找不到可执行文件 'ffmpeg'")

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", boom)
    assert await ffmpeg_version() is None
