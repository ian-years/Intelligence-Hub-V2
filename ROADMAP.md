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

### V2.0「骨架可用」 — 16 条判据：12 勾 / 4 未勾，未勾的全是"某一跑"，没有一条是代码缺口

> 2026-09-23：Task 8-16 全落（后端 + 七个前端页面 + 契约测试 + 迁移 + CI 与全栈冒烟）。
> 2026-09-24（V2.1 的 T0.1）：**CDP 桥已移植进 V2 并真机验过**（`bridge/server.py` + 真 Chrome），
> "桥"那一条整条勾上。**没打 tag**。
>
> **2026-09-26 状态对账**（上一份写这段时是 09-23，之后 09-25 那一整天真机 5/5 + 总闸都发生在本节
> 下面那 16 条判据的盲区里）：本节此前挂着 5 条 `[ ]`，逐条对代码与 live 库核过之后，
> **"迁移脚本"那一条的前提是错的**（原文写"这台机器没有真实 V1 `data/`"，实际 V1 库在
> `Intelligence-Hub/downloads/local.sqlite3`，我今天只读打开数过：`creators=4 videos=21`，
> 与 `docs/progress/2026-09-25.md` §9.4 那句 dry-run 的 4/21 逐格对得上），已勾。
> 剩下 4 条各写成"还差哪一跑"，并给出能粘贴的命令；判据文本里点名的东西**只要有一件没真跑过就不勾**，
> 所以抖音那一条即使三家真采都通了也仍然挂着 —— 它点名了短链那一档。
> 对账当天空跑门禁复量：`pytest -m "not real_network and not e2e"` **2488 passed / 15 deselected**、
> `ruff format --check` 252 files clean、`ruff check` clean、`mypy src/` 138 files clean。

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
  —— 代码与契约测试完成（Task 6：`platforms/douyin/`，91 条用例，§7.1/§7.2/§7.3 各有看护）。
  **真机那三件里两件已经过了**（2026-09-25 §9.1，写进 live 库的真跑）：`douyin_collect {"limit":2}`
  → `downloaded=2 failed=0`，两条媒体真落盘（57,668,479 B / 37,437,709 B，`ffprobe` 认得出
  `h264 + aac`），两条的 `media_source` 都是 `page_play_url` 而 `yt_dlp_error` 留的是原文
  —— **§7.2 那条兜底第一次在真数据上成立**；cookie 阶梯两档都试过且各自留下证据。
  **整条不勾只剩一格：短链 302 → sec_uid 那一档没在真机上走过。** 我核的是 live 库
  `task_runs.params_json`：09-25 那次 `add_creator` 的输入是
  `https://www.douyin.com/user/MS4wLjABAAAA…`（**长链**），也就是 `parse_creator_url` 真的执行了、
  真的交回了 sec_uid，但没有一次输入是 `v.douyin.com/xxxx` 那种分享短链。
  补这一跑要什么：手机抖音复制一条博主主页分享链接，`POST /api/tasks {"name":"add_creator",
  "params":{"url":"<那条 v.douyin.com 短链>"}}`，判据是库里那行的 `platform_id` 是 `MS4w` 开头
  而不是把短链原样存下来（V1 §7.1 存的就是这个错，`docs/lessons.md` 有那条）。
- [ ] **B站 Adapter**：移植 V1 `download_bili_following_latest.py`，含 cookie 三档（§7.15）、DASH 未合并分片处理（§7.21）、字幕优先
  —— 代码与 169 条用例完成（Task 7）。**2026-09-22 匿名验过**三个接口的真响应形状
  （`view` / `player/v2` / `card`，存成 `tests/fixtures/bilibili/`，见 `docs/lessons.md` 经验 19）、
  `yt-dlp -J` 的 15 条 formats、`--flat-playlist` 匿名那句真 412。
  **登录档在 2026-09-25 真过了**（§9.7，live 库）：`bilibili_collect` → `downloaded=1`、
  23,634,711 B、单文件里 `av1 + aac` → **DASH 真合上（§7.21）**，而清单里
  `cookie_rungs: 带导出的登录 cookie=1` —— 这一格在修复前是 `failed` + `FileNotFoundError:
  data\cookies\bilibili.com.txt`（`--cookies` 传相对路径，登录档从没生效过；live 库里那两条
  failed run 就是它，10:13 与 10:17）。字幕那条链也真跑了，但**只验到"问得出'没有'"**：
  `subtitle_missed=1` + 回落 ASR 出 85 句 / 8112 字，那条 BV 本来就没有轨。
  **整条不勾就剩一格：非空字幕轨。** 补法：找一条确定挂了字幕的 BV（番剧/发布会那类），
  `POST /api/tasks {"name":"single_link","params":{"url":"https://www.bilibili.com/video/BV…"}}`
  → `postprocess`，判据是清单里 `subtitle_used`（不是 `subtitle_missed`）且稿子句数与轨对上。
