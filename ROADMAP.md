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

- [x] **后端骨架**：FastAPI 起服务、`/api/health` 通、CORS、structlog JSON 日志、自定义异常层次、全局异常处理器
  —— 2026-09-23 Task 9 完成。`create_app` + lifespan 装配全链、`/api/health`、CORS 按
  `app.cors_origins` 条件挂载、`_register_exception_handlers` 把 `NotFoundError`→404 /
  `ConflictError`→409 / `TaskRejected`→422 / `ValidationError`→422 统一映射（看护 `test_api_*`）。
- [x] **数据层**：SQLAlchemy Core schema（platforms / creators / videos / transcripts / task_runs / task_events / manifests）+ Alembic 初始迁移 + Repository 层 + `update_fields()` 字段级更新（V1 §7.4 看护）
  —— 2026-09-22 Task 3 完成。真机判据：`alembic upgrade head` → `downgrade base` → `upgrade head`
  往返通过；`alembic check` 回 `No new upgrade operations detected.`；
  `check_schema_matches_migrations()` 差异 0 条；两档后端（`create_all` / 真迁移）跑同一批 486 条用例。
  看护清单见 `docs/specs/contract-tests.md §3.1`。
- [x] **EventBus**：asyncio.Queue 多播 + SQLite 持久化 + SSE 端点
  —— 多播 + 持久化（Task 4，`core/event_bus.py`，有界队列 + 掉包记账）+ **SSE 端点**
  （Task 9，`api/v1/events.py`：`/api/events` 全局流 + `/api/tasks/runs/{id}/events` 单任务流，
  先 subscribe 再 replay，`sse-starlette` 出 `text/event-stream`）三条都齐 → 整条勾。
- [ ] **配置层**：Pydantic Settings + YAML 加载（优先级 默认 < app.yaml < platforms.yaml < env < CLI）+ `PUT /api/platforms/{name}/config` 原子写盘 + 热加载 + ConfigChanged 事件
- [x] **平台契约**：`PlatformAdapter` Protocol + `Capabilities` dataclass + 显式注册表 + 契约测试抽象基类
  —— 四件齐了。Protocol / 注册表 / infra 五件包装（Task 5）；抖音 + B站 两实现（Task 6/7）；
  `PLATFORMS` 与 `PLATFORM_CONFIG_SCHEMAS` 差集为空（注册表快照用例钉着）；
  **`PlatformAdapterContractTests` 抽象基类**（Task 14，`tests/contracts/test_platform_adapter.py`）
  —— 抖音 / B站 各继承它跑通用契约（名字规范 / isinstance Protocol / capabilities 冻结声明 /
  config_schema 子类 / healthcheck 结构化 + `is_healthy` 只认 ok / parse 交回非 URL id /
  不支持字幕返 None）。V3 加平台继承即得整套。§7→用例名索引看护防文档说谎。
- [ ] **抖音 Adapter**：移植 V1 `download_douyin_latest.py`，含 `parse_creator_url`（短链 302 → sec_uid，§7.1）、媒体下载（yt-dlp → 页面播放直链兜底，§7.2）、cookie 优先级阶梯
  —— 代码与契约测试已完成（Task 6：`platforms/douyin/`，91 条用例，§7.1/§7.2/§7.3 各有看护）。
  **整条不勾，因为"真机验过"这一半还没有**：跑通需要本机 CDP 桥 + 一个已登录的 Chrome，
  本会话没有那个状态（见 `docs/progress/2026-09-22.md` Task 6 的"未验证"清单）。
