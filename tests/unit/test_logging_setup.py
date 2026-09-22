"""logging 装配的单元测试。

`setup_logging()` 会**替换 root logger 的 handler**，这是全局副作用。
所以这里用一个 autouse fixture 把 root handler / level 与 structlog 配置
在每条用例前后都还原 —— 否则 pytest 自己的 caplog 会被冲掉，
后面的用例开始出现"日志莫名其妙没了"的假故障。
"""

from __future__ import annotations

import io
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest
import structlog

from intelligence_hub_v2.logging import (
    QUIETED_LOGGERS,
    _ensure_utf8_stream,
    get_logger,
    setup_logging,
)


class _NoReconfigureStdout(io.StringIO):
    """模拟被重定向的 stdout（capsys / StringIO 都没有 reconfigure）。

    显式把属性置 None，`getattr(..., None)` 那条分支才会走到。
    """

    reconfigure = None  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _restore_logging() -> Any:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in QUIETED_LOGGERS}
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


def _last_stdout_line(captured: str) -> dict[str, Any]:
    lines = [line for line in captured.splitlines() if line.strip()]
    assert lines, "stdout 上没有任何日志输出"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# JSON 输出（生产档）
# ---------------------------------------------------------------------------


def test_json_output_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="INFO", fmt="json")
    get_logger("test").info("hello.world", answer=42)

    record = _last_stdout_line(capsys.readouterr().out)
    assert record["event"] == "hello.world"
    assert record["answer"] == 42
    assert record["level"] == "info"
    assert record["logger"] == "test"
    assert "timestamp" in record


def test_bound_initial_values_appear_on_every_record(capsys: pytest.CaptureFixture[str]) -> None:
    """`get_logger(name, platform='douyin')` 绑的字段每条都带。"""
    setup_logging(fmt="json")
    logger = get_logger("adapter", platform="douyin")
    logger.info("list.started")
    logger.warning("list.slow")

    out = capsys.readouterr().out
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    assert [line["platform"] for line in lines] == ["douyin", "douyin"]


def test_contextvars_are_merged(capsys: pytest.CaptureFixture[str]) -> None:
    """task_id 走 contextvars 而不是环境变量（V1 §7.19 那条经验）。"""
    setup_logging(fmt="json")
    structlog.contextvars.bind_contextvars(task_id="task-abc")
    try:
        get_logger("runner").info("task.started")
        record = _last_stdout_line(capsys.readouterr().out)
        assert record["task_id"] == "task-abc"
    finally:
        structlog.contextvars.clear_contextvars()


def test_unicode_survives_json(capsys: pytest.CaptureFixture[str]) -> None:
    """博主昵称是外部输入，中文不能被转义成 \\uXXXX 糊掉。

    `ensure_ascii=True`（json.dumps 的默认）会把它写成 `\\u59dc\\u80e1\\u8bf4`：
    人读不了、grep 搜不到，而本项目的日志里**全是**中文（昵称、标题、平台错误原文）。
    """
    setup_logging(fmt="json")
    get_logger("t").info("creator", name="姜胡说", title="为什么你的内容没人看")
    raw = capsys.readouterr().out
    assert "姜胡说" in raw
    assert "为什么你的内容没人看" in raw
    assert "\\u" not in raw


def test_level_filters_below_threshold(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="WARNING", fmt="json")
    logger = get_logger("t")
    logger.info("should.not.appear")
    logger.warning("should.appear")

    out = capsys.readouterr().out
    assert "should.not.appear" not in out
    assert "should.appear" in out


def test_unknown_level_falls_back_to_info(capsys: pytest.CaptureFixture[str]) -> None:
    """配置里写了个不认识的 level，不能变成"什么都不输出"。"""
    setup_logging(level="VERBOSE", fmt="json")
    assert logging.getLogger().level == logging.INFO
    get_logger("t").info("still.visible")
    assert "still.visible" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# console 输出（开发档）
# ---------------------------------------------------------------------------


def test_console_format_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(fmt="console")
    get_logger("t").info("dev.mode", x=1)
    out = capsys.readouterr().out
    assert "dev.mode" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# 文件输出 + 轮转
# ---------------------------------------------------------------------------


def test_log_file_parent_dir_is_created(tmp_path: Path) -> None:
    """data/logs/ 不用手工建（V1 §4.1：落盘点自己 mkdir）。"""
    target = tmp_path / "nested" / "logs" / "server.log"
    setup_logging(fmt="json", log_file=target)
    get_logger("t").info("to.file")

    for handler in logging.getLogger().handlers:
        handler.flush()
    assert target.is_file()
    assert "to.file" in target.read_text(encoding="utf-8")


