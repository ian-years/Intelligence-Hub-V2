"""`manifests` 表的 Repository 测试。

清单是**双写**的（data-model.md §2.7）：文件是权威源，这张表是索引。
所以这里要钉住的是"索引不会撒谎"：

- `file_path` 存**相对 `data/`** 的路径 —— 存绝对路径的话整个数据目录搬一次就全废；
- `content_json` 是冗余副本，坏了要抛（`ManifestRecord.content()`），不静默返回空清单；
- `check_files_exist()` 直接问磁盘，**必须**给 `data_dir`（不给就抛，不兜一个错的默认值）——
  V1 §7.12 的教训是"预检查的是另一个库，红的绿的都对不上"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete

from intelligence_hub_v2.errors import NotFoundError, StorageError
from intelligence_hub_v2.models.manifest import Manifest, ManifestBuilder, ManifestRecord
from intelligence_hub_v2.models.task import FailureRecord, TaskKind
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.repositories.manifests import SCHEMA_VERSION
from intelligence_hub_v2.storage.schema import task_runs_table


def _manifest(
    task_id: str,
    *,
    status: str = "success",
    summary: dict[str, int | str] | None = None,
    error: str | None = None,
    failures: list[FailureRecord] | None = None,
    started_at: datetime | None = None,
) -> Manifest:
    started = started_at or datetime.now(UTC)
    return Manifest(
        task_name="抖音采集",
        task_id=task_id,
        kind=TaskKind.PLATFORM_COLLECT,
        status=status,  # type: ignore[arg-type]
        started_at=started,
        ended_at=started + timedelta(seconds=30),
        summary=summary or {"downloaded": 2, "failed": 0},
        platforms=["douyin"],
        failures=failures or [],
        error=error,
    )


async def _record(
    storage: SqliteStorage,
    task_id: str,
    *,
    file_path: str = "manifests/20260922-055626-platform_collect.json",
    written_at: datetime | None = None,
    **manifest_kwargs: object,
) -> ManifestRecord:
    await storage.task_runs.start(task_id=task_id, task_name="抖音采集", kind="platform_collect")
    manifest = _manifest(task_id, **manifest_kwargs)  # type: ignore[arg-type]
    return await storage.manifests.record(task_id, manifest, file_path, written_at=written_at)


# ---------------------------------------------------------------------------
# 写 / 读
# ---------------------------------------------------------------------------


async def test_record_then_get(storage: SqliteStorage) -> None:
    record = await _record(storage, "t1")
    assert record.id > 0
    assert record.task_id == "t1"
    assert record.schema_version == SCHEMA_VERSION == "2.0"
    assert record.file_path == "manifests/20260922-055626-platform_collect.json"

    fetched = await storage.manifests.get(record.id)
    assert fetched is not None
    assert fetched.file_path == record.file_path
    assert await storage.manifests.get(999_999) is None


async def test_get_or_raise(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError, match="manifest"):
        await storage.manifests.get_or_raise(999_999)


async def test_record_requires_an_existing_task(storage: SqliteStorage) -> None:
    """外键是真的。清单必须能回答"我属于哪次任务" —— 不许留指向幽灵任务的索引行。"""
    with pytest.raises(StorageError):
        await storage.manifests.record("ghost", _manifest("ghost"), "manifests/x.json")


async def test_file_path_is_stored_verbatim_and_relative(storage: SqliteStorage) -> None:
    """**存相对路径**：数据目录要能整体搬走 / 备份 / 换机器。"""
    relative = "manifests/20260922-055626-platform_collect.json"
    record = await _record(storage, "t1", file_path=relative)
    assert record.file_path == relative
    assert not Path(record.file_path).is_absolute()


async def test_content_roundtrips_through_the_redundant_copy(storage: SqliteStorage) -> None:
    record = await _record(storage, "t1", summary={"downloaded": 3, "failed": 1})
    content = record.content()
    assert isinstance(content, Manifest)
    assert content.task_id == "t1"
    assert content.status == "success"
    assert content.summary == {"downloaded": 3, "failed": 1}
    assert content.kind is TaskKind.PLATFORM_COLLECT


async def test_content_json_keeps_chinese_error_text_readable(storage: SqliteStorage) -> None:
    """**V1 §1.3**：错误原文落库要人能读，不是 `\\u5931\\u8d25`。"""
    original = "yt-dlp: Request is blocked by server (412)；带登录 cookie 也不行"
    record = await _record(
        storage,
        "t1",
        status="failed",
        error=original,
        summary={"downloaded": 0, "failed": 1},
        failures=[
            FailureRecord(
                platform="bilibili", stage="list", error=original, error_kind="PlatformError"
            )
        ],
    )
    assert original in record.content_json
    assert "\\u" not in record.content_json
    parsed = json.loads(record.content_json)
    assert parsed["error"] == original
    assert parsed["failures"][0]["error"] == original


async def test_content_on_corrupt_json_raises(storage: SqliteStorage) -> None:
    """库里存着坏 JSON 是数据损坏。静默跳过等于把损坏藏起来（V1 §1.3）。"""

    broken = ManifestRecord(
        id=1,
        task_id="t1",
        file_path="manifests/x.json",
        written_at=datetime.now(UTC),
        content_json="{not json",
    )
    with pytest.raises(ValueError):
        broken.content()


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


async def test_list_for_task_is_ascending(storage: SqliteStorage) -> None:
    base = datetime.now(UTC)
    await storage.task_runs.start(task_id="t1", task_name="抖音采集", kind="platform_collect")
    for i in range(3):
        await storage.manifests.record(
            "t1",
            _manifest("t1", started_at=base + timedelta(minutes=i)),
            f"manifests/m{i}.json",
            written_at=base + timedelta(minutes=i),
        )
    paths = [r.file_path for r in await storage.manifests.list_for_task("t1")]
    assert paths == ["manifests/m0.json", "manifests/m1.json", "manifests/m2.json"]


async def test_more_than_one_manifest_per_task_is_not_merged(storage: SqliteStorage) -> None:
    """>1 行是 `finalize()` 被调两次的 bug 信号，原样交出去让调用方看见。"""
    await storage.task_runs.start(task_id="t1", task_name="抖音采集", kind="platform_collect")
    base = datetime.now(UTC)
    for i in range(2):
        await storage.manifests.record(
            "t1", _manifest("t1"), f"manifests/dup{i}.json", written_at=base + timedelta(seconds=i)
        )
    assert len(await storage.manifests.list_for_task("t1")) == 2
    latest = await storage.manifests.latest_for_task("t1")
    assert latest is not None
    assert latest.file_path == "manifests/dup1.json"


async def test_latest_for_task_on_nothing_returns_none(storage: SqliteStorage) -> None:
    assert await storage.manifests.latest_for_task("ghost") is None


async def test_list_recent_is_ascending_newest_last(storage: SqliteStorage) -> None:
    base = datetime.now(UTC)
    for i in range(4):
        task_id = f"t{i}"
        await storage.task_runs.start(
            task_id=task_id, task_name="抖音采集", kind="platform_collect"
        )
        await storage.manifests.record(
            task_id,
            _manifest(task_id),
            f"manifests/m{i}.json",
            written_at=base + timedelta(minutes=i),
        )

    all_rows = await storage.manifests.list_recent(limit=50)
    assert [r.file_path for r in all_rows] == [f"manifests/m{i}.json" for i in range(4)]

    last_two = await storage.manifests.list_recent(limit=2)
    assert [r.file_path for r in last_two] == ["manifests/m2.json", "manifests/m3.json"]


async def test_count(storage: SqliteStorage) -> None:
    assert await storage.manifests.count() == 0
    await _record(storage, "t1")
    await _record(storage, "t2", file_path="manifests/other.json")
    assert await storage.manifests.count() == 2


async def test_deleting_a_task_cascades_to_its_manifests(storage: SqliteStorage) -> None:
    """索引行不能比它指向的任务活得久。"""
    await _record(storage, "t1")
    assert await storage.manifests.count() == 1

    async with storage.sessionmaker() as session, session.begin():
        await session.execute(delete(task_runs_table).where(task_runs_table.c.id == "t1"))

    assert await storage.manifests.count() == 0


# ---------------------------------------------------------------------------
# 一致性抽查（preflight 用）
# ---------------------------------------------------------------------------


async def test_check_files_exist_requires_data_dir(storage: SqliteStorage) -> None:
    """不兜默认值：兜一个错的目录会得到"全部缺失"或"全部存在"两种同样误导的结果。"""
    with pytest.raises(StorageError, match="data_dir"):
        await storage.manifests.check_files_exist()


async def test_check_files_exist_reports_missing_relative_paths(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "manifests").mkdir(parents=True)
    present = data_dir / "manifests" / "here.json"
    present.write_text("{}", encoding="utf-8")

    await _record(storage, "t1", file_path="manifests/here.json")
    await _record(storage, "t2", file_path="manifests/gone.json")

    missing = await storage.manifests.check_files_exist(data_dir=data_dir)
    assert missing == ["manifests/gone.json"]


async def test_check_files_exist_returns_empty_when_all_present(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    target = data_dir / "manifests" / "here.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    await _record(storage, "t1", file_path="manifests/here.json")
    assert await storage.manifests.check_files_exist(data_dir=data_dir) == []


async def test_check_files_exist_accepts_an_absolute_stored_path(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    """库里存的**应该**是相对路径，但历史行/V1 迁移过来的行可能是绝对的。
    抽查要按原样处理，不能把绝对路径再拼一次 data_dir 得到必然缺失。"""
    real = tmp_path / "somewhere-else" / "m.json"
    real.parent.mkdir(parents=True)
    real.write_text("{}", encoding="utf-8")

    await _record(storage, "t1", file_path=str(real))
    missing = await storage.manifests.check_files_exist(data_dir=tmp_path / "data")
    assert missing == []


async def test_check_files_exist_honours_limit(storage: SqliteStorage, tmp_path: Path) -> None:
    for i in range(3):
        await _record(storage, f"t{i}", file_path=f"manifests/gone{i}.json")
    missing = await storage.manifests.check_files_exist(limit=2, data_dir=tmp_path)
    assert len(missing) == 2


# ---------------------------------------------------------------------------
# 与 ManifestBuilder 的衔接（V1 §2 契约二）
# ---------------------------------------------------------------------------


async def test_a_builder_manifest_can_be_recorded_as_is(storage: SqliteStorage) -> None:
    """`ManifestBuilder.finalize()` 的产物直接喂给 `record()`，中间不需要任何转换。

    这条用例是 Task 4 `manifest_writer` 的接缝预演：如果这里需要手工补字段，
    那个 ctx manager 就会长出"忘了补"的分支。
    """
    await storage.task_runs.start(task_id="b1", task_name="B站采集", kind="platform_collect")
    builder = ManifestBuilder("B站采集", "b1", TaskKind.PLATFORM_COLLECT, {"platforms": {}})
    builder.partial({"downloaded": 1, "failed": 1})
    builder.add_failure(FailureRecord(platform="bilibili", stage="download", error="412 blocked"))
    manifest = builder.finalize()

    record = await storage.manifests.record("b1", manifest, "manifests/b1.json")
    restored = record.content()
    assert restored.status == "partial"
    assert restored.summary == {"downloaded": 1, "failed": 1}
    assert restored.failures[0].error == "412 blocked"
    assert restored.ended_at >= restored.started_at


async def test_finalize_without_an_explicit_status_lands_as_failed(storage: SqliteStorage) -> None:
    """**V1 §2 契约二**：停在"没有 status"的初稿会被计数兜底猜成绿灯。

    `Manifest.status` 是必填 Literal，所以"没有 status 的清单"在模型层就构造不出来；
    builder 的兜底是自动记 `failed`。这条断言的是那条兜底真的存在。
    """
    await storage.task_runs.start(task_id="b2", task_name="B站采集", kind="platform_collect")
    builder = ManifestBuilder("B站采集", "b2", TaskKind.PLATFORM_COLLECT, {})
    manifest = builder.finalize()
    assert manifest.status == "failed"
    assert manifest.error is not None

    record = await storage.manifests.record("b2", manifest, "manifests/b2.json")
    assert record.content().status == "failed"
