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

### V2.0「骨架可用」 — 代码全部落地：16 条判据里 12 条已勾，剩 4 条全卡在"真机/真 CI 那一跑"

> 2026-09-23：Task 8-16 全落（后端 + 七个前端页面 + 契约测试 + 迁移 + CI 与全栈冒烟）。
> 2026-09-24（V2.1 的 T0.1）：**CDP 桥已移植进 V2 并真机验过**（`bridge/server.py` + 真 Chrome），
> "桥"那一条整条勾上。**没打 tag**。剩下四条没有一条是代码缺口：抖音 Adapter 与 B站 Adapter 的
> 真采一跑（要已登录的 Chrome + shell 侧看得见的 ffmpeg）、
> `make ci-local` / GitHub Actions 那条从未真跑过
> （本机连 `make` 都没有，`AGENTS.md §4` 已改）、live `data/` 那一次迁移要不要点头。

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
- [x] **配置层**：Pydantic Settings + YAML 加载（优先级 默认 < app.yaml < platforms.yaml < env < CLI）+ `PUT /api/platforms/{name}/config` 原子写盘 + 热加载 + ConfigChanged 事件
  —— 写盘 + 热重载 + 广播那一条链**真跑过一次**，跑在临时目录：
  `test_put_config_persists_and_gates` 对一份 tmp 的 `platforms.yaml` 做真 PUT，
  断言"关掉之后 `/api/tasks` 里没了 **且** `scheduler._configs` 那份也变了"
  （那是审查轮抓到的真 bug：三处各握一份 configs 快照，只改 manager 等于关不掉平台）。
  env 侧两种拼写（`..._DATA_DIR` 与 `..._DATA__DIR`）现在都吃，嵌套那份优先。
  **没做**：对仓库自己那份 `config/platforms.yaml` 下手 —— 那会改写真实配置，留给手动。
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
  **前置已解一半**：桥在 2026-09-24 的 T0.1 移完并真机验过（见上面"CDP 桥"那一格），
  剩下来的是人的那一步 —— 在桥拉起的 Chrome 里扫码登录一次。
- [ ] **B站 Adapter**：移植 V1 `download_bili_following_latest.py`，含 cookie 三档（§7.15）、DASH 未合并分片处理（§7.21）、字幕优先
  —— 代码与 169 条用例完成（Task 7）。**已真机验过的部分**（2026-09-22 本机，匿名）：
  `x/web-interface/view` / `x/player/v2` / `x/web-interface/card` 三个接口的真响应形状
  （存成 `tests/fixtures/bilibili/`，见 `docs/lessons.md` 经验 19）、
  `yt-dlp -J` 的 15 条 formats（证实未合并 DASH 是默认形状）、
  `yt-dlp --flat-playlist` 匿名那句真 `Request is blocked by server (412)`。
  **未验**：带登录 cookie 的实际媒体下载、非空字幕轨（两者都要有效会话 cookie，
  而 V2 的 `data/cookies/` 现在没有）。整条不勾。
- [x] **CDP 桥**：移植 V1 `cdp_bridge_server.py`（保留只绑回环约束）+ `BridgeClient` 包装 + 桥健康检查 + 自愈逻辑（§7.20）
  —— 2026-09-24（V2.1 T0.1）落 `src/intelligence_hub_v2/bridge/server.py`；客户端在
  `infra/cdp_bridge.py`（Task 5 就有），默认 profile 与重建冷却改成从 `config/app.yaml` 取，
  `make bridge` 从 `false` 占位改成真命令。**真机跑过**（本机 Chrome，`pytest -m real_network`
  7.6 秒）：`python -m intelligence_hub_v2.bridge.server --headless` 起服务 → `BridgeClient`
  真连 → `/health` 200 `ok:true` → `/navigate` 到本机一页 → `/evaluate () => document.title`
  取回 `cdp-bridge-probe` → `/cookies` 回列表。**自愈也真验了**：杀掉桥拉起的 8 个 chrome 进程后
  `/health` 回 503（`page_url` 空、`last_page_url` 只当线索），下一条 `/evaluate` 2.1 秒用
  **同一个 profile** 重建并成功，`restarts=1`（所以 `BROWSER_DEAD_MARKERS` 那几句原文在
  Playwright 1.63 上仍然认得）。**未验**：扫码登录后的真采集 —— 那是 T3.1/T3.2，不欠在桥这一条上。
