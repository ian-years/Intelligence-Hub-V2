# ROADMAP

V2 的总目标、里程碑、当前状态、待办。**新会话进来先读这份**，再读最近一份 `docs/progress/`。

---

## 总目标

把 V1（`Intelligence-Hub`，一堆顶层脚本 + `launcher_server.py` 字典调度 + 原生 JS 前端）重构成 V2：

1. **工程化标准**：FastAPI + uv + ruff + mypy strict + pytest + Alembic + structlog，前端 React + TS + Vite + Tailwind
2. **平台配置化**：抖音 / B站 / 小红书 / YouTube 都做成 `PlatformAdapter` Protocol 实现，前端配置面板可逐个开关，关掉的平台所有任务自动禁用
3. **UI 重做**：孟菲斯风格（亮色块、粗黑边、硬阴影、几何形状、不对称布局）
4. **为 V3 留契约**：所有边界（Platform Adapter / Task Runner / Event Schema / Data Model / UI Tokens / OpenAPI）都是 V3 重写时不能动的契约，实现层可弃

V1 工作区一行不动，V2 独立目录、独立 git、独立 `data/`，通过 `tools/migrate_from_v1.py` 一次性迁移历史数据。

---

## 里程碑

### V2.0「骨架可用」 — 进行中

**完成判据**（每条都要真机验过才算）：

- [ ] **后端骨架**：FastAPI 起服务、`/api/health` 通、CORS、structlog JSON 日志、自定义异常层次、全局异常处理器
- [ ] **数据层**：SQLAlchemy Core schema（platforms / creators / videos / transcripts / task_runs / task_events / manifests）+ Alembic 初始迁移 + Repository 层 + `update_fields()` 字段级更新（V1 §7.4 看护）
- [ ] **EventBus**：asyncio.Queue 多播 + SQLite 持久化 + SSE 端点
- [ ] **配置层**：Pydantic Settings + YAML 加载（优先级 默认 < app.yaml < platforms.yaml < env < CLI）+ `PUT /api/platforms/{name}/config` 原子写盘 + 热加载 + ConfigChanged 事件
- [ ] **平台契约**：`PlatformAdapter` Protocol + `Capabilities` dataclass + 显式注册表 + 契约测试抽象基类
- [ ] **抖音 Adapter**：移植 V1 `download_douyin_latest.py`，含 `parse_creator_url`（短链 302 → sec_uid，§7.1）、媒体下载（yt-dlp → 页面播放直链兜底，§7.2）、cookie 优先级阶梯
- [ ] **B站 Adapter**：移植 V1 `download_bili_following_latest.py`，含 cookie 三档（§7.15）、DASH 未合并分片处理（§7.21）、字幕优先
- [ ] **CDP 桥**：移植 V1 `cdp_bridge_server.py`（保留只绑回环约束）+ `BridgeClient` 包装 + 桥健康检查 + 自愈逻辑（§7.20）
- [ ] **任务调度**：`TaskRegistry` + 6 个核心任务（preflight / douyin_collect / bilibili_collect / single_link / add_creator / postprocess）+ 平台级 Semaphore 限流 + CancelToken + 超时 + 清单双写（文件 + SQLite，强制终态）
- [ ] **前端骨架**：React + TS + Vite + Tailwind + shadcn/ui 装好，孟菲斯设计令牌（色板、形状、排版、图案、动效）落 `tailwind.config.ts` + CSS 变量 + `tokens.json`
- [ ] **前端三页**：Dashboard（四平台健康卡片 + 最近任务 + 最新作品）/ Feed（虚拟滚动 + 过滤 + 隐藏）/ Settings（平台开关 + JSON Schema 自动渲染表单 + Preflight 子页）
- [ ] **前后端打通**：OpenAPI → TS 类型自动生成（`openapi-typescript`）+ TanStack Query + SSE 订阅 + 平台开关切到后端通
- [ ] **测试**：L0-L4 全绿，coverage ≥80%（`platforms/` 与 `tasks/` 模块 ≥90%），V1 §7 中需要测试看护的每条都有对应契约测试（详见 `docs/specs/contract-tests.md`）
- [ ] **DevEx**：Makefile + pre-commit + GitHub Actions CI（lint / test-backend / test-frontend / build / e2e 手动触发）+ `make ci-local` 一键本地跑全套
- [ ] **文档**：10 份 ADR + 7 份 spec + AGENTS.md + README + CONTRIBUTING + architecture.md + lessons.md 全部落盘并 commit
- [ ] **迁移脚本**：`tools/migrate_from_v1.py` dry-run + 真机跑通一次（creators / videos / transcripts / hidden-videos 墓碑内化），媒体 hardlink 回退 copy

**为什么先做抖音 + B站**：V1 §6 表里这两个平台真机已实测全通；抖音的 `a_bogus` 兜底与 B站的 cookie 三档 + DASH 分片是 V1 最难的两条经验，先用 V2 接口表达清楚，剩下两个平台照葫芦画瓢。

### V2.1「四平台齐全 + 转写完整」 — 规划中

- [ ] 小红书 Adapter（依赖桥的页面 JS 注入）
- [ ] YouTube Adapter（纯 yt-dlp，本机网络不可达时如实失败）
- [ ] ASR 引擎接入（sherpa-onnx SenseVoice，§7.9 标点注入）
- [ ] `PostprocessTask` 跨平台统一（废 V1 三份 postprocess_*）
- [ ] 字幕优先路径（B站 / YouTube）
- [ ] 前端 Video Detail 页（视频播放 + metadata + 口播稿时间戳跳转）
- [ ] 前端 Creators 页（博主库 + 跟踪开关 + 添加博主 + 爆款回溯入口）
- [ ] 前端 Tasks 页（任务卡片墙 + 运行历史 + 实时事件流 + 取消）
- [ ] `BackfillTask` 实现（§7.22 按 URL 直接定位，不退化全库扫描）
- [ ] 真机烟雾测试：抖音 / B站 / 小红书 三平台各采一条 + 转写

