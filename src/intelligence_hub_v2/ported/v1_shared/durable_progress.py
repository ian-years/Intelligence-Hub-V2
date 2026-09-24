# ruff: noqa
# TODO(v2-adapt): 这一份是**重复的**：V2 的对应物是 core/durable_progress.py（T4.5 已按 V2 范式适配，
# "先落盘后打屏"那四条不变量由它和它的用例看护），适配时合并过去、本文件删除。它现在还在的唯一理由
# 是形状：两个 sync 脚本按 V1 的函数式口径调 `emit_durable(message, log_path=...)`，而那个 log_path
# 来自 <ROOT>/downloads/logs 这份 V1 目录假设（V2 是 storage/files.FileStorage.logs_dir）。
# 别在这里长出第三份实现 —— 要加口径就改 core/durable_progress.py，那边的用例是变异检查过的。
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _append_log(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def emit_durable(
    message: str,
    *,
    log_path: Path,
    stream: TextIO | None = None,
) -> None:
    """Persist progress before attempting terminal output.

    Codex command runners may close their output handle when the caller times out.
    That is an observability-channel failure, not a data-sync failure. The durable
    log remains authoritative; failures to write that log are deliberately raised.
    """

    timestamp = datetime.now().strftime(TIME_FORMAT)
    prefix = f"{timestamp} pid={os.getpid()}"
    _append_log(log_path, f"{prefix} {message}\n")
    target = stream if stream is not None else sys.stdout
    try:
        print(message, file=target, flush=True)
    except (OSError, ValueError) as exc:
        _append_log(
            log_path,
            f"{prefix} terminal_output_failed={type(exc).__name__}: {exc}\n",
        )