- [ ] **任务调度**：`TaskRegistry` + 6 个核心任务（preflight / douyin_collect / bilibili_collect / single_link / add_creator / postprocess）+ 平台级 Semaphore 限流 + CancelToken + 超时 + 清单双写（文件 + SQLite，强制终态）
  —— **真机已经通了不带平台的那一半**：2026-09-23 全栈冒烟里从 `#/tasks` 点「跑一次」，
  `preflight` 真起了一条 run，事件按 `task.started → manifest.written → task.finished`
  落地、清单文件真在磁盘上、summary 说实话（`platforms_ok=0 degraded=2`）。
  采集类（`douyin_collect`/`bilibili_collect`）仍缺**登录态**与 shell 侧看得见的 ffmpeg（桥本身 2026-09-24 已通）。
  —— 代码 + 离线用例完成（Task 8：`core/task_runner.py` / `core/task_registry.py` /
  `tasks/`）。12 个任务全登记、V2.0 只给 6 个真 handler（其余 6 个 `implemented=False`，
  不进 `/api/tasks`、点名运行红在 `NotImplementedError`）。`TaskRunner` 五条退出路径
  （成功/partial/报告失败/抛异常/取消/超时）都在 `manifest_writer` 上落终态 + 发对应事件，
  `TaskScheduler` 收口平台开关门 + 全局/每平台 Semaphore + 协作式 `CancelToken`。
  **整条不勾，因为"真机验过"这一半还没有**：采集类任务（douyin_collect/bilibili_collect）
  跑通仍需登录态的 Chrome + ffmpeg（桥那一段 T0.1 已通），与 Task 6/7 同一前置。Task 9 通了之后
  随抖音/B站 里程碑一起补那一跑。
- [x] **前端骨架**：React 19 + TS 5.9 + Vite 8 + Tailwind v4 装好，孟菲斯设计令牌落盘
  —— 措辞按实际改过两处：**没有 shadcn/ui 也没有 `tailwind.config.ts` / `postcss.config.js`**
  （ADR-0013 定了 `tokens.css` 的 `@theme` 是唯一真源，`tokens.json` 是它的单向投影）。
  令牌的"三件事"在真浏览器里量过 computed style：卡片 `3px solid` + `radius 0` + 阴影无模糊，
  全站非 0 圆角只有徽章。五关（tsc / vitest / eslint+stylelint / prettier / build）全绿。
- [x] **前端七页**：Dashboard / Feed / Settings / Preflight（Task 12）+
  VideoDetail / Creators / Tasks（Task 13），全部在真浏览器里带真 V1 数据量过
  —— 一处**故意没做**：Dashboard 不画"平台健康灯"。那三列（`platforms.health_status`）
  在生产里没有任何写者（`set_health` 只有测试调用），画出来会永远灰着"从没检查过"；
  要它就得让 preflight 回写镜像 → 先过 ADR（`docs/lessons.md` 经验 40）。
  页面画的是配置事实（`enabled` / `implemented`）并写明"探测去预检页"。
  虚拟滚动实测 19 条数据只画 13 个 DOM 节点；`#/tasks` 的发起按钮由 `params_schema` 判据决定（经验 42）。
- [x] **前后端打通**：OpenAPI → TS 类型自动生成（`openapi-typescript`，从**快照文件**生成，
  不要求后端在跑）+ TanStack Query + SSE 订阅 + `ui:hidden` 由 `lib/schema-form.ts` 执行
  —— 真机实测：SPA 由 FastAPI 单端口 serve，`#/tasks` 点「跑一次」真起了任务，
  事件流那栏亮的是"事件流已连上"。**没在真机跑的只有半句**：
  "平台开关切到后端通"里那次 `PUT /api/platforms/{name}/config` 会重写仓库自己的
  `config/platforms.yaml`，所以只在 jsdom 用例里钉了请求体形状，没对真配置目录下手。