- [ ] **B站 Adapter**：移植 V1 `download_bili_following_latest.py`，含 cookie 三档（§7.15）、DASH 未合并分片处理（§7.21）、字幕优先
  —— 代码与 169 条用例完成（Task 7）。**已真机验过的部分**（2026-09-22 本机，匿名）：
  `x/web-interface/view` / `x/player/v2` / `x/web-interface/card` 三个接口的真响应形状
  （存成 `tests/fixtures/bilibili/`，见 `docs/lessons.md` 经验 19）、
  `yt-dlp -J` 的 15 条 formats（证实未合并 DASH 是默认形状）、
  `yt-dlp --flat-playlist` 匿名那句真 `Request is blocked by server (412)`。
  **未验**：带登录 cookie 的实际媒体下载、非空字幕轨（两者都要有效会话 cookie，
  而 V2 的 `data/cookies/` 现在没有）。整条不勾。
- [ ] **CDP 桥**：移植 V1 `cdp_bridge_server.py`（保留只绑回环约束）+ `BridgeClient` 包装 + 桥健康检查 + 自愈逻辑（§7.20）
- [ ] **任务调度**：`TaskRegistry` + 6 个核心任务（preflight / douyin_collect / bilibili_collect / single_link / add_creator / postprocess）+ 平台级 Semaphore 限流 + CancelToken + 超时 + 清单双写（文件 + SQLite，强制终态）
  —— 代码 + 离线用例完成（Task 8：`core/task_runner.py` / `core/task_registry.py` /
  `tasks/`）。12 个任务全登记、V2.0 只给 6 个真 handler（其余 6 个 `implemented=False`，
  不进 `/api/tasks`、点名运行红在 `NotImplementedError`）。`TaskRunner` 五条退出路径
  （成功/partial/报告失败/抛异常/取消/超时）都在 `manifest_writer` 上落终态 + 发对应事件，
  `TaskScheduler` 收口平台开关门 + 全局/每平台 Semaphore + 协作式 `CancelToken`。
  **整条不勾，因为"真机验过"这一半还没有**：采集类任务（douyin_collect/bilibili_collect）
  跑通仍需本机 CDP 桥 + 已登录 Chrome + ffmpeg，与 Task 6/7 同一前置。Task 9 通了之后
  随抖音/B站 里程碑一起补那一跑。
- [ ] **前端骨架**：React + TS + Vite + Tailwind + shadcn/ui 装好，孟菲斯设计令牌（色板、形状、排版、图案、动效）落 `tailwind.config.ts` + CSS 变量 + `tokens.json`
- [ ] **前端三页**：Dashboard（四平台健康卡片 + 最近任务 + 最新作品）/ Feed（虚拟滚动 + 过滤 + 隐藏）/ Settings（平台开关 + JSON Schema 自动渲染表单 + Preflight 子页）
- [ ] **前后端打通**：OpenAPI → TS 类型自动生成（`openapi-typescript`）+ TanStack Query + SSE 订阅 + 平台开关切到后端通
- [ ] **测试**：L0-L4 全绿，coverage ≥80%（`platforms/` 与 `tasks/` 模块 ≥90%），V1 §7 中需要测试看护的每条都有对应契约测试（详见 `docs/specs/contract-tests.md`）
- [ ] **DevEx**：Makefile + pre-commit + GitHub Actions CI（lint / test-backend / test-frontend / build / e2e 手动触发）+ `make ci-local` 一键本地跑全套
- [ ] **文档**：10 份 ADR + 7 份 spec + AGENTS.md + README + CONTRIBUTING + architecture.md + lessons.md 全部落盘并 commit
- [ ] **迁移脚本**：`tools/migrate_from_v1.py` dry-run + 真机跑通一次（creators / videos / transcripts / hidden-videos 墓碑内化），媒体 hardlink 回退 copy
  —— 代码 + 离线用例完成（Task 15）。V1 以 `mode=ro` URI 打开（写它当场抛，不靠约定）；
  幂等（`(platform, platform_id/platform_video_id)` 命中跳过）+ `.migration_state.json` 续跑；
  墓碑从 `hidden-videos.json` 内化成 `videos.is_hidden`；V1 内联的 `clean_transcript` 落成 V2
  `transcripts` 表 + 磁盘 `speech-clean.txt`；媒体 hardlink、跨卷退 copy。**整条不勾**：
  这台机器没有真实 V1 `data/`（gitignore），dry-run 与真迁移是对**造出的 V1 schema** 跑的；
  对真 V1 库那一次要你有数据时执行（命令已写进脚本 docstring）。

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