- [x] **CDP 桥**：移植 V1 `cdp_bridge_server.py`（保留只绑回环约束）+ `BridgeClient` 包装 + 桥健康检查 + 自愈逻辑（§7.20）
  —— 2026-09-24（V2.1 T0.1）落 `src/intelligence_hub_v2/bridge/server.py`；客户端在
  `infra/cdp_bridge.py`（Task 5 就有），默认 profile 与重建冷却改成从 `config/app.yaml` 取，
  `make bridge` 从 `false` 占位改成真命令。**真机跑过**（本机 Chrome，`pytest -m real_network`
  7.6 秒）：`python -m intelligence_hub_v2.bridge.server --headless` 起服务 → `BridgeClient`
  真连 → `/health` 200 `ok:true` → `/navigate` 到本机一页 → `/evaluate () => document.title`
  取回 `cdp-bridge-probe` → `/cookies` 回列表。**自愈也真验了**：杀掉桥拉起的 8 个 chrome 进程后
  `/health` 回 503（`page_url` 空、`last_page_url` 只当线索），下一条 `/evaluate` 2.1 秒用
  **同一个 profile** 重建并成功，`restarts=1`（所以 `BROWSER_DEAD_MARKERS` 那几句原文在
  Playwright 1.63 上仍然认得）。**扫码登录后的真采集 2026-09-25 也过了**（三家各走桥采到真东西，
  §9.1/§9.7），桥这一格从此没有欠账。
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
  —— **登记数我今天从 `task_registry.TASKS` 重数过：13 条，不是 12**；其中 **9 条有真 handler**
  （那 6 条 + `enrich_metrics` + `xiaohongshu_collect` + `youtube_collect`），4 条
  `implemented=False`：`all_platforms` / `backfill` / `feishu_sync` / `migrate_from_v1`。
  **采集类那一半在 2026-09-25 真过了**：live 库 `task_runs` 18 条 run，六个核心任务每个都有
  `success` 记录（`params_json` 与 `error_text` 我逐条读过）。
  **整条不勾剩一格：取消与超时没有真机证据。** 同一个库 `select status,count(*)` 得到
  `failed=5 / partial=2 / success=11` —— **`cancelled` 零条**，也就是那五条退出路径只有四条
  在真进程上走过，"用户点取消"这一条全靠替身层用例撑着（连带"清单强制终态"这件事在取消
  这一支上也只在替身层验过）。补法：`douyin_collect {"limit":20}` 起一条，进入 running 后
  `POST /api/tasks/runs/{id}/cancel`；判据三条：清单落 `cancelled` 终态、事件流有
  `task.cancelled`、**已落盘的媒体没有被回滚删掉**（这一条最容易悄悄做错）。
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
  —— 四件里三件在树里（Makefile 有 recipe、`.pre-commit-config.yaml` 22 个 hook 全绿、
  `.github/workflows/` 那份 CI 文件存在），**但从没有一台机器真跑过它**：
  ① `git remote -v` 是空的 —— 没有远端就没有 Actions，那一份 workflow 从写下起一行日志都没产出过；
  ② 本机 Git for Windows 里没有 `make.exe`（`AGENTS.md §4` 记着），所以 `make ci-local`
  这条"一键跑全套"从未按它自己的形式跑过一次，日常是照 recipe 逐条手跑；
  ③ 逐条手跑的数字见上面"测试"那一格与 `docs/progress/2026-09-26.md`。
  **2026-09-26 用户点头**：建远端并允许 push，让 Actions 第一次真跑（这是这一格唯一的堵点，
  不是代码缺口）。跑通之前整条不勾。
