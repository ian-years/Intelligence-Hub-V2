"""/api/schedule（T6.3 第二片）：配置写盘、job 当场重排、以及"立即跑一次"走的是同一条路。

这个文件要钉的三件事，每一件都可以只用"返回 200"糊过去，所以断言全部写成**关系**：

- 改了 cron 之后，**盘上那份**、**内存里那份**、**调度器上真排着的 job** 三处必须一致。
  只断言 HTTP 响应等于放弃这条链上唯一值钱的部分 —— 前两处任一没落到，症状都是
  "设置页显示 08:00，实际还按昨天那条跑"。
- 校验不过的配置**一行都不许落盘**。写一半再红，比不写更糟：下一次启动会红在
  `_add_collect_jobs`，而人已经忘了是自己十分钟前点过保存。
- 请求体里**没出现**的键保持原值。这一条挡的是"前端总是提交全表"那种实现，
  它会把别人的字段顺手写一遍。

调度器是**真的** `AsyncIOScheduler`（`start()` 过），不是假对象：`next_run_time` 这个字段
的意义就是"APScheduler 自己认为下次什么时候响"，拿假件返回一个算好的时间戳，
那条断言就成了自证。这里只验注册与下次触发时刻，不验"到点真起任务"—— 那一条在
`test_scheduler.py` 里已经用秒级 cron 真跑过了，两份各管一段。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.core.config import ConfigManager
from intelligence_hub_v2.main import (
    build_components,
    create_app,
    reschedule_collect_jobs,
)
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

pytestmark = pytest.mark.integration

_PLATFORMS = "douyin:\n  enabled: true\nbilibili:\n  enabled: true\n"
_APP_YAML = (
    "scheduler:\n"
    "  enabled: true\n"
    '  collect_cron: "0 8 * * *"\n'
    "  collect_platforms: [douyin]\n"
    "  collect_limit: 5\n"
)


def _write_config(dir_path: Path, app_yaml: str = _APP_YAML) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "platforms.yaml").write_text(_PLATFORMS, encoding="utf-8")
    (dir_path / "app.yaml").write_text(app_yaml, encoding="utf-8")
    return dir_path


async def _state(tmp_path: Path, *, app_yaml: str = _APP_YAML) -> AppState:
    """一份跑起来的 state：**带真 APScheduler**，`start()` 过。

    `state.apscheduler` 平时由 `main._start_background_jobs` 在 lifespan 里装；这里直接建，
    为的是让"排上了没有 / 下次几点"这两栏有真值可断。
    """
    manager = ConfigManager(_write_config(tmp_path / "config", app_yaml))
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
    state.owns_http = False
    # 用**产品代码**排 job，不在测试里复刻一遍 add_job：这一份 fixture 要验的正是
    # "改完配置之后调度器上真有什么"，自己 add_job 就等于把判据换成了"我以为会这样"。
    scheduler = AsyncIOScheduler(timezone=state.config.scheduler.timezone)
    scheduler.start()
    state.apscheduler = scheduler
    reschedule_collect_jobs(state)
    return state


async def _close(state: AppState) -> None:
    """顺序与 `test_scheduler.py` 那份一致：**先停 job，再关 http，最后关库**。

    漏掉 `scheduler.shutdown()` 的话，采集 job 会在库连接已经关闭之后再提交一条 run，
    症状是下一条用例期间冒出来的 `ResourceWarning`（`filterwarnings=["error"]` 让它红，
    但红的是无辜的那条）。
    """
    if state.apscheduler is not None:
        state.apscheduler.shutdown(wait=False)
        state.apscheduler = None
    await state.scheduler.shutdown()
    await state.http.aclose()
    await state.storage.close()


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[tuple[AppState, httpx.AsyncClient]]:
    state = await _state(tmp_path)
    try:
        transport = httpx.ASGITransport(app=create_app(state=state))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield state, ac
    finally:
        await _close(state)


def _scheduler_app_yaml(tmp_path: Path) -> dict[str, object]:
    """盘上那份 app.yaml 里的 scheduler 段（读回来，而不是拿响应当"写进去了"的证据）。"""
    text = (tmp_path / "config" / "app.yaml").read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    section = loaded.get("scheduler")
    return section if isinstance(section, dict) else {}


# --------------------------------------------------------------------------- #
# GET
# --------------------------------------------------------------------------- #


async def test_get_reports_the_config_and_the_live_job_side_by_side(harness, tmp_path) -> None:
    """两份并排：`collect_cron` 来自配置，`jobs[].next_run_time` 来自 APScheduler。"""
    _, ac = harness
    body = (await ac.get("/api/schedule")).json()
    assert body["collect_cron"] == "0 8 * * *"
    assert body["scheduler_running"] is True
    assert [job["platform"] for job in body["jobs"]] == ["douyin"]
    # `next_run_time` 是 APScheduler 算的：调度器 start 过就必须有值，None 说明根本没排上。
    stamp = body["jobs"][0]["next_run_time"]
    assert stamp is not None and datetime.fromisoformat(stamp).tzinfo is not None


async def test_effective_and_skipped_platforms_answer_different_questions(harness) -> None:
    """名单里只有一个平台时，`effective_platforms` 与 `skipped_platforms` 各说什么。

    这一条存在是因为"配置里写了 douyin"与"今天真的会去采 douyin"是两个事实；
    把它们合成一栏的话，被关掉的平台就会从响应里消失，而它正是需要被看见的那一位。
    """
    _, ac = harness
    body = (await ac.get("/api/schedule")).json()
    assert body["effective_platforms"] == ["douyin"]
    assert body["skipped_platforms"] == []


# --------------------------------------------------------------------------- #
# PUT：三处一致
# --------------------------------------------------------------------------- #


async def test_put_cron_lands_in_disk_memory_and_the_live_job(harness, tmp_path) -> None:
    """改 cron 之后：盘上、内存里、调度器上那条 job 的下次触发时刻，三处都得跟着动。

    断言的是**三处互相对得上**，不是"响应里那句新 cron"。响应可以是内存里那份的复读，
    而"内存换了但 job 还是旧的"正是这一片最容易写错、且只有真调度器才暴露得出的一种半套状态。
    """
    state, ac = harness
    before = (await ac.get("/api/schedule")).json()["jobs"][0]["next_run_time"]

    resp = await ac.put("/api/schedule", json={"collect_cron": "30 6 * * 1-5"})
    assert resp.status_code == 200
    after = resp.json()["schedule"]["jobs"][0]["next_run_time"]
    assert after is not None and after != before

    assert state.config.scheduler.collect_cron == "30 6 * * 1-5"
    on_disk = _scheduler_app_yaml(tmp_path)
    assert on_disk["collect_cron"] == "30 6 * * 1-5"
    assert resp.json()["changed_fields"] == ["collect_cron"]
    assert resp.json()["requires_restart"] is False


async def test_put_touches_only_the_keys_the_body_carried(harness, tmp_path) -> None:
    """只发 `collect_limit` 不许把 cron / platforms / timezone 一起写一遍。

    判据是"没提到的键，盘上那份仍然保持原值"，而不是"响应里它们还在" —— 响应可以
    拿内存拼一个看起来正确的答案，而盘上已经被写成默认值。
    """
    _, ac = harness
    body = (await ac.put("/api/schedule", json={"collect_limit": 12})).json()

    on_disk = _scheduler_app_yaml(tmp_path)
    assert on_disk["collect_limit"] == 12
    assert on_disk["collect_cron"] == "0 8 * * *"
    assert on_disk["collect_platforms"] == ["douyin"]
    assert body["changed_fields"] == ["collect_limit"]


async def test_explicit_null_cron_means_turn_it_off_and_removes_the_job(harness, tmp_path) -> None:
    """`collect_cron: null` = 关掉。区别开"没提这个键"与"把它清掉"是这一条的全部内容。

    关掉之后必须**一条采集 job 都不剩**：留下旧 job 就是"设置页说没开，明早照样 08:00 起"。
    """
    state, ac = harness
    body = (await ac.put("/api/schedule", json={"collect_cron": None})).json()

    assert body["schedule"]["collect_cron"] is None
    assert body["schedule"]["jobs"] == []
    assert [job.id for job in state.apscheduler.get_jobs() if job.id.startswith("collect:")] == []
    assert _scheduler_app_yaml(tmp_path)["collect_cron"] is None


async def test_a_cron_that_passes_the_shape_check_but_is_not_valid_writes_nothing(
    harness, tmp_path
) -> None:
    """`99 99 * * *` 是**五个 token**，配置层那关过得去，APScheder 那关过不去。

    这条要的是"当场 422 且盘上一个字节没动"。写进去再红，等于把下一次启动变成
    一个跟"十分钟前点过保存"毫无关系的地方 —— 那是最难查的一类红。
    """
    _, ac = harness
    before = (tmp_path / "config" / "app.yaml").read_text(encoding="utf-8")

    resp = await ac.put("/api/schedule", json={"collect_cron": "99 99 * * *"})
    assert resp.status_code == 422
    assert "collect_cron" in resp.text or "解析不了" in resp.text
    assert (tmp_path / "config" / "app.yaml").read_text(encoding="utf-8") == before


async def test_an_unknown_platform_name_is_rejected_before_the_write(harness, tmp_path) -> None:
    """平台名单走 `SchedulerSection` 的校验：写错名字不许落到盘上。

    与上一条同一个道理 —— 一个不认识的名字如果在启动时才红，症状是"服务起不来"，
    而人会先怀疑是那次改动以外的原因。
    """
    _, ac = harness
    before = (tmp_path / "config" / "app.yaml").read_text(encoding="utf-8")
    resp = await ac.put("/api/schedule", json={"collect_platforms": ["douyin", "douYin"]})
    assert resp.status_code == 422
    assert (tmp_path / "config" / "app.yaml").read_text(encoding="utf-8") == before


async def test_the_other_app_sections_survive_a_scheduler_write(harness, tmp_path) -> None:
    """写 scheduler 段不许顺手把 app.yaml 的**其他段**重建掉。

    判据取的是"顶层段的集合前后一样"+ `data.dir` 那个值原样还在，不是"scheduler 段的键
    前后一样" —— 后者是错的期望：`write_scheduler` 覆盖式写整段（`SchedulerSection` 是
    `extra="forbid"`，段里不可能有我们不认识的键，理由写在那个小节的 docstring 里），
    所以未提到的键会以其**默认值**出现，这是设计而不是丢数据。
    而 `data.dir` 一旦被抹掉，下一次启动会静默回到默认路径，那看起来像"数据丢了"。
    """
    cfg = tmp_path / "config"
    _write_config(cfg)
    (cfg / "app.yaml").write_text(
        _APP_YAML + "\ndata:\n  dir: data-dir-elsewhere\n",
        encoding="utf-8",
    )
    _, ac = harness

    await ac.put("/api/schedule", json={"collect_limit": 3})
    text = (cfg / "app.yaml").read_text(encoding="utf-8")
    assert "data-dir-elsewhere" in text, "写 scheduler 段把 data.dir 抹掉了"

    sections = set(yaml.safe_load(text))
    assert "data" in sections and "scheduler" in sections
    assert _scheduler_app_yaml(tmp_path)["collect_limit"] == 3


async def test_a_scheduler_change_broadcasts_config_changed_as_app_scope(harness) -> None:
    """事件流里要有这一笔，`scope="app"`，字段名带 `scheduler.` 前缀。

    前缀是给人看的：设置页同时订着平台配置与应用配置两类改动，
    没有前缀就分不出"改了 douyin 的开关"与"改了 cron"。
    """
    state, ac = harness
    seen: list[dict] = []
    sub = state.events.subscribe()

    async def _pump() -> None:
        async for event in sub:
            if event.type is EventType.CONFIG_CHANGED:
                seen.append(event.payload)
                return

    pump = asyncio.ensure_future(_pump())
    await ac.put("/api/schedule", json={"collect_limit": 9})
    await asyncio.wait_for(pump, timeout=2.0)
    assert seen and seen[0]["scope"] == "app"
    assert seen[0]["changed_fields"] == ["scheduler.collect_limit"]
    await sub.aclose()


# --------------------------------------------------------------------------- #
# run-now
# --------------------------------------------------------------------------- #


async def test_run_now_submits_the_same_task_the_cron_would(harness) -> None:
    """ "立即跑一次"必须走 `<platform>_collect` 那条**同一个任务名**。

    同名不是命名巧合：那条路才有开关闸门、`requires` 检查、限流信号量、清单终态与事件流。
    给手动触发另开一条路，那条路就会长成绕过闸门的后门。
    """
    _, ac = harness
    resp = await ac.post("/api/schedule/run-now", json={"platform": "douyin"})
    assert resp.status_code == 202
    assert resp.json()["task_name"] == "douyin_collect"
    assert resp.json()["task_id"]

    runs = (await ac.get("/api/tasks/runs")).json()
    names = [run["task_name"] for run in runs]
    assert "douyin_collect" in names


async def test_a_key_also_set_in_the_environment_is_reported_as_shadowed(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env 里设着 `collect_cron` 时，PUT 成功**并且**要说"下一次启动会被 env 盖回去"。

    这一条守的是"保存成功"这四个字的含金量：优先级 `yaml < env` 意味着这个入口写不动
    被环境变量占住的那一格，而今天它确实生效了（内存里就是新值）。只报成功不报遮挡，
    症状是"设置页改完重启又变回去"，没有人会怀疑到一个看不见的环境变量上。
    """
    monkeypatch.setenv("INTELLIGENCE_HUB_SCHEDULER__COLLECT_CRON", "15 3 * * *")
    _, ac = harness
    body = (await ac.put("/api/schedule", json={"collect_cron": "30 6 * * *"})).json()

    assert body["schedule"]["collect_cron"] == "30 6 * * *"  # 现在就生效
    assert body["schedule"]["shadowed_by_env"] == ["collect_cron"]  # 但下次启动不是它


async def test_run_now_with_an_unregistered_platform_is_404(harness) -> None:
    """不存在的平台 = 404 且说得出有哪些。202 之后才发现采不了是骗人。"""
    _, ac = harness
    resp = await ac.post("/api/schedule/run-now", json={"platform": "weibo"})
    assert resp.status_code == 404
    assert "douyin" in resp.text


async def test_run_now_on_a_disabled_platform_does_not_claim_success(harness) -> None:
    """平台被关掉 → 不许 202。

    这条与"关掉平台 → 它的任务从 /api/tasks 消失"是同一条语义的两个入口。
    """
    _, ac = harness
    await ac.put("/api/platforms/bilibili/config", json={"enabled": False})
    resp = await ac.post("/api/schedule/run-now", json={"platform": "bilibili"})
    assert resp.status_code in (409, 422)
