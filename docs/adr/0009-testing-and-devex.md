# ADR-0009: 测试策略与 DevEx

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q9、`docs/specs/contract-tests.md`

## 背景

V1 有 466 个测试但**契约层薄** —— 很多陷阱靠 `AGENTS.md` 文档而不是测试看护。V1 §7 那 25 条陷阱里：

- 约 9 条在 V2 设计中**结构性消除**（V2 让它不可能再发生）
- 约 16 条需要**契约测试看护**（行为保留，测试守住）

V2 要：
1. 把 V1 §7 每条陷阱映射到一个测试，让"V3 重写后跑同一套测试全绿 = 行为等价"成立
2. L2 适配器契约用「抽象基类 + 平台无关用例」，V3 加新平台或重写老平台自动跑同一套
3. coverage 阈值门禁
4. CI 全自动化，但保留本地一键跑（断网时用）

## 决定

### 测试分层（L0-L7）

| 层 | 范围 | 工具 | 跑在哪 |
|---|---|---|---|
| L0 单元 | 纯函数（URL 解析、safe_filename、配置合并） | pytest + hypothesis | 每次提交 |
| L1 仓库 | Repository 方法 + Alembic 迁移可逆 | pytest + aiosqlite `:memory:` | 每次提交 |
| L2 适配器契约 | 每个平台 Adapter 满足 Protocol + 平台特有契约 | pytest + respx + pytest-vcr | 每次提交 |
| L3 调度契约 | EventBus 事件序列、清单终态、取消、超时、平台禁用 | pytest + pytest-asyncio | 每次提交 |
| L4 API 集成 | FastAPI 端点 + OpenAPI snapshot | pytest + httpx AsyncClient | 每次提交 |
| L5 前端组件 | 纯逻辑 + 组件渲染 | Vitest + Testing Library | 每次提交 |
| L6 E2E | 关键流程（添加博主→采集→Feed→详情→隐藏→切平台开关） | Playwright | 仅 main + 手动触发 |
| L7 真机烟雾 | 真跑抖音/B站采集 | pytest `-m real_network` | **本地手动**，CI 不跑 |

### L2 适配器契约用抽象基类

```python
# tests/contracts/test_platform_adapter.py
class PlatformAdapterContractTests(abc.ABC):
    """每个平台 Adapter 的测试类继承它，自动获得整套契约用例。
    V3 加新平台或重写老平台，这套测试一字不改、自动复用。"""

    @abc.abstractmethod
    def adapter(self) -> PlatformAdapter: ...

    async def test_healthcheck_returns_structured_report(self): ...
    async def test_parse_creator_url_normalizes_to_platform_id(self): ...
    async def test_list_creator_videos_yields_video_meta_with_required_fields(self): ...
    async def test_download_media_artifact_includes_source_and_error_when_fallback(self): ...
    async def test_capabilities_match_config_schema(self): ...

class TestDouyinAdapter(PlatformAdapterContractTests, IsolatedAsyncioTestCase):
    def adapter(self): return DouyinAdapter(...)
    # 抖音特有契约
    async def test_short_url_follows_302_to_sec_uid(self): ...        # §7.1
    async def test_yt_dlp_failure_triggers_page_play_url(self): ...    # §7.2
```

### V1 §7 25 条陷阱 → V2 契约测试映射

完整表见 `docs/specs/contract-tests.md`。分类：

**结构性消除（V2 设计让它不可能再发生，不需要测试）**：
- §7.4 整行覆盖 → `update_fields()` 字段级更新
- §7.6 LocalCreatorStore 参数 → 废掉这个类
- §7.7 双源 → 废 `creators.json`
- §7.10 路由靠记忆 → FastAPI 自动出 OpenAPI
- §7.11 三种命名 → schema 强制 `platform_id`
- §7.12 预检主库错位 → 只有一个库
- §7.17 head 接常驻服务 → uvicorn 不再 head
- §7.23 用时抖动门禁 → 不用用时做门禁
- §7.25 墓碑散落 → 内化为 `is_hidden` 列

