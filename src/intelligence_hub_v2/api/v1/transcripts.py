"""`/api/videos/{id}/transcript`：读一条作品的口播稿。

正文在磁盘上（`transcripts.text_path` 相对 `data/`），库里只存元数据。这个端点把两者拼起来
一次交出去 —— 前端详情/逐句高亮要的是"正文 + segments"，不该自己再去猜文件在哪
（V1 §7.5：口播稿目录按平台不对称，改错一处前端就读不到）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, status

from intelligence_hub_v2.api.deps import get_state

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["transcripts"])


@router.get("/videos/{video_id}/transcript")
async def get_transcript(video_id: int, state: AppState = Depends(get_state)) -> dict[str, Any]:
    record = await state.storage.transcripts.get_for_video(video_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"video {video_id} 还没有口播稿")

    path = state.files.abs(record.text_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # 库里有行、文件读不出来 = 数据损坏/磁盘问题，如实 500，不能空串糊过去（V1 §1.3）
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"口播稿文件读不出来：{record.text_path}（{exc}）",
        ) from exc

    return {
        "video_id": record.video_id,
        "engine": record.engine,
        "language": record.language,
        "char_count": record.char_count,
        "sentence_count": record.sentence_count,
        "text": text,
        "segments_json": record.segments_json,
        # ADR-0015：摘要与要点跟着稿子走，所以也跟着这一次请求一起交出。
        # `summary_method` 不是装饰 —— 本地抽取式（≤600 字片段）与 V1 搬来的整篇改写
        # 长得一样，不标来源前端就分不出该把哪一条当"参考"、哪一条当"结论"。
        "content_summary": record.content_summary,
        "key_points": record.key_points,
        "summary_method": record.summary_method,
    }