- [x] **文档**：**24 份** ADR（编号到 0025，`0014` 空着不复用；判据要的是 ≥10）+ 7 份 spec +
  AGENTS.md + README +
  CONTRIBUTING + architecture.md + lessons.md 全部落盘并已 commit
  —— 判据不是"文件在不在"，是 `docs/specs/contract-tests.md §3/§4` 与 `AGENTS.md §5`
  那两张表**有用例在双向核**：文档点名的用例不存在 → 红；文档漏了一条在跑的守卫 → 也红。
  这一轮又添了经验 38-43（六条里有四条是"文档/注释承诺了代码里没有的东西"同一族）。
  **这一格原来那句"lessons.md（43 条经验）"是一个数不出来的数**（2026-09-26 对账时删掉）：
  `docs/lessons.md` 里有两种体裁并存 —— 编号到 `#### 经验 52 ·` 的那一批，和 09-25 之后
  不编号的 15 条 `### 经验：…`。写一个总数就等于写一个没人能复核的数，所以只写"编号到哪一条"。
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
  `transcripts` 表 + 磁盘 `speech-clean.txt`；媒体 hardlink、跨卷退 copy。
  **这一段原来还写着"整条不勾：这台机器没有真实 V1 `data/`，dry-run 是对造出的 V1 schema 跑的"
  —— 那句话是错的**（2026-09-26 对账时核出）：真 V1 库一直在 `Intelligence-Hub/downloads/local.sqlite3`，
  我今天只读打开数过 `creators=4 videos=21`，与上面那句 dry-run 的 4/21 逐格对得上，
  而 §9.4 那一跑是**对 live `data/` 写的真迁移**（`creators=4 videos=19 reference=21 已存在跳过=2 missing=0`）。
  也就是说这一格从来不是"缺一次跑"，是缺一次改文档。

**为什么先做抖音 + B站**：V1 §6 表里这两个平台真机已实测全通；抖音的 `a_bogus` 兜底与 B站的 cookie 三档 + DASH 分片是 V1 最难的两条经验，先用 V2 接口表达清楚，剩下两个平台照葫芦画瓢。

### V2.1「四平台齐全 + 转写完整」 — 规划中

> 2026-09-25（V2.1 批量推进 + 同日补完 + 当晚真机）：**Phase 2 / 3 / 4 / 5 / 6 全收满**。
> Phase 3 五条当晚全部真跑过（抖音 2 条 / B站 1 条 / 小红书 1 条图文 / 三条口播稿 /
> 对 live 库迁移 21 条），并且**只有真跑才炸出来的四个缺陷当场修掉**：
> B站 评论的身份键名是合成 fixture 编的（这功能从没成功过）、`--cookies` 传到子进程是相对路径
> （登录档从没生效过，§7.15 的静默退档）、`single_link` 把 `xsec_token` 压掉（小红书详情一律被
> 跳去风控页）、ADR-0019 的 `has_video` 从没出口到前端（图文笔记挂出黑播放器）。
> 另修一个 e2e 关不上服务的竞态。判定过程与数字在 `docs/progress/2026-09-25.md` §9。
> 上午那一批欠的三样下午都清了：`PlatformAdapter` 的契约口（T4.1/T4.2 调用侧，ADR-0020 决定二）、
> 工坊页截图包的真数据源（ADR-0024）、工坊页的 Playwright 那一腿。
> 一次会话落了 T2.1 二片 / T2.2 / T5.2-T5.7 / T6.3 二片 / T6.5-T6.8，加 7 份新 ADR（0019-0024 +
> 0020 的"分期"改为已落）。
> 全程数字与逐条归属见 `docs/progress/2026-09-25.md`；**待你点头的 26 件事在它的第 7 节**。
> 一句话摘要（2026-09-25 深夜，含平台总闸那一批之后）：`pytest` 2488 passed / 覆盖率 94.93%（`platforms+tasks+tools` 95%）、
> 前端 325 passed、e2e 11 条（真 Chromium + 真 uvicorn，`make e2e`），
> 另有一条真 ffmpeg 的 `-m real_network` 烟雾（3 passed）。

- [x] 小红书 Adapter（依赖桥的页面 JS 注入）
  —— 2026-09-24/25（V2.1 T2.1）三片齐：共用纯解析层（ADR-0016）→ 适配器落地并注册 →
  202 条契约测试 + 16 份合成 fixture（`tests/fixtures/xiaohongshu/README.md` 明写了它挡不住什么）。
  四个模块覆盖率 23/35/37/33% → 98/100/100/99%。**真采一条仍缺登录态**（T3.3）。
