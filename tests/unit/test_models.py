"""模型 smoke 测试：校验字段、序列化、约束。"""

from datetime import UTC, datetime

from intelligence_hub_v2.models import (
    CreatorRef,
    Event,
    EventType,
    ManifestBuilder,
    Page,
    PagedResult,
    TaskKind,
    VideoMeta,
)


def test_creator_ref_requires_platform_id():
    ref = CreatorRef(
        platform="douyin",
        platform_id="MS4wLjABAAAA",
        profile_url="https://www.douyin.com/user/MS4wLjABAAAA",
    )
    assert ref.platform_id == "MS4wLjABAAAA"
    assert not ref.platform_id.startswith("http")  # V1 §7.1


def test_video_meta_required_fields():
    ref = CreatorRef(
        platform="douyin",
        platform_id="x",
        profile_url="https://www.douyin.com/user/x",
    )
    v = VideoMeta(
        platform="douyin",
        platform_video_id="123",
        creator_ref=ref,
        title="test",
        webpage_url="https://www.douyin.com/video/123",
    )
    assert v.title == "test"


def test_event_payload_is_dict():
    e = Event(
        type=EventType.TASK_STARTED,
        task_id="abc",
        timestamp=datetime.now(UTC),
        payload={"task_name": "douyin_collect"},
    )
    assert e.payload["task_name"] == "douyin_collect"


def test_manifest_builder_forces_terminal_state():
    b = ManifestBuilder("douyin_collect", "id-1", TaskKind.PLATFORM_COLLECT, {})
    # 不调 succeed/fail → finalize 自动 failed
    m = b.finalize()
    assert m.status == "failed"
    assert m.ended_at is not None


def test_manifest_builder_succeed():
    b = ManifestBuilder("test", "id-2", TaskKind.PREFLIGHT, {})
    b.succeed({"checked": 5})
    m = b.finalize()
    assert m.status == "success"
    assert m.summary == {"checked": 5}


def test_page_offset():
    p = Page(page=3, size=10)
    assert p.offset == 20


def test_paged_result_pages():
    r: PagedResult[int] = PagedResult(items=[1, 2], total=25, page=1, size=10)
    assert r.pages == 3
