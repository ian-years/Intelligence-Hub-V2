"""断点持久化进度：**先落盘、后打屏**（T4.5，V1 `durable_progress.py` 的 V2 适配版）。

**那个顺序就是本模块存在的全部理由。** 终端是一条随时会没掉的可观测性通道：
Windows 上关掉控制台、调用方超时把进程 kill 掉、`pythonw` 下压根没有 stdout 句柄
（V1 那份 docstring 的原话就是"命令包装器超时会把输出句柄关掉"）。
进度如果先打屏再落盘，进程死在第 7 条上时盘上只剩 6 条，
读的人分不清"它走到哪一步了"和"它死在第几步"。
反过来先落盘，屏那一路什么时候断都无所谓 —— 盘上最后一行就是它真的走到过的最后一步。
所以 `emit()` 里那两行的**先后是契约**，不是实现细节：
`tests/unit/core/test_durable_progress.py::test_the_line_is_on_disk_before_the_screen_ever_sees_it`
把它钉成了写反就会当场红的东西（改这块记得重做那条的变异检查）。

四条从 V1 原样保住的行为不变量：

1. **盘上写失败照实抛出**，且此时屏上什么都没有。权威记录写不进去还要打一条好看的
   进度，就是 AGENTS.md §1.3 禁的那个"看起来在跑"。
2. **屏上写失败不抛**，并把这件事本身追加进盘上那份（`terminal_output_failed=…`）。
   只丢屏可以接受，悄悄丢掉不行。V1 抓的是 `OSError`（坏管道）与 `ValueError`
   （流已 close）；这里额外把 `sys.stdout is None` 也归进同一条降级路径 ——
   那是 `pythonw` / 无控制台启动时的形状（CPython 的既定行为，本机没实测过），
   让它在一个跑到一半的同步里炸出 `AttributeError` 没有任何意义。
3. **盘上那条带 `%Y-%m-%d %H:%M:%S pid=NNN` 前缀，屏上是原文、不加前缀**（V1 的形状）。
   终端是给人跟着滚的，前缀会吃掉本该给正文的宽度；文件是给事后 grep 的，而 V1 的
   日志按天一份、多次运行共用（`sync_feishu_base_to_local.py:1850`），
   `grep 'pid=1234'` 是把这一次跑挑出来的抓手。
4. **落盘 = 交给操作系统**（每次 `open("a") … close()`，不攒缓冲、不留跨生命周期的句柄），
   **不是 `fsync` 到物理介质**。进程被 kill、句柄被关够用；机器掉电不够用。
   别把它读成比这更强的保证。

**打屏那一路为什么不走 structlog**（这条任务唯一真要判断的地方）：

- `logging.setup_logging()` 已经把 stdout 交给了 JSON renderer。往同一个 fd 上再写裸文本，
  等于让下游那根 `jq` 在某个进度行当场崩；反过来把进度塞进 structlog 就**丢掉了本体的
  那一半**：文件 handler 是可选的（`log_file=None` 是默认），而 stdlib logging 在 handler
  抛异常时按 `raiseExceptions` 吞掉 —— "盘上到底有没有这一行"就变成要看 logging 的内部
  行为，而这里要的恰恰是自己写、写在前面、写不进去就抛。
- 给机器读的进度本来就该发 `Event`（`core/event_bus.py`，契约见 `docs/specs/event-schema.md`），
  那条通道不承诺"先落盘"，也不面向"人盯着跑的一长串"。两条通道各有各的读者，不并成一条。
- 附带收益：`stream` 是可注入的普通对象，测试不必去动全局 logging 配置 ——
  `setup_logging()` 会清掉 root 上所有 handler（包括 pytest 的 caplog），见它自己的 docstring。

**形状为什么是一个类而不是 V1 那个函数**：V1 那两个飞书脚本里 19 处调用点每处都得自己
带 `log_path=`（`emit_durable(msg, log_path=log_path)`），漏传一处的后果是"这条进度只存在于
已经没了的终端里" —— 而它恰恰不可能被当场发现。这里把路径**绑一次**：
`p = DurableProgress(storage.log_file("durable.log"))`，调用点只剩 `p.emit("…")`。
V1 的 `emit_durable(m, log_path=p)` 一一对应 `DurableProgress(p).emit(m)`，搬 V1 用例照这行换。

**正文按原样写，不折行不转义**：V1 就吃过这个亏的形状 —— 多行错误原文会被写成多行，
只有第一行带前缀（有用例钉着这条，见测试
`test_a_multiline_message_lands_verbatim_and_only_the_first_line_is_prefixed`）。
要一行一条的机器可读记录，那是清单（`core/manifest.py`）与事件流的活。

**未接入主流程**（计划里 T4.5 那格"未适配"的另一半仍然成立）：今天没有调用方。
真正的接线点是"把 `FileStorage.logs_dir` 下某个文件名递进来"那一句（飞书薄壳与长任务
各一处），以及 `config` 要不要多一个 durable 开关 —— 后者属于改契约，要走 ADR。
`tests/unit/core/test_durable_progress.py::test_no_main_flow_imports_it_yet` 守着
"还没人 import"这句话；真接线时把它和这份 docstring 一起改掉，别绕过去。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

__all__ = ["TIME_FORMAT", "DurableProgress", "local_wall_clock"]

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
"""盘上前缀用的格式，与 V1 逐字一致（见模块 docstring 不变量 3）。"""

_SCREEN_WRITE_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError)
"""打屏那一路**允许**失败成什么样：坏管道（`OSError`）、流已 close（`ValueError`）。

