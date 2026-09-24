"""`/api/videos/{id}/transcript`：把库里的行 + 磁盘上的正文拼一次交出去。"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.models.transcript import TranscriptDraft
from intelligence_hub_v2.models.video import VideoDraft

pytestmark = pytest.mark.integration


async def test_get_transcript_reads_body_from_disk(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    files = app_state.files
    media_rel = "media/bilibili/某UP/BV1x/media.mp4"
    media_abs = files.abs(media_rel)
    media_abs.parent.mkdir(parents=True, exist_ok=True)
    media_abs.write_bytes(b"fake-media")

    row = await app_state.storage.videos.insert(
        VideoDraft(platform="bilibili", platform_video_id="BV1x", title="t", media_path=media_rel)
    )

    transcript_path = files.transcript_path(media_abs.parent)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("第一句。第二句。", encoding="utf-8")
    await app_state.storage.transcripts.attach(
        row.id,
        TranscriptDraft(
            engine="bilibili_subtitle",
            char_count=8,
            sentence_count=2,
            text_path=files.rel(transcript_path),
        ),
    )

    resp = await client.get(f"/api/videos/{row.id}/transcript")
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "第一句。第二句。"
    assert body["engine"] == "bilibili_subtitle"
    # ADR-0015：那三列没值时是 `null`，不是空串 —— 前端据此决定整块要不要渲染。
    assert (body["content_summary"], body["key_points"], body["summary_method"]) == (
        None,
        None,
        None,
    )


async def test_the_reference_columns_travel_with_the_transcript(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """摘要与要点跟着稿子走：同一个端点、同一次请求，前端不必再去猜第二个来源。"""
    row = await app_state.storage.videos.insert(
        VideoDraft(
            platform="douyin", platform_video_id="BV4", title="t", media_path="media/y/media.mp4"
        )
    )
    path = app_state.files.root / "media/y/media/transcript/speech-clean.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("第一句。第二句。", encoding="utf-8")
    await app_state.storage.transcripts.attach(
        row.id,
        TranscriptDraft(
            engine="sherpa_sense_voice",
            char_count=8,
            sentence_count=2,
            text_path=app_state.files.rel(path),
            content_summary="讲两句。",
            key_points="- 第一句。",
            summary_method="local-extractive",
        ),
    )

    body = (await client.get(f"/api/videos/{row.id}/transcript")).json()
    assert body["content_summary"] == "讲两句。"
    assert body["key_points"] == "- 第一句。"
    assert body["summary_method"] == "local-extractive"


async def test_transcript_missing_is_404(client: httpx.AsyncClient, app_state: AppState) -> None:
    row = await app_state.storage.videos.insert(
        VideoDraft(platform="bilibili", platform_video_id="BV2", title="t", media_path="media/x")
    )
    resp = await client.get(f"/api/videos/{row.id}/transcript")
    assert resp.status_code == 404


async def test_transcript_body_file_gone_is_500(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """库里有行、文件读不出来 = 数据损坏，如实 500 而不是空串糊过去（V1 §1.3）。"""
    row = await app_state.storage.videos.insert(
        VideoDraft(platform="bilibili", platform_video_id="BV3", title="t", media_path="media/x")
    )
    await app_state.storage.transcripts.attach(
        row.id,
        TranscriptDraft(
            engine="manual", char_count=0, sentence_count=0, text_path="media/没了/speech-clean.txt"
        ),
    )
    resp = await client.get(f"/api/videos/{row.id}/transcript")
    assert resp.status_code == 500
