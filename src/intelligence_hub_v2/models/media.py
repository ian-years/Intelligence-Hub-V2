"""媒体下载产物的数据模型。

契约来源：docs/specs/platform-adapter.md §2.4（`SingleFileArtifact` / `VideoAudioPairArtifact`）

**为什么单独一个模块而不是塞进 `models/video.py`**：`Video` 是**存储层的行**
（`videos` 表的映射，字段名与 DB 列一一对应），而 `MediaArtifact` 是**采集层一次下载的产出**
（还没入库，路径也还没归一化成相对 `data/`）。放一起会让人以为
"`video.media_path` 就是从 artifact 拷过来的"，而实际中间隔着
"谁负责算路径"这件事（`FileStorage`，见 V1 §7.5 的教训）。

**判别联合按 `kind` 区分**，转写层因此只需要一个函数就能拿到音频轨：
`audio_path_of()`。V1 §7.21 那次事故就是转写只认 `*.mp4`，
把 B站未合并分片里的**纯视频轨**喂给 `ffmpeg -vn`，
报 `Output file does not contain any stream` —— 那句长得和"ffmpeg 没装"一模一样，
方向却完全不同。把"音频在哪"收进类型，就不该再有第二处猜的地方。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

MediaSource = Literal["yt_dlp", "page_play_url", "dash_merged", "dash_split"]
"""实际走了哪条路拿到的媒体。

**与 `storage.schema.MEDIA_SOURCES` 是同一份清单的两处定义**（Python 层与 DB 层 CHECK），
和 `models/platform.py` 那对 `HEALTH_STATUSES` 同一个形状 —— 改一处必须改两处。
`tests/unit/storage/test_schema_types.py` 会把 DB 侧的字面量与 schema 常量对回去。
"""


class SingleFileArtifact(BaseModel):
    """单文件媒体（合并后的 mp4，或抖音"页面播放直链"那条兜底片）。"""

    kind: Literal["single_file"] = "single_file"
    path: Path
    """**相对 `data/`** 的路径。绝对路径只在下载过程中存在，出库前必须过
    `FileStorage.rel()` —— 否则数据目录一搬走，库里所有媒体就都"找不到文件"。
    """

    size_bytes: int = Field(ge=0)
    media_source: MediaSource
    yt_dlp_error: str | None = None
    """兜底成功时 yt-dlp 的**失败原文**。

    V1 §7.2：抖音这条路上 yt-dlp 从来没产出一片媒体（缺页面里那层 `a_bogus` 签名），
    所以"yt-dlp 未拿到媒体，已改用页面播放直链"是**常态而不是故障**。
    早期版本一兜底成功就把 yt-dlp 的原文丢掉，日志里只剩"未拿到媒体"，
    等于永远判断不出该修什么。这个字段就是为了让那句原文有个家。
    """

    duration_seconds: float | None = None
    has_audio: bool = True
    has_video: bool = True


class VideoAudioPairArtifact(BaseModel):
    """DASH 未合并分片（yt-dlp 合并失败或被拆开，V1 §7.21 的 B站陷阱）。"""

    kind: Literal["video_audio_pair"] = "video_audio_pair"
    video_path: Path
    """`...media.f137.mp4` —— 纯视频轨，**没有音频**。"""

    audio_path: Path
    """`...media.f140.m4a` —— 纯音频轨。转写要吃的是这条。"""

    video_size_bytes: int = Field(ge=0)
    audio_size_bytes: int = Field(ge=0)
    media_source: Literal["dash_split"] = "dash_split"
    yt_dlp_error: str | None = None
    duration_seconds: float | None = None


MediaArtifact = SingleFileArtifact | VideoAudioPairArtifact
"""一次媒体下载的产物。`download_media()` 的返回类型。"""


def audio_path_of(artifact: MediaArtifact) -> Path:
    """该喂给转写链的那个文件。

    只有这一个答案是可信的：`SingleFileArtifact.has_audio=False` 时**没有**音频可转，
    这里抛而不是返回视频路径 —— 返回了就会得到 ffmpeg 那句
    `Output file does not contain any stream`，而它的字面意思会把人引向"ffmpeg 没装"。
    """
    if isinstance(artifact, VideoAudioPairArtifact):
        return artifact.audio_path
    if not artifact.has_audio:
        msg = (
            f"这条媒体没有音频轨（{artifact.path}，来源 {artifact.media_source}），"
            f"不能转写。跳过它并记下原因，不要把视频轨喂给 ffmpeg。"
        )
        raise ValueError(msg)
    return artifact.path


def total_size_bytes(artifact: MediaArtifact) -> int:
    """产物占的磁盘字节数（分片是两条轨之和）。"""
    if isinstance(artifact, VideoAudioPairArtifact):
        return artifact.video_size_bytes + artifact.audio_size_bytes
    return artifact.size_bytes
