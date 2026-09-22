"""集成：`manifest_writer` 在所有退出路径都写终态（V1 §2 契约二的端到端看护）。

`tests/unit/core/test_manifest.py`（Task 4）验的是 ctx manager 本体；这里把它接到
真 SQLite + 真文件目录上，确认"清单文件 + DB 索引 + task_runs 终态"三件事在每条路径上
都一起成立 —— 停在半截清单正是 V1 看板那个"看起来成功"的根因。
"""

from __future__ import annotations

import pytest
from tests.unit.tasks.conftest import FakeBus

from intelligence_hub_v2.core.manifest import manifest_writer
from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.task import TaskKind

pytestmark = pytest.mark.integration


async def _scenario(storage, files, scenario: str, bus: FakeBus) -> None:
    tid = f"m-{scenario}"
    await storage.task_runs.start(task_id=tid, task_name="t", kind=TaskKind.PREFLIGHT.value)
    try:
        async with manifest_writer(
            "t", tid, TaskKind.PREFLIGHT, {}, storage=storage, files=files, bus=bus
        ) as builder:
            if scenario == "success":
                builder.succeed({"n": 1})
            elif scenario == "partial":
                builder.partial({"ok": 1, "failed": 1})
            elif scenario == "exception":
                raise RuntimeError("boom")
            elif scenario == "cancel":
                raise TaskCancelled("用户点了停止")
            elif scenario == "timeout":
                raise TimeoutError
    except (RuntimeError, TaskCancelled, TimeoutError):
        # ctx manager 已把终态记进清单；异常继续向外传播是它该有的行为
        pass


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("success", "success"),
        ("partial", "partial"),
        ("exception", "failed"),
        ("cancel", "cancelled"),
        ("timeout", "timeout"),
    ],
)
async def test_manifest_finalizes_on_every_exit_path(storage, files, scenario, expected) -> None:
    bus = FakeBus()

    await _scenario(storage, files, scenario, bus)

    record = await storage.task_runs.get_or_raise(f"m-{scenario}")
    assert record.is_terminal, "任何退出方式都必须落终态，不许停在 running"
    assert record.status == expected
    assert record.ended_at is not None

    # 清单双写：DB 索引 + 磁盘权威文件
    manifest_row = await storage.manifests.latest_for_task(f"m-{scenario}")
    assert manifest_row is not None
    assert record.manifest_path == manifest_row.file_path
    assert files.abs(manifest_row.file_path).is_file()
    assert manifest_row.content().status == expected

    assert any(e.type == "manifest.written" for e in bus.events), "清单写成都该广播一次"
