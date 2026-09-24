"""`/api/videos/{id}/media`：把这条作品落地的视频按 HTTP Range 交出去（T6.5，ADR-0023）。

为什么必须有 Range：`<video>` 拖进度条发的是 `bytes=N-`，不支持就得整段先下完才能动 ——
一个几百 MB 的本地文件被读两遍，症状还长得像"播放器卡住了"。

这是本仓库**第一个把磁盘字节交给 HTTP 的端点**，所以两条判据都写死在这里：

1. **只认 `data/media/` 底下的文件**。库里 `media_path` 那一列是我们自己写的，但同一个
   `data/` 根下就躺着 `cookies/`（有效会话凭证）和 CDP profile（登录态）—— 一个被手改过的行
   或者一句 `../cookies/x.txt`，就能让一个无鉴权的回环端点变成凭证读取器。越界一律 403，
   不做"顺手解析到 root 里面去"。
2. **只出视频容器扩展名**。"在媒体目录下"不足以说明"这是能播的字节"：同一条作品目录里
   还有 `transcript/`、`metadata.json`、`segments.json`、封面与 ASR 用的 wav
   （见 `storage/files.py` 那一路命名）。白名单挡的是"把稿子当媒体流出去"这一类。

应用绑回环（AGENTS.md 硬约束 2），所以这里**没有**鉴权：加一层没有真凭据的 token 检查
只会让人以为那一层存在。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.video import Video

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["media"])

#: 放行的容器 → Content-Type。B站 DASH 的分片（`.m4s`）不在这里：那不是能单独播的文件，
#: 混流后的成片才是（§7.21），而 `videos.media_path` 存的正是混流那一个。
MEDIA_CONTENT_TYPES: Final[dict[str, str]] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
}

_CHUNK_BYTES: Final = 1 << 20

#: 认 `bytes=a-b` / `bytes=a-` / `bytes=-b`。多区间不认 —— 浏览器与 `<video>` 都不发多区间，
#: 认了要多背一套语义；不认就按"没有 Range"整段给，那是合法的降级。
_RANGE_HEADER: Final = re.compile(r"^bytes=(?P<start>\d*)-(?P<end>\d*)$")


class _Slice:
    """一段字节区间：起点 + 长度，`total` 只为了写 `Content-Range`。"""

    def __init__(self, start: int, length: int, total: int) -> None:
        self.start = start
        self.length = length
        self.total = total

    @property
    def last(self) -> int:
        return self.start + self.length - 1


def resolve_media_file(state: AppState, record: Video) -> Path:
    """把库里那一列变成一个**确认可供出**的绝对路径。四种拒绝各有原文。"""
    stored = (record.media_path or "").strip()
    if not stored:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"video {record.id} 没有落地媒体（只采到元数据，或下载那一步失败了）",
        )
    media_root = state.files.media_root
    candidate = state.files.abs(stored)
    if not _is_under(candidate.resolve(), media_root.resolve()):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"媒体路径 {stored!r} 不在媒体目录 {media_root} 底下，不出图",
        )
    content_type = MEDIA_CONTENT_TYPES.get(candidate.suffix.lower())
    if content_type is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                f"{candidate.name} 的扩展名不在媒体白名单里（{', '.join(MEDIA_CONTENT_TYPES)}）："
                "同一个目录下还有稿子与元数据，不能都当媒体出"
            ),
        )
    if not candidate.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"库里有行但文件不在：{stored}（被移动过，或还没跑过磁盘重扫）",
        )
    return candidate


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def parse_range(raw: str | None, total: int) -> _Slice | None:
    """`Range` → 那一段。没有、看不懂、或者压根不是 `bytes=` 单位 → None（整段给）。"""
    if not raw:
        return None
    match = _RANGE_HEADER.match(raw.strip())
    if match is None:
        return None
    start_text, end_text = match.group("start"), match.group("end")
    if not start_text and not end_text:
        return None
    if not start_text:
        # `bytes=-N` = 最后 N 个字节。
        suffix = min(int(end_text), total)
        return _Slice(total - suffix, suffix, total) if suffix else None
    start = int(start_text)
    if start >= total:
        raise HTTPException(
            status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{total}"},
        )
    end = min(int(end_text) if end_text else total - 1, total - 1)
    return _Slice(start, end - start + 1, total)


async def iter_slice(path: Path, piece: _Slice) -> AsyncIterator[bytes]:
    """按块读那一段。**必须**是异步文件 IO：同步 read 会把事件循环钉在磁盘上，
    而这个端点正是"边看边拖进度条"时被反复打到的。"""
    async with await anyio.open_file(path, "rb") as handle:
        await handle.seek(piece.start)
        remaining = piece.length
        while remaining > 0:
            block = await handle.read(min(_CHUNK_BYTES, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


@router.get("/videos/{video_id}/media")
async def stream_video_media(
    video_id: int,
    state: AppState = Depends(get_state),
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    """播放这条作品的本地媒体（支持 Range）。"""
    record = await state.storage.videos.get(video_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"video {video_id} 不存在")
    path = resolve_media_file(state, record)
    total = path.stat().st_size
    if total == 0:
        # 0 字节的媒体是失败产物，不是"能播的空片"。如实报，别给一个 200 让播放器猜。
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"{path.name} 是 0 字节，这条媒体没有真内容"
        )

    partial = parse_range(range_header, total)
    piece = partial if partial is not None else _Slice(0, total, total)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(piece.length)}
    if partial is not None:
        headers["Content-Range"] = f"bytes {piece.start}-{piece.last}/{piece.total}"
    return StreamingResponse(
        iter_slice(path, piece),
        media_type=MEDIA_CONTENT_TYPES[path.suffix.lower()],
        status_code=status.HTTP_206_PARTIAL_CONTENT if partial is not None else status.HTTP_200_OK,
        headers=headers,
    )
