# Spec: Contract Tests（V1 §7 陷阱映射）

> **状态**：Locked（V2.0 起契约稳定，改动需走 ADR）
> **源文件**：`tests/contracts/` + `tests/unit/` + `tests/integration/`
> **相关 ADR**：[0009](../adr/0009-testing-and-devex.md)

V1 `AGENTS.md` §7 那 25 条陷阱在 V2 的归宿。**V3 重写后跑同一套测试全绿 = 行为等价**。

---

## 1. 三类归宿

| 类别 | 数量 | 含义 |
|---|---|---|
| **结构性消除** | 9 | V2 设计让它不可能再发生，不需要测试看护 |
| **契约测试看护** | 16 | 行为保留，测试守住 |
| **V2 新增** | 持续 | 实施过程踩的新坑，追加到 `docs/lessons.md` |

---

## 2. 结构性消除（不需要测试）

| V1 陷阱 | V2 解决方式 | 相关 ADR/Spec |
|---|---|---|
| §7.4 `upsert_video` 整行覆盖 | `VideoRepository.update_fields()` 字段级更新，TypedDict 限定可更新字段集 | ADR-0006 / data-model.md §3 |
| §7.6 `LocalCreatorStore` 第一个位置参数是 root | 废掉这个类，统一走 `Storage.creators` Repository | ADR-0006 |
| §7.7 `creators.json` 顶层是 list，与 SQLite 双源 | 废 `creators.json`，单一 SQLite 真源 | ADR-0006 / data-model.md §2.2 |
| §7.10 `/api/state` 之类"顺手猜的路由"不存在 | FastAPI 自动出 OpenAPI，路由不再靠记忆；snapshot 测试看护 | ADR-0002 / ADR-0009 |
| §7.11 `creator_id` / `creator_platform_id` / `mid` 三种命名 | schema 强制 `platform_id`，Pydantic 入口校验 | ADR-0006 / data-model.md §2.2 |
| §7.12 预检里的 `sqlite` 检查的是飞书镜像库不是主库 | 只有一个库，无可错位 | ADR-0006 |
| §7.17 别把常驻服务接在 `\| head` 后面 | uvicorn 不再 head，常驻服务走 `make dev` / systemd | ADR-0002 |
| §7.23 pytest 用时抖动有三倍 | 不用用时做门禁，用用例数和红绿 | ADR-0009 |
| §7.25 删视频要双删（库行 + `hidden-videos.json` 墓碑） | 墓碑内化为 `is_hidden` 列，所有查询走 `list_visible()` 自动过滤 | ADR-0006 / data-model.md §2.3 |

---

## 3. 契约测试看护（行为保留，测试守住）

