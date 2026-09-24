"""`core/durable_progress.py`：T4.5 那条「进度先落盘、后打屏」。

核心关系只有一句：**"盘上已写入"严格先于"屏上可见"**。为了不去真杀进程，
打屏那一路换成一个探针（`DiskAwareStream`），它在被调用的**当下**自己去数盘上有几行 ——
实现改成"先 print 再 append"时它数到 0，用例当场红（2026-09-24 做过这个变异检查）。

其余判据也写成关系，不写"本机时间戳长什么样"那种样本：换台机器就红。
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from intelligence_hub_v2.core import durable_progress
from intelligence_hub_v2.core.durable_progress import (
    TIME_FORMAT,
    DurableProgress,
    local_wall_clock,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DURABLE_MODULE = "intelligence_hub_v2.core.durable_progress"
STAMP = datetime(2026, 9, 24, 13, 14, 15, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return STAMP


def _prefix() -> str:
    return f"{STAMP.strftime(TIME_FORMAT)} pid={os.getpid()}"


def _disk_lines(path: Path) -> list[str]:
    """盘上现在有几行。探针与断言共用它，免得两边各数一套。"""
    return path.read_text(encoding="utf-8").splitlines()


class DiskAwareStream:
    """会"回头看盘"的打屏探针。

    每次 `write` 之前先记下盘上当前的行数：顺序正确时永远是 1、2、3…，
    写反了第一条就是 0 —— 那条不变量全部的机器形状就这一行。
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.texts: list[str] = []
        self.disk_lines_at_write: list[int] = []
        self.flushes = 0

    def write(self, text: str, /) -> int:
        self.disk_lines_at_write.append(len(_disk_lines(self.log_path)))
        self.texts.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1


class ExplodingStream:
    """一条已经死掉的终端：一被调用就抛给定的异常。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def write(self, text: str, /) -> int:
        self.calls += 1
        raise self.exc

    def flush(self) -> None:
        self.calls += 1
        raise self.exc


class FlushOnlyBreaksStream:
    """写进缓冲成功、`flush()` 才报坏管道 —— 真终端更常见的死法。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def write(self, text: str, /) -> int:
        self.texts.append(text)
        return len(text)

    def flush(self) -> None:
        raise OSError(5, "设备已断开（flush 时才报出来）")


