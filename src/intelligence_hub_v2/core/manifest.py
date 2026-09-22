"""清单上下文管理器：任何分支退出都必须写终态。

契约来源：docs/specs/task-runner.md §2.6（`manifest_writer`）

**这一处消灭的是 V1 §2 契约二**：清单必须写到终态。V1 的清单停在
"没有 `status` 的初稿"时，启动器的计数兜底会把它猜成绿灯（全 0 = 成功）——
用户看到"任务成功"，实际什么都没跑。所以这里的保证不是"记得调 `finalize()`"，
而是**没有别的路径**：不进 `manifest_writer` 就写不出清单，
进了就一定落一份终态清单。

四条实现纪律，每条对应一种真实的踩法：

1. **收尾代码不许盖掉真正的异常**。写文件/写库都在退出路径上，那里再抛一个异常，
   会把用户唯一需要的那条信息（`yt-dlp` 的 412 原文）换成一句 SQLite 栈 ——
   那是最贵的信息损失。所以收尾整体包在 `try` 里：失败只记日志 + 写进
   `task_runs.error_text`，不改写正在传播的异常。
2. **文件是权威源，DB 行只是索引**。写文件失败时**不登记索引** ——
   一条指向不存在文件的索引行比没有索引更糟（点进去 404，列表却看着一切正常）。
3. **清单文件先写临时名再 `os.replace`**。半截 JSON 会让审计工具读不出 `status`，
   而"读不出 status 的清单"正是这条契约最初要防的东西。
4. **清单写不成不改任务的状态**。跑成的那 30 条已经在库里了，挂掉的是我们的记账。
   把 run 标成 `failed` 会引导人去重跑采集，而真正该修的是磁盘 ——
   所以状态照 handler 说的，原因写进 `error_text`。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from intelligence_hub_v2.core.event_bus import EventBus
from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.event import Event, EventType
from intelligence_hub_v2.models.manifest import Manifest, ManifestBuilder
from intelligence_hub_v2.models.task import TaskKind

if TYPE_CHECKING:
    from intelligence_hub_v2.storage.db import SqliteStorage
    from intelligence_hub_v2.storage.files import FileStorage

logger = get_logger(__name__)

__all__ = ["manifest_writer"]


@asynccontextmanager
async def manifest_writer(
    task_name: str,
    task_id: str,
    kind: TaskKind,
    config_snapshot: dict[str, Any],
    *,
    storage: SqliteStorage,
    files: FileStorage,
    bus: EventBus | None = None,
    timeout_seconds: float | None = None,
) -> AsyncIterator[ManifestBuilder]:
    """给 handler 一个 `ManifestBuilder`，退出时把清单双写到盘 + 库 + 任务档案。

    前置条件：**`storage.task_runs.start(task_id=...)` 已经调过**。
    收尾要 `finish()` 那一行，外键不成立就红 —— 那是装配错误，
    该在启动期就响，而不是等任务跑完才发现档案压根没开过。

    `bus` 给了就在双写成功后发一条 `manifest.written`。为什么由这里发：
    只有这段代码同时知道"文件写成了 / 索引登记了 / 终态是什么"，
    让调用方发就会出现"前端收到事件、点进去 404"。

    用法::

        async with manifest_writer(
            "抖音采集", tid, TaskKind.PLATFORM_COLLECT, snapshot,
            storage=storage, files=files, bus=bus,
        ) as builder:
            builder.partial({"downloaded": 1, "failed": 1})
            builder.add_failure(FailureRecord(platform="douyin", stage="download", error=text))

    **五条退出路径与 `finally` 的位置是这段代码的全部要点**：
    状态由 `_settle` 按退出方式补，写清单则由 `finally` 无条件执行 ——
    写成 `else:` 分支里调用就等于"异常时不写清单"，那正好退回 V1 的半截清单。

    **关于 `finally` 里的 await**：`TaskRunner` 用协作式取消
    （`CancelToken` + 抛 `TaskCancelled`，task-runner.md §2.5），不是 `task.cancel()`，
    所以这里不存在"收尾 await 被同一轮取消再打断"的窗口。
    V3 若改硬取消，这条要重新评估。
    """
    builder = ManifestBuilder(task_name, task_id, kind, config_snapshot)
    try:
        yield builder
    except asyncio.CancelledError:
        _settle(builder, "cancelled", reason="外部取消（asyncio.CancelledError）")
        raise
    except TaskCancelled as exc:
        # 协作式取消走这条路。它不是"失败" —— 那是人主动停的，
        # 标 failed 会引导一个值班的人去查一个没坏的东西。
        _settle(builder, "cancelled", reason=str(exc) or type(exc).__name__)
        raise
    except TimeoutError as exc:
        _settle(builder, "timeout", exc=exc, timeout_seconds=timeout_seconds)
        raise
    except Exception as exc:
        _settle(builder, "failed", exc=exc)
        raise
    else:
        _settle(builder, "success")
    finally:
        # 必须在 finally：上面每个 except 分支都 `raise`，写在它们后面就永远执行不到，
        # 于是"任务抛异常"与"清单没写终态"变成同一件事 —— 正是契约二原本要防的。
        await _write_manifest(
            builder, task_id=task_id, kind=kind, storage=storage, files=files, bus=bus
        )


def _settle(
    builder: ManifestBuilder,
    outcome: Literal["success", "failed", "cancelled", "timeout"],
    *,
    exc: Exception | None = None,
    reason: str | None = None,
    timeout_seconds: float | None = None,
) -> None:
    """按退出方式补终态，规则见 `_may_settle`。

    为什么长成"传参"而不是"传一个 lambda 进去"：`except ... as exc` 的 `exc`
    在块尾被 Python 删掉，闭包捕获它的 lambda 只要延迟调用就是 `NameError`
    （pyflakes 当场报 F821）。把"要不要设"与"设成什么"分开之后这个坑不存在。
    """
    if not _may_settle(builder, outcome):
        return
    if outcome == "success":
        builder.succeed()
    elif outcome == "failed" and exc is not None:
        builder.fail(exc)
    elif outcome == "cancelled":
        builder.cancel(reason)
    else:
        builder.timeout(timeout_seconds)


def _may_settle(builder: ManifestBuilder, outcome: str) -> bool:
    """ "知道得更多的一方赢"。三条分支，方向不能反：

    - **没人设过** → 按退出方式补一个。这是 V1 §2 契约二的结构性保证。
    - **handler 设了 `success`，随后却抛出异常/超时/取消** → 覆盖掉 `success`。
      抛了异常还报成功，就是 V1 §1.3 那句"看起来在跑"。
    - **handler 设了 `partial` / `failed` / `cancelled` / `timeout`** → 不动。
      handler 知道"跑完了但有些 item 失败"，wrapper 只知道"这里 raise 了"，
      前者的信息量严格更大；`failures[]` 与 `error` 里仍然留着 handler 记的明细。

    `docs/specs/task-runner.md §2.6` 的草图在 `else` 分支**无条件** `builder.succeed()`，
    那会把 handler 设的 `partial` 改成 `success` —— 就是把"两条下载失败"显示成
    "全成功"的那类 bug。这条偏差已回写 spec。
    """
    current = builder.status
    if current is None:
        return True
    # handler 说过 `success`，而现在是异常退出 → 异常赢。
    # 反过来（handler 设了 partial/failed/…，退出方式是 success 或同一种异常）都不该覆盖。
    return current == "success" and outcome != "success"


async def _write_manifest(
    builder: ManifestBuilder,
    *,
    task_id: str,
    kind: TaskKind,
    storage: SqliteStorage,
    files: FileStorage,
    bus: EventBus | None,
) -> None:
    """收尾：`finalize()` → 写文件 → 登记索引 + 标任务终态（同一个事务）→ 发事件。

    整段自己吞异常（纪律 1）。调用点是 `finally`，这里再抛就会盖掉用户的异常。
    """
    manifest = builder.finalize()
    cleanup_failures: list[str] = []
    path = files.manifest_path(builder.started_at, kind, task_id)
    relative = files.rel(path)

    if not await _write_file(path, manifest, task_id=task_id, cleanup_failures=cleanup_failures):
        # 权威源没落成 → 不登记索引（纪律 2），但**仍要**标任务终态，
        # 否则前端是一个永远转不出来的进度条（V1 §7.22 的形状）。
        await _finish_run(storage, task_id, manifest, None, cleanup_failures)
        await _log_cleanup_failures(cleanup_failures, task_id, manifest)
        return

    registered = False
    try:
        async with storage.transaction():
            await storage.manifests.record(task_id, manifest, relative)
            await storage.task_runs.finish(
                task_id,
                status=manifest.status,
                summary=dict(manifest.summary),
                manifest_path=relative,
                error_text=_error_text(manifest, []),
                ended_at=manifest.ended_at,
            )
        registered = True
    except Exception as exc:
        cleanup_failures.append(f"清单登记失败: {type(exc).__name__}: {exc}")
        logger.exception("manifest.record_failed", task_id=task_id, path=relative)
        # 不回滚成"什么都没发生"：文件已经在盘上（权威源）。单独再标一次终态，
        # 至少任务不会停在 running —— 这一步失败也只记日志，见 _finish_run。
        await _finish_run(storage, task_id, manifest, relative, cleanup_failures)

    if bus is not None and registered:
        await bus.publish(
            Event(
                type=EventType.MANIFEST_WRITTEN,
                task_id=task_id,
                timestamp=manifest.ended_at,
                payload={
                    "manifest_path": relative,
                    "schema_version": manifest.schema_version,
                    "status": manifest.status,
                    "summary": dict(manifest.summary),
                },
            )
        )

    await _log_cleanup_failures(cleanup_failures, task_id, manifest)


async def _write_file(
    path: Path, manifest: Manifest, *, task_id: str, cleanup_failures: list[str]
) -> bool:
    """先写 `.tmp` 再 `os.replace`，返回是否写成。

    `os.replace` 在同一文件系统上是原子的：读者要么看到"还没有"，要么看到完整的
    新文件，**永远看不到半截 JSON**。清单是审计凭据，半截文件比没有文件更糟 ——
    它长得像"跑过了但没结果"。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 目录建不出来（磁盘满 / 权限）与写失败同等处理
        cleanup_failures.append(f"清单目录创建失败: {type(exc).__name__}: {exc}")
        logger.exception("manifest.dir_create_failed", task_id=task_id, path=str(path))
        return False

    temp = path.with_name(f"{path.name}.tmp")
    try:
        temp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temp, path)  # noqa: PTH105 - 原子替换没有 pathlib 等价物
    except OSError as exc:
        cleanup_failures.append(f"清单文件写入失败: {type(exc).__name__}: {exc}")
        logger.exception("manifest.file_write_failed", task_id=task_id, path=str(path))
        return False
    finally:
        with suppress(OSError):  # replace 成功后这里自然是 no-op
            temp.unlink(missing_ok=True)
    return True