**契约测试看护（行为保留，测试守住）**：
- §7.1 抖音 sec_uid → `test_douyin_short_url_follows_redirect` (L2)
- §7.2 yt-dlp 必失败 → `test_media_artifact_includes_source_and_yt_dlp_error` (L2)
- §7.3 Windows cookie → `test_cookie_variant_ladder_order` (L2)
- §7.5 转写路径 → `test_transcript_path_unified_across_platforms` (L2)
- §7.8 safe_filename → `test_safe_filename_property_based` (L0, hypothesis)
- §7.9 SenseVoice 标点 → `test_asr_engine_punctuation_injection` (L2)
- §7.13 技能脚本漂移 → `test_skill_script_no_drift` (L0)
- §7.14 SkipTest 不是 fail → `test_bilibili_listing_runs_when_script_present` (L2)
- §7.15 B站 cookie 三档 → `test_bilibili_cookie_variants_argv` (L2)
- §7.16 Node playwright → `test_bilibili_search_fallback_detects_missing_node_playwright` (L2)
- §7.19 注册表 PATH → `test_prepare_runtime_environment_merges_registry_path` (L0)
- §7.20 桥死了报绿 → `test_bridge_health_returns_503_when_browser_dead` + `test_bridge_self_heal_on_first_request` (L2)
- §7.21 B站 DASH → `test_bilibili_media_artifact_handles_dash_split` (L2)
- §7.22 按位扫描 → `test_backfill_task_uses_creator_url_not_full_scan` (L3)
- §7.24 跟踪开关 → `test_creator_tracking_default_is_true` + `test_set_tracking_rejects_non_bool` (L1 + L4)

### Coverage 阈值

- 全局 ≥80%
- `src/intelligence_hub_v2/platforms/` 与 `tasks/` 模块 **≥90%**（核心契约不能漏）
- 前端 ≥70%（UI 代码不强求）
- CI 阈值不达标直接 fail

### DevEx 工具链

**Makefile 目标**（完整列表见 `Makefile`）：
- `install` / `dev` / `test` / `lint` / `format` / `coverage` / `e2e` / `build` / `migrate-v1` / `clean`
- `ci-local`：本地一键跑 CI 全套（断网时用）

**pre-commit hooks**（`.pre-commit-config.yaml`）：
- ruff format / ruff check --fix
- mypy
- eslint --fix / prettier / stylelint / tsc
- detect-secrets（防 token 泄露，V1 §1.1 那条硬约束）
- check-yaml / check-json / end-of-file-fixer / trailing-whitespace
- uv-lock（确保 `uv.lock` 与 `pyproject.toml` 同步）
- conventional-pre-commit（commit message 格式）

**CI（GitHub Actions）**：
- jobs: `lint` / `test-backend` / `test-frontend` / `build` / `e2e`（仅 main + 手动）/ `openapi-snapshot`
- 不跑：L7 真机烟雾测试（要 cookie + 桥 + Chrome）
- 不跑：Visual regression（Chromatic/Percy 太贵，本地 `make screenshots` 抓基线，PR 时人眼比对）
- **不上传 codecov**（多一个第三方依赖，本地 `coverage html` 已经够用）

**E2E 测试策略**：用**真后端 + 假数据 fixture**（启动时注入 in-memory SQLite 测试数据），更接近真实使用，能发现前后端集成 bug。

### 工具选型

- **Makefile vs justfile**：选 Makefile。理由：无需额外装 just，Windows 上 `make` 通过 Git Bash 自带或 chocolatey 装一下即可
- **GitHub Actions vs 本地脚本**：选 GitHub Actions，但保留 `make ci-local` 一键本地跑（V1 §4.1 提到过 `github.com:443` 偶发不通的历史）
- **codecov**：不上（多一个第三方依赖，本地 `coverage html` 够用）

## 后果

**好处**：
- V1 §7 那 25 条陷阱每条都有归宿（结构性消除 / 契约测试看护）
- L2 抽象基类 = V3 的可执行验收标准。V3 即使把抖音采集器从 Python 重写成 Go（通过 subprocess + JSON-RPC 接入），同一套契约测试照样能跑（测试只调 Adapter Protocol，不关心底层语言）
- OpenAPI snapshot 测试 = API 契约看护。V3 后端换语言时，snapshot 不破就能保证前端不挂
- 设计令牌 snapshot = UI 契约看护（V3 换框架时，色板与间距不破）
- `make ci-local` 让断网时也能验

**代价**：
- 测试基础设施前期投入大（抽象基类 + fixture 工厂 + mock 配置）
- coverage 阈值门禁会让 PR 偶尔被卡（缓解：阈值分模块设，核心模块严，外围松）
- pre-commit hook 多，commit 速度慢（缓解：ruff/mypy 用 cache，整体 <10s）

**对 V3 的意义**：
- **契约测试套件本身就是 V3 的验收标准**：V3 重写实现后，跑同一套测试，全绿就等于行为等价
- 抽象基类设计让 V3 加新平台或重写老平台都自动跑同一套
- OpenAPI snapshot + 设计令牌 snapshot = 前后端契约看护
