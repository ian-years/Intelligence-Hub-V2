"""`/api/videos/{id}/shots`：工坊页「截图包」的数据源。

**为什么是 POST 而不是 GET**：这个动作有副作用（往磁盘写帧），而且副作用是用户点出来的
（工坊页那个按钮）。GET 在 HTTP 语义里必须安全，一个会被爬虫/预取打中的写盘端点
等于"没人点也在那儿截帧"。

**为什么同步做完而不是起任务**：ffmpeg 截 12 帧是秒级的事，而任务化的代价是界面变成
"点了之后不知道在等什么"（任务队列的语义是"稍后会有人来收拾"，见 `docs/specs/task-runner.md`）。
这条判据是这一整个端点存在的理由，改它要先想清楚前端怎么表达"进行中"。

产物**不进库**（不加列、不加表、不做迁移）：截图是从成片**可再生**的派生物，
不是真相。库里那一列 `videos.media_path` 指的成片才是真相 —— 记一份"上次截了哪些帧"
等于造第二个真源，V1 §7.7 那一族（双源）就是这么来的。"有没有截过"由磁盘本身回答
（文件名对时间点单射，见 `FileStorage.shot_file`），所以 `cached` 不需要记账也能算准。

三条边界，都是"读磁盘"这件事在这个仓库里必须带的（同 `media.py` 那两条判据）：

1. **只认该作品自己 `shots/` 目录底下的文件**。出图端点收的是文件名，而同一个 `data/` 根下
   躺着 `cookies/`（有效会话凭证）与 CDP profile（登录态）：一次 `%2e%2e%2f` 就能让一个
   无鉴权的回环端点变成凭证读取器。这里比 `media.py` 更紧一层 —— 连"目录"都不是参数，
   而是从库里的 `media_path` 反推出来的，换不到别人的作品上去。
2. **只出图片扩展名**。"在 shots 目录下"不足以说明"这是能给 `<img>` 的字节"。
3. **每种失败各有原文**。作品不存在 / 没有媒体 / 文件是 0 字节 / ffmpeg 不在 / 某几秒截不出，
   这五种在界面上长得一模一样（网格空白），后端必须给得出区别 —— 尤其不许把
   "这台机器没装 ffmpeg" 报成 200 + 空列表（AGENTS.md §1.3）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.api.v1.media import resolve_media_file
from intelligence_hub_v2.infra.ffmpeg import FrameTarget, extract_frames, ffmpeg_version
from intelligence_hub_v2.storage.files import shot_stem

if TYPE_CHECKING:
    from pathlib import Path

    from intelligence_hub_v2.api.deps import AppState
    from intelligence_hub_v2.infra.ffmpeg import ExtractedFrame, FramesResult

router = APIRouter(tags=["shots"])

MAX_SHOTS_PER_REQUEST: Final = 12
"""一次最多截几帧。上限存在的理由不是"磁盘怕多"，是**这个端点必须同步返回**：
一帧几十到几百毫秒，12 帧还在"点一下等一会儿"的范围内，200 帧就不是了。
超了不截一半 —— 那会让"分镜 20 格"与"截图包 12 张"两个数字同时出现在界面上。
"""

MIN_GAP_SECONDS: Final = 2.0
"""省略 `at_seconds` 时两个时间点之间的最小间隔：再密下去，缩略图上看不出镜头换了什么。"""

#: 出图白名单。**只有 `.png/.jpg/.jpeg`**：这一层产出的就是 jpg（`FileStorage.shot_file`），
#: 放行 `.mp4` 会让 `shots/` 变成第二条视频出口，而 `media.py` 那条"容器/图片分开"的判据
#: 就是靠两边各自收窄才成立的。
IMAGE_CONTENT_TYPES: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

#: 分隔符 / 空字节 / 结尾点号（Windows 会静默剥掉，`shot-1.jpg.` 与 `shot-1.jpg` 变同一个）。
_PATH_UNSAFE: Final = re.compile(r"[/\\\x00]|\.$")


class ShotRequest(BaseModel):
    """请求体。`at_seconds` 省略 = 按 `videos.duration_seconds` 均分。"""

    model_config = ConfigDict(extra="forbid")

    at_seconds: list[float] | None = Field(
        default=None,
        description=(
            "要截的时间点（秒），来自分镜行；"
            f"省略则按作品时长均分（最多 {MAX_SHOTS_PER_REQUEST} 帧）"
        ),
    )


class ShotItem(BaseModel):
    at_seconds: float = Field(description="这一帧对应的时间点（已按文件名的量化精度对齐）")
    path: str = Field(description="相对 `data/` 的路径（与 `media_path` 同一套约定）")
    url: str = Field(description="出图地址，前端直接用；**不拼磁盘路径**")
    size_bytes: int
    produced: bool = Field(description="True = 这次真的截了；False = 复用上一次的同一帧")


class ShotFailureItem(BaseModel):
    at_seconds: float
    reason: str = Field(description="ffmpeg 的失败原文（截不出这一帧的真实原因，不并成一句'失败'）")


class ShotsResponse(BaseModel):
    video_id: int
    requested_at: list[float] = Field(description="实际去截的时间点（已去重、已限量）")
    shots: list[ShotItem]
    cached: bool = Field(
        description="True = 这一帧都没有新截，全是复用上次的；界面上必须与'刚截好的'区分开"
    )
    #: 这两个字段**故意不给默认值**：给了就会从 OpenAPI 的 `required` 里掉出去，
    #: 前端拿到的是 `failures?: ...`，于是每处都要 `?? []` —— 而"这次有没有失败的帧"
    #: 正是那一栏存在的理由，不许变成"后端没说的话就当没有"。
    failures: list[ShotFailureItem] = Field(
        description="某几秒截不出来时，原因在这里（不伪装成没发生）"
    )
    ffmpeg_version_or_error: str | None = Field(
        description="产出这些帧的 ffmpeg 是哪一支；纯复用时为 null"
    )


# ---------------------------------------------------------------------------
# 时间点：解析请求 / 均分
# ---------------------------------------------------------------------------


def even_split_times(duration_seconds: float, count: int) -> list[float]:
    """时长 `duration` 上均分 `count` 个点，**从 0 开始、不取最后那一秒**。

    取 `duration * i / count`（i 从 0 起）而不是 `duration * (i+1) / count`：
    后者会把最后一帧压在片尾那一瞬，而 ffmpeg 对"落在片尾/越界的 `-ss`"的回应是
    **退出码 0 且不产出文件** —— 那不是失败，是空手而归，会伪装成"截图包坏了一半"。
    """
    if count <= 0:
        return []
    return [round(duration_seconds * index / count, 3) for index in range(count)]


def plan_shot_times(payload: ShotRequest, duration_seconds: float | None) -> list[float]:
    """请求里的时间点 → **真要去截**的那几个（去重、限量、非负）。

    三种拒绝各有原文与各自的修法：超上限（删几格或省略参数）、空数组（至少一个）、
    没有时长又没给时间点（这条作品没时长元数据，只能显式给）。
    全部 422：这三条都是"请求本身不成立"，不是服务端状态问题。

    状态码写字面量 `422`：`status.HTTP_422_UNPROCESSABLE_ENTITY` 在这套依赖里已经
    deprecated，而 pytest 的 `filterwarnings = error` 会把那句警告变成一条红
    （`drafts.py` 同一处同一个理由）。
    """
    raw = payload.at_seconds
    if raw is None:
        if duration_seconds is None or duration_seconds <= 0:
            msg = (
                f"这条作品没有可用的 duration_seconds（{duration_seconds!r}），算不出均分点："
                "请在请求体里显式给 at_seconds"
            )
            raise HTTPException(422, detail=msg)
        count = max(1, min(MAX_SHOTS_PER_REQUEST, int(duration_seconds // MIN_GAP_SECONDS)))
        return even_split_times(duration_seconds, count)
    if not raw:
        msg = "at_seconds 是空数组：没有任何要截的时间点（省略这个字段才会走均分）"
        raise HTTPException(422, detail=msg)
    if len(raw) > MAX_SHOTS_PER_REQUEST:
        msg = (
            f"一次最多截 {MAX_SHOTS_PER_REQUEST} 帧（收到 {len(raw)} 个时间点）。"
            f"这一步是同步返回的，帧数多了就不是'点一下等一会儿'。"
            "修法：删掉几格分镜，或者省略 at_seconds 让后端按 "
            f"{MIN_GAP_SECONDS:g} 秒的间隔均分。"
        )
        raise HTTPException(422, detail=msg)

    planned: list[float] = []
    seen: set[str] = set()
    for value in raw:
        # `shot_stem()` 是命名规则的唯一权威，负数/NaN/inf 在它那里抛 ——
        # 先过它再入队，等于"能命名才可能落盘"，不会出现写了一半才发现的错。
        try:
            stem = shot_stem(value)
        except ValueError as exc:
            raise HTTPException(
                422,
                detail=f"at_seconds 里有一个时间点用不了：{value!r}（{exc}）",
            ) from exc
        if stem in seen:
            continue
        seen.add(stem)
        planned.append(float(value))
    return planned


def _quantized(at_seconds: float) -> float:
    """响应里那一帧的时间点：与**文件名**同一个量化精度。

    不做这件事的话，响应里是 `8.571428571428571`，文件名是 `shot-8.571.jpg`，
    前端拿响应去对文件名就对不上 —— 而"点哪一格看哪张图"要的正是这个对应。
    """
    return float(shot_stem(at_seconds))


# ---------------------------------------------------------------------------
# POST：截
# ---------------------------------------------------------------------------


@router.post("/videos/{video_id}/shots", response_model=ShotsResponse)
async def create_shots(
    video_id: int,
    payload: ShotRequest,
    state: AppState = Depends(get_state),
) -> ShotsResponse:
    """从这条作品已落地的成片里按时间点截一批帧，返回缩略图清单。"""
    record = await state.storage.videos.get(video_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"video {video_id} 不存在")

    # 复用 `media.py` 那一条链：没有媒体 / 越界 / 扩展名不认 / 文件不在，四句原文都写好在那边。
    source = resolve_media_file(state, record)
    if source.stat().st_size == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"{source.name} 是 0 字节，这条媒体没有真内容"
        )

    times = plan_shot_times(payload, record.duration_seconds)
    media_dir = source.parent
    targets = [
        FrameTarget(at_seconds=value, output_path=state.files.shot_file(media_dir, value))
        for value in times
    ]

    try:
        result: FramesResult = await extract_frames(
            source, targets, ffmpeg_path=state.config.paths.ffmpeg
        )
    except LookupError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=_ffmpeg_missing_text(exc)
        ) from exc

    if not result.frames:
        # 一帧都没有：这不是"截图包是空的"，是**这一步失败了**。回 200 + 空列表会把
        # "这台机器截不了图"渲染成"这条作品没有分镜"，两者在界面上完全同形。
        reasons = "；".join(item.reason for item in result.failures) or "ffmpeg 没有产出任何帧"
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"截帧没成功（{len(result.failures)} 个时间点全失败）：{reasons}",
        )

    shots = [_to_shot(item, state, video_id) for item in result.frames]
    version = (
        await ffmpeg_version(ffmpeg_path=state.config.paths.ffmpeg)
        if any(frame.produced for frame in result.frames)
        else None
    )
    return ShotsResponse(
        video_id=video_id,
        requested_at=[_quantized(frame.at_seconds) for frame in result.frames],
        shots=shots,
        cached=result.all_reused,
        failures=[
            ShotFailureItem(at_seconds=_quantized(item.at_seconds), reason=item.reason)
            for item in result.failures
        ],
        ffmpeg_version_or_error=version,
    )


def _to_shot(frame: ExtractedFrame, state: AppState, video_id: int) -> ShotItem:
    """一帧 → 响应项。`url` 在这里算，前端不许自己拼磁盘路径或目录结构。"""
    exists = frame.path.is_file()
    return ShotItem(
        at_seconds=_quantized(frame.at_seconds),
        path=state.files.rel(frame.path),
        url=f"/api/videos/{video_id}/shots/{frame.path.name}",
        size_bytes=frame.path.stat().st_size if exists else 0,
        produced=frame.produced,
    )


def _ffmpeg_missing_text(exc: LookupError) -> str:
    """ffmpeg 不在场时那句**能照着做**的话。

    写这么长是因为这一条的错误最容易被误读成"这个功能坏了"：`run_subprocess` 的原文
    只说"找不到可执行文件"，而 AGENTS.md §7.19 的实测是"注册表里有、进程 PATH 没有"
    也算找不到 —— 所以两个修法都要给出来，并且点明"改了要重启服务"。
    """
    return (
        "截不了帧：这台机器上没有可用的 ffmpeg。"
        f"（{exc}）下一步二选一："
        "① 装 ffmpeg（Windows 上 `winget install Gyan.FFmpeg`）后重启服务；"
        "② 已经有 ffmpeg 的话，把 config/app.yaml 的 paths.ffmpeg 指到 ffmpeg.exe 的绝对路径"
        "（改完同样要重启：进程 PATH 是启动方那份快照，V1 §7.19）。"
    )


# ---------------------------------------------------------------------------
# GET：出图
# ---------------------------------------------------------------------------


@router.get("/videos/{video_id}/shots/{name:path}")
async def get_shot_image(
    video_id: int,
    name: str,
    state: AppState = Depends(get_state),
) -> FileResponse:
    """把 `shots/` 里那张图交出去。三道判据见模块 docstring，越界一律 403。"""
    record = await state.storage.videos.get(video_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"video {video_id} 不存在")
    source = resolve_media_file(state, record)
    shots_dir = state.files.shots_dir(source.parent)

    if _PATH_UNSAFE.search(name) or name in {"", ".", ".."}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                f"截图文件名 {name!r} 不是一个单纯的文件名："
                "目录由这条作品的 media_path 决定，不接受调用方指定"
            ),
        )
    media_type = IMAGE_CONTENT_TYPES.get(_suffix_of(name))
    if media_type is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                f"{name} 的扩展名不在图片白名单里（{', '.join(IMAGE_CONTENT_TYPES)}）："
                "shots 目录下只有帧，别的字节不该由这个端点出"
            ),
        )
    candidate = (shots_dir / name).resolve()
    if not _is_under(candidate, shots_dir.resolve()):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"{name} 解析后落到了截图目录 {shots_dir} 外面，不出图",
        )
    if not candidate.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"没有这一帧：{candidate.name}（先 POST 一次，或它那次没截出来）",
        )
    if candidate.stat().st_size == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"{candidate.name} 是 0 字节：那次截帧没产出内容，删掉重截即可",
        )
    return FileResponse(candidate, media_type=media_type, filename=candidate.name)


def _suffix_of(name: str) -> str:
    """扩展名（小写、带点）。不用 `Path(name).suffix`：`name` 是外部输入，
    `Path("..\\")` 那一类在这个函数里会抛出让人摸不着的东西。"""
    _, _, tail = name.rpartition(".")
    return f".{tail.lower()}" if tail and tail != name else ""


def _is_under(path: Path, root: Path) -> bool:
    """`path` 是否在 `root` 底下（含等于）。与 `media.py` 的同名判据同一条规矩。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