- [x] YouTube Adapter（纯 yt-dlp，本机网络不可达时如实失败）
  —— 2026-09-25（T2.2）四平台齐：枚举走共用的 `--flat-playlist -j`，字幕是 V2 新兑现的一条
  （V1 那 630 行没有字幕码）。`needs_cookies=False` → 配置里那一格不出现 `cookies_file`（ADR-0012 同口径）。
  V1 的"下载完 glob 第一个 mp4"那个静默交纯视频轨的缺陷没有复刻。**本机到 YouTube 不通，真采未验**。
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
- [x] 前端 Video Detail 页（视频播放 + metadata + 口播稿时间戳跳转）
  —— 2026-09-25（T6.5）：字节走新的 `GET /api/videos/{id}/media`（HTTP Range + 只认 `data/media/` 底下
  + 只放视频容器扩展名，两条判据同一个文件里），口播稿有 `segments_json` 就渲染成可点时间轴。
  —— V2.0 已有最小版（`pages/VideoDetail.tsx`：metadata + 口播稿全文，**无播放器**，
  页面上写明了）。这一条要的是播放器与时间戳跳转，别从零再建一遍。
- [x] 前端 Creators 页（博主库 + 跟踪开关 + 添加博主 + 爆款回溯入口）
  —— 2026-09-25（T6.6）：「爆款回溯」面板就是 `/api/videos?creator_id=&sort=benchmark`，
  面板画的顺序等于后端给的顺序（前端不重排）。按的是 `videos.like_count`（最近一次入库读数 =
  已知最高水位），不是快照峰值 —— 理由写在那条 commit 与代码注释里。
  —— V2.0 已有最小版（列表 + 开关 + 收录表单，真数据下量过 4 张卡）。缺的是爆款回溯入口。
  **别把这一格和下面那条 `BackfillTask` 读成同一件事**：这个面板只排**库里已有**的作品，
  一次网络请求都不发；"去平台上把这位博主的代表作捞回来"那条路是 `backfill`，还没实现。
- [x] 前端 Tasks 页（任务卡片墙 + 运行历史 + 实时事件流 + 取消）
  —— V2.0 的最小版就把这四件做全了（`pages/Tasks.tsx`）：卡片墙的发起判据来自
  `params_schema`，事件流是真 SSE（连上时亮"事件流已连上"），取消只对 `running` 出现。
  **注意**：这一格勾着是因为"界面上有这一个按钮且它对 `running` 才出现"是界面判据；
  按钮按下去之后那条路在真进程上从没走过 —— 那一条欠账记在上面的"任务调度"那一格。
- [x] `BackfillTask` 实现（§7.22 按 URL 直接定位，不退化全库扫描）
  —— **2026-09-26 落地**：`tasks/backfill.py` + `params.BackfillParams`，注册表从
  13 登记 / 9 实现变成 **13 / 10**（`tests/unit/core/test_task_registry.py` 那份集合快照
  双向核过，多一条少一条都红）。四条定稿各有出处：`creator_url` 必填就是
  `docs/specs/task-runner.md §457` 契约化的那个形状；库里没有这位博主 → `stage="task"`
  一条失败、文案给出 `add_creator`，且**一次枚举都不发**（`adapter.list_calls == []`）；
  `like_count=None` 一律排最后并单独计 `unknown_metrics`（把"不知道"当 0 排序等于让缺数的
  旧作品冒充爆款）；逐条下载复用 `collect._collect_one_video`，不再写第二份"查重+下载+入库"。
  `scan`（扫描窗口）与 `top`（交付条数）是**两个参数、清单里两栏**：合并成一栏就分不出
  "看了 32 条挑 5 条"与"只看最新 5 条"，后者压根不算回溯。
  §7.22 从此有看护，**三处同改一起做了**（`AGENTS.md §5` 搬进"契约测试看护"、
  `docs/specs/contract-tests.md` 那一行、`test_contract_guard_index.py` 的 `GUARD_INDEX` 三条）；
  `NOT_YET_GUARDED` 第一次为空 —— 那一格**留成空元组而不是删掉**，理由写在它上面。
  还差两件，各有名字：① **前端那一枚按钮**（Creators 页每张卡上的「爆款回溯」，参数从
  `profile_url` 来）—— 现在唯一的入口是 Tasks 页那张自动出现的卡片；② **真机那一跑**
  （要点名一位博主、扫一轮、收几条），与"小红书 `add_creator` 真机从未成功"同一次能补完。
