"""`tasks/preflight.py`：如实汇总环境红绿灯（V1 §1.3：不许臆造成功）。"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.models.task import TaskKind
from intelligence_hub_v2.platforms.base import HealthReport
from intelligence_hub_v2.tasks.params import PreflightParams
from intelligence_hub_v2.tasks.preflight import run_preflight


def _report(platform: str, status: str, detail: str | None = None) -> HealthReport:
    return HealthReport(
        platform=platform, status=status, detail=detail, checked_at=datetime.now(UTC)
    )


async def test_all_green_is_success(storage, files, monkeypatch) -> None:
    monkeypatch.setattr(
        "intelligence_hub_v2.tasks.preflight.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    reg = FakeRegistry(
        {"douyin": FakeAdapter("douyin", health=_report("douyin", "ok"))}, {"douyin": FakeConfig()}
    )
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.status == "success"
    assert result.summary["platforms_ok"] == 1
    assert result.summary["storage"] == "ok"
    assert result.failures == []


async def test_unreachable_platform_fails_and_keeps_detail(storage, files) -> None:
    reg = FakeRegistry(
        {
            "bilibili": FakeAdapter(
                "bilibili",
                health=_report("bilibili", "unreachable", "cookie 文件不在 data/cookies/x.txt"),
            )
        },
        {"bilibili": FakeConfig()},
    )
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.status == "failed"
    assert result.summary["platforms_unreachable"] == 1
    assert any("cookie 文件不在" in f.error for f in result.failures)  # 原文不许丢


async def test_degraded_platform_is_partial(storage, files) -> None:
    reg = FakeRegistry(
        {"douyin": FakeAdapter("douyin", health=_report("douyin", "degraded", "只缺导出 cookie"))},
        {"douyin": FakeConfig()},
    )
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.status == "partial"
    assert result.summary["platforms_degraded"] == 1


async def test_healthcheck_raising_becomes_unreachable_not_swallowed(storage, files) -> None:
    """适配器 healthcheck 自己炸了 —— 预检要降级成 unreachable，而不是让整任务 traceback。"""

    class Boom(FakeAdapter):
        async def healthcheck(self) -> HealthReport:
            raise RuntimeError("桥连不上")

    reg = FakeRegistry({"douyin": Boom("douyin")}, {"douyin": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.status == "failed"
    assert any("桥连不上" in f.error for f in result.failures)


async def test_inconsistency_counts_as_unreachable(storage, files) -> None:
    reg = FakeRegistry(
        {"douyin": FakeAdapter("douyin", health=_report("douyin", "ok"))}, {"douyin": FakeConfig()}
    )
    reg.inconsistencies = lambda: ["xiaohongshu: 配置里有这一节但当前构建没有适配器实现"]  # type: ignore[method-assign]
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.status == "failed"
    assert any("xiaohongshu" in f.error for f in result.failures)


async def test_storage_down_fails_even_with_green_platforms(storage, files) -> None:
    async def _down() -> bool:
        return False

    storage.healthcheck = _down  # type: ignore[method-assign]
    reg = FakeRegistry(
        {"douyin": FakeAdapter("douyin", health=_report("douyin", "ok"))}, {"douyin": FakeConfig()}
    )
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.status == "failed"
    assert result.summary["storage"] == "unreachable"


async def test_missing_binaries_surfaced_in_summary(storage, files, monkeypatch) -> None:
    monkeypatch.setattr("intelligence_hub_v2.tasks.preflight.shutil.which", lambda name: None)
    reg = FakeRegistry({}, {})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert "ffmpeg" in result.summary["tools_missing"]
    assert result.summary["platform_status"] == "（无启用的平台）"


def test_kind_is_preflight() -> None:
    assert TaskKind.PREFLIGHT == "preflight"
