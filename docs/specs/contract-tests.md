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
| §7.1 抖音的身份是 `sec_uid`，不是 URL 里的东西；短链不含身份必须跟一次 302 | `test_share_link_follows_302_to_sec_uid`＋`test_sec_uid_recognised_from_each_shape`（5 形）＋`test_the_path_wins_over_a_conflicting_query_sec_uid`＋`test_url_encoded_in_the_sec_uid_slot_is_refused`＋`test_unrecognisable_input_raises_parse_url_with_the_original_text`（含 V2 新补的域名闸门，坑 20） | L2 | `tests/contracts/test_douyin_adapter.py` |
| §7.2 抖音对非浏览器客户端做风控，yt-dlp 必失败 → 页面播放直链兜底是常态；`yt_dlp_error` 原文必须保留 | `test_fallback_marks_the_source_and_keeps_the_yt_dlp_original_text`＋`test_signed_play_url_never_reaches_the_artifact`＋`test_both_rounds_failures_are_reported_together`＋`test_missing_yt_dlp_binary_still_falls_back_and_says_so` | L2 | `tests/contracts/test_douyin_adapter.py` |
| §7.3 Windows 上 yt-dlp 读不了 Chrome 的 cookie 库；唯一稳定路径是 `--cookies <文件>` | `TestCookieLadder`：`test_order_comes_from_capabilities_and_starts_with_the_exported_file`／`test_header_only_cookie_file_is_not_offered_as_a_login_rung`／`test_browser_rung_absent_unless_somewhere_names_a_browser`／`test_the_ladder_actually_reaches_yt_dlp_argv`。顺序的**唯一真源**是 `capabilities`，见 `docs/adr/0011`（经验 17） | L2 | `tests/contracts/test_douyin_adapter.py`（B站 那份在 `tests/contracts/test_bilibili_adapter.py`，Task 7） |
| §7.5 转写落盘目录按平台不对称 | `test_transcript_path_is_identical_across_platforms` | L1 | `tests/unit/storage/test_files.py` |
| §7.8 `safe_filename()` 不止换 `/`，还要处理 `..`、结尾点/空格、Windows 设备名 | hypothesis 一组：`test_result_is_always_a_single_path_segment`、`test_result_is_never_a_path_segment_escape`、`test_result_is_usable_as_a_real_path` | L0 | `tests/unit/test_safe_filename.py` |
| §7.9 SenseVoice 不产标点，按静音切句补 `。`，否则 `split_sentences` 全废 | **未落地**（ASR 在 V2.1，这一行现在只是计划，没有看护） | — | — |
| §7.13 技能脚本物理上有两份（仓库 + 用户级），会漂 | 清单形状不认识时报错**点名仓库内那份生产者**：`test_an_unreadable_manifest_raises_naming_the_producer`；`diff -rq` 的 L0 那条留到 Task 14 | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.14 找不到生产者时是 SkipTest 不是 fail | 一律**红**，没有 skip 这条路：`test_exit_zero_with_nothing_parsed_is_still_a_failure`、`test_a_missing_manifest_file_is_reported_with_its_path`、`test_an_unreadable_manifest_raises_naming_the_producer`（缺文件 / 空结果 / 形状不对三种都覆盖） | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.15 B站 cookie 分两条路（枚举 + 媒体下载），都得带导出文件；档位差别是画质 | `test_the_enumeration_carries_the_same_cookie_rungs_as_download`（两条路的 argv 各断一次）＋ `test_three_rungs_in_the_v1_order` ＋ `test_header_only_file_is_not_a_login_rung` ＋ `test_a_healthy_ladder_says_nothing` | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.16 B站搜索兜底要 Node 版 playwright + `NODE_PATH`，pip 那个不算数 | `test_search_fallback_is_refused_rather_than_silently_skipped`：勾了它而 V2 尚未实现时**如实红**，不静默跳过 | L2 | `tests/contracts/test_bilibili_adapter.py` |
| §7.19 "注册表里有 PATH" ≠ "进程拿得到" | **未落地**：`prepare_runtime_environment()` 从未实现，今天只有 preflight 把缺的二进制报进 `summary["tools_missing"]` | — | — |
| §7.20 桥的浏览器被人关掉后 formerly 会一直报绿；现已收口（`/health` 503 + 真请求自愈） | `test_503_means_browser_dead_not_bridge_down` + `test_bridge_available_is_true_on_503` + `test_dead_browser_during_navigate_raises_so_self_heal_can_run` | L1 | `tests/unit/infra/test_cdp_bridge.py` |
| §7.21 B站媒体可能是未合并的 DASH 分片，转写必须认音频轨 | `TestDashSplit`：`test_pair_reaches_the_caller_as_a_video_audio_pair`、`test_only_yt_dlp_reported_paths_are_considered`（不扫目录）、`test_two_webm_tracks_are_told_apart_by_size`、`test_a_pair_artifact_points_the_transcriber_at_the_audio_track`（与 `audio_path_of()` 接通） | L2 | `tests/contracts/test_bilibili_adapter.py` + `tests/contracts/test_bilibili_helpers.py` |
| §7.22 「🔥 抓取爆款 Top 5」必须按位扫描，跟踪开关只管整库/定时那条路 | **未落地**（V2.1 的 Backfill 任务） | — | — |
| §7.24 「持续跟踪」的值必须是真布尔 | `test_set_tracking_rejects_non_bool` + `test_tracking_rejects_non_boolean_with_422` | L1 + L3 | `tests/unit/storage/test_creators_repo.py` + `tests/integration/test_api_creators.py`（后半句"默认值只能有一处"**未落地**：要等前端 settings store，见 §3.1） |
| §2 契约二：清单必须写终态（半路抛异常要走 `abandon()` 收尾） | `test_manifest_finalizes_on_every_exit_path`（参数化：成功/异常/取消/超时） | L3 | `tests/integration/test_manifest_finalization.py` |