def _imports_durable(path: Path) -> bool:
    """这个文件是否 import 了 durable_progress（AST，不靠字符串撞运气）。

    先用"整份文本里连名字都没出现过"筛掉绝大多数文件（那种不可能 import），只对出现过的做
    AST。这不只是快：2026-09-24 这条看护一开始就是全树 parse，结果替别人那份正在中间态、
    语法还不通的 `platforms/bilibili/external_manifest.py` 背了一块红 —— 看护不该去替
    全仓的语法负责，它负责的只是"有没有人引这个模块"。
    """
    text = path.read_text(encoding="utf-8")
    if "durable_progress" not in text:
        return False
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if isinstance(node, ast.Import):
            if any(alias.name.endswith("durable_progress") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith(DURABLE_MODULE):
                return True
            if any(alias.name.endswith("durable_progress") for alias in node.names):
                return True  # `from intelligence_hub_v2.core import durable_progress`
    return False


# --------------------------------------------------------------------------- #
# 那条不变量：盘先、屏后
# --------------------------------------------------------------------------- #


def test_the_line_is_on_disk_before_the_screen_ever_sees_it(tmp_path: Path) -> None:
    """屏被"打"的那一刻，这一条已经在盘上了 —— 是探针自己去数的，不是事后对比。"""
    log = tmp_path / "durable.log"
    probe = DiskAwareStream(log)
    emitter = DurableProgress(log, stream=probe, clock=_fixed_clock)

    emitter.emit("第一条")
    emitter.emit("第二条")

    # 防空转：探针确实被打过两次，否则下面那条"顺序"断言是在空列表上绿的。
    assert probe.texts == ["第一条\n", "第二条\n"], "屏这一路根本没走到，顺序断言是空转"
    assert probe.disk_lines_at_write == [1, 2], (
        f"打屏时盘上行数={probe.disk_lines_at_write}，应为 [1, 2]（先写盘才可能这样）"
    )
    assert _disk_lines(log) == [f"{_prefix()} 第一条", f"{_prefix()} 第二条"]
    assert probe.flushes == 2


def test_the_two_channels_agree_on_the_message_and_only_the_disk_adds_a_prefix(
    tmp_path: Path,
) -> None:
    """同一条不变量的另一个方向：屏上每一条的**正文**都能在盘上找到，且顺序一致。"""
    log = tmp_path / "durable.log"
    probe = DiskAwareStream(log)
    emitter = DurableProgress(log, stream=probe, clock=_fixed_clock)
    messages = ["采集 douyin 开始", "转写 3/7", "完成"]

    for message in messages:
        emitter.emit(message)

    assert probe.texts == [f"{m}\n" for m in messages]
    assert [line.removeprefix(f"{_prefix()} ") for line in _disk_lines(log)] == messages, (
        "盘与屏在说两件不同的事，或者前缀形状变了"
    )


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(OSError(22, "已关闭的输出句柄"), id="oserror"),
        pytest.param(ValueError("I/O operation on closed file"), id="valueerror-closed"),
        pytest.param(BrokenPipeError(32, "管道正在被关闭"), id="brokenpipe-windows-console"),
    ],
)
def test_a_dead_terminal_never_breaks_the_run_and_leaves_a_trace_on_disk(
    tmp_path: Path,
    exc: Exception,
) -> None:
    """终端没了 ≠ 这次跑没了：不外抛，且"没了"这件事本身进盘上那份。

    V1 抓的是 `OSError`（坏管道）与 `ValueError`（流已 close）两条，逐个 parametrize 是为了
    让"少抓了一个类型"红在**那一个** id 上而不是混在一起。`BrokenPipeError` 本来就是
    `OSError` 的子类，但它才是 Windows 上关掉控制台时真正报出来的那个，所以单列一条。
    """
    log = tmp_path / "durable.log"
    boom = ExplodingStream(exc)

    DurableProgress(log, stream=boom, clock=_fixed_clock).emit("进度正文")

    assert boom.calls == 1, "探针一次都没被调用，那这条用例什么都没验"
    lines = _disk_lines(log)
    assert len(lines) == 2, f"进度本身丢了（或被记了两次）：{lines}"
    assert lines[0] == f"{_prefix()} 进度正文"
    assert f"terminal_output_failed={type(exc).__name__}:" in lines[1], lines[1]


def test_a_pipe_that_only_breaks_at_flush_is_still_just_a_lost_screen(
    tmp_path: Path,
) -> None:
    """坏管道常常在 `flush()` 才报出来：那一路也在降级范围内，而不是炸出去。"""
    log = tmp_path / "durable.log"
    stream = FlushOnlyBreaksStream()

    DurableProgress(log, stream=stream, clock=_fixed_clock).emit("写进了缓冲区")

    assert stream.texts == ["写进了缓冲区\n"], "失败点不在 flush，这条验的就不是它想验的"
    assert "terminal_output_failed=OSError" in _disk_lines(log)[-1]


def test_an_unrelated_bug_is_not_disguised_as_a_dead_terminal(tmp_path: Path) -> None:
    """只吞那两种 I/O 形状：把 `TypeError` 也记成"终端没了"是换个方向撒谎。"""
    log = tmp_path / "durable.log"

    with pytest.raises(TypeError):
        DurableProgress(
            log, stream=ExplodingStream(TypeError("实现错了")), clock=_fixed_clock
        ).emit("正文")

    assert _disk_lines(log) == [f"{_prefix()} 正文"], "屏怎么死的都要记，但只在降级那条上记"


def test_a_really_closed_handle_is_the_value_error_case(tmp_path: Path) -> None:
    """上面几条抛的是编出来的异常；这条用**真的**已关闭的流（V1 现场的那个形状）。"""
    log = tmp_path / "durable.log"
    with (tmp_path / "screen.txt").open("w", encoding="utf-8") as opened:
        handle = opened  # 出了 with 就是**已关闭**的流，正是 V1 现场那个形状

    DurableProgress(log, stream=handle, clock=_fixed_clock).emit("终端已经没了")

    lines = _disk_lines(log)
    assert len(lines) == 2, lines
    assert lines[0] == f"{_prefix()} 终端已经没了"
    assert "terminal_output_failed=ValueError" in lines[1], lines[1]


