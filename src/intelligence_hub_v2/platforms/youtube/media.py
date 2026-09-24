"""YouTube 的媒体与字幕：全部经 yt-dlp，**没有第二条路**。

三件事住在这个文件里，各自的"为什么"不一样：

1. **argv**（`ytdlp_extra_args` / `subtitle_extra_args`）：V1 那串
   `-f bv*[height<=1080]+ba/b[height<=1080] --merge-output-format mp4` 原样搬，
   加上 `--js-runtimes node`（只在探到 node 时给，V1 同）与 `--proxy`。
   **cookie 一个都不带** —— `capabilities.cookie_variants=("none",)` 是真的：
   阶梯只有一档、argv 里没有 `--cookies`，所以 V1 §7.15 那条"枚举与下载都要带
   导出 cookie"的坑在 YouTube 这一族不存在（也因此别照着 B站 抄一段阶梯进来）。
2. **未合并的 DASH 分片在这里是失败**（V1 §7.21 的反面用法）。
   `supports_dash_split=False` 的语义是"本适配器绝不交出分片对"，所以拿到
   `media.f137.mp4` + `media.f140.m4a` 时**判失败并点名 ffmpeg**。
   为什么不能像 B站 那样交出去：那要先把 `supports_dash_split` 改成 True，
   而 `Capabilities` 的声明是调度器的前置条件，改它要走 ADR。
   顺带记一笔 V1 的真实缺陷：它 `glob("*.mp4")` 取第一个，
   分片情形下会把**纯视频轨**当成品交出去 —— 症状正是 §7.21 那句
   `Output file does not contain any stream` 长得像"ffmpeg 没装"。
   看护在 `test_a_dash_pair_is_a_failure_not_a_silent_single_file`。
3. **字幕**（`supports_subtitles=True` 的兑现）：让 yt-dlp 把 `.vtt` 落到一个
   **专用**的临时目录，再解析成 `Transcript`。判"有轨/没轨"看目录里有没有文件，
   不看 yt-dlp 打了哪句话 —— 理由写在 `find_subtitle_files` 的 docstring 里
   （那与 §7.21 禁的"扫目录认媒体"不是同一件事，也写在里面）。
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from intelligence_hub_v2.infra.ytdlp import YtDlpResult
from intelligence_hub_v2.models.transcript import Transcript, TranscriptSegment
from intelligence_hub_v2.platforms.youtube.config import YouTubeConfig

__all__ = [
    "MEDIA_FILE_TEMPLATE",
    "SUBTITLE_FILE_TEMPLATE",
    "build_transcript",
    "choose_subtitle_file",
    "find_subtitle_files",
    "node_available",
    "parse_vtt",
    "subtitle_extra_args",
    "ytdlp_extra_args",
    "ytdlp_failure_reason",
]

MEDIA_FILE_TEMPLATE = "media.%(ext)s"
"""与 `data-model.md §1` 的 Locked 布局一致（B站 同名同值）：
合并成功 → `media.mp4`；没合并 → `media.f137.mp4` + `media.f140.m4a`。"""

SUBTITLE_FILE_TEMPLATE = "subtitles.%(ext)s"
"""字幕落在**另一个目录**（专用临时目录），不进作品目录：
`data-model.md §1` 的产物清单里没有 `subtitles/`，多一个约定就多一处要维护的东西。
真正的口播稿由 `tasks/postprocess.py` 写进 `transcript/`。"""

_MERGE_OUTPUT_FORMAT = "mp4"

_MIN_SUBTITLE_NAME_PARTS = 3
"""`subtitles.<语言>.<ext>` 至少三段才谈得上"这是哪条语言的轨"。"""

_MAX_CLOCK_SEGMENT = 59
"""vtt 时钟里"分"与"秒"的上界。越界的时间轴整条丢，见 `_clock_to_seconds`。"""


def node_available() -> bool:
    """PATH 上有没有 node。V1 的判据是同一条（`shutil.which("node")`）。"""
    return shutil.which("node") is not None


def _proxy_args(config: YouTubeConfig) -> tuple[str, ...]:
    return ("--proxy", config.proxy) if config.proxy else ()


def _js_runtime_args(config: YouTubeConfig, *, node: bool | None = None) -> tuple[str, ...]:
    """带不带 `--js-runtimes node`。

    探到才给（V1 同）：老版本 yt-dlp 不认这个 flag。V1 遇到不认时**去掉它重试一次**，
    V2 不做那次重试 —— `infra.ytdlp` 的退档只按 cookie 走，为一种参数变体再开一条
    阶梯不值当。代价写在 `YouTubeAdvanced.node_as_js_runtime` 的 description 里。
    """
    wanted = node_available() if node is None else node
    if wanted and config.advanced.node_as_js_runtime:
        return ("--js-runtimes", "node")
    return ()


def ytdlp_extra_args(
    config: YouTubeConfig, *, format_preference: str | None = None, node: bool | None = None
) -> tuple[str, ...]:
    """下载与枚举共用的那组 argv。顺序照 V1，值来自配置。"""
    fmt = format_preference or config.advanced.format_preference
    return (
        "--no-update",
        "--no-write-comments",
        *_js_runtime_args(config, node=node),
        *_proxy_args(config),
        "-f",
        fmt,
        "--merge-output-format",
        _MERGE_OUTPUT_FORMAT,
    )


def subtitle_extra_args(config: YouTubeConfig, *, node: bool | None = None) -> tuple[str, ...]:
    """只要字幕、不下媒体的一趟。

    `--sub-langs` 用逗号串（yt-dlp 的形状），语言顺序 = 配置里的顺序，
    所以"要哪些语言"与"多轨时挑哪一条"是同一份真相（见 `YouTubeAdvanced.subtitle_languages`）。
    `--sub-format vtt` 而不是 `srt`：vtt 是 YouTube 原生格式，转 srt 要过一次
    ffmpeg 的 `SubtitlesConverterPP`，而这个平台可能压根没装 ffmpeg。
    """
    langs = ",".join(config.advanced.subtitle_languages)
    return (
        "--no-update",
        *_js_runtime_args(config, node=node),
        *_proxy_args(config),
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        langs,
        "--sub-format",
        "vtt",
    )


def ytdlp_failure_reason(result: YtDlpResult) -> str:
    """每一档的退出码 + stderr 尾行。

    与 `platforms/bilibili/media.py` 里那份**同名同判据**的重复是已知的
    （正解在 `infra/ytdlp.py`，那才是这份判据该住的地方）。
    用例 `test_the_two_failure_notes_still_agree` 把同一批结果喂两份并要求相等，
    所以"只改一份"会红。
    """
    tail = [line for line in result.stderr.strip().splitlines() if line.strip()]
    last = tail[-1] if tail else f"exit {result.returncode}"
    return f"{result.attempts_note()}｜{last}"


# --------------------------------------------------------------------------- #
# 字幕文件：找哪一条、怎么解析
# --------------------------------------------------------------------------- #

_VTT_CUE_RE = re.compile(r"^(?P<start>[^\s>]+)\s*-->\s*(?P<end>[^\s>]+)")
"""字幕轨的一条时间轴，例如：

    00:00:04.479 --> 00:00:07.439 align:start position:0%