### 3.1 已落地的看护（截至 Task 3，2026-09-22）

上面那张表是**计划**。这一节是**已经能跑的现状** —— 分开写是因为实施时会撞见
"计划里的测试名/文件路径与实际不同"，而未来的会话需要知道哪一条是真的存在：

| 陷阱 | 现在真正跑着的用例 | 层级 | 文件 |
|------|-------------------|------|------|
| §7.4 整行覆盖 | `test_update_fields_does_not_clobber_other_fields` + `test_update_fields_rejects_identity_and_tombstone_fields` + `test_update_fields_rejects_unknown_field` + `test_update_fields_can_set_a_field_to_none` | L1 | `tests/unit/storage/test_videos_repo.py` |
| §7.4（博主侧同形） | 同名四件（`test_update_fields_does_not_clobber_other_fields` / `..._rejects_identity_fields` / `..._rejects_unknown_field` / `..._with_no_fields_is_a_noop`） | L1 | `tests/unit/storage/test_creators_repo.py` |
| §7.11 列名三种写法 | `test_insert_and_find`（唯一身份入口就是 `find(platform, platform_id)`，没有第二种名字的入口）+ `test_update_fields_rejects_identity_fields` | L1 | `tests/unit/storage/test_creators_repo.py` |
| §7.24 布尔值 + 默认值一处 | `test_set_tracking_rejects_non_bool[0/1/"false"/"true"/None/""/[]]` + `test_set_tracking_roundtrips_both_ways` + `test_update_fields_refuses_to_touch_tracking` | L1 | `tests/unit/storage/test_creators_repo.py` |
| §7.25 墓碑双删 | `test_hide_removes_from_list_but_get_still_works` + `test_unhide_restores_and_clears_the_tombstone` + `test_hide_requires_a_non_blank_reason` + `test_video_count_respects_hidden`（V2 里墓碑是**一列三字段**，"少一半都不算数"由 `hide()`/`unhide()` 这唯一写入口消除） | L1 | `tests/unit/storage/test_videos_repo.py` + `test_creators_repo.py` |
| §7.22 按位扫描 vs 开关筛全库 | `test_list_tracked_excludes_switched_off_but_find_still_hits` + `test_list_all_includes_untracked` | L1 | `tests/unit/storage/test_creators_repo.py` |
| §7.5 转写目录按平台不对称 | `test_text_path_is_stored_verbatim`（两平台同尾段布局）+ `test_transcript_path_is_identical_across_platforms` + `test_audio_dir_is_not_under_transcript`（§7.21 的中间产物隔离） | L1 | `tests/unit/storage/test_transcripts_repo.py` + `test_files.py` |
| §7.8 `safe_filename` 不止换 `/` | 9 条 hypothesis 属性 + 16 个定向函数（展开 68 条），含 `test_result_is_never_a_bare_windows_device_name`、`test_result_is_always_a_single_path_segment`、`test_result_is_usable_as_a_real_path`（真建目录真写文件） | L0 | `tests/unit/test_safe_filename.py` |
| §2 契约二（DB 层那一半） | `test_terminal_status_without_ended_at_is_rejected_by_the_db`（绕开 `finish()` 直接写 SQL，验 `ck_task_runs_terminal_has_ended_at`）+ `test_unknown_status_is_rejected` | L1 | `tests/unit/storage/test_task_runs_repo.py` |
| §1.3 不许吞错（错误原文落库） | `test_finish_preserves_the_error_text_verbatim` + `test_content_json_keeps_chinese_error_text_readable` + `test_payload_keeps_chinese_readable` | L1 | `tests/unit/storage/test_{task_runs,manifests,events}_repo.py` |
| §7.20 健康灯"测不到"≠"正常" | `test_is_healthy_only_trusts_an_explicit_ok`（6 组参数）+ `test_upsert_does_not_clobber_health_columns` + `test_clear_health_returns_to_never_checked` | L1 | `tests/unit/storage/test_platforms_repo.py` |
| §7.12 预检查错库（V2 结构性消除） | `test_check_files_exist_requires_data_dir`（不给 `data_dir` 就抛，不兜一个错的默认值）+ 全仓只有一个库路径解析函数 `resolve_db_path()` | L1 | `tests/unit/storage/test_manifests_repo.py` |