| V1 陷阱 | V2 测试 | 层 | 文件 |
|---|---|---|---|
| §7.1 抖音的身份是 `sec_uid`，不是 URL 里的东西；短链不含身份必须跟一次 302 | `test_douyin_short_url_follows_302_to_sec_uid` | L2 | `tests/contracts/test_douyin_adapter.py` |
| §7.2 抖音对非浏览器客户端做风控，yt-dlp 必失败 → 页面播放直链兜底是常态；`yt_dlp_error` 原文必须保留 | `test_douyin_media_artifact_includes_source_and_yt_dlp_error` | L2 | `tests/contracts/test_douyin_adapter.py` |
| §7.3 Windows 上 yt-dlp 读不了 Chrome 的 cookie 库；唯一稳定路径是 `--cookies <文件>` | `test_cookie_variant_ladder_order`（参数化覆盖四平台） | L2 | `tests/contracts/test_cookie_ladder.py` |
| §7.5 转写落盘目录按平台不对称 | `test_transcript_path_unified_across_platforms` | L2 | `tests/contracts/test_transcript_path.py` |
| §7.8 `safe_filename()` 不止换 `/`，还要处理 `..`、结尾点/空格、Windows 设备名 | `test_safe_filename_property_based`（hypothesis） | L0 | `tests/unit/test_safe_filename.py` |
| §7.9 SenseVoice 不产标点，按静音切句补 `。`，否则 `split_sentences` 全废 | `test_asr_engine_punctuation_injection` | L2 | `tests/unit/asr/test_sherpa.py` |
| §7.13 技能脚本物理上有两份（仓库 + 用户级），会漂 | `test_skill_script_no_drift`（diff -rq） | L0 | `tests/unit/test_skill_drift.py` |
| §7.14 `tests/test_bilibili_browser_listing.py` 找不到生产者脚本时是 `SkipTest` 不是 fail | `test_bilibili_listing_runs_when_script_present`（强制不 skip） | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.15 B站 cookie 分两条路（枚举 + 媒体下载），都得带导出文件；档位差别是画质 | `test_bilibili_cookie_variants_argv`（参数化覆盖三档） | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.16 B站搜索兜底要 Node 版 playwright + `NODE_PATH`，pip 那个不算数 | `test_bilibili_search_fallback_detects_missing_node_playwright` | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.19 "注册表里有 PATH" ≠ "进程拿得到"；工作台已收口（`prepare_runtime_environment()`） | `test_prepare_runtime_environment_merges_registry_path` | L0 | `tests/unit/test_runtime_env.py` |
| §7.20 桥的浏览器被人关掉后 formerly 会一直报绿；现已收口（`/health` 503 + 真请求自愈） | `test_bridge_health_returns_503_when_browser_dead` + `test_bridge_self_heal_on_first_request` + `test_bridge_503_semantics_not_bridge_down` | L2 | `tests/contracts/test_bridge_client.py` |
| §7.21 B站媒体可能是未合并的 DASH 分片，转写必须认音频轨 | `test_bilibili_media_artifact_handles_dash_split` | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.22 「🔥 抓取爆款 Top 5」必须按位扫描，跟踪开关只管整库/定时那条路 | `test_backfill_task_uses_creator_url_not_full_scan` | L3 | `tests/integration/tasks/test_backfill.py` |
| §7.24 「持续跟踪」的值必须是真布尔，且默认值只能有一处 | `test_creator_tracking_default_is_true` + `test_set_tracking_rejects_non_bool` + `test_tracking_default_single_source_of_truth` | L1 + L4 | `tests/unit/storage/test_creators.py` + `tests/integration/api/test_creators.py` |
| §2 契约二：清单必须写终态（半路抛异常要走 `abandon()` 收尾） | `test_manifest_writer_finalizes_on_all_exit_paths`（参数化：成功/异常/取消/超时） | L3 | `tests/integration/tasks/test_manifest.py` |

---

## 4. L2 适配器契约测试抽象基类

```python
# tests/contracts/test_platform_adapter.py
import abc
import pytest
from intelligence_hub_v2.platforms.base import PlatformAdapter, Capabilities

class PlatformAdapterContractTests(abc.ABC):
    """每个平台 Adapter 的测试类继承它，自动获得整套契约用例。
    V3 加新平台或重写老平台，这套测试一字不改、自动复用。"""

    @abc.abstractmethod
    def adapter(self) -> PlatformAdapter:
        """子类返回被测 adapter 实例（用 mock deps）。"""

    @abc.abstractmethod
    def expected_capabilities(self) -> Capabilities:
        """子类返回期望的 capabilities，用于校验声明一致性。"""

    # ---- 通用契约 ----

    def test_name_is_lowercase_underscore(self):
        assert self.adapter().name == self.adapter().name.lower()
        assert " " not in self.adapter().name

    def test_capabilities_match_expected(self):
        assert self.adapter().capabilities == self.expected_capabilities()

    def test_config_schema_is_platform_config_subclass(self):
        from intelligence_hub_v2.platforms.base import PlatformConfig
        assert issubclass(self.adapter().config_schema(), PlatformConfig)

    async def test_healthcheck_returns_structured_report(self):
        report = await self.adapter().healthcheck()
        assert report.platform == self.adapter().name
        assert report.status in ("ok", "degraded", "unreachable", "unknown")
        assert report.checked_at is not None

    async def test_parse_creator_url_normalizes_to_platform_id(self):
        # 子类提供有效 URL fixture
        url = self.valid_creator_url()
        ref = await self.adapter().parse_creator_url(url)
        assert ref.platform == self.adapter().name
        assert ref.platform_id  # 非空
        assert not ref.platform_id.startswith("http")  # V1 §7.1：不是 URL

    async def test_list_creator_videos_yields_video_meta_with_required_fields(self):
        ref = await self.adapter().parse_creator_url(self.valid_creator_url())
        videos = []
        async for v in self.adapter().list_creator_videos(ref, limit=3):
            videos.append(v)
            assert v.platform == self.adapter().name
            assert v.platform_video_id
            assert v.title
            assert v.webpage_url
        assert len(videos) > 0 or pytest.skip("no videos available")

    async def test_download_media_artifact_includes_source_and_error_when_fallback(self):
        # 用 fixture 视频
        video = await self.first_video_fixture()
        artifact = await self.adapter().download_media(video, dest=tmp_path)
        assert artifact.media_source in ("yt_dlp", "page_play_url", "dash_merged", "dash_split")
        if artifact.media_source != "yt_dlp":
            assert artifact.yt_dlp_error is not None  # V1 §7.2：兜底时原文必须保留

    # ---- 子类要提供的 fixture ----

    @abc.abstractmethod
    def valid_creator_url(self) -> str: ...

    @abc.abstractmethod
    async def first_video_fixture(self): ...
```