- [x] **测试**：2026-09-23 实测 `pytest -m "not real_network and not e2e"` **1301 passed**、
  两道覆盖率门（全局 ≥80 / `platforms`+`tasks`+`tools` ≥90）都过、
  前端 **197 passed**（覆盖率 93.74% stmts / 92.24% funcs，门槛 70）、
  `pre-commit run --all-files` **exit 0（22 个 hook）**、alembic 往返 + `alembic check` 干净。
  §7 逐条归属由 `test_contract_guard_index.py` 与 `AGENTS.md §5` 三栏互核（文档说有而实际没有，
  是这个仓库踩过两次的坑）。
- [ ] **DevEx**：Makefile + pre-commit + GitHub Actions CI（lint / test-backend / test-frontend / build / e2e 手动触发）+ `make ci-local` 一键本地跑全套
- [x] **文档**：13 份 ADR（≥10，编号不复用）+ 7 份 spec + AGENTS.md + README +
  CONTRIBUTING + architecture.md + lessons.md（43 条经验）全部落盘并已 commit
  —— 判据不是"文件在不在"，是 `docs/specs/contract-tests.md §3/§4` 与 `AGENTS.md §5`
  那两张表**有用例在双向核**：文档点名的用例不存在 → 红；文档漏了一条在跑的守卫 → 也红。
  这一轮又添了经验 38-43（六条里有四条是"文档/注释承诺了代码里没有的东西"同一族）。
- [x] **迁移脚本**：`tools/migrate_from_v1.py` dry-run + **真数据写库一跑都过了**
  —— 2026-09-23 对真 V1 库跑进临时目录（与 V1 同卷）：
  `creators=4 videos=21 transcripts=16 墓碑=2 媒体 link=21 copy=0 missing=0`。
  两处"看着像丢数据"的只读复核：8 条 B站 作品 `creator_id` 为空 = V1 那几行本来就是 `''`
  （V1 的 videos 表没有 mid 列）；`view_count` 全 null = V1 的 `metrics_json` 21 条全空。
  **等点头的那道闸已经装上**：`--rollback` 于 2026-09-24（T6.2）实现，live `data/` 那一跑
  现在写得也撤得回（撤的是数据行 + 状态文件，媒体文件按设计不动）。真数据量过一遍：
  真迁移 `link=21 copy=0` → `--rollback --dry-run` 报 `videos=21 creators=4 transcripts=16` →
  真回滚后三张表归零、21 个媒体文件仍在盘上 → 换 `--media-strategy=reference` 重迁，
  又搬进 21 条（这一跑就是为了证明"状态文件跟着回滚走"这件事成立）。
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
- [x] ASR 引擎接入（sherpa-onnx SenseVoice，§7.9 标点注入）
  —— 2026-09-24（V2.1 T1.1）落 `src/intelligence_hub_v2/asr/`：静音切句 + 反幻觉闸 +
  切点补标点（§7.9 已升进契约测试看护）+  recognizer 缓存；引擎交回契约对象 `Transcript`
  （带逐句时间戳），抽音频仍走 `infra/ffmpeg.extract_audio()`。**真机量过**：真权重 +
  一支 67 秒真口播 → 7.5 秒转完、11 句 / 538 字、9.0× 实时，每行以标点收尾。
  还差下一格：`postprocess` 把它接进任务链（T1.2），"输出 speech-clean.txt"那一步在今晚之后。
- [x] `PostprocessTask` 跨平台统一（废 V1 三份 postprocess_*）
  —— 2026-09-24（T1.2）：`tasks/postprocess.py` 现在真的跑 ASR（抽音频 → 切句转写 →
  归一 → `speech-clean.txt` + `segments.json` + `reference.md` → `transcripts` 行）。
  V1 的两份脚本（platform / bili）合成一份；DASH 未合并时喂 ffmpeg 的是音频轨（§7.21）。
  产物三份，V1 的 `speech-raw.txt` 不落（与 clean 只差空白折叠）。
  **拆出一条**：摘要/要点这两样当时只在磁盘上 → 见下一条（T1.2b，同日落地）。
