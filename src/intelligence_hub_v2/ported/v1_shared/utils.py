# ruff: noqa
# TODO(v2-adapt): V1 全家共用的 IO/格式化小工具，未适配，而且**大半在 V2 已有更强的对应物**：
# safe_filename → storage/files.py（签名不同：那边是 keyword-only + fallback，§7.8 有契约测试看护，
# 对 Windows 设备名与结尾点/空格的口径要逐条对过才敢并）、write_json → core/manifest.py 的清单落盘、
# utf8_child_env → core/runtime_env.py + infra/subprocess.py。now_str/ts_slug 用的是本机 naive 墙上
# 时钟，而 V2 的库与清单统一 UTC（storage/schema.UTCDateTime），两套时间口径同时在场就是双源。
# 适配要做的那件事：把飞书脚本里的调用点逐个改去上面四处，本文件随最后一个搬运脚本一起删。
"""Universal utility helpers for IO, formatting, atomic file writes, and text normalization."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_str() -> str:
    """Return current ISO timestamp in YYYY-MM-DD HH:MM:SS format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_slug() -> str:
    """Return slug format YYYYMMDD-HHMMSS for file naming."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


_WINDOWS_DEVICE_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$", re.IGNORECASE)


def safe_filename(name: str, max_length: int = 120) -> str:
    """Convert an arbitrary string into a safe, valid file or directory name.

    昵称是外部输入：只把 ``/`` 换掉挡不住 ``..``，Windows 还会静默剥掉结尾的点和空格。
    """
    text = re.sub(r'[/\\?:"<>|\x00-\x1f*]', "_", str(name or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[:max_length]
    text = text.rstrip(". ")
    if not text:
        return "unnamed"
    if _WINDOWS_DEVICE_NAME.match(text):
        text = "_" + text
    return text


def load_json(path: Path | str, default: Any = None) -> Any:
    """Read JSON from path with UTF-8 encoding and resilient error fallback."""
    p = Path(path)
    if not p.is_file():
        return default if default is not None else {}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default if default is not None else {}


def write_json(path: Path | str, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> Path:
    """Atomic write JSON data to file."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii) + "\n"

    # Write to temp file then replace atomically
    tmp_path = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(serialized, encoding="utf-8")
        tmp_path.replace(p)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        p.write_text(serialized, encoding="utf-8")
    return p


def to_simplified_zh(text: str) -> str:
    """Convert traditional Chinese to simplified Chinese if zhconv is available."""
    if not text:
        return ""
    try:
        import zhconv  # type: ignore

        return zhconv.convert(str(text), "zh-hans")
    except ImportError:
        return str(text)


def utf8_child_env() -> dict[str, str]:
    """子进程环境：强制 UTF-8 读写 + 不缓冲，口径与 ``launcher_server.child_env`` 一致。

    Windows 上子脚本默认按代码页（GBK）输出，采集脚本用 ``encoding="utf-8"`` 解码或对外的
    日志管道就会把这行中文读成乱码；采集器自己拉起转写子进程时必须带上这份 env。
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def format_duration(seconds: int | float | None) -> str:
    """Format duration in seconds to MM:SS or HH:MM:SS."""
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"