**每个平台的测试类**：

```python
# tests/contracts/test_douyin_adapter.py
class TestDouyinAdapter(PlatformAdapterContractTests, IsolatedAsyncioTestCase):
    def adapter(self): return make_douyin_adapter_with_mocks()
    def expected_capabilities(self):
        return Capabilities(
            needs_browser=True, needs_cookies=True,
            cookie_variants=("exported_file", "browser", "none"),
            supports_subtitles=False, supports_dash_split=False,
            list_strategy="browser_scroll", media_strategy="yt_dlp_with_fallback",
        )
    def valid_creator_url(self): return "https://v.douyin.com/abc123/"
    async def first_video_fixture(self): return load_douyin_video_fixture()

    # ---- 抖音特有契约 ----
    async def test_short_url_follows_302_to_sec_uid(self): ...        # §7.1
    async def test_yt_dlp_failure_triggers_page_play_url(self): ...    # §7.2
    async def test_page_play_url_does_not_persist_signed_cdn_url(self): ...  # §7.2 补充
```

---

## 5. Coverage 阈值

```toml
# pyproject.toml
[tool.coverage.report]
fail_under = 80

# CI 额外检查核心模块
[[tool.coverage.report.include]]
pattern = "src/intelligence_hub_v2/platforms/*"
fail_under = 90

[[tool.coverage.report.include]]
pattern = "src/intelligence_hub_v2/tasks/*"
fail_under = 90
```

CI job `test-backend` 跑：

```bash
uv run coverage report --fail-under=80
uv run coverage report --include='src/intelligence_hub_v2/platforms/*,src/intelligence_hub_v2/tasks/*' --fail-under=90
```

---

## 6. 跑法陷阱（V1 §5 延续）

- **不要把 `-k` 和显式 node id 混在一行**：`pytest -k "..." tests/x.py::Cls::test` 会把那条用例**静默 deselected**，只报别的用例通过。看红/绿就单独跑那个 node id
- **`tests/contracts/test_bilibili_*.py` 依赖 `BILI_PAGE_JS` 环境变量**（由 `tests/test_bilibili_browser_listing.py` 注入），单独跑会全部 skip（这是预期，不是坏了）
- **看 skip 明细**：`pytest -rs`，别只看总数。V1 §7.14：「15 passed」可能是「脚本压根不在」的另一种写法
- **不要用用时做门禁**：V1 §7.23，本机 35s ~ 107s 抖动

---

## 7. V3 重写时的契约

V3 即使把某个平台的实现从 Python 重写成 Go/Rust（通过 subprocess + JSON-RPC 接入）：

1. `PlatformAdapterContractTests` 抽象基类一字不改
2. 平台特有契约测试用例一字不改
3. 跑同一套测试，全绿 = 行为等价

→ V3 重写的验收标准就是这套测试。
