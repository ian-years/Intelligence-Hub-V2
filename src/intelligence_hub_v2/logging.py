"""structlog 配置。JSON 输出到 stdout + 可选文件。

V1 §7.19 经验：子进程继承的是启动方那份环境快照。
V2 用 structlog 的 contextvars 绑定 task_id / platform，不依赖环境变量。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import cast

import structlog


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
    log_file: Path | None = None,
) -> None:
    """配置 structlog + stdlib logging。

    Args:
        level: DEBUG / INFO / WARNING / ERROR
        fmt: 'json'（生产）或 'console'（开发，彩色）
        log_file: 可选文件路径，None = 只 stdout
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.types.Processor
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 降低第三方库噪音
    for name in ("uvicorn.access", "httpx", "httpcore", "apscheduler", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str, **initial_values: object) -> structlog.stdlib.BoundLogger:
    """获取已绑定的 logger。"""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name, **initial_values))