- [x] 真机烟雾测试：抖音 / B站 / 小红书 三平台各采一条 + 转写
  —— **2026-09-25 收口 5/5**（判定过程与数字在 `docs/progress/2026-09-25.md` §9，那是写进 live
  `data/` 的真跑：桥 3458 + 人扫的码）：抖音 2 条 + B站 1 条 + 小红书 1 条图文，三条出口播稿
  （抖音两条、B站一条 85 句 / 8112 字），迁移 21 条，截图包 4 帧 sha256 互不相同。
  这一跑真正值钱的不是"通了"，是**当场炸出四个从没成功过的功能**（评论键名是 fixture 编的、
  `--cookies` 相对路径、`xsec_token` 被压、`has_video` 没出口）—— 四个都有 commit 与红过的用例。
  仍然没验的两件，各自有名字：YouTube 真采（本机直连不通，要代理）、非空字幕轨（挂在上面
  B站 Adapter 那一格）。

- [x] 四平台的**全局开关**（总闸）
  —— 2026-09-25（ADR-0025）：`app.yaml` 的 `platform_control.enabled`，语义是
  `可用 = 总闸 AND 这一家自己的 enabled`，**不改写任何一家自己的值**。
  四态判据（`available / own_off / master_off / absent`）只有一处定义
  （`platforms/base.py::resolve_platform_availability`），`/api/tasks` 的过滤、跑前那道门、
  平台注册表、cron 名单四处都从这一处进货 —— 本地各 AND 一次就是"按钮在、点下去被拒"那一族。
  只管新提交，不打断在跑的那一轮。界面：设置页一张 `PlatformControlCard`（总闸 + 四行只读三态），
  总览页那四枚牌与它共用 `lib/platform-state.ts` 一份说法。
  验证：15 条变异全 KILLED（其中"PUT 不重排采集 job"第一轮真的活了，补了带真
  `AsyncIOScheduler` 的用例才杀掉）；e2e 一条在真浏览器里翻闸看三处口径。
  **仍然待点头的范围问题**在第 7 节第 24 行：关掉总闸后 `postprocess` 取字幕也会被拒
  （与"这一家自己关了"完全同范围，不是新行为，但第一次会让人意外）。
  **09-25 §10.4 那条"真机没为总闸重跑"已结**（2026-09-26，22 条判据全 PASS，真 uvicorn + 真桥，
  config 与 data 全隔离在 `.scratch/master-switch/`）：闸关时 `POST /api/tasks/douyin_collect/run`
  → 422 且文案点名"被总闸关着（app.yaml 的 platform_control.enabled: false…）"、
  **被拒的提交连 run 行都不落**（scratch 库 `like '%_collect'` 查得空）、桥整轮
  `last_page_url` 与 `restarts` 一字未变（没有一次 navigate）、`/api/schedule` 的
  `effective_platforms` 是 `[]` 而 `master_enabled:false` 说得出原因；翻回来不用重启。
  过程里第一版脚本 6 条红**全部**是脚本自己猜错了响应形状（其中那条 405 让核心断言整条没执行），
  判据与教训记在 `docs/lessons.md` 那两条新的与 `docs/progress/2026-09-26.md` §7。

### V2.2「飞书 + 分析层 + 暗色」 — 2026-09-26 对账：7 条里 5 条已经在树里，剩 2 条各有明确堵点

> 这一节原来七条全挂 `[ ]`，而 `docs/plans/v2.1-migration-plan.md` 的 Phase 5 全标 `[x]` ——
> 两边说的不是同一件事：**计划那栏记的是"V1 代码搬进来了"，本节判据要的是"接进 V2 主流程"**。
> 所以逐条按代码与 live 库核，别照抄任何一边的勾。

- [ ] 飞书同步移植（`feishu_sync` 任务，`feishu-base` 状态收进主库，废独立镜像库）
  —— 现状说清楚：`ported/feishu/` **5,362 行**（6 个脚本，V1 原结构照搬，顶部 `# TODO(v2-adapt)`），
  但 **`tasks/` 里没有任何 handler 调它** —— 计划 T5.1 那栏写的产出"`tasks/feishu_sync.py` 薄 handler"
  从未落地（`task_registry.TASKS["feishu_sync"].implemented is False`，我今晚直接读的对象）。
  镜像库也没废：`--db <镜像库>` 是 `tools/render_reports.py` 的必填输入，而本机
  `find Intelligence-Hub -name feishu-base.sqlite3` 是空的。所以这一格是**两件真活**：
  薄 handler + 状态收进主库（后者要迁移与 ADR）。
