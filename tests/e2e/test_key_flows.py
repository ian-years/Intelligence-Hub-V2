"""关键流程的端到端一遍（T5.7）：真 Chromium + 真 uvicorn 线程 + 真装配，只有外世界是替身。

覆盖的是 jsdom 量不到的那一类：SSE 从服务线程推到浏览器、`data-theme` 换下去之后 computed
style 到底变了没有、草稿式设置页**按没按保存**在刷新之后看得见、工坊那条"改字→落库→重开还在"。

地址一律写成 `/#/tasks`：`main.tsx` 用的是 `HashRouter`，所以没有服务端深链接这回事
（`/tasks` 会安静地停在总览页，然后每条用例都因为"找不到那个按钮"而红）。

用例全是 async —— 不是为了好看：浏览器必须跑在 pytest-asyncio 那个 loop 上。
`sync_playwright` 会在主线程再引入一个 loop 主人并留下脏的"当前 running loop"，
把同一进程里后面的每条 async 用例都炸掉（机制与实测数字写在 `conftest.py::page`）。

**没覆盖**的写在这里，不留成"看起来全绿"：
1. **真平台采集 / 真 ASR / 真下载** → `-m real_network` 那一档（要 cookie、要桥、要网）。
   这里适配器全是 `tests/unit/tasks/conftest.py` 的 `FakeAdapter`，一条外网请求都不发。
2. **采集 → 详情那条长链**：需要先有一个博主行，而出网之外的入库路子只有 `add_creator` 任务
   （假适配器那条跑不通）。下一步第一步：给 `conftest._stub_adapter` 补 `resolve_profile`
   / `resolve_share_url`（`FakeAdapter` 已有这两个入参位）。这几段今天各有看护：
   采集入库 `tests/unit/tasks/test_collect.py` + `tests/integration/test_task_flow.py`，
   媒体与 Range `tests/integration/test_api_media.py`，播放器与时间轴
   `pages/video-detail.spec.tsx`。
3. **报告**：T5.4 那三份 V1 生成器搬进 `ported/` 后没有 V2 界面（只有 `tools/render_reports.py`
   那层薄壳）。计划里"看板→报告"那条在 V2 今天对应的就是看板。
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

pytestmark = pytest.mark.e2e


def _card(page: Page, text: str) -> Locator:
    """按卡内文本定位一张 `HardShadowCard`（任务页一张卡一个任务名）。"""
    return page.locator(".memphis-card").filter(has_text=text).first


async def _run(page: Page, task_name: str) -> None:
    """在任务页点某个任务的「跑一次」，等到那句"已排队"出现。"""
    await _card(page, task_name).get_by_role("button", name="跑一次").click()
    await page.get_by_text("已排队").first.wait_for(state="visible", timeout=15000)


async def _wait_row_out_of_running(page: Page, task_name: str) -> str:
    """等运行历史里那一行离开 running，返回它最终的状态。

    判据是 `data-status`（`TaskTimeline` 每行都带），不是文案：状态标签是中文，
    改文案不该改坏这条用例；也不是"睡三秒"：sleep 出来的绿在慢机器上是运气。
    """
    await page.wait_for_function(
        """(name) => {
          const rows = [...document.querySelectorAll('[data-run-id]')].filter(
            (row) => (row.textContent ?? '').includes(name));
          return rows.length > 0 && rows[0].getAttribute('data-status') !== 'running';
        }""",
        arg=task_name,
        timeout=45000,
    )
    row = page.locator("[data-run-id]").filter(has_text=task_name).first
    status = await row.get_attribute("data-status")
    assert status is not None
    return status


# --------------------------------------------------------------------------- #
# 看板与预检（ADR-0022 那条链，走真 SSE）
# --------------------------------------------------------------------------- #


async def test_dashboard_lists_the_platforms_and_draws_no_light(page: Page) -> None:
    """从没预检过时总览页**不画那一行**，而不是画一个"未知"的灰圈。

    "没探过"与"探到 unknown"是两件事（ADR-0022 第三条判据），界面上也必须两样。
    """
    await page.goto("/#/")
    await page.locator("body").get_by_text("douyin").first.wait_for(state="visible", timeout=15000)
    assert await page.get_by_text(re.compile("上次探测")).count() == 0


async def test_a_preflight_run_streams_events_and_turns_the_light(page: Page) -> None:
    """点一次预检 → 运行历史自己走到终态 → 重载后总览页的灯带着时刻出现。

    三段各有归属（preflight 写镜像 / SSE 推进浏览器 / 接口把镜像带出去），
    这一条量的是它们**接起来通**。预检在这台机器上是失败的（没有桥、没有 ASR 权重），
    失败也要写、也要看得见 —— "跑失败了但界面一片空白"才是这条要挡的。
    """
    await page.goto("/#/tasks")
    await _run(page, "preflight")
    assert await _wait_row_out_of_running(page, "preflight") in {
        "success",
        "partial",
        "failed",
        "timeout",
        "cancelled",
    }

    # 整页重载 = 一次干净的读：只改 hash 的话 `/api/platforms` 那份查询还在
    # 5 秒 staleTime 里，会把预检之前的结果再喂一次，那时红的是缓存不是库。
    await page.goto("/#/")
    await page.reload()
    probe_line = page.get_by_text(re.compile("上次探测"))
    await probe_line.first.wait_for(state="visible", timeout=30000)
    # 灯必须带时刻：只有颜色没有"什么时候查的"，就是 §7.20 那个形状的灰灯。
    assert re.search(r"上次探测\s*\S+", await probe_line.first.inner_text())


# --------------------------------------------------------------------------- #
# 工坊页（T4.7）：改字 → 自动保存 → 重载之后内容还在
# --------------------------------------------------------------------------- #


async def test_the_workshop_autosaves_and_the_text_survives_a_reload(page: Page) -> None:
    """工坊的自动保存在真浏览器里闭合：打字 → 落库 → **重载之后稿子还在**。

    "落库了"这件事不看界面的绿色标签信 —— 那可以只改 DOM。看的是重新拉一次
    `/api/drafts` 之后标题还在列表里，那就是数据库里真有。
    """
    await page.goto("/#/workshop")
    title = page.get_by_label("标题")
    body = page.get_by_label("正文")
    await title.fill("端到端这条稿子")
    await body.fill("第一格\n第二格")
    # 1.6 秒的 debounce + 一次往返；轮询等"已保存"，不等固定秒数
    await page.wait_for_function("() => document.body.innerText.includes('已保存')", timeout=20000)

    await page.reload()
    # 用 wait_for 而不是 is_visible()：后者是"此刻看一眼"，而草稿列表要等
    # `/api/drafts` 回来才画 —— 一次性判定就是这条用例的闪断来源（第一次全跑就红在它）。
    await page.get_by_text("端到端这条稿子").first.wait_for(state="visible", timeout=20000)


async def test_the_workshop_shots_cell_names_the_input_that_is_missing(page: Page) -> None:
    """没挑对标作品时，截图包那一格说的是"缺什么"，不是空网格、也不是一个能按的按钮。

    空网格会被读成"功能坏了"，而事实是这一格的输入（成片）不在这条作品上 ——
    这一句区别就是 `AGENTS.md §1.3` 在前端的落点。
    按钮**不该存在**也是断言的一部分：按下去只会得到一次注定失败的往返
    （判据在 `lib/workshop.ts::canRequestShots`，那条的组件级看护在 `workshop.spec.tsx`）。
    """
    await page.goto("/#/workshop")
    await page.get_by_role("button", name="截图包").click()
    await page.get_by_text(re.compile("没有落地成片")).first.wait_for(
        state="visible", timeout=15000
    )
    assert await page.get_by_role("button", name="生成截图包").count() == 0


# --------------------------------------------------------------------------- #
# 一条从"库里什么都没有"开始的长链：收录 → 采集 → 工坊挑它 → 截图包真的出图
# --------------------------------------------------------------------------- #


@pytest.fixture
def collected_video(api: httpx.Client) -> dict[str, Any]:
    """先真跑一遍"收录博主 → 采集入库"，交回那条**带成片**的作品行。

    做成 fixture 而不是写进用例，有两层理由：
    ① `api` 是**同步**客户端（它存在的理由就是"先塞数据、再开浏览器"），
      在 async 用例里跑阻塞 HTTP 正是 ruff `ASYNC212` 要挡的那个形状；
    ② 这段前置与"截图包"无关，混在一起会让那条用例读起来像在测任务链。
    """
    accepted = api.post(
        "/creators", json={"url": "https://douyin.com/user/c-e2e", "platform": "douyin"}
    )
    assert accepted.status_code == 202, accepted.text
    added = _wait_run(api, accepted.json()["task_id"])
    assert added["run"]["status"] == "success", _failures_of(added)

    accepted = api.post("/tasks/douyin_collect/run", json={})
    assert accepted.status_code == 202, accepted.text
    collected = _wait_run(api, accepted.json()["task_id"])
    assert collected["run"]["status"] == "success", _failures_of(collected)

    videos = api.get("/videos", params={"size": 8}).json()["items"]
    with_media = [video for video in videos if video.get("media_path")]
    assert with_media, f"采集跑完了却没有一条带成片：{videos}"
    return with_media[0]


async def test_a_collected_video_gets_a_shot_pack_the_browser_actually_renders(
    page: Page, collected_video: dict[str, Any]
) -> None:
    """采集入库 → 工坊里选中它 → 点「生成截图包」→ 浏览器里长出**解得开**的帧。

    前置（`collected_video`）是 L6 唯一一段真跑任务链的地方（本文件 docstring 之前
    把这条链列为"下一步第一步"）：收录与采集都是真任务、真入库、真落盘。
    整段只有两处替身，各自都写清了：
    ① 平台适配器是 `conftest._stub_adapter`（一条外网请求都不发）；
    ② ffmpeg 是 tmp 里现写的假二进制（见 `conftest._write_fake_ffmpeg` 那段）——
      进程真起、字节真写进媒体树、图真由 Chromium 取并解码，
      但**"画面是不是那一秒的内容"这条用例回答不了**，那要真 ffmpeg。

    断的是 `naturalWidth > 0`，不是"DOM 里有个 img"：后者对一个 404 的 src 也成立
    （元素在，只是坏了），而这一格要验的恰恰是"端点出的字节能被图片解码器吃掉"。
    """
    await page.goto("/#/workshop")
    title = str(collected_video["title"])
    await page.get_by_role("button", name=title).first.wait_for(state="visible", timeout=15000)
    await page.get_by_role("button", name=title).first.click()
    await page.get_by_label("正文").fill("第一格：开场\n第二格：方法\n第三格：结尾")
    await page.get_by_role("button", name="截图包").click()

    await page.get_by_role("button", name="生成截图包").wait_for(state="visible", timeout=15000)
    await page.get_by_role("button", name="生成截图包").click()

    grid = page.get_by_role("list", name="截图包")
    await grid.first.wait_for(state="visible", timeout=30000)
    images = grid.locator("img")
    # `loading="lazy"` 的图不滚进视口就永远不 fetch，所以先逐张带进视口再等解码。
    for index in range(await images.count()):
        await images.nth(index).scroll_into_view_if_needed()
    await page.wait_for_function(
        """() => {
          const imgs = [...document.querySelectorAll('[aria-label="截图包"] img')];
          return imgs.length > 0 && imgs.every((i) => i.complete && i.naturalWidth > 0);
        }""",
        timeout=30000,
    )
    # 格数与时间的对应关系是前端的判据（`shotTimesOf`），这里只看它闭合：
    # 正文三格 → 三张，且每张都带着"第几秒"的 alt（不是匿名缩略图）。
    assert await images.count() == 3, await grid.first.inner_text()
    alts = await grid.locator("img").evaluate_all("els => els.map(e => e.alt)")
    assert alts == ["第 0 秒那一帧", "第 4 秒那一帧", "第 8 秒那一帧"], alts


def _failures_of(detail: dict[str, Any]) -> str:
    """把那次运行的失败原文抽出来当断言消息：红了要能一眼看见原因，不是看见一个 dict。"""
    manifest = detail.get("manifest") or {}
    return json.dumps(
        {"status": detail["run"].get("status"), "failures": manifest.get("failures", [])[:3]},
        ensure_ascii=False,
    )


def _wait_run(api: httpx.Client, task_id: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
    """轮询到那次运行离开 running，交回那一行的档案。走真 HTTP，不碰 repository。"""
    # 档案的形状是 `RunDetail = {run: TaskRunRecord, manifest: ...}`（`RunDetail` 自己
    # 没有 status 那一格）：状态在 `run` 里，摘要在 `manifest` 里。
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        got = api.get(f"/tasks/runs/{task_id}")
        # 404 是"还没写那一行"，不是终态：`submit` 先返回 task_id，那一行由 runner 起。
        # 把它当答案返回的话，调用方拿到的是 `{"detail": ...}`，症状是 KeyError 而不是超时。
        if got.status_code == 404:
            time.sleep(0.2)
            continue
        got.raise_for_status()
        detail = got.json()
        if str(detail["run"].get("status")) not in {"running", "queued", "pending"}:
            return detail
        time.sleep(0.2)
    pytest.fail(f"任务 {task_id} 在 {timeout_seconds} 秒内没到终态")
    return {}  # pragma: no cover


# --------------------------------------------------------------------------- #
# 设置页与主题
# --------------------------------------------------------------------------- #


async def test_a_platform_setting_needs_save_to_survive_a_reload(page: Page) -> None:
    """改一格 → 刷新回原样；再改 + 点保存 → 刷新才留得住。

    两头都验：设置页是草稿式的，只点开关不点保存，改动根本没发出去。少了中间那步
    对照，这条测不出"到底是 PUT 成了还是页面把草稿留着了"。
    磁盘上那份 YAML 的内容由 `test_api_config` 逐字钉。

    定位用的是 `#f-enabled` 而不是"页面上第一个 switch"：总闸那一格（ADR-0025）现在
    排在表单**上面**，而它是**点了就写盘**的、没有草稿这一步。用 `.first` 的话
    这条用例会在点上总闸的那一刻起测的就是另一个功能 —— 而且失败方式看着像
    "草稿没退回初值"，完全指不到真正的成因。
    """
    await page.goto("/#/settings")
    toggle = page.locator("#f-enabled")
    await toggle.wait_for(state="visible", timeout=15000)
    assert await toggle.get_attribute("role") == "switch", "这一格不是那个开关：定位漂了"
    before = await toggle.get_attribute("data-on")
    assert before is not None, "开关没渲染出 data-on，这条会在空转"

    await toggle.click()
    await page.reload()
    untouched = page.locator("#f-enabled")
    await untouched.wait_for(state="visible", timeout=15000)
    assert await untouched.get_attribute("data-on") == before, "没点保存就该回到草稿前"

    await untouched.click()
    await page.get_by_role("button", name="保存").first.click()
    await page.wait_for_function(
        """(before) => {
          const now = document.querySelector('#f-enabled');
          return now !== null && now.dataset.on !== before
            && !document.body.innerText.includes('未保存的改动');
        }""",
        arg=before,
        timeout=20000,
    )

    await page.reload()
    again = page.locator("#f-enabled")
    await again.wait_for(state="visible", timeout=15000)
    assert await again.get_attribute("data-on") != before, "写盘没生效：刷新之后回了初值"


@pytest.fixture
def platform_rows(api: httpx.Client) -> list[dict[str, Any]]:
    """`/api/platforms` 那几行 —— 总闸那条用例要用它推"该有几行"，不写死 4。

    做成 fixture 而不是在用例里 `api.get`：`api` 是**同步**客户端，
    在 async 用例里发阻塞请求正是 ruff `ASYNC212` 要挡的形状
    （`collected_video` 那份 docstring 讲的是同一条）。

    这里也是那条用例的前置：e2e 那份 `platforms.yaml` 只配了两家且都开着，
    所以"总闸关掉之后每一行都该说被总闸盖住"有一句现成的锚。
    """
    body = api.get("/platforms").json()
    assert body["master_enabled"] is True, "这份夹具该从「总闸开着」开始"
    rows: list[dict[str, Any]] = body["platforms"]
    assert len(rows) >= 2, rows
    assert all(row["availability"] == "available" for row in rows), rows
    return rows


async def test_the_master_switch_gates_the_whole_ui_without_a_restart(
    page: Page, platform_rows: list[dict[str, Any]]
) -> None:
    """浏览器里翻总闸：不重启就改掉任务页的按钮、四行的说法，翻回来各家回到原位。

    这一段是 L6 独有的：集成测试那边 `state.apscheduler is None`，而前端"翻转之后
    三份缓存各失效各的"这件事只有真浏览器看得见 —— 少 invalidate 一个 query key 的症状
    正是"设置页显示总闸已开，任务列表还是空的"。

    行数从 `platform_rows` 推而不是写死 4：写死的数字在有人补 e2e 夹具时会反着红一次
    （看起来像实现坏了）。
    """
    names = [row["name"] for row in platform_rows]

    # 先把第一家单独关掉：总闸翻回来之后它**必须还是关着的**（AND 语义的全部理由）
    await page.goto("/#/settings")
    first_toggle = page.locator("#f-enabled")
    await first_toggle.wait_for(state="visible", timeout=15000)
    assert await first_toggle.get_attribute("data-on") == "true", "默认选中的不是第一家"
    await first_toggle.click()
    await page.get_by_role("button", name="保存").first.click()
    await page.get_by_text("已写盘并热加载").wait_for(state="visible", timeout=20000)

    master = page.get_by_role("switch", name="平台总闸")
    await master.wait_for(state="visible", timeout=15000)
    assert await master.get_attribute("data-on") == "true"

    await master.click()
    rows = page.get_by_role("list", name="各家当前状态")
    await rows.wait_for(state="visible", timeout=15000)
    await page.wait_for_function(
        """(expected) => {
          const list = document.querySelector('[aria-label="各家当前状态"]');
          if (!list) return false;
          const text = list.innerText;
          return (
            (text.match(/被总闸关着/g) ?? []).length === expected &&
            !text.includes('已关闭')
          );
        }""",
        arg=len(names),
        timeout=20000,
    )
    # 「已关闭」一次都不该出现：每一行此刻都是"被总闸盖住"，
    # 而那句谎（把 master_off 说成"这一家关了"）正是这一整块改动要挡的东西。

    await page.goto("/#/tasks")
    await page.get_by_text("preflight").first.wait_for(state="visible", timeout=15000)
    for name in names:
        gone = f"{name}_collect"
        assert await page.get_by_text(gone).count() == 0, f"总闸关着，{gone} 的卡片还在"
    # 跨平台那几个不属于任何一家，必须还在（否则"关掉采集"就变成了"整个系统不能用"）
    for here in ("preflight", "single_link", "postprocess", "enrich_metrics"):
        assert await page.get_by_text(here).count() > 0, f"跨平台任务 {here} 被总闸带走了"

    # 翻回来：写盘是真的（整页重载之后还开着），而抖音**回到它自己那个关着的状态**
    await page.goto("/#/settings")
    await master.click()
    await rows.wait_for(state="visible", timeout=15000)
    await page.wait_for_function(
        """() => {
          const list = document.querySelector('[aria-label="各家当前状态"]');
          return list !== null && list.innerText.includes('已关闭')
            && !list.innerText.includes('被总闸关着');
        }""",
        timeout=20000,
    )
    await page.reload()
    await master.wait_for(state="visible", timeout=15000)
    assert await master.get_attribute("data-on") == "true", "总闸没写住：刷新之后又关了"
    assert await page.locator("#f-enabled").get_attribute("data-on") == "false", (
        "打开总闸把第一家自己那个开关也复原了 —— 那正是「批量写四次」的那种实现"
    )
    # 重载之后从**浏览器**里逐行读这份名单（不再走同步 api 客户端）：
    # 第一家该是"已关闭"（它自己关的，总闸翻回来不会替它开），其余该是"已启用"。
    listbox = page.get_by_role("list", name="各家当前状态")
    for index, name in enumerate(names):
        line = await listbox.locator("li").filter(has_text=name).first.inner_text()
        want = "已关闭" if index == 0 else "已启用"
        assert want in line, (name, want, line)
        assert "被总闸关着" not in line, (name, line)


async def test_dark_mode_changes_the_computed_page_background(page: Page) -> None:
    """切暗色：`data-theme` 是 dark，且**真浏览器算出来的**页面底色是那个令牌值。

    补的是 Vitest 量不到的那一半 —— jsdom 不加载样式表，自定义属性与层叠它都不算
    （ADR-0023 那组对比度是算出来的，不是量屏的）。这里量的就是量屏。
    读 `<html>` 不读 `<body>`：底色写在 `html` 上，body 透明，读它会拿到
    `rgba(0, 0, 0, 0)` 而什么都证明不了。
    """
    probe = "getComputedStyle(document.documentElement).backgroundColor"
    await page.goto("/#/")
    light = await page.evaluate(probe)
    assert light == "rgb(250, 247, 242)", light

    await page.get_by_role("button", name="暗").click()
    await page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    assert await page.evaluate(probe) == "rgb(26, 26, 26)", "暗色没真的换下去"

    await page.get_by_role("button", name="亮").click()
    await page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    assert await page.evaluate(probe) == light, "切回去没生效：那叫一次性变暗，不叫切换"


# --------------------------------------------------------------------------- #
# 静态挂载与说实话的界面
# --------------------------------------------------------------------------- #


async def test_the_single_port_serves_the_built_app(page: Page) -> None:
    """单端口：产物由 FastAPI 自己 serve（`make build` + 起后端就是生产形状）。

    标题是从 `index.html` 真读回来的，挡的是"静态挂载没挂上、而 404 页恰好也长这样"。
    """
    await page.goto("/#/")
    assert "Intelligence" in await page.title(), await page.title()
    assert await page.locator("#root").count() == 1


async def test_an_unparsable_video_id_says_so_instead_of_blanking(page: Page) -> None:
    """`/#/video/not-an-id` 要说"这个地址不是作品 id"。

    路由层已有集成用例，这一条要的是**渲染结果**：那句实话真的出现在屏幕上，
    而不是 React 抛了个白屏（白屏 + 控制台干净同时成立时最难查）。
    """
    await page.goto("/#/video/not-an-id")
    await page.get_by_text(re.compile("不是一个作品 id")).first.wait_for(
        state="visible", timeout=15000
    )


async def test_the_feed_says_its_empty_rather_than_its_broken(page: Page) -> None:
    """空库 + 后端活着：作品流说的是"没有内容"，不是"读不到"。

    这两句在 `AGENTS.md §1.3` 那头是同一类错误的两个方向，而在界面上样式几乎一样。
    """
    await page.goto("/#/feed")
    text = await page.locator("main").inner_text()
    assert re.search(r"没有|空", text), text[:400]
    assert "读不到" not in text
