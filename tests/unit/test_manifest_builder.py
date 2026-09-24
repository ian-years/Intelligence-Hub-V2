"""ManifestBuilder 的终态强制契约。

V1 §2 契约二「清单必须写终态」在 V2 的结构性保证就在这里：
停在"没有 status"的初稿会被下游计数兜底猜成绿灯（全 0 = 成功），
所以 finalize() 必须**任何路径**都产出带 status + ended_at 的 Manifest。

同时看护 V1 §1.3「不许吞错」：任务级失败的原文必须落进 Manifest.error。
"""

from __future__ import annotations

import pytest

from intelligence_hub_v2.errors import ListError, MediaDownloadError, PlatformError
from intelligence_hub_v2.models import (
    ArtifactRef,
    FailureRecord,
    Manifest,
    ManifestBuilder,
    TaskKind,
)


def _builder() -> ManifestBuilder:
    return ManifestBuilder(
        task_name="douyin_collect",
        task_id="task-abc",
        kind=TaskKind.PLATFORM_COLLECT,
        config_snapshot={"platforms": {"douyin": {"enabled": True}}},
    )


# ---------------------------------------------------------------------------
# 终态强制
# ---------------------------------------------------------------------------


def test_finalize_without_explicit_status_records_failed() -> None:
    """没调过任何置态方法 → failed，而不是 success、也不是"没有 status"。"""
    manifest = _builder().finalize()
    assert manifest.status == "failed"
    assert manifest.error == "manifest finalized without explicit status"
    assert manifest.ended_at is not None


def test_finalize_always_sets_ended_at() -> None:
    """ended_at 必填。半截清单（没有 ended_at）在 V2 构造不出来。"""
    for status_setter in ("succeed", "partial", "timeout", "cancel"):
        builder = _builder()
        if status_setter == "partial":
            builder.partial({"downloaded": 1})
        else:
            getattr(builder, status_setter)()
        manifest = builder.finalize()
        assert manifest.ended_at is not None, status_setter
        assert manifest.ended_at >= manifest.started_at, status_setter


def test_finalize_is_idempotent_on_status() -> None:
    """finalize 可以调多次（ctx manager 的 finally 与显式调用撞车时）。"""
    builder = _builder()
    builder.succeed({"downloaded": 3})
    first = builder.finalize()
    second = builder.finalize()
    assert first.status == second.status == "success"
    assert first.summary == second.summary
    # review P1-6：两次必须是**同一个对象** —— ended_at 各取一次 now 的话，
    # 事件里的 duration 与落盘的 ended_at 会对不上（同一份凭据两个"结束时刻"）。
    assert first is second
    assert first.ended_at == second.ended_at


def test_status_setters_are_all_covered() -> None:
    """五个终态一个都不能漏（spec §2.6 的 Literal 全集）。"""
    cases: list[tuple[str, object]] = [
        ("succeed", lambda b: b.succeed({"downloaded": 1})),
        ("partial", lambda b: b.partial({"downloaded": 1, "failed": 1})),
        ("fail", lambda b: b.fail(RuntimeError("boom"))),
        ("timeout", lambda b: b.timeout(after_seconds=3600)),
        ("cancel", lambda b: b.cancel("user pressed stop")),
    ]
    expected = ["success", "partial", "failed", "timeout", "cancelled"]
    got = []
    for _name, action in cases:
        builder = _builder()
        action(builder)  # type: ignore[operator]
        got.append(builder.finalize().status)
    assert got == expected


# ---------------------------------------------------------------------------
# 不许吞错（V1 §1.3）
# ---------------------------------------------------------------------------


def test_fail_preserves_error_verbatim() -> None:
    """异常原文一字不改地进 Manifest.error。"""
    raw = "Failed to decrypt with DPAPI: 0x80090005"
    builder = _builder()
    builder.fail(RuntimeError(raw))
    manifest = builder.finalize()
    assert manifest.status == "failed"
    assert manifest.error == raw


def test_fail_on_exception_with_empty_message_still_records_something() -> None:
    """`str(exc)` 为空时不能留下 error=None —— 那等于吞错。"""
    builder = _builder()
    builder.fail(RuntimeError())
    manifest = builder.finalize()
    assert manifest.error is not None
    assert "RuntimeError" in manifest.error


def test_fail_on_platform_error_also_appends_failure_record() -> None:
    """PlatformError 带 platform/stage，顺手补一条 failures[]，前端不必解析字符串。"""
    builder = _builder()
    builder.fail(MediaDownloadError("douyin", "download", "所有 cookie 档位都试过，仍然 403"))
    manifest = builder.finalize()
    assert manifest.status == "failed"
    assert len(manifest.failures) == 1
    record = manifest.failures[0]
    assert record.platform == "douyin"
    assert record.stage == "download"
    assert record.error_kind == "MediaDownloadError"
    assert "403" in record.error


def test_fail_on_platform_error_with_unknown_stage_falls_back_to_task() -> None:
    """V1 §7.22 那类 stage='creators' 的失败：收窄成 'task'，原文不丢。"""
    builder = _builder()
    builder.fail(ListError("bilibili", "creators", "读博主库失败：库里两位博主开关都关着"))
    manifest = builder.finalize()
    record = manifest.failures[0]
    assert record.stage == "task"
    assert "读博主库失败" in record.error
    assert "creators" not in record.stage


