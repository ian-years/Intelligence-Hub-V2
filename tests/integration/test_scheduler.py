"""定时采集调度（T6.3 第一片：配置驱动的 cron job；API 与 Settings 是第二片）。

配置走 **tmp 目录里的一份真 app.yaml**，不是改模型对象：要验的是
"scheduler 那一段从 YAML 读进来 → 校验 → 变成 APScheduler 的 job"这条完整的路。

"到点真起采集"那一条**没有 mock 时钟**：用秒级 cron 让 APScheduler 真响一次、真调
`TaskScheduler.submit`，再看 `task_runs` 里那一行。采集本身会因为没桥而失败 ——
那不是这一条问的问题，它问的是"定时器响了以后任务有没有被提交"。拿假时钟验的话，
trigger 解析、job 注册、executor 派发这半截一行都不会被跑到。

每条用例自己 `try/finally` 关 state，**没有 autouse 收尸 fixture**：那种 fixture 得是
async 的，会把这个文件里那条同步用例（只读配置、不建 state）一起拖垮。而漏关的代价
真遇见过 —— 连接在**下一条**用例期间被 GC，报成 `ResourceWarning: deleted before being
closed`，`filterwarnings = ["error"]` 让下一条替它背锅。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import ValidationError
from structlog.testing import capture_logs

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.errors import ConfigError
from intelligence_hub_v2.main import _add_collect_jobs, build_components, collect_job_platforms
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

pytestmark = pytest.mark.integration

_DOUYIN_ON = "douyin:\n  enabled: true\n"
_BOTH_ON = "douyin:\n  enabled: true\nbilibili:\n  enabled: true\n"
_BILI_OFF = "douyin:\n  enabled: true\nbilibili:\n  enabled: false\n"

#: "每天 08:00"，验收那一栏写的就是它。
CRON_0800 = 'enabled: true\ncollect_cron: "0 8 * * *"'


def _app_yaml(scheduler_block: str) -> str:
    """把 `scheduler:` 段的内容统一缩进两格。

    逐行 strip 再加缩进，是因为"每条用例自己管缩进"一定会写出漏缩进的第一行 ——
    这个文件的初版就是这样红的（`mapping values are not allowed here`）。
    过这里的段都是**平铺的标量键**（没有下一层嵌套），所以抹平缩进不丢信息。
    """
    lines = [line.strip() for line in scheduler_block.strip().splitlines() if line.strip()]
    return "scheduler:\n" + "\n".join(f"  {line}" for line in lines) + "\n"


async def _state(tmp_path: Path, *, platforms_yaml: str, scheduler_block: str) -> AppState:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "platforms.yaml").write_text(platforms_yaml, encoding="utf-8")
    (config_dir / "app.yaml").write_text(_app_yaml(scheduler_block), encoding="utf-8")
    manager = ConfigManager(config_dir)
    manager.load()
    storage = SqliteStorage.in_memory()
    await storage.initialize()
    for name in manager.platform_names():
        cfg = manager.get_platform(name)
        await storage.platforms.upsert(
            name, enabled=cfg.enabled, config=cfg.model_dump(mode="json")
        )
    files = FileStorage(tmp_path / "data")
    files.ensure_dirs()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    state = build_components(manager, storage=storage, files=files, http=http)
    state.owns_http = False  # 客户端是测试建的，关闭也由测试负责
    return state


async def _close(state: AppState) -> None:
    """按 lifespan 的顺序收：**先停 scheduler**，再关 http，最后关库。

    跑着的任务被 `storage.close()` 抽掉连接，症状不是这条红，而是它的连接在下一条
    用例期间被 GC 时报 `ResourceWarning`。定时触发那条尤其容易踩（它确实有任务在跑）。
    """
    await state.scheduler.shutdown()
    await state.http.aclose()
    await state.storage.close()


def _register(state: AppState) -> AsyncIOScheduler:
    """跑一遍注册逻辑，返回**没 start() 的** scheduler：注册与触发是两件事。"""
    scheduler = AsyncIOScheduler(timezone=state.config.scheduler.timezone)
    _add_collect_jobs(state, scheduler)
    return scheduler


def _ids(scheduler: AsyncIOScheduler) -> list[str]:
    return sorted(str(job.id) for job in scheduler.get_jobs())


# --------------------------------------------------------------------------- #
# 配置 → job
# --------------------------------------------------------------------------- #


async def test_no_cron_configured_means_no_collect_jobs(tmp_path: Path) -> None:
    """没配 cron 时**一条采集 job 都不该有**。

    默认值是 None 而不是"每天 08:00"：会自动往外发采集请求的服务不是贴心，
    是在人没同意的情况下动他的账号配额。
    """
    state = await _state(tmp_path, platforms_yaml=_BOTH_ON, scheduler_block="enabled: true")
    try:
        assert _ids(_register(state)) == []
    finally:
        await _close(state)


async def test_the_cron_lands_one_job_per_enabled_platform(tmp_path: Path) -> None:
    state = await _state(tmp_path, platforms_yaml=_BOTH_ON, scheduler_block=CRON_0800)
    try:
        scheduled = {str(job.id): job for job in _register(state).get_jobs()}
        # 两家都有已实现的 `<name>_collect`。这一条要的是：该排的都排上、id 带前缀、
        # 触发器与并发参数不是随手填的。
        assert sorted(scheduled) == ["collect:bilibili", "collect:douyin"]
        job = scheduled["collect:douyin"]
        assert type(job.trigger).__name__ == "CronTrigger", f"不是 cron 触发器：{job.trigger!r}"
        assert job.max_instances == 1, "同一个博主集不允许被两条 job 同时扫"
        assert job.coalesce is True, "错过几次并成一次，不要补跑一串"
    finally:
        await _close(state)


async def test_the_platform_list_defaults_to_every_enabled_platform(tmp_path: Path) -> None:
    """没写 `collect_platforms` = 所有启用的平台；关着的平台自然不在里面。"""
    state = await _state(tmp_path, platforms_yaml=_BILI_OFF, scheduler_block=CRON_0800)
    try:
        assert _ids(_register(state)) == ["collect:douyin"]
        assert collect_job_platforms(state) == ["douyin"]
    finally:
        await _close(state)


async def test_a_disabled_platform_is_not_scheduled_but_says_so(tmp_path: Path) -> None:
    """名单里写了 B站 而 B站 是关的 → 不排，**并且要说话**。

    不排是既有语义（关掉平台的任务从 `/api/tasks` 就消失了，两处必须同一个口径）；
    要留那条 warning，是因为"配置里有、实际上没排"这件事从配置本身看不出来 —— 症状是
    "那个平台的博主永远不更新，而配置看着完全正常"。
    用 `capture_logs` 而不是 `caplog`：本仓库没有一条用例走 stdio handler 那条路
    （`tests/unit/test_logging_setup.py` 整份文件就是在处理它）。
    """
    block = f'{CRON_0800}\ncollect_platforms: ["douyin", "bilibili"]'
    state = await _state(tmp_path, platforms_yaml=_BILI_OFF, scheduler_block=block)
    try:
        with capture_logs() as events:
            assert collect_job_platforms(state) == ["douyin"]
        warned = [item for item in events if item.get("event") == "jobs.collect_platform_disabled"]
        assert warned and warned[0]["skipped"] == ["bilibili"], events
        assert _ids(_register(state)) == ["collect:douyin"]
    finally:
        await _close(state)


# --------------------------------------------------------------------------- #
# 配置校验：两层都要拦，各有各的漏法
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "block",
    [
        'collect_cron: "0 8 * *"',  # 四段
        'collect_cron: "每天八点"',  # 一段都不是
        'collect_cron: "0 8 * * %?"',  # 字符集不认识（`?` 合法，`%` 不是）
        'collect_cron: "0 8 * * *"\ncollect_platforms: ["douin"]',  # 平台名拼错
    ],
    ids=["四段", "非 cron", "怪字符", "平台名拼错"],
)
def test_the_config_layer_rejects_it_before_the_service_starts(tmp_path: Path, block: str) -> None:
    """红在 `load()`，不是启动后一条 warning。

    异常类型是 pydantic 的 `ValidationError` 而不是 `ConfigError` —— 与这一段配置里
    每个既有字段一致（实测：`health_check_interval_seconds: 5` 与
    `task_timeout_seconds` 写错 TaskKind 都抛 `ValidationError`）。
    要不要把 app.yaml 的校验失败统一包成 `ConfigError` 是整段配置的事，不在这一格改。
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platforms.yaml").write_text(_DOUYIN_ON, encoding="utf-8")
    (config_dir / "app.yaml").write_text(_app_yaml(f"enabled: true\n{block}"), encoding="utf-8")
    with pytest.raises(ValidationError):
        ConfigManager(config_dir).load()


