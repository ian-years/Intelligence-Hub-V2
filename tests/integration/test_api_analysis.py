"""`/api/benchmark-analysis` 与 `/api/generate-draft-script` 两条新路由（T5.2 / T5.3）。

要看的是**装配**而不是算法（算法在 `tests/unit/analysis/`）：
一条作品的三个来源（`videos` 行、`creators` 行、磁盘上的口播稿）怎么拼成拆解输入，
以及三种"没有数据"的形状怎么分开：没有作品 404 / 没有稿子 404 / 稿子文件读不出来 500。
最后一种如果退化成空稿，看板上就会把"数据损坏"写成"这条视频没说活"（AGENTS.md §1.3）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.transcript import TranscriptDraft
from intelligence_hub_v2.models.video import VideoDraft

if TYPE_CHECKING:
    from pathlib import Path

    from intelligence_hub_v2.api.deps import AppState

TRANSCRIPT_LINES = [
    "你有没有发现自己每天都在手动整理口播稿",
    "最大的痛点是重复劳动太多效率太低",
    "其实核心是先定义清楚交付物",
    "跟着这三步做一遍就够了",
    "现在看这段演示的效果",
    "还有一点要注意的地方",
    "觉得有用就收藏起来吧",
]
"""一份真 schema 的中文口播稿（7 行，覆盖钩子 + 中间 5 档 + 位置兜底的 CTA）。"""


async def _seed_video(
    app_state: AppState,
    *,
    title: str = "如何把口播稿拆成爆款结构",
    platform: str = "bilibili",
    platform_video_id: str = "BV-seed-1",
    creator_name: str | None = "测试博主",
    transcript_text: str | None = "\n".join(TRANSCRIPT_LINES),
    duration_seconds: float | None = 96.0,
    hidden: bool = False,
) -> int:
    """插一条作品（可带博主），并把口播稿正文写到 data 目录下。"""
    creator_id: int | None = None
    if creator_name is not None:
        creator = await app_state.storage.creators.insert(
            CreatorDraft(
                platform=platform,
                platform_id=f"{platform_video_id}-owner",
                name=creator_name,
                profile_url="https://example.com/space/1",
            )
        )
        creator_id = creator.id
    video = await app_state.storage.videos.insert(
        VideoDraft(
            platform=platform,
            platform_video_id=platform_video_id,
            title=title,
            creator_id=creator_id,
            duration_seconds=duration_seconds,
            media_path=f"media/{platform}/{platform_video_id}/media.mp4",
        )
    )
    if hidden:
        await app_state.storage.videos.hide(video.id, "用例隐藏")
    if transcript_text is not None:
        path = app_state.files.root / "transcripts" / f"{video.id}.txt"
        _write(path, transcript_text)
        await app_state.storage.transcripts.attach(
            video.id,
            TranscriptDraft(
                engine="sherpa_sense_voice",
                char_count=len(transcript_text),
                sentence_count=len(transcript_text.splitlines()),
                text_path=app_state.files.rel(path),
            ),
        )
    return video.id


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- 拆解


async def test_benchmark_analysis_reads_all_three_sources(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    video_id = await _seed_video(app_state)
    body = (await client.get("/api/benchmark-analysis", params={"video_id": video_id})).json()

    assert body["version"] == "viral-video-benchmark/v1"
    assert body["title"] == "如何把口播稿拆成爆款结构"
    assert body["creator"] == "测试博主"
    assert body["platform"] == "bilibili"
    assert body["duration_seconds"] == 96.0
    assert body["total_chars"] == sum(len(line) for line in TRANSCRIPT_LINES)
    assert [line["sentence"] for line in body["line_by_line"]] == TRANSCRIPT_LINES
    assert body["line_by_line"][0]["role_key"] == "hook"
    assert body["line_by_line"][-1]["role_key"] == "cta"
    assert body["hook"]["sentence"] == TRANSCRIPT_LINES[0]
    # 96 秒不算短，但一百来个字 < 500 → 第二条判据把它拉回 SHORT
    assert body["total_chars"] < 500
    assert body["handoff"]["recommended_mode"] == "SHORT"
    assert len(body["dos_and_donts"]["borrow"]) == 4


async def test_orphan_video_and_missing_creator_fall_back_to_the_labels(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """`creator_id` 可空（V1 的孤儿作品）→ 引擎的兜底标签，而不是 500。"""
    video_id = await _seed_video(app_state, creator_name=None, platform_video_id="BV-orphan")
    body = (await client.get("/api/benchmark-analysis", params={"video_id": video_id})).json()
    assert body["creator"] == "未知创作者"


async def test_a_hidden_video_is_still_analyzable(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """§7.25：墓碑只管列表，不管按 ID 取详情。"""
    video_id = await _seed_video(app_state, platform_video_id="BV-hidden", hidden=True)
    resp = await client.get("/api/benchmark-analysis", params={"video_id": video_id})
    assert resp.status_code == 200


async def test_transcript_without_body_is_a_finding_not_an_error(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """有稿、稿子是空的 → 200 + 空拆解。它与"没有稿子"（404）必须是两个答案。"""
    video_id = await _seed_video(
        app_state, platform_video_id="BV-empty", transcript_text="# 转写没抓到口播\n\n"
    )
    resp = await client.get("/api/benchmark-analysis", params={"video_id": video_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["line_by_line"] == []
    assert body["total_chars"] == 0
    assert body["hook"]["type"] == "基础标题"


async def test_unknown_video_is_404(client: httpx.AsyncClient, app_state: AppState) -> None:
    resp = await client.get("/api/benchmark-analysis", params={"video_id": 4242})
    assert resp.status_code == 404


async def test_video_without_transcript_is_404(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    video_id = await _seed_video(app_state, platform_video_id="BV-none", transcript_text=None)
    resp = await client.get("/api/benchmark-analysis", params={"video_id": video_id})
    assert resp.status_code == 404
    assert "还没有口播稿" in resp.json()["detail"]


async def test_unreadable_transcript_body_is_500_not_an_empty_analysis(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """库里有行、文件没了 = 数据损坏，如实 500（与 `/videos/{id}/transcript` 同一判据）。"""
    video_id = await _seed_video(app_state, platform_video_id="BV-gone", transcript_text=None)
    path = app_state.files.root / "transcripts" / "gone.txt"
    _write(path, "第一句")
    await app_state.storage.transcripts.attach(
        video_id,
        TranscriptDraft(
            engine="manual",
            char_count=3,
            sentence_count=1,
            text_path=app_state.files.rel(path),
        ),
    )
    path.unlink()
    resp = await client.get("/api/benchmark-analysis", params={"video_id": video_id})
    assert resp.status_code == 500


async def test_video_id_is_required_and_positive(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    assert (await client.get("/api/benchmark-analysis")).status_code == 422
    assert (await client.get("/api/benchmark-analysis", params={"video_id": 0})).status_code == 422


# --------------------------------------------------------------------------- 脚本


async def test_generate_draft_script_needs_no_database_rows(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """主题可以直接给：这个端点连库都不查（引擎从来没读过口播稿，见模块 docstring）。"""
    resp = await client.post(
        "/api/generate-draft-script",
        json={"topic": "量子堆肥", "template_key": "short_fast", "mode": "SHORT"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "draft-ai-work-video/v1"
    assert body["template_name"] == "极速短视频 (35-65s)"
    assert body["beats_template_key"] == "short_fast"
    assert len(body["beats"]) == 4
    assert "量子堆肥" in body["beats"][0]["speech"]
    assert body["script_markdown"].startswith("# 二创导演口播脚本：量子堆肥")


async def test_generate_draft_script_can_take_its_topic_from_a_video_title(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    video_id = await _seed_video(
        app_state, title="如何整理口播稿", platform_video_id="BV-draft", transcript_text=None
    )
    body = (await client.post("/api/generate-draft-script", json={"video_id": video_id})).json()
    assert body["title"] == "如何整理口播稿"
    assert "整理口播稿" in body["beats"][0]["speech"]
    assert body["template_key"] == "tutorial_save_loop"
    assert body["beats_template_key"] == "tutorial_save_loop"


async def test_generate_draft_script_refuses_an_unknown_template(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """V1 会静默换成 `tutorial_save_loop`；V2 直接 422（不许把请求改掉还不说）。"""
    resp = await client.post(
        "/api/generate-draft-script", json={"topic": "量子堆肥", "template_key": "no_such"}
    )
    assert resp.status_code == 422


async def test_generate_draft_script_refuses_custom_instructions(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    resp = await client.post(
        "/api/generate-draft-script",
        json={"topic": "量子堆肥", "custom_instructions": "写得口语化一点"},
    )
    assert resp.status_code == 422
    assert "从未使用" in resp.json()["detail"]

    # 空串与不给是同一件事：都不该被当成"用户写了指示"
    ok = await client.post(
        "/api/generate-draft-script", json={"topic": "量子堆肥", "custom_instructions": "   "}
    )
    assert ok.status_code == 200


async def test_generate_draft_script_refuses_to_invent_a_topic(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """V1 会凭空写一个"AI短视频二创"；V2 拒绝。"""
    resp = await client.post("/api/generate-draft-script", json={})
    assert resp.status_code == 422
    assert "至少给一个" in resp.json()["detail"]


async def test_generate_draft_script_rejects_a_bogus_mode(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    resp = await client.post(
        "/api/generate-draft-script", json={"topic": "量子堆肥", "mode": "MEDIUM"}
    )
    assert resp.status_code == 422


async def test_generate_draft_script_on_an_unknown_video_is_404(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    resp = await client.post("/api/generate-draft-script", json={"video_id": 9999})
    assert resp.status_code == 404


# ------------------------------------------------------------------- 契约面


async def test_both_routes_are_in_the_openapi_document(client: httpx.AsyncClient) -> None:
    """两条路由挂在 `/api` 前缀下、且没弄坏 OpenAPI 生成（AGENTS.md §6 那条自检）。"""
    spec = (await client.get("/openapi.json")).json()
    assert "/api/benchmark-analysis" in spec["paths"]
    assert "/api/generate-draft-script" in spec["paths"]
    assert "BenchmarkAnalysis" in spec["components"]["schemas"]
    assert "DraftScript" in spec["components"]["schemas"]
