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

import re
from typing import TYPE_CHECKING

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


async def test_the_workshop_shots_view_says_it_has_no_data_source(page: Page) -> None:
    """截图包那一格说的是"没有这一步"，不是空网格。

    空网格会被读成"功能坏了"，而事实是 V2 没有按分镜截图的任务 —— 这一句区别
    就是 `AGENTS.md §1.3` 在前端的落点。
    """
    await page.goto("/#/workshop")
    await page.get_by_role("button", name="截图包").click()
    assert await page.get_by_text(re.compile("V2 没有截图包这一步")).first.is_visible()


# --------------------------------------------------------------------------- #
# 设置页与主题
# --------------------------------------------------------------------------- #


async def test_a_platform_setting_needs_save_to_survive_a_reload(page: Page) -> None:
    """改一格 → 刷新回原样；再改 + 点保存 → 刷新才留得住。

    两头都验：设置页是草稿式的，只点开关不点保存，改动根本没发出去。少了中间那步
    对照，这条测不出"到底是 PUT 成了还是页面把草稿留着了"。
    磁盘上那份 YAML 的内容由 `test_api_config` 逐字钉。
    """
    await page.goto("/#/settings")
    toggle = page.get_by_role("switch").first
    await toggle.wait_for(state="visible", timeout=15000)
    before = await toggle.get_attribute("data-on")
    assert before is not None, "开关没渲染出 data-on，这条会在空转"

    await toggle.click()
    await page.reload()
    untouched = page.get_by_role("switch").first
    await untouched.wait_for(state="visible", timeout=15000)
    assert await untouched.get_attribute("data-on") == before, "没点保存就该回到草稿前"

    await untouched.click()
    await page.get_by_role("button", name="保存").first.click()
    await page.wait_for_function(
        """(before) => {
          const now = document.querySelector('[role=switch]');
          return now !== null && now.dataset.on !== before
            && !document.body.innerText.includes('未保存的改动');
        }""",
        arg=before,
        timeout=20000,
    )

    await page.reload()
    again = page.get_by_role("switch").first
    await again.wait_for(state="visible", timeout=15000)
    assert await again.get_attribute("data-on") != before, "写盘没生效：刷新之后回了初值"


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