- [x] 参考材料两列出库：`transcripts.content_summary` / `.key_points`（+ `.summary_method`）
  —— 2026-09-24（T1.2b，ADR-0015）：V1 那两列在迁移里**会静默丢掉**（脚本对它们的引用数是 0），
  现在搬得过来了，而且是逐行验过的：真 V1 库 → 真迁移 → 13/13 行与 V1 **字节一致**，最长 2982 字
  不截断（迁移不许编辑数据）。放 `transcripts` 不放 `videos` 的依据是实测：13 条摘要**全部**
  落在"已转写"那 16 行上（"有摘要没稿子"= 0 条），而 `attach()` 整行替换让摘要与稿子天然同源。
  第三列 `summary_method` 是量出来必须加的 —— 同一条《作弊》V1 那份是 1459 字整篇改写、
  V2 的抽取式是 600 字原文片段，不标来源就分不出该信哪条。
  真机另一跑：同一条 V1 抖音作品（63.8 MB / 589.7 秒音频）过真 ffmpeg + 真权重 →
  `SELECT` 得到 `local-extractive` 的摘要与 8 条要点，且**每条要点都能在正文里找到原样片段**。
  字幕那条路从此也产参考材料（否则 B站 有轨作品的两列永远是空的）。
- [x] 字幕优先路径（B站 / YouTube）
  —— 2026-09-24（T1.3）：`postprocess` 现在的顺序是"先问字幕 → 拿到就用；确实没有轨
  就回落到本地 ASR；问失败了（412 这类）记失败、**不**去起 ffmpeg"。
  计数从 `no_subtitle`（V2.0 记一笔就过去，那条作品永远不会有稿子）改成
  `subtitle_missed`（含"已回落"这半件事）。用例：
  `tests/integration/test_bili_subtitle_preferred.py` 三条。
  真 B站 视频 + 真字幕 API 那一跑仍欠在 T3.2（要网络与登录 cookie）。
- [ ] 前端 Video Detail 页（视频播放 + metadata + 口播稿时间戳跳转）
  —— V2.0 已有最小版（`pages/VideoDetail.tsx`：metadata + 口播稿全文，**无播放器**，
  页面上写明了）。这一条要的是播放器与时间戳跳转，别从零再建一遍。
- [ ] 前端 Creators 页（博主库 + 跟踪开关 + 添加博主 + 爆款回溯入口）
  —— V2.0 已有最小版（列表 + 开关 + 收录表单，真数据下量过 4 张卡）。缺的是爆款回溯入口。
