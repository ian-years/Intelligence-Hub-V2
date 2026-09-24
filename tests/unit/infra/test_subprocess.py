"""`infra.subprocess` 测试。

三条不显然的（都是 V1 的形状）：
- 两路管道并发排空（顺序读会死锁，而 yt-dlp 的诊断走 stderr）；
- 超时之后必须真的收尸（否则攒僵尸进程），且**超时也要带 stderr 尾部**；
- 缺二进制时错误里要点出**是哪个可执行文件**。

不碰网络也不碰真媒体：全部用 `sys.executable -c ...`，
这样"跑通了"证明的是包装层本身，而不是某台机器上装了 ffmpeg。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from intelligence_hub_v2.infra.subprocess import (
    KILL_GRACE_SECONDS,
    SubprocessTimeoutError,
    run_subprocess,
)

PY = sys.executable


def _py(code: str) -> list[str]:
    return [PY, "-X", "utf8", "-c", code]


# ---------------------------------------------------------------------------
# 基本形状
# ---------------------------------------------------------------------------


async def test_captures_both_streams_separately() -> None:
    result = await run_subprocess(
        _py("import sys; print('到 stdout'); print('到 stderr', file=sys.stderr)")
    )
    assert result.ok
    assert result.returncode == 0
    assert "到 stdout" in result.stdout
    assert "到 stderr" not in result.stdout
    assert "到 stderr" in result.stderr
    assert "到 stdout" not in result.stderr


async def test_tail_defaults_to_stderr_because_that_is_where_failures_live() -> None:
    result = await run_subprocess(
        _py("import sys; [print(f'行{i}', file=sys.stderr) for i in range(20)]")
    )
    lines = result.tail().splitlines()
    assert lines[-1] == "行19"
    assert len(lines) == 5


async def test_nonzero_exit_is_data_not_exception() -> None:
    """退出码非零**不抛**。抛不抛由调用方决定：yt-dlp 一档失败还要试下一档。"""
    result = await run_subprocess(_py("import sys; sys.exit(3)"))
    assert result.returncode == 3
    assert result.ok is False


async def test_empty_is_a_zero_byte_success() -> None:
    result = await run_subprocess(_py("pass"))
    assert result.ok and result.stdout == "" and result.duration_seconds >= 0.0


async def test_empty_argv_is_rejected_before_spawning() -> None:
    with pytest.raises(ValueError, match="空的 argv"):
        await run_subprocess([])


# ---------------------------------------------------------------------------
# 死锁与背压（模块 docstring 的第一条）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
async def test_a_flooded_pipe_does_not_deadlock(stream: str) -> None:
    """子进程把**另一路**管道写满时也必须能读完。

    4000 行 × 100 字节 ≈ 400 KB，远超 64 KB 的管道缓冲。
    顺序读的实现会在这里卡死（孩子写不进、大人读不到尾），所以这条用例
    是"两路并发排空"唯一的看护。
    """
    code = f"import sys;[print(str(i)*100, file=sys.{stream}) for i in range(4000)]"
    result = await run_subprocess(_py(code), timeout=60)
    assert result.ok
    assert len(getattr(result, stream).splitlines()) == 4000


async def test_stdout_lines_are_streamed_to_the_progress_callback() -> None:
    """yt-dlp 的百分比在 stdout 的行里。攒到最后一次性交出来，SSE 就白做了。"""
    seen: list[str] = []
    result = await run_subprocess(
        _py("import sys;[print(f'{i}', flush=True) for i in range(5)]"),
        on_stdout_line=seen.append,
    )
    assert result.ok
    assert [line for line in seen if line.strip().isdigit()] == ["0", "1", "2", "3", "4"]


# ---------------------------------------------------------------------------
# 超时与收尸
# ---------------------------------------------------------------------------


async def test_timeout_raises_a_timeouterror_subclass_with_the_stderr_tail() -> None:
    """`manifest_writer` 靠 `except TimeoutError` 认它，所以**必须**是它的子类。"""
    with pytest.raises(SubprocessTimeoutError) as caught:
        await run_subprocess(
            _py("import sys, time; print('先说一句', file=sys.stderr, flush=True); time.sleep(30)"),
            timeout=1.0,
        )
    error = caught.value
    assert isinstance(error, TimeoutError)
    assert error.timeout == 1.0
    assert "先说一句" in error.stderr
    assert "先说一句" in str(error)  # 光有属性不够，日志里必须直接看得见
    assert "1s" in str(error)


async def test_the_process_is_reaped_after_a_timeout() -> None:
    """超时不能留下活着的子进程 —— 否则一天跑下来攒一堆僵尸 yt-dlp。

    判据：超时返回后再起一条同名命令能正常跑完，且 `run_subprocess` 自己没挂。
    （比"数进程"可靠，也不依赖 ps 输出格式。）
    """
    with pytest.raises(SubprocessTimeoutError):
        await run_subprocess(_py("import time; time.sleep(30)"), timeout=0.5)
    after = await run_subprocess(_py("print('还活着')"), timeout=5)
    assert after.ok


async def test_the_timeout_budget_is_respected_loosely() -> None:
    """不该等到 30 秒才返回（那说明超时没生效），也不该在 0.2 秒就返回（没等够）。"""
    started = asyncio.get_running_loop().time()
    with pytest.raises(SubprocessTimeoutError):
        await run_subprocess(_py("import time; time.sleep(30)"), timeout=1.0)
    elapsed = asyncio.get_running_loop().time() - started
    assert 1.0 <= elapsed < 1.0 + KILL_GRACE_SECONDS


async def test_a_hard_cancel_kills_the_child_instead_of_waiting_it_out() -> None:
    """外部 `cancel()` 也要**杀掉子进程**并立刻返回（review P1-1）。

    旧实现只在 `except TimeoutError` 分支杀进程：`CancelledError` 是
    `BaseException` 走不到，`finally` 的 `await proc.wait()` 变成
    "等子进程自然结束"—— 睡 30 秒的子进程让取消挂满 30 秒。
    真实触发：runner 的任务级 `wait_for` 超时、uvicorn 关停、scheduler 硬取消。
    判据：取消后**远小于**子进程的 30 秒内返回。回归时这条会挂满 30 秒再红。
    """
    task = asyncio.create_task(run_subprocess(_py("import time; time.sleep(30)"), timeout=None))
    await asyncio.sleep(0.5)  # 让子进程真的起来
    started = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 15.0, f"取消后 {elapsed:.1f}s 才返回 —— 在等子进程自然结束，收尸挂住了"


# ---------------------------------------------------------------------------
# 缺二进制与 cwd / env
# ---------------------------------------------------------------------------


async def test_missing_executable_names_the_binary() -> None:
    """ "装了什么"与"为什么失败"必须一眼看出来（V1 §1.3 + §4.1）。

    让它变成一次"网络失败"或直接跳过，就是这个仓库历史上"看起来在跑"的成因。
    """
    with pytest.raises(LookupError, match="definitely-not-a-real-binary"):
        await run_subprocess(["definitely-not-a-real-binary", "--version"])


async def test_a_file_that_is_not_executable_reports_oserror() -> None:
    with pytest.raises(OSError, match="启动"):
        await run_subprocess([__file__])  # 一个 .py 文件，不是可执行文件


async def test_cwd_changes_the_working_directory(tmp_path: Path) -> None:
    result = await run_subprocess(_py("import os; print(os.getcwd())"), cwd=tmp_path)
    assert result.ok
    # 只比 tmp_path 的那一段：Windows 上 getcwd() 会解析成短路径/带盘符的规范形式，
    # 与 pytest 给的 tmp_path 字面量不一定逐字相等（比字面量会得到一个假红）。
    assert tmp_path.name.lower() in result.stdout.strip().lower()


async def test_env_replaces_instead_of_merging() -> None:
    """`env=` 是**整个替换**，不是往现有环境上叠一层。

    这个语义必须写进测试：有人会传 `env={"PATH": ...}` 然后奇怪为什么 `HOME` 没了。
    继承要自己 `{**os.environ, ...}` 拼。
    """
    inherited = await run_subprocess(_py("import os; print(len(os.environ) > 0)"))
    assert "True" in inherited.stdout

    stripped = await run_subprocess(
        _py("import os; print('HOME' in os.environ, 'PATH' in os.environ)"),
        env={"ONLY": "1"},
    )
    assert "False False" in stripped.stdout
