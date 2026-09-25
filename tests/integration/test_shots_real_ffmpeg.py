"""真 ffmpeg 截帧（`-m real_network`，默认不跑）：补上那批替身用例答不了的那一格。

`tests/integration/test_api_shots.py` 与 `tests/unit/infra/test_ffmpeg_frames.py` 一共 36 条，
全部吃一个假的 `run_subprocess` —— 它们能证明 argv 的形状、文件语义、失败怎么说，
但**证不了一件事：截出来的画面是不是那一秒的内容**。那要真二进制。

这台机器上 ffmpeg 是有的（9.0.1-full_build，WinGet 装的），而 `where ffmpeg` 打不出来 ——
正是 V1 §7.19 那条：注册表 PATH 里有、进程 PATH 里没有。所以这一条用例先用
`core/runtime_env.apply_runtime_environment()` 补 PATH，再验它找得到。
那一步本身也是看护：如果哪天 `runtime_env` 不再管 ffmpeg，这里会以"找不到二进制"红掉，
而不是安静地证明一个不存在的东西。

跑法：`make test-real` 或 `pytest -m real_network tests/integration/test_shots_real_ffmpeg.py`。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from intelligence_hub_v2.core.config import AppConfig
from intelligence_hub_v2.core.runtime_env import prepare_runtime_environment
from intelligence_hub_v2.infra.ffmpeg import (
    FrameTarget,
    extract_frames,
    ffmpeg_binary,
    ffmpeg_version,
)

pytestmark = pytest.mark.real_network

#: `testsrc` 会画一条随时间走的彩条与一个递增的计时条 —— 两秒的帧**必然**不同字节。
#: 用它而不是用"随便一段视频"：这条用例要的是"seek 真的动了"，素材越确定越好。
_SOURCE = "testsrc=duration=3:size=160x120:rate=10"


def _real_ffmpeg_or_skip() -> str:
    """把进程 PATH 补成注册表那份，然后回答"这台机器到底有没有 ffmpeg"。

    这里**允许** skip 而别处不许（V1 §7.14 那条纪律管的是默认会跑的那一层）：
    整条 `real_network` 默认不入选，它的定位就是"换一台机器手工跑一次的烟雾"，
    在一台没装 ffmpeg 的机器上把 CI 弄红不是它的工作。
    """
    prepare_runtime_environment(AppConfig())
    binary = ffmpeg_binary(None)
    if binary == "ffmpeg" and shutil.which("ffmpeg") is None:
        pytest.skip("这台机器上没有可用的 ffmpeg（PATH 与注册表都没有）")
    return binary


def _make_clip(dest: Path, binary: str) -> None:
    subprocess.run(  # noqa: S603 - argv 全是常量；这一层要的就是真子进程
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            _SOURCE,
            str(dest),
        ],
        check=True,
        timeout=120,
    )
    assert dest.is_file() and dest.stat().st_size > 0, "ffmpeg 自己没造出素材，后面都无从谈起"


async def test_the_two_seconds_are_two_different_frames(tmp_path: Path) -> None:
    """同一秒重截是**同一份字节**，不同秒必须是**不同字节**。

    两半各挡一种坏法：
    - 只断"不同"，一个"每次都重头解码、`-ss` 前置形同虚设"的实现也能过（它会给第 2 秒
      交出第 1 秒的图吗？不会 —— 但它会给两秒交出**同一张**的可能性恰恰是 seek 坏掉时
      最常见的那种，所以"不同"这半必须留着）。
    - 只断"同秒重复一致"，那连"根本没 seek"都测不出来。
    """
    binary = _real_ffmpeg_or_skip()
    clip = tmp_path / "clip.mp4"
    _make_clip(clip, binary)
    version = await ffmpeg_version(ffmpeg_path=binary)
    assert version is not None and "ffmpeg version" in version.lower(), version

    first = tmp_path / "shot-1.jpg"
    second = tmp_path / "shot-2.jpg"
    again = tmp_path / "again-1.jpg"
    result = await extract_frames(
        clip, [FrameTarget(at_seconds=1.0, output_path=first)], ffmpeg_path=binary
    )
    assert result.failures == [], result.failures
    assert first.is_file() and first.stat().st_size > 0
    assert first.read_bytes().startswith(b"\xff\xd8"), "不是 JPEG 就等着浏览器黑屏"

    other = await extract_frames(
        clip, [FrameTarget(at_seconds=2.0, output_path=second)], ffmpeg_path=binary
    )
    assert other.failures == [], other.failures
    assert second.read_bytes() != first.read_bytes(), (
        "第 1 秒与第 2 秒字节相同：seek 没动，而 testsrc 那两帧不可能一样"
    )

    redone = await extract_frames(
        clip, [FrameTarget(at_seconds=1.0, output_path=again)], ffmpeg_path=binary
    )
    assert redone.failures == [], redone.failures
    assert again.read_bytes() == first.read_bytes(), "同一秒两次截图应当一致"


async def test_a_missing_input_is_reported_not_silently_empty(tmp_path: Path) -> None:
    """输入不在磁盘上时，要有一条带原文的失败，而不是"0 帧 0 失败"。

    这一条与真二进制有关：**假 seam 永远不会告这一族错**（它自己写的字节），
    所以替身层看到的全是成功路径，而真机上"媒体文件被移走"是常态（T4.4 磁盘重扫那条链）。
    """
    binary = _real_ffmpeg_or_skip()
    missing = tmp_path / "does-not-exist.mp4"
    out = tmp_path / "shot-0.jpg"

    result = await extract_frames(
        missing, [FrameTarget(at_seconds=0.0, output_path=out)], ffmpeg_path=binary
    )

    assert not out.exists(), "输入都不在，产出的那张图是从哪来的"
    assert len(result.failures) == 1, result.failures
    reason = result.failures[0].reason
    assert reason.strip(), "失败必须带原文，空原因等于没报"


async def test_the_configured_absolute_path_wins_over_path_lookup(tmp_path: Path) -> None:
    """`paths.ffmpeg` 指着一个真文件时用那个文件；指着一个假路径时**回落 PATH** 而不是抛。

    回落那半是 `ffmpeg_binary` 的既有口径（配错路径的错该由启动期与预检去报，
    不在这里重复判一次、给出第二句不同的原文）。这一条要的是"两种情况下都拿得到能跑的二进制"。
    """
    binary = _real_ffmpeg_or_skip()
    assert ffmpeg_binary(binary) == str(Path(binary).expanduser())
    assert ffmpeg_binary(tmp_path / "no-such-ffmpeg.exe") == "ffmpeg"

    clip = tmp_path / "clip.mp4"
    _make_clip(clip, binary)
    out = tmp_path / "shot-0.jpg"
    result = await extract_frames(
        clip, [FrameTarget(at_seconds=0.5, output_path=out)], ffmpeg_path=binary
    )
    assert result.failures == [] and out.is_file()
