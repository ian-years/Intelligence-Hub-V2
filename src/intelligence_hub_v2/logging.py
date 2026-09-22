"""structlog 配置。JSON 输出到 stdout + 可选轮转文件。

V1 §7.19 经验：子进程继承的是启动方那份环境快照。
V2 用 structlog 的 contextvars 绑定 task_id / platform，不依赖环境变量。
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TypeVar, cast

import structlog

_StreamT = TypeVar("_StreamT")
"""`_ensure_utf8_stream` 用 TypeVar 而不是 `Any`：进来的流原样出去，
调用方拿到的仍然是它交进去的那个类型（`sys.stdout` 还是 `TextIOWrapper`）。"""

QUIETED_LOGGERS = ("uvicorn.access", "httpx", "httpcore", "apscheduler", "aiosqlite")
"""压到 WARNING 的第三方 logger。

`uvicorn.access` 每个请求一行，跑采集任务时会把真正的错误冲走。
"""

_json_dumps_utf8 = functools.partial(json.dumps, ensure_ascii=False)
"""JSON 渲染用的 serializer。

`ensure_ascii=False` 是**必须的**：日志里全是中文（博主昵称、视频标题、
平台返回的错误原文），默认的 `ensure_ascii=True` 会把它们写成
`\\u59dc\\u80e1\\u8bf4` —— 人读不了，grep 也搜不到，等于把日志废掉一半。

代价是 stdout 必须能编码中文，见 `_ensure_utf8_stream()`。
"""


def _ensure_utf8_stream(stream: _StreamT) -> _StreamT:
    """把 stdout/stderr 重配成 UTF-8，编不了的字符降级成 `?` 而不是抛异常。

    V1 那条「Windows 上所有命令都要 `-X utf8`」的纪律（V1 AGENTS.md §4）
    在 V2 里改成**进程自己负责**：GBK 控制台写中文会 UnicodeEncodeError，
    一个常驻服务因为打日志崩掉是不可接受的。

    `errors="replace"` 是兜底：宁可日志里出现一个 `?`，也不要抛异常。
    重配失败（流被替换成不支持 reconfigure 的对象，比如 pytest 的捕获器）就原样返回。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return stream
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        return stream
    return stream


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
    log_file: Path | None = None,
    *,
    rotate_max_bytes: int = 52_428_800,
    rotate_backup_count: int = 5,
) -> None:
    """配置 structlog + stdlib logging。

    Args:
        level: DEBUG / INFO / WARNING / ERROR
        fmt: 'json'（生产）或 'console'（开发，彩色）
        log_file: 可选文件路径，None = 只 stdout。父目录自动创建（V1 §4.1 的 mkdir 纪律）。
        rotate_max_bytes: 单文件上限，超了轮转。默认 50 MB。
        rotate_backup_count: 保留几个旧文件。默认 5。

    **幂等**：重复调用会先清掉 root 上自己装的 handler，不会越叠越多。
    代价是也会清掉别人的 handler（比如 pytest 的 caplog），
    所以测试里用完要还原 —— 见 tests/unit/test_logging_setup.py。
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
        renderer = structlog.processors.JSONRenderer(serializer=_json_dumps_utf8)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # cache_logger_on_first_use=False：setup_logging 可能在进程里被调多次
        # （测试、配置热重载），缓存住第一个 logger 会让后来的配置不生效。
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        # 第三方库（uvicorn / sqlalchemy / httpx）用 stdlib logging。
        # 不给 foreign_pre_chain 的话它们的记录会缺 logger/level/timestamp 字段，
        # 于是同一份日志文件里一半是结构化 JSON、一半是裸文本，grep 不动也喂不进机器。
        foreign_pre_chain=shared_processors,
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(_ensure_utf8_stream(sys.stdout))]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=rotate_max_bytes,
                backupCount=rotate_backup_count,
                encoding="utf-8",
            )
        )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for name in QUIETED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str, **initial_values: object) -> structlog.BoundLogger:
    """获取已绑定的 logger。

    返回类型是 `structlog.BoundLogger`（typing.Protocol），**不是**
    `structlog.stdlib.BoundLogger`：`structlog.get_logger()` 实际交出来的是
    `BoundLoggerLazyProxy`，它按 Protocol 结构化地满足前者、但不是后者的实例。
    写成后者就得靠 `cast` 把谎话压过去 —— 2026-09-22 真写过这个，
    被 `isinstance` 测试当场抓住（见 docs/lessons.md）。

    `initial_values` 会绑进每条记录（如 `platform='douyin'`）。
    task_id 这种按任务变的走 `structlog.contextvars.bind_contextvars()`，
    不要走这里 —— V1 §7.19 那条经验：靠环境/全局传上下文的办法在子进程边界上会断。
    """
    return cast("structlog.BoundLogger", structlog.get_logger(name, **initial_values))