async def _finish_run(
    storage: SqliteStorage,
    task_id: str,
    manifest: Manifest,
    manifest_path: str | None,
    cleanup_failures: list[str],
) -> None:
    """把任务档案标成终态。失败只记日志。

    单独抽出来是因为有两条路径都需要它（文件写失败、登记失败），
    而这两条都在"正在传播用户异常"的栈上 —— 这里绝不能往外抛。
    """
    try:
        await storage.task_runs.finish(
            task_id,
            status=manifest.status,
            summary=dict(manifest.summary),
            manifest_path=manifest_path,
            error_text=_error_text(manifest, cleanup_failures),
            ended_at=manifest.ended_at,
        )
    except Exception as exc:
        cleanup_failures.append(f"任务档案终态写入失败: {type(exc).__name__}: {exc}")
        logger.exception("manifest.task_run_finish_failed", task_id=task_id)


def _error_text(manifest: Manifest, extra_failures: list[str]) -> str | None:
    """`error_text` = 任务级失败原文 + 我们自己在收尾时遇到的失败。

    两段都必须留。前者回答"这轮为什么没成"，后者回答"这条记录为什么可能不完整" ——
    只留前者会得到一条看起来完整、其实索引没登记上的记录。
    """
    parts = [manifest.error] if manifest.error else []
    parts.extend(extra_failures)
    return "\n".join(parts) if parts else None


async def _log_cleanup_failures(failures: list[str], task_id: str, manifest: Manifest) -> None:
    """收尾失败逐条打日志。**不抛**：正在传播的是用户那条更值钱的异常。"""
    for message in failures:
        logger.error(
            "manifest.cleanup_failed", task_id=task_id, status=manifest.status, detail=message
        )