- [x] 爆款拆解引擎（移植 V1 `launcher/engine/benchmark_engine.py`）
  —— 2026-09-25（T5.2）：`core/analysis/benchmark_engine.py` + `GET /api/benchmark-analysis`。
  **与计划那句"给一条 video 出拆解结果**入库**"不同：它是现算的**，端点读 `videos` + `transcripts`
  当场算完返回，不写表（我看的是 `api/v1/benchmark_analysis.py`，里面只有 `get_or_raise` /
  `get_for_video` 这类读）。看护在 `tests/unit/analysis/test_benchmark_engine.py` 与
  `tests/integration/test_api_analysis.py`。**没有界面**：拆解结果不在任何页上显示。
- [x] 脚本生成（移植 V1 `launcher/engine/draft_engine.py`）
  —— 2026-09-25（T5.3）：`core/analysis/draft_engine.py` + `api/v1/drafts_generation.py`；
  计划里那条 T5.2→T5.3 的依赖是纸面的（V1 两者无调用边）。
- [x] 话题 / 草稿表与前端页
  —— 2026-09-25（T5.5，ADR-0021）：`topics` / `drafts` 两张表（我在 live 库
  `sqlite_master` 里点到了这两张名）+ Alembic 0004 + `api/v1/topics.py` / `drafts.py` +
  `frontend/src/pages/Topics.tsx`。`video_topics` 按 ADR-0021 故意不建。
- [ ] 报告生成（移植 V1 `prepare_creator_analysis.py` / `render_creator_analysis_report.py` / `generate_creator_insight_dashboard.py`）
  —— 代码齐（`ported/reports/` **5,172 行** + `tools/render_reports.py` 薄壳，走子进程不 import，
  三条"不臆造成功"的规矩写在脚本 docstring 里：退出码不是判据、产物必须非空壳且内嵌 JSON 可解析、
  路径必须落在源码树之外）。**没勾的理由只有一条：从未产出过一份真产物** —— `prepare` 与
  `dashboard` 的 `--db` 要的是飞书镜像库（本机不存在），`render` 要的 `<window>-report.json`
  按计划"由分析方手写"。造一份假的跑通等于给自己做个好看的绿，所以留在这里。
  要解这一格得先决定：报告的**数据源到底是不是飞书镜像库**（那是范围决定，不是 bug）。
- [x] 暗色模式（孟菲斯暗色版色板）
  —— 2026-09-25（T5.6，ADR-0023）：换角色不换组件引用、新增 `on_accent`、`electric-blue`
  暗色下故意调亮；对比度两个主题逐对量过。真浏览器判据由 e2e 那条
  `test_dark_mode_changes_the_computed_page_background` 盯着（改的是 computed background，
  不是"类名出现了"）。
- [x] E2E 测试覆盖关键流程
  —— 2026-09-25（T5.7）：**11 条**，真 Chromium + 真 uvicorn（每条用例一只浏览器、每次重构建
  `dist`）。2026-09-26 今天实测 `pytest -m e2e tests/e2e` → **11 passed in 77.52s**（昨天那份
  记的是 39.95s，同一批用例，差在构建与机器负载）。计划 T5.7 那句"采集→详情那条长链仍缺"
  已经不成立：`test_a_collected_video_gets_a_shot_pack_the_browser_actually_renders` 走的就是
  收录 → 采集 → 工坊 → 浏览器真解码出 3 帧。
  **但 `make e2e` 这一条命令按今天的形状不会绿**（对账时顺手量到的，不是推的）：
  `pyproject.toml` 的 `addopts` 无条件带 `--cov-fail-under=80`，而 e2e 单独一档只覆盖 42.30%，
  所以那 11 条后面紧跟一行 `FAIL Required test coverage of 80% not reached`。
  `ci-local` 与 CI 都不含 e2e（`Makefile:100-107`），所以今天没有任何人天天撞它 —— 但判据里
  "`make e2e` 全绿"这一句在字面上不成立。要么 e2e 那一档带 `--no-cov`，要么把判据改成
  "`pytest -m e2e` 11 条全过"。**这条待你点头，见 `docs/progress/2026-09-26.md` §11 第 28 行。**

### V2.x 稳定后 — 远期

- 修 bug、性能调优、文档完善
- 等 V3 启动（V3 = 全量重写，契约不变，实现可弃；可能换语言）

---

## 当前状态（2026-09-26 对账）

