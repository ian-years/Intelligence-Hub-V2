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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intelligence_hub_v2.infra.subprocess import run_subprocess
from intelligence_hub_v2.logging import get_logger

logger = get_logger(__name__)
"""缺二进制的 probe 会往这里记一条 debug。没有这一句，"这台机器没装 ffprobe"
在日志里就是个空白 —— 而它正是"为什么这批稿件的时长全是空的"那条线索的起点。"""

__all__ = [
    "ExtractedFrame",
    "FrameFailure",
    "FrameTarget",
    "FramesResult",
    "StreamInfo",
    "extract_audio",
    "extract_frame",
    "extract_frames",
    "ffmpeg_binary",
    "ffmpeg_version",
    "frame_argv",
    "has_audio_stream",
    "probe_streams",
]

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

WAV_HEADER_BYTES = 44
"""一个 WAV 文件的头。产出的音频**只比这长一点**就等于没解出任何采样。"""

FRAME_TIMEOUT_SECONDS = 60.0
"""单帧的预算。截一帧是秒级（快进 + 解一帧），给到 60 秒已经是"磁盘或解码器不对劲"。

为什么按帧算而不是按整批算：一批 12 帧共用一个预算时，第一帧卡住就把后面 11 帧的
配额一起吃光，报出来的原因还是"超时" —— 而真相是"第 3 秒那个位置损坏"。
"""


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
    except (TimeoutError, OSError) as exc:
        # 超时 / 启动失败与"没装"是同一类"问不出来"（review P1-9）：让它们冒出去的话，
        # ffprobe 卡 30 秒就把一条**已经下好的媒体**判成采集失败 —— 与下面那条
        # LookupError 注释是同一个理由、同一个兜底答案。
        logger.debug(
            "ffprobe.unusable", reason=f"{type(exc).__name__}: {exc}", media=str(media_path)
        )
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


# ---------------------------------------------------------------------------
# 分镜截图（工坊页「截图包」的数据源）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameTarget:
    """要截的一帧：时间点 + **由 `FileStorage.shot_file()` 算出来的**落点。

    落点不在这里算：命名规则（`shot-<秒>.jpg`）的权威是 `storage/files.py`
    （"产物在哪"只有一处答案这条纪律）。这一层只负责把它截出来。
    """

    at_seconds: float
    output_path: Path


@dataclass(frozen=True)
class ExtractedFrame:
    """一帧的产出。`produced` 是 `cached` 那个标志的来源。"""

    at_seconds: float
    path: Path
    produced: bool
    """True = 这一次真的调 ffmpeg 截出来了；False = 复用上一次的同一帧。"""


@dataclass(frozen=True)
class FrameFailure:
    """某一帧没截出来。**逐帧**记，不要把整批并成一句"失败"。"""

    at_seconds: float
    path: Path
    reason: str


@dataclass(frozen=True)
class FramesResult:
    """一批帧的结局。空 `frames` 不等于"什么都没发生" —— 看 `failures`。"""

    frames: list[ExtractedFrame]
    failures: list[FrameFailure]

    @property
    def all_reused(self) -> bool:
        """一帧都没新截，但**全都拿到手了**（= 界面上该写"这是上次截好的"）。"""
        return bool(self.frames) and not any(frame.produced for frame in self.frames)


def ffmpeg_binary(configured: Path | str | None = None) -> str:
    """这一句要用的 ffmpeg：`config.paths.ffmpeg` 指的文件赢，否则回落 `FFMPEG` 走 PATH。

    回落是**有意的**，不是省事：这台机器上 ffmpeg 装在 WinGet 的目录里，
    而 `where ffmpeg` 打不出来 —— 正是 V1 §7.19 那个形状（注册表 PATH 里有、
    进程拿到的那份快照里没有）。`core/runtime_env.py` 只在常驻服务启动与每轮预检时
    把那一段补进 `os.environ`，测试与任何直接构造 `AppState` 的路径都不跑那一步。
    所以两边各吃一次同一个配置项：启动期补 PATH（管所有子进程），
    这里再显式认一次（管这一条命令）。
    配了却指着一个不存在的文件时**不报错也不静默换人**：那条已经在启动期的
    `explicit_path_dirs()` 里被记成 problem 了（V1 §7.19），这里再判一次只会让
    "配错路径"同时在两处红、而两处的原文还不一样。
    """
    if configured is None or not str(configured).strip():
        return FFMPEG
    candidate = Path(str(configured)).expanduser()
    if not candidate.is_file():
        return FFMPEG
    return str(candidate)


