"""`/api/topics*` 与 `/api/drafts*` 的集成用例（V2.2 T5.5，ADR-0021）。

装配用 `tests/integration/conftest.py` 的那一份：真 app + 内存库 + tmp 配置目录。

为什么 HTTP 这一层还要再写一遍"部分更新不许抹掉别的列"（`tests/unit/storage` 里已经有一条）：
仓库层之上还有一道**只在这里存在**的转换 —— `api/v1/drafts.py:_changes()` 决定
"请求体里没带的键"到底是不发、还是发一个 null 过去。那道判断写错（比如把整个
`model_dump()` 直接摊给 `update_fields`）在仓库层的用例里完全看不出来，
症状却是"改了正文之后稿子的来源不见了"。判据同样是关系：逐列问原值。
"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.video import VideoDraft


async def _video(app_state: AppState) -> int:
    """库里先有一条真作品：草稿的 `source_video_id` 要指得到一个真实主键。"""
    creator = await app_state.storage.creators.insert(
        CreatorDraft(
            platform="douyin",
            platform_id="MS4wLjABAAAA-topics-drafts",
            name="选题测试博主",
            profile_url="https://www.douyin.com/user/MS4wLjABAAAA-topics-drafts",
        )
    )
    video = await app_state.storage.videos.insert(
        VideoDraft(
            platform="douyin",
            platform_video_id="7642363455722229043",
            title="被引用的作品",
            creator_id=creator.id,
        )
    )
    return video.id


# ---------------------------------------------------------------------------
# topics
# ---------------------------------------------------------------------------


async def test_topic_crud_round_trip(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/topics")).json() == []

    created = await client.post("/api/topics", json={"name": "AI 做内容", "description": "第一批"})
    assert created.status_code == 201, created.text
    topic_id = created.json()["id"]

    assert [t["id"] for t in (await client.get("/api/topics")).json()] == [topic_id]
    assert (await client.get(f"/api/topics/{topic_id}")).json()["name"] == "AI 做内容"

    patched = await client.patch(f"/api/topics/{topic_id}", json={"description": "改过的"})
    assert patched.status_code == 200
    assert patched.json()["description"] == "改过的"
    assert patched.json()["name"] == "AI 做内容", "PATCH 描述不许顺手改名字"

    assert (await client.delete(f"/api/topics/{topic_id}")).status_code == 204
    assert (await client.get(f"/api/topics/{topic_id}")).status_code == 404
    assert (await client.delete(f"/api/topics/{topic_id}")).status_code == 404
    assert (await client.get("/api/topics")).json() == []


async def test_a_duplicate_topic_name_is_409_not_a_silent_merge(
    client: httpx.AsyncClient,
) -> None:
    """撞名如实回 409。合并两条选题是一个**决定**，不是路由里一个 `.get(默认)`。"""
    first = await client.post("/api/topics", json={"name": "同一条", "description": "甲"})
    second = await client.post("/api/topics", json={"name": "同一条", "description": "乙"})
    assert second.status_code == 409, second.text
    assert (await client.get(f"/api/topics/{first.json()['id']}")).json()["description"] == "甲"
    assert len((await client.get("/api/topics")).json()) == 1


@pytest.mark.parametrize("name", ["", "   "])
async def test_a_blank_topic_name_is_422(client: httpx.AsyncClient, name: str) -> None:
    bad = await client.post("/api/topics", json={"name": name})
    assert bad.status_code == 422, bad.text
    assert (await client.get("/api/topics")).json() == []


async def test_search_does_not_treat_percent_as_a_wildcard(client: httpx.AsyncClient) -> None:
    """`?search=%` 不许把全表捞出来（LIKE 的通配符要转义）。"""
    await client.post("/api/topics", json={"name": "正常选题"})
    hits = (await client.get("/api/topics", params={"search": "%"})).json()
    assert hits == []


# ---------------------------------------------------------------------------
# drafts
# ---------------------------------------------------------------------------


async def test_draft_crud_round_trip(client: httpx.AsyncClient, app_state: AppState) -> None:
    video_id = await _video(app_state)
    created = await client.post(
        "/api/drafts",
        json={"title": "三条路", "content": "第一段", "source_video_id": video_id},
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["status"] == "draft", "默认状态是 draft，不是空串"
    assert row["created_at"] and row["updated_at"]

    draft_id = row["id"]
    assert [d["id"] for d in (await client.get("/api/drafts")).json()] == [draft_id]
    assert (await client.get(f"/api/drafts/{draft_id}")).json()["title"] == "三条路"

    assert (await client.delete(f"/api/drafts/{draft_id}")).status_code == 204
    assert (await client.get(f"/api/drafts/{draft_id}")).status_code == 404
    assert (await client.get("/api/drafts")).json() == []


async def test_patch_with_one_field_leaves_every_other_column_alone(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """**HTTP 边界的 §7.4 看护**：只发 `content`，其余四列逐条问原值。"""
    video_id = await _video(app_state)
    created = await client.post(
        "/api/drafts",
        json={"title": "原标题", "content": "原正文", "source_video_id": video_id},
    )
    body = created.json()
    draft_id = body["id"]

    patched = await client.patch(f"/api/drafts/{draft_id}", json={"content": "新正文"})
    assert patched.status_code == 200, patched.text
    after = patched.json()

    assert after["content"] == "新正文"
    for column in ("id", "title", "source_video_id", "status", "created_at"):
        assert after[column] == body[column], f"只发了 content，{column} 却被改了"


async def test_patch_can_publish_a_draft_without_rewriting_the_text(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    video_id = await _video(app_state)
    created = await client.post(
        "/api/drafts", json={"title": "T", "content": "C", "source_video_id": video_id}
    )
    draft_id = created.json()["id"]
    published = await client.patch(f"/api/drafts/{draft_id}", json={"status": "published"})
    assert published.json()["status"] == "published"
    assert published.json()["content"] == "C"
    # 筛选项跟着走：只按 status 问，库里那一条要正好出现在结果里。
    assert [
        d["id"] for d in (await client.get("/api/drafts", params={"status": "published"})).json()
    ] == [draft_id]
    assert (await client.get("/api/drafts", params={"status": "archived"})).json() == []


async def test_an_unknown_status_filter_is_422_not_an_empty_list(client: httpx.AsyncClient) -> None:
    """悄悄返回空列表 = 把"你筛错了"说成"没有这种东西"（AGENTS §1.3）。"""
    await client.post("/api/drafts", json={"title": "T", "content": "C"})
    assert (await client.get("/api/drafts", params={"status": "drafted"})).status_code == 422
    assert len((await client.get("/api/drafts")).json()) == 1


async def test_a_draft_pointing_at_a_missing_video_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """来源作品不存在 → 404（而不是让外键炸成 500，也不是收下一条断链草稿）。"""
    bad = await client.post(
        "/api/drafts", json={"title": "T", "content": "C", "source_video_id": 999}
    )
    assert bad.status_code == 404, bad.text
    assert "999" in bad.json()["detail"]
    assert (await client.get("/api/drafts")).json() == []


@pytest.mark.parametrize("payload", [{"title": "", "content": "C"}, {"title": "T", "content": ""}])
async def test_an_empty_title_or_body_is_422(client: httpx.AsyncClient, payload: dict) -> None:
    assert (await client.post("/api/drafts", json=payload)).status_code == 422
    assert (await client.get("/api/drafts")).json() == []


async def test_patch_rejects_blank_text_from_the_same_single_rule(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """**POST 与 PATCH 两条形同一条规则**（`assert_non_blank_text` 只有一处定义）。

    这条用例看的是"改的那一半有没有跟着改"：只发正文 `"   "` 时 `DraftInput` 根本不参与
    （部分更新不凑整份草稿），所以漏了这道判据的症状是 200 + 一条空白正文的草稿，
    而它的 `updated_at` 还显示"刚刚改过"。
    """
    video_id = await _video(app_state)
    created = await client.post(
        "/api/drafts", json={"title": "T", "content": "C", "source_video_id": video_id}
    )
    body = created.json()
    bad = await client.patch(f"/api/drafts/{body['id']}", json={"content": "   "})
    assert bad.status_code == 422, bad.text
    still = (await client.get(f"/api/drafts/{body['id']}")).json()
    assert still["content"] == "C"
    assert still["source_video_id"] == video_id
