"""`tasks/add_creator.py`：收录一位博主（V1 §7.1 短链跟 302 由适配器负责，这里管入库）。"""

from __future__ import annotations

import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.creator import Creator, CreatorDraft, CreatorProfile, CreatorRef
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.tasks.add_creator import run_add_creator
from intelligence_hub_v2.tasks.params import AddCreatorParams


def _ref(platform: str = "douyin", pid: str = "sec_abc") -> CreatorRef:
    return CreatorRef.model_validate(
        {
            "platform": platform,
            "platform_id": pid,
            "profile_url": f"https://{platform}.com/user/{pid}",
        }
    )


def _profile(
    pid: str = "sec_abc", *, name: str = "张三", extra: dict | None = None
) -> CreatorProfile:
    return CreatorProfile.model_validate(
        {"ref": _ref(pid=pid), "name": name, "follower_count": 12345, "extra": extra or {}}
    )


async def test_creates_new_creator_and_publishes(storage, files) -> None:
    ref = _ref()
    adapter = FakeAdapter("douyin", ref=ref, profile=_profile())
    reg = FakeRegistry({"douyin": adapter}, {"douyin": FakeConfig()})
    bus = FakeBus()
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=bus)

    result = await run_add_creator(
        ctx, AddCreatorParams(url="https://douyin.com/user/sec_abc", platform="douyin")
    )

    assert result.status == "success"
    assert result.summary["created"] == 1
    creator = await storage.creators.find("douyin", "sec_abc")
    assert creator is not None and creator.name == "张三" and creator.is_tracking
    added = [e for e in bus.events if e.type is EventType.CREATOR_ADDED]
    assert len(added) == 1


async def test_existing_creator_updated_not_duplicated(storage, files) -> None:
    ref = _ref()
    adapter = FakeAdapter("douyin", ref=ref, profile=_profile(name="新昵称"))
    reg = FakeRegistry({"douyin": adapter}, {"douyin": FakeConfig()})
    bus = FakeBus()
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=bus)
    params = AddCreatorParams(url="https://douyin.com/user/sec_abc", platform="douyin")

    await run_add_creator(ctx, params)
    second = await run_add_creator(ctx, params)

    assert second.summary["created"] == 0
    assert await storage.creators.count(platform="douyin") == 1
    assert any(e.type is EventType.CREATOR_UPDATED for e in bus.events)


async def test_placeholder_nickname_does_not_overwrite_real_name(storage, files) -> None:
    ref = _ref()
    # 第一次收成真名，第二次页面被降级回占位名 —— 不许把真昵称刷坏（V1 §7.24 同类）。
    adapter = FakeAdapter(
        "douyin",
        ref=ref,
        profile=_profile(name="抖音创作者", extra={"nickname_is_placeholder": True}),
    )
    reg = FakeRegistry({"douyin": adapter}, {"douyin": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    await storage.creators.insert(_draft(name="真名"))

    await run_add_creator(ctx, AddCreatorParams(url="x", platform="douyin"))

    after = await storage.creators.find("douyin", "sec_abc")
    assert isinstance(after, Creator)
    assert after.name == "真名"


async def test_platform_inferred_from_url_when_not_given(storage, files) -> None:
    ref = _ref(platform="bilibili", pid="12345")
    adapter = FakeAdapter("bilibili", ref=ref, profile=_profile(pid="12345"))
    reg = FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_add_creator(ctx, AddCreatorParams(url="https://space.bilibili.com/12345"))

    assert result.summary["platform"] == "bilibili"


async def test_cancelled_before_start_raises(storage, files) -> None:
    reg = FakeRegistry({"douyin": FakeAdapter("douyin")}, {"douyin": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    ctx.cancel_token.cancel()

    with pytest.raises(TaskCancelled):
        await run_add_creator(ctx, AddCreatorParams(url="x", platform="douyin"))


def _draft(*, name: str) -> CreatorDraft:
    return CreatorDraft(
        platform="douyin",
        platform_id="sec_abc",
        name=name,
        profile_url="https://douyin.com/user/sec_abc",
        is_tracking=True,
    )