- **设计阶段**：完成（10 节决策全部锁定，详见 `docs/adr/`；ADR 编号到 **0025**）
- **实施计划**：完成（`docs/plans/v2.0-implementation.md` 16 个任务全落；
  `docs/plans/v2.1-migration-plan.md` Phase 0-6 的勾全打上，但**它那一栏记的是"V1 代码搬进来了"，
  不等于"接进 V2 主流程"** —— 差在 `ported/` 那 11,585 行上，见上面 V2.2 那节的开头）
- **V2.0**：16 条判据 12 勾 / 4 未勾，未勾的全是"某一跑"（抖音短链、B站非空字幕轨、真机点一次取消、
  CI 第一次真跑）—— 见本节上面那四条，每条都写了补法与判据
- **V2.1**：**功能面收满**（`BackfillTask` 于 09-26 落地，注册表 13 登记 / 10 实现），
  真机 5/5；剩的是三跑一按钮（短链、非空字幕轨、真点一次取消 + Creators 页那枚回溯按钮），
  全在待办池与上面那一格里点名
- **V2.2**：7 条里 5 条在树里，2 条卡在"飞书镜像库根本不存在于这台机器"（同步适配 + 报告真产物）
- **V1 工作区**：未动（今天只读打开了它的 `downloads/local.sqlite3` 数行数，`mode=ro`）

### 实施进度明细

> 下面这张表是 **V2.0 那 16 个 Task 的落地账**，写完就没再动过，所以里面的"测试"那一栏数字
> 全是当时的累计值（最小的一个 15 passed，最大的 1301）—— 今天的数字看
> `docs/progress/2026-09-26.md`，别看这张表。**这张表本身没有错项**（16 个 Task 确实全勾了），
> 只是它下面的"门禁现状"那段引用的是 09-22/09-23 的量法。

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

**门禁现状**（每次提交前都要全绿；2026-09-26 重量的四条命令与原文输出）：

```bash
cd E:/08-Codework/Intelligence-Hub-V2
.venv/Scripts/python.exe -X utf8 -m pytest -m "not real_network and not e2e" -q   # 2488 passed, 15 deselected / 94.93%（门槛 80）
.venv/Scripts/python.exe -X utf8 -m coverage report --include='src/intelligence_hub_v2/platforms/*,src/intelligence_hub_v2/tasks/*,tools/*'   # 95%（门槛 90）
.venv/Scripts/python.exe -X utf8 -m ruff format --check src/ tests/ tools/     # 252 files already formatted
.venv/Scripts/python.exe -X utf8 -m ruff check src/ tests/ tools/              # All checks passed!
.venv/Scripts/python.exe -X utf8 -m mypy src/                                  # Success: no issues found in 138 source files
```

**别再用 `pytest tests/ -q` 这一条当门禁了**：09-25 把 `addopts` 里那条 `-m` 排除删掉之后，
裸跑会连 e2e 一起带上（要真 Chromium + 一次 `vite build`，多出的不只是时间，还有"这台机器
必须有浏览器"这个前置）。日常那道门是 `make test-backend` 的那一句，带 `-m`。

**耗时（09-26 实测，两个数都留着）**：同一条 `-m "not real_network and not e2e"` 带覆盖率
**227.49s**；另一次加 `--no-cov` 反而 **298.43s** —— 那一次旁边并跑了 ruff/mypy。
昨天（09-25 §10.3）记的是 162.47s，比今天快 65s，**差因没查明**（今天后台多了一台旧后端与
V2 桥活着；也可能昨天那次机器更闲），不当结论用。e2e 那一档今天 11 条 77.52s（昨天 39.95s，
同一条判据别只看数）。Task 8 之前整套 ~50 秒 —— 净增的用例里有一部分走真 SQLite + 真清单落盘
（集成层），这笔账记在 `docs/lessons.md` 经验 15 的延长线上。

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

- [x] **`platforms` 镜像的健康三列在生产里没人写**
  —— **这条已经做完了，是 2026-09-26 对账时才发现这里还挂着**。三件都对上了：
  ① 那条 ADR 是 **ADR-0022**（不是计划里写的 0014 —— 0014 仍然空着给 `_check_requires`，
  下面那条）；② 写者真存在：`tasks/preflight.py:157` 调 `storage.platforms.set_health(...)`，
  我今天 grep 过整棵 `src/`（排除 `ported/`）这是唯一的生产调用点，不是注释里的愿望；
  ③ "探测之后镜像列非 NULL"那条集成用例是 `tests/integration/test_platform_health_mirror.py`。
  `repositories/platforms.py` 那段 docstring 现在说的"它由 preflight / 采集任务写"**只对了一半**：
  `set_health` 在整棵 `src/`（排除 `ported/`）里只有 preflight 那一个生产调用点，
  采集任务并不写它。所以那句话仍然是一处小型"注释承诺了代码没有的东西"，改注释就行，别去加写者。
  **仍然故意没做的那一半**：总览页不画健康灯（e2e 里
  `test_dashboard_lists_the_platforms_and_draws_no_light` 就是在钉"不画"这件事，见经验 40）。