不是这两个的一律照实抛出 —— 把 `TypeError` 也吞掉就等于把自己的实现 bug 记成
"终端没了"，那还是撒谎，只是换了个方向。
"""


def local_wall_clock() -> datetime:
    """本地墙上时钟，但是**带时区**的。

    先 `datetime.now(UTC)` 再 `.astimezone()`：渲染出来的数字和本地终端一致
    （V1 用的是 naive 的 `datetime.now()`），而对象是 aware 的。裸 `now()` 除了被
    `DTZ005` 拦，真正的代价是一份没有时区标记的 forensic 日志 —— 跨机对时的时候
    要人猜这台机器当时是几点。
    """
    return datetime.now(UTC).astimezone()


class ProgressStream(Protocol):
    """打屏那一路只要 `write` + `flush`。

    不写成 `TextIO` 是因为那要求一整个文件对象（`readline` / `encoding` / `closed` …），
    而测试里那个"我被打过、并且我去数盘上已有几行"的探针只想实现两个方法。
    形参写成只位置，`sys.stdout` 才结构上对得上（typeshed 里是 `write(self, __s, /)`）。
    `write` 返回 `object` 而不是 `int`：真实流返回写入长度，探针没义务报数。
    """

    def write(self, text: str, /) -> object: ...

    def flush(self) -> None: ...


@dataclass(frozen=True)
class DurableProgress:
    """一条进度：追加进 `log_path`，然后打给 `stream`。**先后不许颠倒。**

    `stream=None` 时在 `emit()` 那一刻取 `sys.stdout`，**不在构造时取** ——
    这类长任务里"先造好发射器、后面才换掉 stdout"是真实形状（测试的 capsys、
    被重定向的子进程），构造时就绑定等于把进度打进一个没人再看的旧句柄。
    """

    log_path: Path
    """盘上那份。父目录每次 emit 时确保存在（V1 §4.1 的 mkdir 纪律，同 `logging.py`）。"""

    stream: ProgressStream | None = None
    """打屏那一路。None = 每次 emit 现取 `sys.stdout`。"""

    clock: Callable[[], datetime] = local_wall_clock
    """时间戳来源，抽出来是为了能让用例钉住一个固定的时刻（不看真实时钟的脸色）。"""

    def emit(self, message: str) -> None:
        """写一条进度。**盘先、屏后**，理由见模块 docstring。"""
        prefix = f"{self.clock().strftime(TIME_FORMAT)} pid={os.getpid()}"
        self._append(f"{prefix} {message}")
        try:
            self._screen(message)
        except _SCREEN_WRITE_ERRORS as exc:
            # 屏没了不是事故，但也不是"什么都没发生"：把事故本身记进权威那份。
            # 这里再抛就是让一条死掉的终端打断一次正在写的同步。
            self._append(f"{prefix} terminal_output_failed={type(exc).__name__}: {exc}")

    def _append(self, line: str) -> None:
        """追加一行。**失败不吞**：权威记录写不进去时，屏上那条就成了假话。"""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # 每次重开文件：不攒缓冲（进程死在半路时，前面说过的话已经在盘上），
        # 也不留一个跨生命周期的句柄（那种句柄要靠解释器在 kill 时兜底关，正是这里躲的开）。
        # newline="\n" 是 Windows 上必需的：默认会把 \n 翻成 \r\n，同一份日志在两台机器上
        # 字节不同，行数和哈希都对不上。encoding 写死见 tests/unit/test_encoding_discipline.py。
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{line}\n")

    def _screen(self, message: str) -> None:
        """把原文（不带前缀）打出去并立刻 flush。

        不用 `print()`：它没有"必须 flush"这条契约的显式形状（要 `flush=True`），
        而库代码里 `print` 本来就等价于 `sys.stdout.write` + `flush`（V2 的库代码里
        只有 `main.py` 的 CLI 收尾在用它）。
        """
        target: ProgressStream | None = sys.stdout if self.stream is None else self.stream
        if target is None:
            # pythonw / 无控制台：句柄根本不存在。抛在这里不是事故 —— emit() 的 except
            # 会把它记成 `terminal_output_failed=OSError`，进度本身已经先在盘上了。
            msg = "没有可用的 stdout 句柄（sys.stdout is None，pythonw / 无控制台启动会出现）"
            raise OSError(msg)
        target.write(f"{message}\n")
        target.flush()  # flush 也可能抛（坏管道是在 flush 时才报出来的），所以也在 try 里