### V2.2「飞书 + 分析层 + 暗色」 — 规划中

- [ ] 飞书同步移植（`feishu_sync` 任务，`feishu-base` 状态收进主库，废独立镜像库）
- [ ] 爆款拆解引擎（移植 V1 `launcher/engine/benchmark_engine.py`）
- [ ] 脚本生成（移植 V1 `launcher/engine/draft_engine.py`）
- [ ] 话题 / 草稿表与前端页
- [ ] 报告生成（移植 V1 `prepare_creator_analysis.py` / `render_creator_analysis_report.py` / `generate_creator_insight_dashboard.py`）
- [ ] 暗色模式（孟菲斯暗色版色板）
- [ ] E2E 测试覆盖关键流程

### V2.x 稳定后 — 远期

- 修 bug、性能调优、文档完善
- 等 V3 启动（V3 = 全量重写，契约不变，实现可弃；可能换语言）

---

## 当前状态（2026-09-22）

- **设计阶段**：完成（10 节决策全部锁定，详见 `docs/adr/`）
- **实施计划**：完成（`docs/plans/v2.0-implementation.md`，16 个任务，67h 估时）
- **V2.0 实施**：进行中 —— Task 1 ✅ / Task 2 ✅ / Task 3-16 待做
- **V1 工作区**：未动

### 实施进度明细

| Task | 内容 | 状态 | 提交 | 测试 |
|------|------|------|------|------|
| 1 | 包骨架 + `errors.py` + `logging.py` + 共享 models | ✅ | `8c5f51a` | 15 passed |
| 2 | 配置层 `AppConfig` / `PlatformConfig` 家族 / `ConfigManager` | ✅ | 见 git log | 96 passed（累计），覆盖率 96.02% |
| 3 | DB schema + Alembic + Repository + Storage | ⬜ | — | — |
| 4 | EventBus + `manifest_writer` 上下文管理器 | ⬜ | — | — |
| 5 | `PlatformAdapter` Protocol + Registry + infra 包装 | ⬜ | — | — |
| 6 | DouyinAdapter（可与 Task 7 并行） | ⬜ | — | — |
| 7 | BilibiliAdapter | ⬜ | — | — |
| 8 | TaskRunner + TaskRegistry + 6 个 handler | ⬜ | — | — |
| 9 | FastAPI app + 全部 API 路由 + SSE | ⬜ | — | — |
| 10 | 前端脚手架 + 孟菲斯 tokens（可与 Task 3-9 并行） | ⬜ | — | — |
| 11 | 前端 API 层 + SSE + stores + Router + Layout | ⬜ | — | — |
| 12 | 页面 Dashboard / Feed / Settings / Preflight | ⬜ | — | — |
| 13 | 页面 VideoDetail / Creators / Tasks（V2.0 最小版） | ⬜ | — | — |
| 14 | 契约测试抽象基类 + 16 条 V1 陷阱看护 | ⬜ | — | — |
| 15 | `tools/migrate_v1.py` | ⬜ | — | — |
| 16 | CI 验证 + 端到端 smoke + 收尾文档 | ⬜ | — | — |

**门禁现状**（每次提交前都要全绿）：

```bash
cd E:/08-Codework/Intelligence-Hub-V2
.venv/Scripts/python.exe -m pytest tests/ -q          # 96 passed，覆盖率 96.02%（门禁 80%）
.venv/Scripts/python.exe -m ruff format src/ tests/
.venv/Scripts/python.exe -m ruff check src/ tests/    # All checks passed
.venv/Scripts/python.exe -m mypy src/                 # no issues found in 18 source files
```

**尚未安装的可选依赖**（Task 6/7/8 需要）：`yt-dlp` / `curl-cffi` / `zhconv` / `sherpa-onnx` /
`numpy` / `playwright`。装法：`.venv/Scripts/python.exe -m pip install -e ".[media,asr,bridge]"`。

**实施期对 spec 的偏离**全部记在 `docs/lessons.md`「V2 新增」一节（7 条坑）与
「实施阶段」一节（5 条方法论），涉及的两份 spec 已就地加修订说明：
`docs/specs/task-runner.md §2.6`（`Manifest.error`）、`docs/specs/config-schema.md §1 / §3`
（YAML 加载方式、`platforms.yaml` 扁平化、`advanced: Any`）。

---

## 待办池（不阻塞里程碑，但记下来不忘）

- [ ] `cdp_bridge_server.py` 移植时考虑是否拆出独立仓库（V1 / V2 / V3 共用同一个桥服务）
- [ ] 暗色模式的设计令牌（孟菲斯暗色版色板需要单独调）
- [ ] Visual regression 测试方案（本地 `make screenshots` 抓基线 + PR 人眼比对，不上 Chromatic/Percy）
- [ ] 多语言 UI（V1 全中文，V2 暂时也全中文，i18n 留 V2.x）
- [ ] 数据导出（CSV / JSON）
- [ ] 定时任务调度 UI（V2.0 后端有 APScheduler，但前端没暴露）

---

## 跨会话续上下文的入口

新会话进来读这三份就够：

1. **本文件**（`ROADMAP.md`） — 知道在哪个里程碑、剩什么没做
2. **最近一份 `docs/progress/YYYY-MM-DD.md`** — 知道上次干到哪、卡在哪
3. **`AGENTS.md`** — 知道工程纪律与已知陷阱