- [ ] `_check_requires`：要先有一份缓存的能力快照（preflight 结果 + TTL + 失效点）→ ADR-0014
- [ ] 桥服务要不要拆成独立仓库（V2 已移植在 `src/intelligence_hub_v2/bridge/server.py`；
  拆出去的好处是 V1 / V2 / 采集脚本共用同一个提供方，代价是多一个发布面）
- [x] 暗色模式的设计令牌（孟菲斯暗色版色板需要单独调）
  —— 2026-09-25（T5.6 / ADR-0023）调完了：`frontend/src/styles/tokens.css` 里是暗色那一版
  `@theme`，按**角色**重指派而不是按名字（`electric-blue` 在暗色下故意调亮，所以令牌名与色相
  在暗色下对不上 —— 这是那条 ADR 认下来的代价）；新增 `on_accent` 是为了"色块上那行字"在两个
  主题下都过对比度。看护：`tokens.spec.ts` + e2e 那条改 computed background 的用例。
  与上面 V2.2 的"暗色模式"是同一件事，记两处是因为判据在两处。
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
- [x] 定时任务调度 UI（V2.0 后端有 APScheduler，但前端没暴露）
  —— **UI 那一半也落了**（2026-09-25，T6.3 第二片；这一格挂着的是 09-24 的账）。今天核到的形状：
  `api/v1/schedule.py` 三个端点齐 —— `GET /schedule`（205）、`PUT /schedule`（211）、
  `POST` 的 `run_now`（259）；界面上是 `components/shared/ScheduleCard.tsx`，它把**三件平时会各说
  各话的事**并排放（配置里的 cron、调度器上真排着的 job、算出来的下次触发时刻），并且明写了
  "不许因为 PUT 回了 200 就只显示已保存"。**改 cron 不用重启也不用编辑 YAML 了。**
  看护：`schedule-card.spec.tsx`，加上总闸那一批带出来的 `test_the_flip_reschedules_the_jobs_on_the_scheduler`
  （真 `AsyncIOScheduler`）与 `test_the_cron_list_and_run_now_follow_the_master`。`docs/adr/0017`。
- [ ] **真机那三跑**（2026-09-26 对账时从上面四条判据里析出来的，都是"补一跑就结一格"）：
  ① 抖音分享短链收录一次（`v.douyin.com/…` → 库里 `platform_id` 必须是 `MS4w` 开头）；
  ② B站 找一条真有字幕轨的作品走 `single_link` → `postprocess`，判据 `subtitle_used`；
  ③ 真机点一次取消（判据三条见"任务调度"那一格）。
  前置比昨天松：`data/cookies/` 三家都在（09-25 18:19~18:21 导出），V2 桥 3458 还在跑
  （`/health` 是 §7.20 那个 `ok:false` + `page_url` 空的形状，第一条真请求会自愈），
  所以**可能一次码都不用扫** —— 先用 `tools/refresh_bridge_cookies.py` 导一次看键在不在，
  再决定要不要扫码。这一跑同时能结掉 §10.4 那条"总闸没在真机重跑"。
- [ ] **`add_creator` 收小红书博主在 V2 真机上从没成功过**（也不算缺陷，就是没试过）：
  live 库里唯一一条是 09-24 01:33 的 failed，原因是当时小红书适配器还没注册（报"主机不在已知
  平台里"）—— 那是过期失败，不能当证据。它和上面 ① 同一次能补完（backfill 那条路也要用博主
  级枚举，所以这一格不补，`BackfillTask` 的真机验收就是空话）。

---

## 跨会话续上下文的入口

新会话进来读这三份就够：

1. **本文件**（`ROADMAP.md`） — 知道在哪个里程碑、剩什么没做
2. **最近一份 `docs/progress/YYYY-MM-DD.md`** — 知道上次干到哪、卡在哪
3. **`AGENTS.md`** — 知道工程纪律与已知陷阱