## 当前状态（2026-09-23）

- **设计阶段**：完成（10 节决策全部锁定，详见 `docs/adr/`）
- **实施计划**：完成（`docs/plans/v2.0-implementation.md`，16 个任务，67h 估时）
- **V2.0 实施**：进行中 —— Task 1-9、14、15 ✅ / Task 10-13、16 待做
- **V1 工作区**：未动

### 实施进度明细

| Task | 内容 | 状态 | 提交 | 测试 |
|------|------|------|------|------|
| 1 | 包骨架 + `errors.py` + `logging.py` + 共享 models | ✅ | `8c5f51a` | 15 passed |
| 2 | 配置层 `AppConfig` / `PlatformConfig` 家族 / `ConfigManager` | ✅ | 见 git log | 96 passed（累计），覆盖率 96.02% |
| 3 | DB schema + Alembic + Repository + Storage | ✅ | 见 git log | 650 passed（累计），覆盖率 95.07%，ruff + mypy 双绿 |
| 4 | EventBus + `manifest_writer` 上下文管理器 | ✅ | 见 git log | 701 passed（累计），覆盖率 95.39%，四关全绿 |
| 5 | `PlatformAdapter` Protocol + Registry + infra 包装 | ✅ | 见 git log | 820 passed（累计），覆盖率 94.04%，四关全绿 |
| 6 | DouyinAdapter（可与 Task 7 并行） | ✅ | 见 git log | 921 passed（累计，净增 101），覆盖率 94.43%，四关全绿；抖音模块 92-95% |
| 7 | BilibiliAdapter | ✅ | 见 git log | 1090 passed（累计，净增 169），覆盖率 94.32%，四关全绿；B站 模块 90-100% |
| 8 | TaskRunner + TaskRegistry + 6 个 handler | ✅ | 见 git log | 1172 passed（累计，净增 82），覆盖率 94.67%，四关全绿；`tasks/` 各模块 92-100%，`task_runner` 93% / `task_registry` 94% |
| 9 | FastAPI app + 全部 API 路由 + SSE | ✅ | 见 git log | 1208 passed（累计，净增 36），覆盖率 93.89%，四关全绿；`api/` 各路由 82-100%，`main.py` 58%（lifespan 胶水 + cli 未全覆盖） |
| 10 | 前端脚手架 + 孟菲斯 tokens（可与 Task 3-9 并行） | ✅ | 见 git log | 前端 20 passed（覆盖率 90.3% stmts / 86.7% funcs，门槛 70），后端 1301 passed 不回归；`tsc --noEmit` / eslint `--max-warnings=0` / stylelint / prettier / `vite build` 五关全绿；令牌的"三件事"用 computed style 验过（ADR-0013） |
| 11 | 前端 API 层 + SSE + stores + Router + Layout | ✅ | 见 git log | 前端 **59 passed**、覆盖率 89.4% stmts / 82.1% funcs（门槛 70）、tsc/eslint+stylelint/prettier/build 全绿；`schema.d.ts` 由快照生成并双向核同源；`event-schema.md §7` 的 `onmessage` 样例改为能跑的形状（经验 35） |
| 12 | 页面 Dashboard / Feed / Settings / Preflight | ✅ 4/4 | 见 git log | 前端 **164 passed**、覆盖率 91.54% stmts / 87.33% funcs（门槛 70）、tsc / eslint+stylelint / prettier / build 五关全绿；`QueryState` 把四态互斥从一页变成五页共用；作品流虚拟滚动 + 墓碑可取消；抓掉两个真 bug（`hidden` 查询参数与契约 enum 漂移、`cn()` 把 `border-memphis` 吃掉 → 经验 38/39） |
| 13 | 页面 VideoDetail / Creators / Tasks（V2.0 最小版） | ⬜ | — | — |
| 14 | 契约测试抽象基类 + 16 条 V1 陷阱看护 | ✅ | 见 git log | 1239 passed（累计，净增 31），覆盖率 93.89%，四关全绿；`PlatformAdapterContractTests` 基类 + 抖音/B站 两实例子类 + §7→用例名索引漂移看护 |
| 15 | `tools/migrate_v1.py` | ✅ | 见 git log | 1243 passed（累计，净增 4），覆盖率 93.89%，四关全绿；对**造出的 V1 schema** 验 dry-run/幂等/墓碑/媒体 hardlink/只读 |
| 16 | CI 验证 + 端到端 smoke + 收尾文档 | ⬜ | — | — |