只取 `-->` 两侧的第一个非空白片段，所以 `align:start position:0%` 那段
vtt **cue settings** 天然被丢掉 —— 它跟着进正文的话，口播稿里会混进一排
`align:start position:0%`，而抽取式摘要会把它们当句子选进候选。
两个时刻都要过 `_clock_to_seconds`，认不出就整条 cue 丢（见 `parse_vtt`）。
"""

_VTT_CLOCK_RE = re.compile(
    r"^(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2})(?:[.,](?P<ms>\d{1,3}))?$"
)
_VTT_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>|<\d{2}:\d{2}:\d{2}\.\d{3}>")
"""内联标签：`<c>`、`</c>`、`<b>`、`<00:00:12.500>`（逐字卡拉OK 时间戳）。

YouTube 的**自动**字幕几乎每行都带 `<00:00:xx.xxx>`，不剥掉的话
`text` 里全是时间戳，字数统计与摘要一起坏掉。
"""


def _clock_to_seconds(text: str) -> float | None:
    """`HH:MM:SS.mmm` / `MM:SS.mmm` → 秒；认不出或**分段越界**都返回 None。

    `00:99:99.000` 这种越界时刻在真 vtt 里不会出现，但"看起来是时间轴"的坏数据
    一旦按 `99*60+99` 折成秒，就会变成一条**位置合理但时间错了 100 倍**的片段 ——
    比整条丢掉难查得多（前端点它会跳到 1:40:39）。
    """
    match = _VTT_CLOCK_RE.match(str(text or "").strip())
    if match is None:
        return None
    groups = match.groupdict()
    minutes = int(groups["m"])
    seconds = int(groups["s"])
    if minutes > _MAX_CLOCK_SEGMENT or seconds > _MAX_CLOCK_SEGMENT:
        return None
    hours = int(groups["h"] or 0)
    millis = int((groups["ms"] or "").ljust(3, "0")[:3])
    total = (hours * 3600 + minutes * 60 + seconds) + millis / 1000.0
    return total if total >= 0 else None


def strip_vtt_tags(line: str) -> str:
    """去掉内联标签与 vtt 注释，留给人看的纯文本。"""
    cleaned = _VTT_TAG_RE.sub("", str(line or ""))
    return " ".join(cleaned.split())


def parse_vtt(text: str, *, collapse_repeats: bool = True) -> list[TranscriptSegment]:
    """`.vtt` 文本 → 带时间戳的片段。

    两处 YouTube 特有的坑：

    1. **自动字幕是"滚动"的**：一条 cue 常常是上一句 + 新半句，
       直接拼 `text` 会得出"同一句重复三遍"。`collapse_repeats` 丢掉与前一条
       **完全相同**的文本（这是最保守的一种去重：不同句永远都保留）。
    2. 一条 cue 的正文可能占多行（vtt 允许），所以要"读到空行为止"而不是"读一行"。

    时间轴读不出来的 cue **整条丢掉**，不把时间戳填 0：填 0 会让前端
    "点击句子跳到 0:00"，与"这句没有时间戳"在界面上同形（V1 §1.3）。
    """
    lines = str(text or "").splitlines()
    segments: list[TranscriptSegment] = []
    index = 0
    while index < len(lines):
        match = _VTT_CUE_RE.match(lines[index].strip())
        if match is None:
            index += 1
            continue
        start = _clock_to_seconds(match.group("start"))
        end = _clock_to_seconds(match.group("end"))
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(strip_vtt_tags(lines[index]))
            index += 1
        content = " ".join(part for part in body if part).strip()
        if not content or start is None or end is None or end < start:
            continue
        if collapse_repeats and segments and segments[-1].text == content:
            previous = segments[-1]
            segments[-1] = previous.model_copy(
                update={"end_seconds": max(end, previous.end_seconds)}
            )
            continue
        segments.append(TranscriptSegment(start_seconds=start, end_seconds=end, text=content))
    return segments


def subtitle_language(path: Path) -> str:
    """`subtitles.zh-Hans.vtt` → `zh-Hans`；认不出返回空串。

    靠**文件名**而不是文件头：yt-dlp 的产物名是 `<模板>.<语言>.<ext>`，
    而 vtt 头部的 `Language:` 行不是必须有（人工上传的字幕常常没有）。
    """
    parts = path.name.split(".")
    if len(parts) < _MIN_SUBTITLE_NAME_PARTS:
        return ""
    return parts[-2]


def choose_subtitle_file(paths: Sequence[Path], preferred: Sequence[str]) -> Path | None:
    """多轨时按 `preferred` 的顺序挑一条。

    匹配用 `startswith` 而不是相等：YouTube 给的语言码有 `zh-Hans`、`zh-CN`、
    `zh-Hant-TW` 好几种写法（同一个 `--sub-langs zh` 能带回一批变体），
    严格相等会因为一个后缀就把中文轨判成"没有"，然后白跑一轮 ASR。
    一条都没匹配上时退回**目录里最小的文件名**，而不是返回 None：
    用户要的是"这条作品的口播稿"，语言不是他要的那一种也比没有强 ——
    但挑中的语言会进 `Transcript.language`，看板上分得出来。
    """
    if not paths:
        return None
    ordered = [lang.strip().lower() for lang in preferred if str(lang).strip()]
    for want in ordered:
        for path in sorted(paths):
            code = subtitle_language(path).lower()
            if code == want or code.startswith(want) or want.startswith(code):
                return path
    return min(paths, key=lambda p: (len(p.name), str(p)))


def find_subtitle_files(directory: Path) -> list[Path]:
    """专用字幕目录里的 `.vtt` 文件。

    **为什么这里可以扫目录**（§7.21 禁的是另一件事）：那个目录是本适配器刚刚为
    这一条作品建的，只有字幕会落进去，建完就删；而 §7.21 的坑长在
    "作品目录里 `rglob('*.mp4')`" —— 那里既躺着源媒体又躺着我们自己产的
    `audio/part-001.m4a`，扫一次就把中间产物当源文件。
    反过来，**不**用 yt-dlp 报告的那句 `[info] Writing subtitles to …`：
    那句话的具体措辞随版本变（本会话没有真输出可核对），
    认错了的症状是"永远判定没有字幕"，而它一条都不会红。
    """
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".vtt")


def build_transcript(segments: Sequence[TranscriptSegment], *, language: str = "") -> Transcript:
    """片段 → `Transcript`（与 B站 那份同构，只有 `engine` 不同）。

    `sentence_count` = 片段数：字幕天然一句一条，与 ASR 那条路（SenseVoice 按静音
    切句补标点，V1 §7.9）的"句子"口径一致，前端可以同一套渲染。
    """
    text = "\n".join(segment.text for segment in segments)
    return Transcript(
        engine="youtube_subtitle",
        language=language or None,
        text=text,
        char_count=len(text),
        sentence_count=len(segments),
        segments=list(segments),
    )
