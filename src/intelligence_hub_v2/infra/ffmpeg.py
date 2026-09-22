"""ffmpeg / ffprobe。转写链的第一步（抽音频）与媒体真相的唯一问法（probe）。

V1 在这一层踩过两类坑，都留了注释在具体函数上：

- **ffprobe 问不出来时不许当成"没有音频"**（§7.21）。判成没有就会**静默少一段口播稿**，
  而"少一条稿子"在看板上完全看不出来；宁可让 ffmpeg 报真实原因。
- **"ffmpeg 没装"与"这条媒体没有音频轨"的报错长得几乎一样**（都是 B站 后处理失败，
  一个 exit 1 一个 exit -22）。所以缺二进制必须**如实报"找不到可执行文件"**，
  不能让它变成一次看起来像代码 bug 的失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intelligence_hub_v2.infra.subprocess import run_subprocess
from intelligence_hub_v2.logging import get_logger

logger = get_logger(__name__)
"""缺二进制的 probe 会往这里记一条 debug。没有这一句，"这台机器没装 ffprobe"
在日志里就是个空白 —— 而它正是"为什么这批稿件的时长全是空的"那条线索的起点。"""

__all__ = ["StreamInfo", "extract_audio", "has_audio_stream", "probe_streams"]

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

WAV_HEADER_BYTES = 44
"""一个 WAV 文件的头。产出的音频**只比这长一点**就等于没解出任何采样。"""


@dataclass(frozen=True)
class StreamInfo:
    """ffprobe 报的一条流。"""

    index: int
    codec_type: str
    """`"video"` / `"audio"` / `"subtitle"` / `"data"`。"""

    codec_name: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class ProbeFailure:
    """probe 没问到答案。与"问到了、答案是纯视频"必须能区分开。"""

    reason: str


async def probe_streams(media_path: Path, *, timeout: float = 30.0) -> list[StreamInfo]:
    """列出所有流。文件不存在、ffprobe 看不懂、**或 ffprobe 根本没装**时返回空列表。

    空列表不等于"没有流"，也等于"问不出来" —— 调用方（`has_audio_stream`）
    必须把这两种情况一起当"问不出来"处理，这正是 V1 §7.21 那条兜底的意义。

    `LookupError`（二进制不在 PATH）**在这里就地消化**是有意偏离"缺二进制要如实报错"
    那条纪律的（见模块 docstring 与 `extract_audio`）：`extract_audio` 是在**产出用户
    要的东西**，缺 ffmpeg 必须红；这里是在**问一个问题**，问不出来就有既定的兜底答案。
    让它冒出去的话，"这台机器没装 ffprobe"会把一条已经下好的媒体变成采集失败 ——
    而那台机器上 V1 一直是能跑完整条链路的（`shutil.which("ffprobe")` 实测为 None）。
    缺 ffmpeg 这件事该红的位置是 preflight，不是每一次 probe。
    """
    if not media_path.is_file():
        return []
    try:
        result = await run_subprocess(
            [
                FFPROBE,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                str(media_path),
            ],
            timeout=timeout,
        )
    except LookupError as exc:
        logger.debug("ffprobe.unavailable", reason=str(exc), media=str(media_path))
        return []
    if not result.ok:
        return []
    return _parse_streams(result.stdout)


def _parse_streams(stdout: str) -> list[StreamInfo]:
    try:
        payload: Any = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return []
    out: list[StreamInfo] = []
    for item in streams:
        if not isinstance(item, dict):
            continue
        out.append(
            StreamInfo(
                index=_as_int(item.get("index")) or 0,
                codec_type=str(item.get("codec_type") or ""),
                codec_name=str(item["codec_name"]) if item.get("codec_name") else None,
                width=_as_int(item.get("width")),
                height=_as_int(item.get("height")),
                duration_seconds=_as_float(item.get("duration")),
            )
        )
    return out


async def has_audio_stream(media_path: Path) -> bool:
    """这条媒体有没有音频轨。**问不出来时返回 True**。

    V1 §7.21 原话：`has_audio_stream()` 答不上来时返回 True（当作有音频）。
    判成"没有"的后果是**白白丢一段口播稿** —— 而丢稿子在看板上完全不可见，
    只会表现为"这个博主的稿子怎么这么少"。宁可让下游 ffmpeg 报出真实原因
    （`Output file does not contain any stream`），一条红的可比一段静默缺失好查得多。
    """
    streams = await probe_streams(media_path)
    if not streams:
        return True
    return any(stream.codec_type == "audio" for stream in streams)


async def extract_audio(
    media_path: Path,
    output_path: Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    timeout: float = 600.0,
) -> Path:
    """抽单声道 16k WAV 给 ASR。返回 `output_path`。

    - `-vn`：丢掉视频轨。**前提是输入真有音频**，否则 ffmpeg 报
      `Output file does not contain any stream`（exit 4294967274 / -22）。
      那句看起来像 ffmpeg 没装，方向完全不同（V1 §7.21）—— 所以调用方必须先问
      `has_audio_stream` / 用 `audio_path_of()`，出错时把 stderr 原文留在清单里。
    - 输出已存在且非空就跳过：转写任务重跑是常态（改了引擎档位想再试一次），
      每次都重解一遍音频能把 CPU 烧光。
    """
    if output_path.is_file() and output_path.stat().st_size > WAV_HEADER_BYTES:
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = await run_subprocess(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            str(output_path),
        ],
        timeout=timeout,
    )
    if not result.ok:
        msg = (
            f"ffmpeg 抽音频失败（exit {result.returncode}）："
            f"{result.tail(lines=8) or '（无 stderr）'}"
        )
        raise RuntimeError(msg)
    if not output_path.is_file() or output_path.stat().st_size <= WAV_HEADER_BYTES:
        msg = f"ffmpeg 退出码 0 却没产出音频（{output_path}）—— 不要当成成功"
        raise RuntimeError(msg)
    return output_path


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