**门禁现状**（每次提交前都要全绿）：

```bash
cd E:/08-Codework/Intelligence-Hub-V2
.venv/Scripts/python.exe -X utf8 -m pytest tests/ -q   # 1243 passed，覆盖率 93.89%（门禁 80%）
.venv/Scripts/python.exe -X utf8 -m ruff format --check src/ tests/
.venv/Scripts/python.exe -X utf8 -m ruff check src/ tests/    # All checks passed
.venv/Scripts/python.exe -X utf8 -m mypy src/                 # no issues found in 77 source files
```

全套 **约 109 秒**（带覆盖率）。Task 8 之前是 ~50 秒 —— 净增的 82 条用例里有一部分
走真 SQLite 内存库 + 真清单落盘（集成层），这笔账记在 `docs/lessons.md` 经验 15 的延长线上。

**可选依赖已装且部分真跑过**（2026-09-22）：`yt_dlp` 2026.8.19 是从 venv 里直接调用的
（`yt-dlp -J <BV…>` 拿到 15 条 formats、`--flat-playlist` 拿到那句真 412），
`curl_cffi` / `zhconv` / `sherpa_onnx` / `numpy` / `playwright` `find_spec` 命中但未使用。
装法留在这里备用：`.venv/Scripts/python.exe -m pip install -e ".[media,asr,bridge]"`。
**仍然缺的是 PATH 上的 `ffmpeg` / `ffprobe`**（V1 §7.19 那个"注册表里有、进程快照过期"
的现象在这个 shell 里照样成立）—— Task 8 之前要走工作台的 PATH 补齐或重开宿主。
`has_audio_stream()` 已经改成"缺二进制算问不出来"（坑 18），所以缺 ffprobe 不会再
把一次成功的下载判成采集失败。

**实施期对 spec 的偏离**全部记在 `docs/lessons.md`「V2 新增」一节（7 条坑）与
「实施阶段」一节（5 条方法论），涉及的两份 spec 已就地加修订说明：
`docs/specs/task-runner.md §2.6`（`Manifest.error`）、`docs/specs/config-schema.md §1 / §3`
（YAML 加载方式、`platforms.yaml` 扁平化、`advanced: Any`）。

---

## 待办池（不阻塞里程碑，但记下来不忘）

- [ ] **`platforms` 镜像的健康三列在生产里没人写**：`set_health` 只有测试调用方，而
  `repositories/platforms.py:93` 的注释写着"由 preflight / 采集任务写"。
  真修要先决定一件事：让 preflight 回写镜像，它就**不再是纯只读探测**（会写库、状态跨重启留下）
  → 过一条 ADR，再补"探测之后镜像列非 NULL"的集成用例，并把那句注释改成实话。
  总览页目前的做法是**不画健康灯**（见 `docs/lessons.md` 经验 40）
- [ ] `_check_requires`：要先有一份缓存的能力快照（preflight 结果 + TTL + 失效点）→ ADR-0014
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