async def test_a_shape_valid_but_out_of_range_cron_still_fails_at_startup(tmp_path: Path) -> None:
    """`99 99 * * *` 过了形状校验（五段、字符都认识），但 APScheduler 拒绝。

    这一条钉"两层校验谁都不许省"：只做形状校验会放过它，症状是每天到点什么都不发生；
    只靠 `from_crontab` 则要把 apscheduler 拖进 `core/config.py` ——
    一个可选依赖不该决定"这份配置对不对"。
    """
    block = 'enabled: true\ncollect_cron: "99 99 * * *"'
    state = await _state(tmp_path, platforms_yaml=_DOUYIN_ON, scheduler_block=block)
    try:
        with pytest.raises(ConfigError, match="解析不了"):
            _register(state)
    finally:
        await _close(state)


# --------------------------------------------------------------------------- #
# 真的响一次
# --------------------------------------------------------------------------- #


async def test_the_cron_job_actually_fires_and_submits_the_task(tmp_path: Path) -> None:
    """端到端：真 APScheduler + 真 `TaskScheduler.submit`，不 mock 时钟。

    用秒级 `CronTrigger(second="*/1")` 代替 `"* * * * *"`（cron 的最小粒度是一分钟，
    等 60 秒不值当），而**除表达式之外**的每份代码都是同一份：trigger 解析、job 注册、
    executor 派发、回调 → `submit` → `task_runs` 落行。注册的是**从配置里拿出来的那条
    job 自己的 `func`**，不另写替身 —— 否则后半截又没人跑。

    这一条还抓到一个真 bug：回调第一版是同步 `def`，而 `AsyncIOExecutor` 会把非协程的
    job 丢进默认线程池，那里没有 running loop，`submit` 里的 `create_task` 当场炸 ——
    线上表现是"定时器响了、日志一条 error、任务永远不出现"。改成 `async def` 才对。
    """
    state = await _state(
        tmp_path,
        platforms_yaml=_DOUYIN_ON,
        scheduler_block=f"{CRON_0800}\ncollect_limit: 3",
    )
    try:
        job = {str(item.id): item for item in _register(state).get_jobs()}["collect:douyin"]
        live = AsyncIOScheduler(timezone=state.config.scheduler.timezone)
        live.add_job(
            job.func,
            CronTrigger(second="*/1", timezone=state.config.scheduler.timezone),
            id="fire-once",
            coalesce=True,
            max_instances=1,
        )
        live.start()
        fired = None
        try:
            for _ in range(80):
                await asyncio.sleep(0.1)
                runs = await state.storage.task_runs.list_recent(limit=10)
                fired = next((run for run in runs if run.task_name == "douyin_collect"), None)
                if fired is not None:
                    break
        finally:
            live.shutdown(wait=False)

        assert fired is not None, "到点了，但没有任何任务被提交"
        assert '"limit": 3' in fired.params_json, f"collect_limit 没进任务参数：{fired.params_json}"
    finally:
        await _close(state)