- [x] 前端 Tasks 页（任务卡片墙 + 运行历史 + 实时事件流 + 取消）
  —— V2.0 的最小版就把这四件做全了（`pages/Tasks.tsx`）：卡片墙的发起判据来自
  `params_schema`，事件流是真 SSE（连上时亮"事件流已连上"），取消只对 `running` 出现。
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
| 13 | 页面 VideoDetail / Creators / Tasks（V2.0 最小版） | ✅ | 见 git log | 前端 **196 passed**、覆盖率 93.74% stmts / 92.24% funcs；五关全绿。"能不能一键发起"由任务自己的 `params_schema.required` 决定（不是前端记清单）；202 只说"已排队"不说"已添加"；抓到一处手抄契约的响应类型并加了 18 个调用点的源码级同源看护（经验 41） |
| 14 | 契约测试抽象基类 + 16 条 V1 陷阱看护 | ✅ | 见 git log | 1239 passed（累计，净增 31），覆盖率 93.89%，四关全绿；`PlatformAdapterContractTests` 基类 + 抖音/B站 两实例子类 + §7→用例名索引漂移看护 |
| 15 | `tools/migrate_v1.py` | ✅ | 见 git log | 1243 passed（累计，净增 4），覆盖率 93.89%，四关全绿；对**造出的 V1 schema** 验 dry-run/幂等/墓碑/媒体 hardlink/只读 |
| 16 | CI 验证 + 端到端 smoke + 收尾文档 | ✅（真机两条除外） | 见 git log | 门禁直接跑（本机**没有 make**）：ruff/mypy/alembic 往返/pytest **1301 passed**/两道覆盖率门 `pre-commit run --all-files` **exit 0（22 hook）**。全栈冒烟用真 V1 数据写进临时目录（4/21/16、link=21 missing=0，**没动 live `data/`**）+ 浏览器量 computed style 与 DOM：七个页面全过、虚拟滚动 19 条只画 13 行、点「跑一次」真的落清单与事件。抓到并修掉 `#/tasks` 把信封当 schema 读（经验 42）。**tag 没打**：抖音/B站 真机采集与 live `data/` 迁移两条还没过，给一个"已交付"的 tag 正是这仓库反对的那种绿 |

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
**仍然缺的是 PATH 上的 `ffmpeg` / `ffprobe`** —— 2026-09-24 由 T6.1 解掉：
`core/runtime_env.py` 在服务启动与每轮 preflight 时向注册表要一次真相（V1 §7.19 那条
「注册表里有 ≠ 进程拿得到」）。本机实测：补之前 `shutil.which('ffmpeg') → None`、
补之后 ffmpeg/ffprobe/node/yt-dlp 四个全部解析到。`config.paths.*` 那一组从此有读者
（显式路径 = 把它的目录插到 PATH 最前面，指的文件不存在就如实记问题）。
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
- [ ] 桥服务要不要拆成独立仓库（V2 已移植在 `src/intelligence_hub_v2/bridge/server.py`；
  拆出去的好处是 V1 / V2 / 采集脚本共用同一个提供方，代价是多一个发布面）
- [ ] 暗色模式的设计令牌（孟菲斯暗色版色板需要单独调）
- [ ] Visual regression 测试方案（本地 `make screenshots` 抓基线 + PR 人眼比对，不上 Chromatic/Percy）
- [ ] 多语言 UI（V1 全中文，V2 暂时也全中文，i18n 留 V2.x）
- [x] 数据导出（CSV / JSON）
  —— 2026-09-24（V2.1 T6.4）：`GET /api/export?entity=videos|creators&format=csv|json`
  带 `platform` / `hidden` / `search` 三个筛选，`Content-Disposition: attachment` 直接下载；
  作品流与博主页各有一对导出链接，**带的是屏幕上已应用的筛选**。
  三件外部可见的规矩都有用例钉着：CSV 中和公式起手（`= + - @` 与带前导空白的变体，
  只中和**字符串**，负数不能被改坏）、UTF-8 BOM（没它中文 Windows 的 Excel 整篇乱码）、
  以及**翻页翻到 total 对齐**（列表端那个 `size<=200` 是列表页的要紧事，不是导出的上限 ——
  少翻一页得到的是一份"看起来完整"的 CSV）。文件名不含任何请求输入（防 header injection）。
  数字：`export.py` 语句覆盖 100%，新用例后端 17 条 + 前端 4 条，全量 1556 passed / 93.57%。
- [ ] 定时任务调度 UI（V2.0 后端有 APScheduler，但前端没暴露）
  —— **第一片已落（2026-09-24，T6.3）**：`scheduler.collect_cron` 那一族配置真的会排 cron job，
  并且有用例证明"到点真起任务"（真 APScheduler，不 mock 时钟）。剩下的是 UI 那一半：
  `api/v1/schedule.py`（列 job / 改配置 / `action: run_now`）+ Settings 上那块开关。
  现在改 cron 要编辑 YAML + 重启 —— 所以这一条**没勾上**，别当成已完。`docs/adr/0017`。

---

## 跨会话续上下文的入口

新会话进来读这三份就够：

1. **本文件**（`ROADMAP.md`） — 知道在哪个里程碑、剩什么没做
2. **最近一份 `docs/progress/YYYY-MM-DD.md`** — 知道上次干到哪、卡在哪
3. **`AGENTS.md`** — 知道工程纪律与已知陷阱