def test_a_disk_write_failure_propagates_and_nothing_reaches_the_screen(
    tmp_path: Path,
) -> None:
    """权威记录写不进去时必须照实失败，且屏上不许先出现一条好看的进度。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()  # log_path 本身是个目录：open() 当场 OSError
    screen = io.StringIO()

    # 防空转：同一个流换到能写的路径上确实会收到内容。
    DurableProgress(tmp_path / "ok.log", stream=screen, clock=_fixed_clock).emit("可写的")
    assert screen.getvalue() == "可写的\n"

    screen.truncate(0)
    screen.seek(0)
    with pytest.raises(OSError):
        DurableProgress(blocked, stream=screen, clock=_fixed_clock).emit("写不进去的")
    assert screen.getvalue() == "", "盘上失败却已经把进度打出去了"


# --------------------------------------------------------------------------- #
# 两份通道各自的形状（模块 docstring 的不变量 3）
# --------------------------------------------------------------------------- #


def test_the_screen_gets_the_bare_message_while_the_disk_gets_the_record(
    tmp_path: Path,
) -> None:
    log = tmp_path / "durable.log"
    screen = io.StringIO()

    DurableProgress(log, stream=screen, clock=_fixed_clock).emit("中文正文 with ascii")

    assert screen.getvalue() == "中文正文 with ascii\n", "屏上是原文，不加时间戳也不加 pid"
    (line,) = _disk_lines(log)
    assert line == f"{_prefix()} 中文正文 with ascii"


def test_the_default_screen_is_resolved_at_emit_time_not_at_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不给 `stream` 时走 `sys.stdout`，而且是 **emit 那一刻**去取（capsys 就是这个形状）。"""
    log = tmp_path / "durable.log"
    emitter = DurableProgress(log, clock=_fixed_clock)  # 先造好发射器
    late = io.StringIO()
    monkeypatch.setattr(sys, "stdout", late)  # 之后才被换掉

    emitter.emit("迟到的终端")

    assert late.getvalue() == "迟到的终端\n", "stdout 在构造时就绑了：进度打进了那个旧句柄"
    assert _disk_lines(log) == [f"{_prefix()} 迟到的终端"]


