"""定时采集排期：把 `scheduler.collect_*` 那三个键变成 APScheduler 上真会响的 job。

ADR-0017 定了"什么时候跑"是配置里的一个 cron 串。这一模块是那个决定的**唯一**执行者，
住在这里而不是 `main.py` 的理由很具体：`/api/schedule` 也要排 job（改完 cron 当场生效），
而 `api/v1/*` 是被 `main` 导入的 —— 路由反过来 import `main` 就是循环导入
（第一版就是这么写的，症状是一句 `cannot import name ... from partially initialized module`，
整个 `main` 都起不来）。所以"排 job"这件纯装配后的事下沉到 `core`，
`main`（启动那一路）与 `api/v1/schedule`（改配置那一路）都是它的调用方。

模块不认识 `AppState`：参数是一个个具体的部件（配置段、平台名单、调度器、提交函数），
这样两侧都能用自己的方式把部件交进来，而不必伪造一个 state。

四条不是随手选的规矩，各有一句理由：

- **`max_instances=1`**：一次采集可能要几十分钟，跨过下一次触发点时不挡住，
  同一个博主集会被两条 job 同时扫 —— 白烧一份配额，还会互相撞 cookie 档位。
  宁可跳过一次（`coalesce=True` 把攒下的几次并成一次）。
- **cron 解析只用 `parse_collect_trigger` 这一个入口**：PUT 那一路要在**写盘之前**
  问一句"这串收不收"，启动那一路要在排 job 时问同一句。两处各写一遍 `from_crontab`
  就会分叉，结果是"API 说合法、重启红在启动"。
- **job id 带 `collect:` 前缀**：`/api/schedule` 要能区分采集 job 与"每天清事件"那条
  `prune_events`，不能靠"猜哪些 id 是平台名"。
- **到点走的是 `submit`，不是自己起协程**：那条路才有开关闸门、`requires` 检查、
  限流信号量、清单终态与事件流。定时触发不该是一个绕过所有闸门的后门。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from intelligence_hub_v2.core.config import SchedulerSection
from intelligence_hub_v2.errors import ConfigError
from intelligence_hub_v2.logging import get_logger

__all__ = [
    "COLLECT_JOB_PREFIX",
    "collect_job_platforms",
    "collect_task_name",
    "fire_collect",
    "make_collect_fire",
    "parse_collect_trigger",
    "platform_from_job_id",
    "reschedule_collect_jobs",
    "schedule_collect_jobs",
]

logger = get_logger(__name__)

COLLECT_JOB_PREFIX = "collect:"
"""采集 job 的 id 前缀（`collect:<platform>`）。见模块 docstring 第三条。"""

COLLECT_TASK_SUFFIX = "_collect"
"""定时采集走的任务名后缀。与前端手动"跑一次"点的是**同一个任务名**，
所以定时器不是第二条采集路径，只是同一个任务的另一个触发方。"""

SubmitFn = Callable[[str, Any], str]
"""`TaskScheduler.submit` 的形状：`(任务名, 参数) -> task_id`。"""


def parse_collect_trigger(cron: str, timezone: str) -> Any:  # noqa: ANN401 - CronTrigger，导入只在这个函数里
    """把一条 cron 串解析成 APScheduler 的 trigger；解析不了抛 `ConfigError`。

    单独一个函数是为了让"合法"这件事只有一个判据（见模块 docstring 第二条）。
    异常用 `ConfigError`：全局异常处理器按它翻 4xx，而消息里带着 cron 串与时区 ——
    "0 8 * * * 在这台机器上不对"这句话必须能自己讲完。
    """
    from apscheduler.triggers.cron import CronTrigger

    try:
        return CronTrigger.from_crontab(cron, timezone=timezone)
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"scheduler.collect_cron={cron!r} 在时区 {timezone!r} 下解析不了：{exc}"
        raise ConfigError(msg) from exc


def collect_job_platforms(
    section: SchedulerSection,
    enabled: Sequence[str],
) -> tuple[list[str], list[str]]:
    """这次该排 job 的平台，以及**名单里写了却被关掉**的平台。

    空 `collect_platforms` = 所有启用的平台。配了名单时，关掉的平台即使在名单里也不排
    （与"关掉平台 → 它的任务从 `/api/tasks` 消失"是同一条语义，两处不能一个排一个不排），
    但第二个返回值必须把它带出来：静默少排一个平台，症状是"那个平台的博主永远不更新，
    而配置看着没问题"。
    """
    wanted = section.collect_platforms or list(enabled)
    skipped = [name for name in wanted if name not in enabled]
    if skipped:
        logger.warning("jobs.collect_platform_disabled", skipped=skipped)
    return [name for name in wanted if name in enabled], skipped


def fire_collect(submit: SubmitFn, task_name: str, limit: int | None) -> str:
    """到点（或人点"立即跑一次"）做的事：发一次采集，返回 task_id。

    `limit=None` 是诚实的 None —— 交给任务自己回落到平台配置的 `videos_per_creator`，
    而不是在这里替它填一个默认值（那会变成第二处"每次收几条"的真相）。
    """
    return submit(f"{task_name}", {"limit": limit})


def make_collect_fire(
    submit: SubmitFn, limit: int | None
) -> Callable[[str], Callable[[], Awaitable[None]]]:
    """造"给某个平台生成 job 回调"的那一层工厂。

    返回的回调**必须是协程函数**。`AsyncIOExecutor` 对非协程的 job 会丢进默认线程池，
    而那个线程里没有 running loop —— `TaskScheduler.submit` 起的 `asyncio.create_task`
    当场 `RuntimeError`，症状是"定时器响了、日志里一条 error、任务永远不出现"。
    集成用例 `test_the_cron_job_actually_fires_and_submits_the_task` 就是为这一条写的
    （T6.3 第一版这里写成 `def`，它红了；见 `docs/lessons.md` 经验 54）。
    """

    def factory(platform: str) -> Callable[[], Awaitable[None]]:
        task_name = f"{platform}{COLLECT_TASK_SUFFIX}"

        async def _fire() -> None:
            task_id = fire_collect(submit, task_name, limit)
            logger.info("jobs.collect_fired", task=task_name, task_id=task_id, limit=limit)

        return _fire

    return factory


def schedule_collect_jobs(
    section: SchedulerSection,
    platforms: Sequence[str],
    scheduler: Any,  # noqa: ANN401 - apscheduler 实例；导入故意只住在调用方与 parse 里
    fire_factory: Callable[[str], Callable[[], Awaitable[None]]],
) -> list[str]:
    """按 `section.collect_cron` 给每个平台排一条 cron job，返回真排上的平台名。

    这里**不**再查"`<platform>_collect` 存不存在、implemented 没有"：
    `collect_platforms` 已经被 `core/config.py` 按 `PLATFORM_CONFIG_SCHEMAS` 校验过，
    而那张表里的每一家都有已实现的采集任务 —— 到这一步能进来的平台，任务必然在。
    加两层今天跑不到的检查只会让人以为它们被验过了（本仓库踩过多次的那种假防护）。
    """
    cron = section.collect_cron
    if not cron:
        return []
    trigger = parse_collect_trigger(cron, section.timezone)
    if not platforms:
        logger.warning("jobs.collect_nothing_to_schedule", cron=cron, reason="没有启用的平台")
        return []
    for platform in platforms:
        scheduler.add_job(
            fire_factory(platform),
            trigger,
            id=f"{COLLECT_JOB_PREFIX}{platform}",
            coalesce=True,
            max_instances=1,
        )
    logger.info("jobs.collect_scheduled", cron=cron, platforms=list(platforms))
    return list(platforms)


def reschedule_collect_jobs(
    section: SchedulerSection,
    enabled: Sequence[str],
    scheduler: Any | None,  # noqa: ANN401 - 同上
    submit: SubmitFn,
) -> list[str]:
    """先删后加，按**当前**配置把采集 job 重排一遍。

    存在的理由只有一句话：`/api/schedule` 改完 cron 必须当场生效。做不到就得在响应里
    写 `requires_restart: true` —— 而"配置写进去了但没人知道它什么时候开始算"正是 V1 的病。

    为什么先删后加而不是 diff 着改：cron 串是所有采集 job **共用**的一条，改它必然要重建
    每一个；而逐条判断"哪些参数变了"要在我们这边复刻一份 APScheduler 的 trigger 比较逻辑，
    那是会漂的第二处真相。

    `scheduler is None`（`scheduler.enabled: false`，或服务还没起完）不是错误：配置照样写得住，
    只是要到下一次启动才生效 —— 返回空清单，由调用方把这句话显示出来，不许静默成功。
    """
    if scheduler is None:
        logger.info("jobs.collect_reschedule_skipped", reason="apscheduler 没在跑")
        return []
    for job in list(scheduler.get_jobs()):
        job_id = getattr(job, "id", None)
        if isinstance(job_id, str) and job_id.startswith(COLLECT_JOB_PREFIX):
            scheduler.remove_job(job_id)
    platforms, _skipped = collect_job_platforms(section, enabled)
    return schedule_collect_jobs(
        section, platforms, scheduler, make_collect_fire(submit, section.collect_limit)
    )


def diff_fields(old: Mapping[str, object], new: Mapping[str, object]) -> list[str]:
    """两次配置之间变了哪些**键名**（只报键名，与平台配置那条 `changed_fields` 同一口径）。"""
    return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


def collect_task_name(platform: str) -> str:
    """平台名 → 采集任务名。**手动"立即跑一次"与定时器走的是同一个名字**，
    这个函数存在的意义就是让两侧不可能拼出两个不同的任务名。"""
    return f"{platform}{COLLECT_TASK_SUFFIX}"


def platform_from_job_id(job_id: str) -> str:
    """job id → 平台名。与 `collect_task_name` 一对，两侧共用那两个常量。"""
    return job_id.removeprefix(COLLECT_JOB_PREFIX)
