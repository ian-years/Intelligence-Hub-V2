"""`tasks/preflight.py`：如实汇总环境红绿灯（V1 §1.3：不许臆造成功）。"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.asr.engine import ENGINE_NAME, EngineStatus
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


async def test_asr_engine_and_asr_dir_are_two_separate_answers(storage, files, monkeypatch) -> None:
    """`asr_model`（那棵目录树在不在）与 `asr_engine`（到底能不能转写）是两问。

    合成一问就会把两种完全不同的修法说成一句"ASR 不可用"：换机器只拷了 `data/`
    → 缺的是 233 MB 权重；全新环境 → 缺的是 `uv sync --extra asr`。
    这里刻意让两个键**互相矛盾**（目录在、权重不在），这才是真实状态。
    """
    files.asr_models_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "intelligence_hub_v2.tasks.preflight.asr_detect",
        lambda **kwargs: EngineStatus(
            available=False,
            engine=ENGINE_NAME,
            reason="找不到 SenseVoice 权重目录。把权重放进 data/asr-models/ 下的一个子目录",
        ),
    )
    reg = FakeRegistry({}, {})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.summary["asr_model"] == "present"
    assert result.summary["asr_engine"] == "weights_missing"
    assert "data/asr-models" in result.summary["asr_detail"]


async def test_a_missing_asr_package_is_reported_as_its_own_state(
    storage, files, monkeypatch
) -> None:
    monkeypatch.setattr(
        "intelligence_hub_v2.tasks.preflight.asr_detect",
        lambda **kwargs: EngineStatus(
            available=False,
            engine=ENGINE_NAME,
            reason="未安装 sherpa-onnx。修复：uv sync --extra asr",
            package_present=False,
        ),
    )
    ctx = make_ctx(storage=storage, files=files, registry=FakeRegistry({}, {}), bus=FakeBus())

    result = await run_preflight(ctx, PreflightParams())

    assert result.summary["asr_model"] == "missing"
    assert result.summary["asr_engine"] == "package_missing"
    # 缺权重/缺包都只是"这一档做不了"，不该把整轮预检判死：采集与字幕那条路照走。
    assert result.status in {"success", "partial"}


def test_kind_is_preflight() -> None:
    assert TaskKind.PREFLIGHT == "preflight"