def test_a_process_without_any_console_degrades_instead_of_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pythonw` 下 `sys.stdout is None`：那是"屏没了"，不是"这次跑失败了"。"""
    log = tmp_path / "durable.log"
    monkeypatch.setattr(sys, "stdout", None, raising=False)

    DurableProgress(log, clock=_fixed_clock).emit("无控制台的一条")

    lines = _disk_lines(log)
    assert len(lines) == 2, f"进度本身没落盘：{lines}"
    assert lines[0] == f"{_prefix()} 无控制台的一条"
    assert "terminal_output_failed=OSError" in lines[1], lines[1]


def test_the_stamp_is_local_wall_clock_and_the_default_is_never_naive() -> None:
    """默认时钟不许 naive（`DTZ005` 只是表面，真正的代价是事后对不上时）。"""
    stamp = local_wall_clock()
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() is not None
    assert DurableProgress(Path("unused-but-never-written")).clock is local_wall_clock
    drift = abs(datetime.now(UTC).astimezone() - stamp)
    assert drift < timedelta(seconds=5), f"默认时钟交出来的不像『现在』：{stamp}"


def test_two_runs_sharing_one_file_are_separable_by_the_pid_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前缀不是装饰：V1 的日志按天一份、多次运行共用，`grep pid=` 是把一次跑挑出来的抓手。

    这里用假 pid 模拟"另一个进程"（真实地 fork 一个进程出来不划算）。
    """
    log = tmp_path / "shared.log"
    for fake_pid in (4242, 9999):

        def fake_getpid(pinned: int = fake_pid) -> int:  # 默认参数绑住循环变量
            return pinned

        monkeypatch.setattr(durable_progress, "os", types.SimpleNamespace(getpid=fake_getpid))
        DurableProgress(log, stream=io.StringIO(), clock=_fixed_clock).emit(f"第 {fake_pid} 次跑")

    lines = _disk_lines(log)
    assert [sum(f"pid={p} " in line for line in lines) for p in (4242, 9999)] == [1, 1]
    assert [line.split(" ", 3)[3] for line in lines] == ["第 4242 次跑", "第 9999 次跑"]


# --------------------------------------------------------------------------- #
# 路径与编码（本仓库的地盘规矩）
# --------------------------------------------------------------------------- #


def test_the_only_thing_written_is_the_path_the_caller_gave(tmp_path: Path) -> None:
    """路径由调用方给、**不许写死**：整棵 tmp_path 里只该长出那一条链。"""
    nested = tmp_path / "logs" / "deep" / "durable.log"
    assert list(tmp_path.rglob("*")) == [], "起点不干净，下面那条断言就什么都不是"

    DurableProgress(nested, stream=io.StringIO(), clock=_fixed_clock).emit("进度")

    assert nested.is_file()
    created = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert created == ["logs", "logs/deep", "logs/deep/durable.log"], created


def test_a_relative_path_stays_relative_to_the_cwd(tmp_path: Path) -> None:
    """给相对路径就写在相对位置：既没写死成绝对路径，也没偷偷 resolve 到仓库根。"""
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        DurableProgress(Path("out/d.log"), stream=io.StringIO(), clock=_fixed_clock).emit("相对")
        assert (tmp_path / "out" / "d.log").is_file()
        assert not (REPO_ROOT / "out").exists(), "被 resolve 到仓库根去了"
    finally:
        os.chdir(previous)


def test_the_durable_file_is_utf8_and_lf_only_whatever_the_platform_says(
    tmp_path: Path,
) -> None:
    """中文必须原样在盘上，且行尾是 `\\n` 而不是 Windows 默认的 `\\r\\n`。"""
    log = tmp_path / "durable.log"
    emitter = DurableProgress(log, stream=io.StringIO(), clock=_fixed_clock)

    emitter.emit("飞书读取失败：游标未推进")
    emitter.emit("第二条")

    raw = log.read_bytes()
    assert "飞书读取失败" in raw.decode("utf-8"), "没按 utf-8 写（本机默认是 cp936）"
    assert b"\r" not in raw, f"Windows 把 \\n 翻成了 \\r\\n：{raw!r}"
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")


def test_a_multiline_message_lands_verbatim_and_only_the_first_line_is_prefixed(
    tmp_path: Path,
) -> None:
    """docstring 里那句"正文按原样写，不折行不转义"是承诺，不是巧合。"""
    log = tmp_path / "durable.log"

    DurableProgress(log, stream=io.StringIO(), clock=_fixed_clock).emit("错误原文\n第二行")

    lines = _disk_lines(log)
    assert lines[0] == f"{_prefix()} 错误原文"
    assert lines[1] == "第二行", "正文被改写过了，而这不是这个模块的职权"


# --------------------------------------------------------------------------- #
# 还没接线（计划里"未适配"的另一半），以及"不许写死"的静态那道
# --------------------------------------------------------------------------- #


def test_no_main_flow_imports_it_yet() -> None:
    """今天没有任何调用方 —— 接线时要把这条和那份 docstring 末段一起改掉。

    如果有人已经把 `durable_progress` 接进主流程而没同步改
    `docs/plans/v2.1-migration-plan.md` T4.5 那一格，这里就是提醒他的那块红。
    """
    scanned = [
        p
        for root in ("src", "tools")
        for p in (REPO_ROOT / root).rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    assert len(scanned) > 50, f"只扫到 {len(scanned)} 个文件，扫描根错了：{REPO_ROOT}"
    # 防空转：检测器对"确实 import 了"的形状真的会响 —— 本文件自己就 import 了。
    assert _imports_durable(Path(__file__))

    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in scanned
        if p.name != "durable_progress.py" and _imports_durable(p)
    ]
    assert offenders == [], (
        f"主流程已经开始用 durable_progress 了：{offenders} —— "
        "改这条看护 + 模块 docstring 末段 + 计划里 T4.5 那一格"
    )


def test_no_path_is_built_from_a_literal_in_the_module() -> None:
    """路径只能从参数进来：模块里不许出现 `Path("…")` 或对字面量文件名 `open("…")`。"""
    source = (REPO_ROOT / "src" / "intelligence_hub_v2" / "core" / "durable_progress.py").read_text(
        encoding="utf-8"
    )
    offenders = [
        f"{node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"Path", "Open", "open", "mkdir"}
        and any(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args)
    ]
    assert offenders == [], f"这个模块里出现了写死的路径字面量：{offenders}"
    assert re.search(r"self\.log_path\.(open|parent)", source), "检测器要认的那个形状不见了"
