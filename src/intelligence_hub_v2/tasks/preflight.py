"""`preflight` handler：把"这套环境到底能不能跑"如实摊开。

对应 `docs/specs/task-runner.md §5` 的 `GET /api/preflight`，也是
`AGENTS.md §1` 硬约束 3（**不许臆造成功**）在运行时的落点：缺 ffmpeg / 桥没起 /
cookie 空壳，就红着写进 summary 与 failures，而不是"没发现问题所以返回 ok"。

复用适配器自己的 `healthcheck()`（`DouyinAdapter` / `BilibiliAdapter` 已把
V1 §7.20 的"桥 503=degraded"这类判断实现了一遍），本模块只负责**聚合**与
补上适配器看不到的三件事：主库连通、注册表三份真相是否自洽、外部二进制在不在 PATH。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from intelligence_hub_v2.models.task import FailureRecord, TaskResult
from intelligence_hub_v2.platforms.base import HealthReport
from intelligence_hub_v2.tasks.params import PreflightParams

if TYPE_CHECKING:
    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["run_preflight"]

_TOOLS: tuple[tuple[str, str], ...] = (
    # (给人看的键, PATH 上找的命令名)
    ("ffmpeg", "ffmpeg"),
    ("ffprobe", "ffprobe"),
    ("yt_dlp", "yt-dlp"),
    ("node", "node"),
)
"""外部二进制探针清单。`node` 是 Playwright 那条路要用的（V1 §7.16）。"""


async def run_preflight(ctx: TaskContext, params: PreflightParams) -> TaskResult:
    """探一轮，返回红绿灯汇总。

    判级规则（宁可多报红，不可假绿）：
    - **failed**：主库连不通、有平台探到 unreachable、或注册表三份真相不一致。
      这三样任一发生，"跑任务"这件事就有明确会挂的地方。
    - **partial**：主库通、没有 unreachable，但有平台 degraded（比如只缺导出 cookie）。
    - **success**：全绿。

    `shutil.which` 的缺项只进 summary 不单独判红：某个二进制到底要不要紧是**平台**决定的
    （抖音缺 yt-dlp 能靠页面直链兜底，B站 缺 yt-dlp 就是死路），那个判断适配器 healthcheck 里
    已经做了 —— 这里再判一次就是第二处真相，而且多半判得更粗。
    """
    del params  # 无参数；签名要合 runner 的 Callable 形状。
    failures: list[FailureRecord] = []
    platforms: dict[str, str] = {}
    ok = degraded = unreachable = 0

    for name in ctx.adapters.enabled_platforms():
        report = await _safe_healthcheck(ctx, name)
        platforms[name] = report.status
        if report.status == "ok":
            ok += 1
        elif report.status == "unreachable":
            unreachable += 1
            failures.append(
                FailureRecord(
                    platform=name,
                    stage="task",
                    error=f"{name} unreachable：{report.detail or 'healthcheck 未给出 detail'}",
                    error_kind="HealthUnreachable",
                )
            )
        else:  # degraded / unknown
            degraded += 1
            if report.detail:
                ctx.logger.warning(
                    "preflight.platform_degraded", platform=name, detail=report.detail
                )

    for problem in ctx.adapters.inconsistencies():
        unreachable += 1  # 自洽性坏了，等同于"这条路不可用"
        failures.append(
            FailureRecord(
                platform=None, stage="task", error=problem, error_kind="RegistryInconsistent"
            )
        )

    storage_ok = await ctx.storage.healthcheck()
    if not storage_ok:
        failures.append(
            FailureRecord(
                platform=None,
                stage="task",
                error="主库 SELECT 1 失败（storage.healthcheck 返回 False）",
                error_kind="StorageUnreachable",
            )
        )

    tools = {key: (shutil.which(cmd) is not None) for key, cmd in _TOOLS}
    asr_present = ctx.files.asr_models_dir.is_dir()

    status = _settle_status(storage_ok, unreachable, degraded)
    summary: dict[str, int | str] = {
        "platforms_ok": ok,
        "platforms_degraded": degraded,
        "platforms_unreachable": unreachable,
        "storage": "ok" if storage_ok else "unreachable",
        "asr_model": "present" if asr_present else "missing",
        "platform_status": ", ".join(f"{k}={v}" for k, v in sorted(platforms.items()))
        or "（无启用的平台）",
        "tools_present": ", ".join(k for k, v in tools.items() if v) or "（PATH 上一个都没有）",
        "tools_missing": ", ".join(k for k, v in tools.items() if not v) or "（无）",
    }
    return TaskResult(status=status, summary=summary, failures=failures)


async def _safe_healthcheck(ctx: TaskContext, name: str) -> HealthReport:
    """问一个平台的 healthcheck，**把装配异常也变成一条 unreachable**。

    `adapters.get(name)` 可能抛（配置坏了 / 桥客户端没注入），healthcheck 自己也可能抛
    （桥连接被拒）。对预检来说"探不下去"和"探到坏了"是同一个答案：红。
    但绝不吞成 ok —— 那是 §1.3 的头号反例。
    """
    try:
        adapter = ctx.adapters.get(name)
        return await adapter.healthcheck()
    except Exception as exc:  # noqa: BLE001 - 预检要把任何异常都如实降级成红灯
        return HealthReport(
            platform=name,
            status="unreachable",
            detail=f"{type(exc).__name__}: {exc}",
            checked_at=datetime.now(UTC),
        )


_ResultStatus = Literal["success", "partial", "failed"]


def _settle_status(storage_ok: bool, unreachable: int, degraded: int) -> _ResultStatus:
    if not storage_ok or unreachable > 0:
        return "failed"
    return "partial" if degraded > 0 else "success"
