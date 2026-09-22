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