def test_timeout_records_the_budget() -> None:
    """超时不说多久等于没说。"""
    builder = _builder()
    builder.timeout(after_seconds=3600)
    manifest = builder.finalize()
    assert manifest.status == "timeout"
    assert "3600.0s" in (manifest.error or "")


def test_timeout_without_budget_still_has_error_text() -> None:
    builder = _builder()
    builder.timeout()
    assert builder.finalize().error == "task timed out"


def test_cancel_records_reason() -> None:
    builder = _builder()
    builder.cancel("user pressed stop")
    manifest = builder.finalize()
    assert manifest.status == "cancelled"
    assert manifest.error == "user pressed stop"


def test_cancel_without_reason_is_allowed() -> None:
    """前端点取消按钮通常不给理由，这不是错误。"""
    builder = _builder()
    builder.cancel()
    manifest = builder.finalize()
    assert manifest.status == "cancelled"
    assert manifest.error is None


def test_succeed_leaves_error_none() -> None:
    builder = _builder()
    builder.succeed({"downloaded": 5})
    assert builder.finalize().error is None


# ---------------------------------------------------------------------------
# 逐条 item 的失败与产物
# ---------------------------------------------------------------------------


def test_add_failure_accumulates() -> None:
    builder = _builder()
    for i in range(3):
        builder.add_failure(
            FailureRecord(
                platform="bilibili",
                stage="download",
                video_id=f"BV{i}",
                error=f"Request is rejected by server (352) #{i}",
            )
        )
    manifest = builder.finalize()
    assert len(manifest.failures) == 3
    # timestamp 有默认值，调用方不必手填
    assert all(f.timestamp is not None for f in manifest.failures)


def test_partial_keeps_failures_and_summary_separate() -> None:
    """partial 的语义：主体成功 + 有 item 失败。两件事都得看得见。"""
    builder = _builder()
    builder.add_failure(
        FailureRecord(
            platform="douyin", stage="download", video_id="7642363455722229042", error="403"
        )
    )
    builder.partial({"downloaded": 2, "failed": 1})
    manifest = builder.finalize()
    assert manifest.status == "partial"
    assert manifest.summary == {"downloaded": 2, "failed": 1}
    assert len(manifest.failures) == 1


def test_add_artifact_and_set_platforms() -> None:
    builder = _builder()
    builder.set_platforms(["douyin"])
    builder.add_artifact(
        ArtifactRef(
            kind="media", path="media/douyin/姜胡说/x.mp4", platform="douyin", size_bytes=1024
        )
    )
    builder.add_artifact(
        ArtifactRef(kind="transcript", path="media/douyin/姜胡说/x/speech-clean.txt")
    )
    manifest = builder.finalize()
    assert manifest.platforms == ["douyin"]
    assert [a.kind for a in manifest.artifacts] == ["media", "transcript"]
    assert manifest.artifacts[0].size_bytes == 1024


def test_set_platforms_copies_the_input_list() -> None:
    """调用方事后改自己那个 list，不能把清单也改了。"""
    builder = _builder()
    platforms = ["douyin"]
    builder.set_platforms(platforms)
    platforms.append("bilibili")
    assert builder.finalize().platforms == ["douyin"]


def test_set_summary_copies_the_input_dict() -> None:
    builder = _builder()
    summary: dict[str, int | str] = {"downloaded": 1}
    builder.set_summary(summary)
    summary["downloaded"] = 999
    assert builder.finalize().summary == {"downloaded": 1}


def test_config_snapshot_roundtrips() -> None:
    """配置快照进清单是审计用的，必须原样带出来。"""
    snapshot = {"platforms": {"douyin": {"enabled": True, "videos_per_creator": 30}}}
    builder = ManifestBuilder(
        task_name="douyin_collect",
        task_id="t1",
        kind=TaskKind.PLATFORM_COLLECT,
        config_snapshot=snapshot,
    )
    builder.succeed()
    assert builder.finalize().config_snapshot == snapshot


def test_manifest_schema_version_is_locked() -> None:
    """V3 契约：清单 JSON Schema 不变。版本号写死 2.0。"""
    manifest = _builder().finalize()
    assert manifest.schema_version == "2.0"
    assert Manifest.model_fields["schema_version"].default == "2.0"


def test_manifest_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="status"):
        Manifest(
            task_name="x",
            task_id="y",
            kind=TaskKind.PREFLIGHT,
            status="mostly-fine",  # type: ignore[arg-type]
            started_at="2026-09-22T10:00:00Z",
            ended_at="2026-09-22T10:01:00Z",
        )


def test_platform_error_without_stage_does_not_add_failure_record() -> None:
    """普通异常没有 platform/stage，不该硬塞一条假的 failures[]。"""
    builder = _builder()
    builder.fail(ValueError("config is broken"))
    manifest = builder.finalize()
    assert manifest.failures == []
    assert manifest.error == "config is broken"


def test_platform_error_base_class_still_records() -> None:
    builder = _builder()
    builder.fail(PlatformError("douyin", "parse_url", "短链跟 302 之后还是拿不到 sec_uid"))
    manifest = builder.finalize()
    assert manifest.failures[0].stage == "parse_url"
    assert manifest.failures[0].platform == "douyin"
