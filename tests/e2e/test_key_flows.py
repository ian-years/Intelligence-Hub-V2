"""关键流程的端到端一遍（T5.7 第一片）：起真服务 + 真 Chromium，验跨页与跨进程边界。

覆盖的是 jsdom 量不到的那一类：SSE 从服务线程推到浏览器、`data-theme` 换下去之后
computed style 到底变没变、草稿式设置页**按没按保存**的差别在刷新后看得见。

地址一律写成 `/#/tasks` 这种**哈希路由**形状：`main.tsx` 用的是 `HashRouter`，
所以那份静态产物不需要任何服务端 fallback（深链接本来就不发给服务器）。
写 `/tasks` 会安静地停在总览页，然后每条用例都因为"找不到那个按钮"而红。

这一片**没覆盖**的，写清楚，不留成"看起来全绿"：

1. **采集 → 作品流 → 详情 → 转写那一条长链**。它需要一个先存在的博主行，
   而 e2e 里没有不出网、又不靠手搓 SQL 的入库路子：`POST /api/creators` 起的是
   `add_creator` 任务（要跟 302、要拉资料），假适配器那条路在这台上跑不通。
   这三段今天各有看护：采集与入库是 `tests/unit/tasks/test_collect.py` +
   `tests/integration/test_task_flow.py`，媒体与 Range 是
   `tests/integration/test_api_media.py`（19 条），播放器与时间轴是
   `src/pages/video-detail.spec.tsx`。缺的只是"浏览器里那一趟往返"。
   **下一步第一步**（做这条的人照着来）：给 `tests/e2e/conftest.py` 的 `_stub_adapter`
   补上 `resolve_profile` / `resolve_share_url` 两个方法（`FakeAdapter` 已经有
   `profile` / `ref` 这两个入参位），让 `add_creator` 在假适配器下能跑成，
   然后 `api.post("/creators", …)` → 等终态 → 再点采集。
2. **真平台采集 / 真 ASR / 真下载**：`-m real_network` 那一档（要 cookie、要桥、要网）。
   这里的适配器全换成 `tests/unit/tasks/conftest.py` 的 `FakeAdapter`，一条外网请求都不发。
3. **报告**：T5.4 那三份 V1 生成器搬进 `ported/` 后没有 V2 界面，只有
   `tools/render_reports.py` 那层薄壳。计划这一句里的"报告"在 V2 今天对应看板，那条在下面。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.e2e


def _card(page: Page, text: str) -> Locator:
    """按卡内文本定位一张 `HardShadowCard`（任务页一张卡一个任务名）。"""
    return page.locator(".memphis-card").filter(has_text=text).first


def _run(page: Page, task_name: str) -> None:
    """在任务页点某个任务的「跑一次」，等到那句"已排队"出现。"""
    _card(page, task_name).get_by_role("button", name="跑一次").click()
    page.get_by_text("已排队").first.wait_for(state="visible", timeout=15000)


def test_dashboard_lists_the_platforms_and_draws_no_light(page: Page) -> None:
    """从没预检过时总览页**不画那一行**，而不是画一个"未知"的灰圈。

    "没探过"与"探到 unknown"是两件事，界面上也必须两样（ADR-0022 第三条判据）。
    """
    page.goto("/#/")
    page.locator("body").get_by_text("douyin").first.wait_for(state="visible", timeout=15000)
    page.locator("body").get_by_text("bilibili").first.wait_for(state="visible")
    assert page.get_by_text(re.compile("上次探测")).count() == 0


def test_a_preflight_run_streams_events_and_turns_the_light(page: Page) -> None:
    """点一次预检 → 实时事件流里看得见 → 换一页读到同一份结论。

    三段各有归属，这条把它们接起来量：preflight 写 `platforms` 镜像（ADR-0022）→
    SSE 把任务事件推到浏览器（不刷新、不轮询）→ `/api/platforms` 把同一份值给另一个页面。
    预检任务是**失败**的（这台机器上没有桥、没有 ASR 权重），但结论照样要写、要看得见 ——
    "跑失败了但界面一片空白"才是这一条要挡的。
    """
    page.goto("/#/tasks")
    _run(page, "preflight")

    # 运行历史那一行走到终态 = 这一趟预检真的做完了（含写镜像那一步）。
    # 只看"实时事件里出现过什么"不够：事件流里有 TASK_STARTED 也算"看得见"。
    row = page.locator("[data-run-id]").filter(has_text="preflight").first
    row.wait_for(state="visible", timeout=30000)
    page.wait_for_function(
        """() => {
          const rows = [...document.querySelectorAll('[data-run-id]')].filter(
            (row) => (row.textContent ?? '').includes('preflight'));
          return rows.length > 0 && rows[0].getAttribute('data-status') !== 'running';
        }""",
        timeout=45000,
    )
    assert row.get_attribute("data-status") in {
        "success",
        "partial",
        "failed",
        "timeout",
        "cancelled",
    }
    # 这一栏不刷新就永远停在"正在跑"是 `useRuns` 的轮询在管（下一条用例专钉它），
    # 所以这里能等到终态本身也是一条证据。

    # 换页要看的是"库里真有其值、接口真把它带出来"，所以先跳到总览、再强制一次整页加载：
    # 哈希路由下只改 hash 是**同一份文档内的跳转**（`page.reload()` 才是干净重读，
    # 少了它 `/api/platforms` 那份查询还在 5 秒 staleTime 里，会把预检之前的结果再喂一次 ——
    # 那时红的是缓存，不是库）。第一版这条就是因为只 reload 在任务页上找"上次探测"而红。
    page.goto("/#/")
    page.reload()
    probe_line = page.get_by_text(re.compile("上次探测"))
    probe_line.first.wait_for(state="visible", timeout=30000)
    # 灯必须带时刻：只有颜色没有"什么时候查的"，就是 §7.20 那个形状的灰灯。
    assert re.search(r"上次探测\s*\S+", probe_line.first.inner_text())


def test_a_run_row_leaves_running_without_a_page_reload(page: Page) -> None:
    """运行历史那一行要自己走出 running。

    这条是 e2e **发现出来的缺陷**补上的看护（不是先有测试再有的实现）：
    `useRuns` 以前只在"提交成功"那一刻 invalidate 一次，于是实时事件在滚、
    运行历史却永远停在"正在跑"，要刷新页面才认账。判据写成" eventually 不再 running"，
    不断具体是哪一格终态 —— 预检在这台机器上就是会失败，那不影响这一条要问的事。
    """
    page.goto("/#/tasks")
    _run(page, "preflight")
    row = page.locator("[data-run-id]").filter(has_text="preflight").first
    row.wait_for(state="visible", timeout=30000)
    page.wait_for_function(
        """() => {
          const rows = [...document.querySelectorAll('[data-run-id]')].filter(
            (row) => (row.textContent ?? '').includes('preflight'));
          return rows.length > 0 && rows[0].getAttribute('data-status') !== 'running';
        }""",
        timeout=45000,
    )
    assert row.get_attribute("data-status") in {
        "success",
        "partial",
        "failed",
        "timeout",
        "cancelled",
    }


def test_a_platform_setting_needs_save_to_survive_a_reload(page: Page) -> None:
    """改一格 → 刷新回原样；再改 + 点保存 → 刷新才留得住。

    两头都验：设置页是草稿式的，只点开关不点保存，改动根本没发出去。
    少了中间那步对照，这条测不出"到底是 PUT 成了还是页面自己把草稿留着了"。
    磁盘上那份 YAML 的内容由 `test_api_config` 逐字钉。
    """
    page.goto("/#/settings")
    toggle = page.get_by_role("switch").first
    toggle.wait_for(state="visible", timeout=15000)
    before = toggle.get_attribute("data-on")
    assert before is not None, "开关没渲染出 data-on，这条会在空转"

    toggle.click()
    page.reload()
    untouched = page.get_by_role("switch").first
    untouched.wait_for(state="visible", timeout=15000)
    assert untouched.get_attribute("data-on") == before, "没点保存就该回到草稿前"

    untouched.click()
    page.get_by_role("button", name="保存").first.click()
    page.wait_for_function(
        """(before) => {
          const now = document.querySelector('[role=switch]');
          return now !== null && now.dataset.on !== before
            && !document.body.innerText.includes('未保存的改动');
        }""",
        arg=before,
        timeout=20000,
    )

    page.reload()
    again = page.get_by_role("switch").first
    again.wait_for(state="visible", timeout=15000)
    assert again.get_attribute("data-on") != before, "写盘没生效：刷新之后回了初值"


def test_dark_mode_changes_the_computed_page_background(page: Page) -> None:
    """切暗色：`data-theme` 是 dark，且**真浏览器算出来的**页面底色是那个令牌值。

    补的是 Vitest 量不到的那一半 —— jsdom 不加载样式表，自定义属性与层叠它都不算
    （ADR-0023 那组对比度是算出来的，不是量屏的）。这里量的就是量屏。
    读 `<html>` 不读 `<body>`：底色写在 `html` 上，body 自己透明，
    读它会拿到 `rgba(0, 0, 0, 0)` 而什么都证明不了。
    """
    probe = "getComputedStyle(document.documentElement).backgroundColor"
    page.goto("/#/")
    light = page.evaluate(probe)
    assert light == "rgb(250, 247, 242)", light

    page.get_by_role("button", name="暗").click()
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    dark = page.evaluate(probe)
    assert dark == "rgb(26, 26, 26)", dark

    page.get_by_role("button", name="亮").click()
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    assert page.evaluate(probe) == light, "切回去没生效：那叫一次性变暗，不叫切换"


def test_the_single_port_serves_the_built_app(page: Page) -> None:
    """单端口：产物由 FastAPI 自己 serve（`make build` + 起后端就是生产形状）。

    标题是从 `index.html` 真读回来的，挡的是"静态挂载没挂上、而 404 页恰好也长这样"。
    """
    page.goto("/#/")
    assert "Intelligence" in page.title(), page.title()
    assert page.locator("#root").count() == 1


def test_an_unparsable_video_id_says_so_instead_of_blanking(page: Page) -> None:
    """错误也要端到端看得见：`/#/video/not-an-id` 要说"这个地址不是作品 id"。

    路由层有集成用例，这一条要的是**渲染结果**：那句实话真的出现在屏幕上，
    而不是 React 抛出一个白屏（白屏与控制台干净同时成立时最难查）。
    """
    page.goto("/#/video/not-an-id")
    page.get_by_text(re.compile("不是一个作品 id")).first.wait_for(state="visible", timeout=15000)


def test_the_feed_says_its_empty_rather_than_its_broken(page: Page) -> None:
    """空库 + 后端活着：作品流说的是"没有内容"，不是"读不到"。

    这两句话在 `AGENTS.md §1.3` 那头是同一类错误的两个方向，
    而在界面上它们的样式几乎一样 —— 所以要在真页面上钉一次。
    """
    page.goto("/#/feed")
    body = page.locator("main").inner_text()
    assert re.search(r"没有|空", body), body[:400]
    assert "读不到" not in body
