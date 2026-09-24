"""子进程包装。所有外部二进制（yt-dlp / ffmpeg / ffprobe / lark-cli）唯一的出口。

V1 的对应物是散在各采集器里的 `subprocess.run(...)` 与启动器的 spawn。
V2 收成一份，是为了把三件在 V1 各写各的事固定下来：

- **不 `shell=True`**。昵称/标题/URL 都是外部输入，拼进 shell 字符串等于把它们当代码执行。
- **两个管道要并发排空**。先读完 stdout 再读 stderr 的写法，会在子进程把
  stderr 管道（64 KB）写满时**互相等死**：孩子在等管道被读，大人在等孩子退出。
  yt-dlp 的诊断输出走 stderr，这是最容易撞到的形状。
- **超时必须真的收尸**。`asyncio.wait_for(proc.wait(), t)` 超时只会取消 await，
  进程还在跑 —— 看板偶尔多出几个僵尸 yt-dlp 就是这个原因。
  这里 terminate → 宽限期 → kill → 最后一定 `wait()`。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SubprocessResult", "SubprocessTimeoutError", "run_subprocess"]

KILL_GRACE_SECONDS = 5.0
"""`terminate()` 之后给它的收尾时间。到点没退就 `kill()`。

5 秒不是拍脑袋：yt-dlp 收到 SIGTERM 会先清掉已下载的临时分片，
低于这个数会留下 `media.f137.mp4.part` 之类的残留（V1 的下载目录里见过）。
"""


@dataclass(frozen=True)
class SubprocessResult:
    """一次已完成的外部命令。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, stream: str = "stderr", *, lines: int = 5) -> str:
        """输出的最后几行。**错误原文必须能进清单**（V1 §1.3）。

        默认取 stderr：外部工具的失败原因几乎都在那一路。
        """
        text = self.stderr if stream == "stderr" else self.stdout
        return "\n".join(text.splitlines()[-lines:])


@dataclass
class _Lines:
    """一路管道的累加器。

    为什么要它而不是只靠 `_drain` 的返回值：**超时那一刻 `gather` 被取消，
    协程的返回值就没了**，而那时 stderr 的尾部恰恰是"为什么这么慢"的唯一线索
    （限速？重试？卡在合并？）。所以边读边往这里落一份。
    """

    lines: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines)


class SubprocessTimeoutError(TimeoutError):
    """子进程超时。

    子类自 `TimeoutError`，这样 `manifest_writer` 的超时分支认得它 ——
    任务级超时与"某条命令跑太久"在 V2 里是同一个语义（都是预算用完了）。

    带上已完成部分的输出，理由见 `_Lines`。
    """

    def __init__(
        self,
        argv: Sequence[str],
        timeout: float,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        self.argv = tuple(str(part) for part in argv)
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        detail = " ".join(self.argv)[:200]
        tail = "\n".join(stderr.splitlines()[-3:]) if stderr else "（无 stderr 输出）"
        super().__init__(f"{detail} 超时（>{timeout:.0f}s）。stderr 尾部：{tail}")


async def _drain(
    stream: asyncio.StreamReader | None,
    sink: _Lines,
    on_line: Callable[[str], None] | None,
) -> str:
    """按行读干一路管道。`on_line` 给进度回调（yt-dlp 的百分比在行里）。"""
    if stream is None:
        return ""
    while True:
        raw = await stream.readline()
        if not raw:
            break
        # errors="replace"：中文 Windows 上子进程可能吐 GBK 字节，
        # 抛 UnicodeDecodeError 会把"命令失败"这个真实信息盖掉。
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        sink.lines.append(line)
        if on_line is not None:
            on_line(line)
    return sink.text()


async def _collect(
    proc: asyncio.subprocess.Process,
    out: _Lines,
    err: _Lines,
    on_stdout_line: Callable[[str], None] | None,
    on_stderr_line: Callable[[str], None] | None,
) -> None:
    """两路管道**并发**排空，再等进程退出。

    顺序读会死锁（模块 docstring 里那条）。先 gather 两个 reader 再 `wait()`，
    不能反过来 —— 进程退了不代表管道里的字节都读完了。
    """
    await asyncio.gather(
        _drain(proc.stdout, out, on_stdout_line),
        _drain(proc.stderr, err, on_stderr_line),
    )
    await proc.wait()


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """terminate → 宽限 → kill。幂等；进程已经退了就是 no-op。"""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), KILL_GRACE_SECONDS)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


async def run_subprocess(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
) -> SubprocessResult:
    """跑一条外部命令并等它结束。**绝不使用 shell**。

    `env=None` 的意思是"继承当前进程的环境"，不是"给一个空环境"。
    要干净环境必须显式传完整 dict —— 两者行为差很多：PATH 没了 `yt-dlp` 就找不到
    node，而它报出来的错长得像"这个网站不支持"（V1 §4.1 的 node 依赖误判过一次）。
    """
    args = [str(part) for part in argv]
    if not args:
        msg = "run_subprocess 收到空的 argv"
        raise ValueError(msg)

    loop = asyncio.get_running_loop()
    started = loop.time()
    out, err = _Lines(), _Lines()

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 两个 None 都是**有语义的默认值**，不是"没传"：
            # cwd=None → 用当前工作目录；env=None → 继承本进程环境（见函数 docstring）。
            # 所以直接传，不用 `**({...} if ... else {})` 那种把类型摊平成 dict 的写法。
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as exc:
        # 二进制不在 PATH。必须**如实失败**并点出是哪一个（V1 §1.3 / §4.1）：
        # 让它变成一次"网络问题"或干脆跳过这一步，就是"看起来在跑"。
        msg = f"找不到可执行文件 {args[0]!r}（命令：{' '.join(args)[:120]}）: {exc}"
        raise LookupError(msg) from exc
    except OSError as exc:
        msg = f"启动 {args[0]!r} 失败: {type(exc).__name__}: {exc}"
        raise OSError(msg) from exc

    try:
        collect = _collect(proc, out, err, on_stdout_line, on_stderr_line)
        if timeout is None:
            await collect
        else:
            await asyncio.wait_for(collect, timeout)
    except TimeoutError as exc:
        await _kill(proc)
        raise SubprocessTimeoutError(
            args,
            float(timeout or 0.0),
            out.text(),
            err.text(),
            proc.returncode,
        ) from exc
    except BaseException:
        # **取消也要先杀进程**（review P1-1）。`CancelledError` 是 `BaseException`
        # 不是 `Exception`，走不到上面的分支；只靠 finally 的 `await proc.wait()`
        # 会一直等到子进程**自然结束**为止（yt-dlp 是 7200s 的量级）——
        # 收尸挂住，取消语义整个失效。真实触发：runner 的任务级 `wait_for`
        # 超时、uvicorn 关停、scheduler 的硬取消，都会把 CancelledError 注入到这里。
        # `_kill` 幂等，重复调用安全。变异验证：去掉这行，上一条用例挂满 30s 才红。
        await _kill(proc)
        raise
    finally:
        # 一定收尸。异常路径（含被取消）下不 wait 就是僵尸进程。
        with contextlib.suppress(Exception):
            await proc.wait()

    return SubprocessResult(
        argv=tuple(args),
        returncode=int(proc.returncode if proc.returncode is not None else 0),
        stdout=out.text(),
        stderr=err.text(),
        duration_seconds=loop.time() - started,
    )