def test_file_handler_is_rotating(tmp_path: Path) -> None:
    """LoggingSection 有 rotate_max_bytes / rotate_backup_count，就必须真轮转。

    普通 FileHandler 会让 server.log 无限长 —— 常驻服务跑几周就是几个 GB。
    """
    target = tmp_path / "server.log"
    setup_logging(
        fmt="json",
        log_file=target,
        rotate_max_bytes=1_048_576,
        rotate_backup_count=3,
    )
    rotating = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 1_048_576
    assert rotating[0].backupCount == 3


def test_no_log_file_means_stdout_only() -> None:
    setup_logging(fmt="json", log_file=None)
    assert len(logging.getLogger().handlers) == 1


# ---------------------------------------------------------------------------
# 幂等与降噪
# ---------------------------------------------------------------------------


def test_setup_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """调两次不能把 handler 叠成两份（否则每条日志打两遍）。"""
    setup_logging(fmt="json")
    setup_logging(fmt="json")
    assert len(logging.getLogger().handlers) == 1

    get_logger("t").info("once")
    out = capsys.readouterr().out
    assert len([line for line in out.splitlines() if line.strip()]) == 1


def test_third_party_loggers_are_quieted() -> None:
    """uvicorn.access 每请求一行，会把真正的错误冲走。"""
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    setup_logging(fmt="json")
    for name in QUIETED_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_stdlib_logging_also_gets_structlog_format(capsys: pytest.CaptureFixture[str]) -> None:
    """第三方库用 stdlib logging，输出格式必须与 structlog 一致，不能一半 JSON 一半纯文本。"""
    setup_logging(fmt="json")
    logging.getLogger("some.library").warning("via stdlib")
    record = _last_stdout_line(capsys.readouterr().out)
    assert record["event"] == "via stdlib"
    assert record["level"] == "warning"


def test_get_logger_satisfies_the_bound_logger_protocol() -> None:
    """`structlog.get_logger()` 交出来的是 LazyProxy，不是 stdlib.BoundLogger 实例。

    所以 `get_logger` 的返回类型必须标成 Protocol（`structlog.BoundLogger`），
    标成具体类就得靠 cast 把谎话压过去。这条测试锁住"标对了"：
    凡是 Protocol 要求的方法都得真的在。
    """
    setup_logging(fmt="json")
    logger = get_logger("x", task_id="t1")

    for method in ("bind", "unbind", "debug", "info", "warning", "error", "critical", "exception"):
        assert callable(getattr(logger, method)), method

    bound = logger.bind(platform="douyin")
    assert callable(bound.info)
    assert not isinstance(logger, structlog.stdlib.BoundLogger)


# ---------------------------------------------------------------------------
# stdout 编码兜底：Windows GBK 控制台不许把服务搞崩
# ---------------------------------------------------------------------------


def test_ensure_utf8_stream_reconfigures_when_possible() -> None:
    """有 reconfigure() 就调它，并且必须带 errors='replace'。

    `errors` 不能省：日志里的中文万一遇到某个不能编码的字符（比如 emoji 昵称），
    抛异常等于把整个任务带崩，而宁可输出一个 `?`。
    """
    calls: list[tuple[object, object]] = []

    class _Stream:
        def reconfigure(self, encoding: str, errors: str) -> None:
            calls.append((encoding, errors))

    stream = _Stream()
    assert _ensure_utf8_stream(stream) is stream
    assert calls == [("utf-8", "replace")]


def test_ensure_utf8_stream_returns_unchanged_without_reconfigure() -> None:
    """某些被替换过的 stdout（capsys、重定向到 StringIO）没有 reconfigure，原样返回。"""
    stream = io.StringIO()
    assert _ensure_utf8_stream(stream) is stream


def test_ensure_utf8_stream_swallows_reconfigure_failure() -> None:
    """reconfigure 抛错也不能往外冒 —— 这是兜底路径，兜底自己不能变成新的故障源。"""

    class _BrokenStream:
        def reconfigure(self, **kwargs: object) -> None:
            raise OSError("closed stream")

    stream = _BrokenStream()
    assert _ensure_utf8_stream(stream) is stream


def test_setup_logging_survives_a_stdout_that_cannot_reconfigure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端：把 sys.stdout 换成没有 reconfigure 的对象，setup_logging 不许炸。"""
    plain = _NoReconfigureStdout()
    monkeypatch.setattr(sys, "stdout", plain)
    setup_logging(fmt="json")
    get_logger("x").info("还活着")
    plain.flush()
    assert "还活着" in plain.getvalue()