def frame_argv(
    media_path: Path,
    output_path: Path,
    at_seconds: float,
    *,
    binary: str = FFMPEG,
) -> list[str]:
    """截一帧的命令行。**纯函数**，为的是 argv 能被逐字钉住（不依赖机器上有 ffmpeg）。

    - `-ss` 放在 `-i` **前面**：那是"先定位再解到准确时间点"（ffmpeg ≥ 2.1 会解到
      指定的那一帧，不是停在最近的关键帧），而放后面是**从 0 开始解过去**——
      截第 600 秒要先把 600 秒解完。工坊那个按钮点的是"秒级返回"。
    - `-frames:v 1`：只要一帧。没有它就是"从这一秒开始把剩下的全编出来"。
    - `-an`：帧里没有音频，留着只会让 ffmpeg 去找音频编码器。
    - `-y`：同一秒重截是**覆盖**（`shot_file()` 的命名规则就是为了这件事成立）。
    - 全程 `shell=False`（`run_subprocess` 那条纪律）：时间点与路径都当独立 argv 传。
    """
    return [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{float(at_seconds):.3f}",
        "-i",
        str(media_path),
        "-frames:v",
        "1",
        "-an",
        "-q:v",
        "2",
        str(output_path),
    ]


async def extract_frame(
    media_path: Path,
    output_path: Path,
    *,
    at_seconds: float,
    overwrite: bool = False,
    timeout: float = FRAME_TIMEOUT_SECONDS,
    ffmpeg_path: Path | str | None = None,
) -> bool:
    """截一帧。返回 `True` = 真的截了，`False` = 复用了已有的同一帧。

    与 `extract_audio()` 同一个形状（`run_subprocess` + timeout + 结构化失败），
    三处差别都是截图特有的：

    - **退出码 0 但文件不在/是 0 字节**同样算失败。ffmpeg 对"时间点超出片长"
      这件事就是回 0 且不产出（不是报错），把它当成功会得到一套空的缩略图网格。
    - **缺二进制不消化**：`run_subprocess` 抛的 `LookupError` 原样往上走。
      这里是在产出用户点着要的东西，不是问一个问题（对比 `probe_streams()`
      那条就地消化的理由），所以"这台机器没装 ffmpeg"必须红。
    """
    if not overwrite and output_path.is_file() and output_path.stat().st_size > 0:
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    argv = frame_argv(media_path, output_path, at_seconds, binary=ffmpeg_binary(ffmpeg_path))
    result = await run_subprocess(argv, timeout=timeout)
    if not result.ok:
        msg = (
            f"ffmpeg 截帧失败（第 {at_seconds:g} 秒，exit {result.returncode}）："
            f"{result.tail(lines=8) or '（无 stderr）'}"
        )
        raise RuntimeError(msg)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        msg = f"ffmpeg 退出码 0 却没产出第 {at_seconds:g} 秒的帧（{output_path}）—— 不要当成成功"
        raise RuntimeError(msg)
    return True


async def extract_frames(
    media_path: Path,
    targets: Sequence[FrameTarget],
    *,
    overwrite: bool = False,
    timeout: float = FRAME_TIMEOUT_SECONDS,
    ffmpeg_path: Path | str | None = None,
) -> FramesResult:
    """按时间点依次截帧。**串行**：并开 12 个 ffmpeg 只会让每一路都变慢，
    而这一条链路的要求是"点完按钮秒级看到缩略图"，不是吞吐。

    失败分两类处理，界线是"这条命令还能不能往下走"：

    - `LookupError`（ffmpeg 不在）→ **就地往上抛，一帧都不再试**。
      缺二进制是全局状态，重试 11 次只会产出 11 条同样的错，还会把 HTTP 响应拖成
      十几次启动失败。调用方（`api/v1/shots.py`）把它翻成 503 + 一句可执行的下一步。
    - 其余（这一秒坏了 / 解不出来 / 超时）→ 记进 `failures`，继续截剩下的。
      一帧截不出与整条链路不可用是两件事，界面上要能分清。
    """
    frames: list[ExtractedFrame] = []
    failures: list[FrameFailure] = []
    for target in targets:
        try:
            produced = await extract_frame(
                media_path,
                target.output_path,
                at_seconds=target.at_seconds,
                overwrite=overwrite,
                timeout=timeout,
                ffmpeg_path=ffmpeg_path,
            )
        except LookupError:
            raise
        except (RuntimeError, TimeoutError, OSError) as exc:
            failures.append(
                FrameFailure(
                    at_seconds=target.at_seconds,
                    path=target.output_path,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        frames.append(
            ExtractedFrame(
                at_seconds=target.at_seconds,
                path=target.output_path,
                produced=produced,
            )
        )
    return FramesResult(frames=frames, failures=failures)


async def ffmpeg_version(
    *, ffmpeg_path: Path | str | None = None, timeout: float = 10.0
) -> str | None:
    """`ffmpeg -version` 的第一行，**问不出来就返回 None**（不抛）。

    为什么值得再花一次子进程：清单与界面上要能回答"这 12 帧是哪个 ffmpeg 截的"。
    V1 §7.19 那类事故（注册表里有、进程 PATH 没有）症状正是"换了个 ffmpeg 而没人知道"。
    它只是**附注**，所以缺二进制 / 超时 / 启动失败全部走 None ——
    不能因为拿不到版本就把已经截好的帧报成失败。
    """
    argv = [ffmpeg_binary(ffmpeg_path), "-hide_banner", "-version"]
    try:
        result = await run_subprocess(argv, timeout=timeout)
    except (LookupError, TimeoutError, OSError):
        return None
    if not result.ok:
        return None
    first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    return first_line or None


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