**表里 §7.8 那行的计划名是 `test_safe_filename_property_based`**，实际拆成了 9 个具名属性
（`test_result_is_never_empty` / `..._contains_no_illegal_characters` /
`..._is_always_a_single_path_segment` / `..._is_idempotent` 等）——
一个巨型属性红了以后没法定位是哪条不变式破的，所以按不变式拆开。**以这一节为准**，
计划表里的名字当作"要覆盖这个陷阱"的意图读。

**注意同名用例**：`test_update_fields_does_not_clobber_other_fields` 在
`test_videos_repo.py` 与 `test_creators_repo.py` 里**各有一条**（两行表格指的是两条），
`-k` 或按 node id 单跑时要带文件名，别以为只有一条。

**仍未落地**（按计划属于后续任务）：§7.9（ASR 标点，V2.1）、
§7.13 的 `diff -rq` 那半、§7.19（PATH 上的 ffmpeg / ffprobe，已由 Task 8 preflight 在
`summary["tools_missing"]` 里报出）、§7.22（按位抓取的 URL 直定位，归 V2.1 的 `BackfillTask`）、
§7.24 的 L4 前端部分（Task 10-13 一起做）。

**已落地**：

- §7.1 / §7.2 / §7.3 —— Task 6 抖音，`tests/contracts/test_douyin_adapter.py`（91 条）
- §7.13 / §7.14 / §7.15 / §7.16 / §7.21 —— Task 7 B站，
  `tests/contracts/test_bilibili_adapter.py`（91 条）＋ `test_bilibili_helpers.py`（纯函数逐形状）
- §7.20 的桥语义 —— Task 5 `infra/cdp_bridge.py` ＋ 两个平台的 `TestHealthcheck`。
  Task 7 补了一条**不对称**的看护：同一个 `yt_dlp` 组件在抖音亮黄（还有页面直链那条路）、
  在 B站 亮红（没有第二条路），见 `test_missing_yt_dlp_is_unreachable_here_not_degraded`。
  两处同色的话，其中一处一定在骗人。
- §4 抽象基类 —— Task 14：`tests/contracts/test_platform_adapter.py` 的
  `PlatformAdapterContractTests`（通用契约：名字规范 / `isinstance` Protocol /
  `capabilities` 冻结声明 / `config_schema` 子类 / healthcheck 结构化 + `is_healthy` 只认 ok /
  parse 非 URL id / 不支持字幕返 None）。抖音 / B站 各一个薄子类复用其 `make_adapter` 过契约
  （**没有**改写两套大测试本身，见 `docs/lessons.md` 经验 22）。
- §3 整张映射表的**存在性** —— Task 14 `tests/contracts/test_contract_guard_index.py`：
  扫 `tests/` 树逐条比对"每条 §7 点名的看护用例还在"，改名/删除即红。

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
    def adapter(self):
        return make_douyin_adapter_with_mocks()

    def expected_capabilities(self):
        return Capabilities(
            needs_browser=True,
            needs_cookies=True,
            cookie_variants=("exported_file", "browser", "none"),
            supports_subtitles=False,
            supports_dash_split=False,
            list_strategy="browser_scroll",
            media_strategy="yt_dlp_with_fallback",
        )

    def valid_creator_url(self):
        return "https://v.douyin.com/abc123/"

    async def first_video_fixture(self):
        return load_douyin_video_fixture()

    # ---- 抖音特有契约 ----
    async def test_short_url_follows_302_to_sec_uid(self): ...  # §7.1
    async def test_yt_dlp_failure_triggers_page_play_url(self): ...  # §7.2
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
