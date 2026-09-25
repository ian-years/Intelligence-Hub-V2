# CHANGELOG

本文件记录 V2 的所有重要变更。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- 项目骨架：目录结构、`pyproject.toml`、`Makefile`、`.pre-commit-config.yaml`、GitHub Actions CI
- 设计文档：10 份 ADR（`docs/adr/0001-0010`）+ 7 份 spec（`docs/specs/`）
- `README.md` / `AGENTS.md` / `ROADMAP.md` / `CONTRIBUTING.md` / `docs/architecture.md` / `docs/lessons.md`
- 配置模板：`config/app.yaml` / `config/platforms.yaml` / `config/feishu.yaml.example`
- 实施计划：`docs/plans/v2.0-implementation.md`（16 个任务，67h 估时）
- **Task 1** · 包骨架（src-layout + hatchling）、`errors.py` 异常层次、`logging.py` structlog 装配、
  `models/` 共享数据模型（task / manifest / video / creator / transcript / event）、`uv.lock`
- **Task 2** · 配置层：`core/config.py`（9 个 section + `AppConfig` + `ConfigManager` 原子写与热重载）、
  `platforms/base.py`（`Capabilities` / `RateLimitConfig` / `PlatformConfig`）、
  `platforms/douyin/config.py`、`platforms/bilibili/config.py`、`platforms/__init__.py` 显式注册表
- **Task 3** · 存储层：`storage/schema.py`（7 张表，SQLAlchemy **Core** 而非 ORM + `UTCDateTime`
  + 从常量生成的 enum CHECK）、`storage/db.py`（engine + `PRAGMA` 每连接装配 + Alembic 迁移
  + `SqliteStorage` + 跨 Repository 的 `transaction()`）、`storage/files.py`
  （`data/` 树的路径唯一真源 + `safe_filename`）、`storage/session.py`（`CURRENT_SESSION` 叶子模块）、
  `storage/repositories/` 七个 Repository（字段级 `update_fields()`、`is_tracking` 唯一写入口、
  `is_hidden` 内化墓碑、`IntegrityError` → 本仓库异常族）、`models/platform.py`、
  Alembic 资产（`alembic.ini` 纯 ASCII + `env.py` 四条纪律 + `0001_initial_schema.py`）
- `.gitattributes`：`* text=auto eol=lf` + 二进制名单（含 `*.sqlite3` / `*.onnx`）。
  本机 `core.autocrlf=true` 会让 checkout 出 CRLF 的 `.py`，而 ruff 配的是 `line-ending = "lf"` ——
  实测内容完全相同的 CRLF 副本：`ruff format --check` 回 exit 1（diff 只有行尾），
  `ruff check` 全过。行尾从此由仓库定，不再依赖每个人的 git 配置
- **Task 4** · 事件与清单：`core/event_bus.py`（`EventBus` Protocol + `InProcessEventBus`
  多播与可选持久化 + 有界订阅队列「保新弃旧 + 掉包计数」+ `_Subscription` 摘除层）、
  `core/manifest.py`（`manifest_writer`：五条退出路径全部落终态清单，
  文件 + 索引 + 任务档案在同一个事务里，收尾代码不改写正在传播的异常）、
  `storage/repositories/base.py` 改为注入"给我一个 session"的工厂、
  `EventRepository.list_for_task()` 新增 `since=`、`models/manifest.py` 新增 `ManifestStatus`
  与 `ManifestBuilder.started_at` / `.status`

- **Task 5** · 平台契约与外部世界边界：`platforms/base.py` 补全
  （`PlatformAdapter` Protocol + `AdapterDeps` + `HealthReport` + 数据类型 re-export）、
  `platforms/registry.py`（`PLATFORMS` 显式注册表 + `register()` 导入期自洽检查 +
  `PlatformRegistry.inconsistencies()`）、`models/media.py`
  （`SingleFileArtifact` / `VideoAudioPairArtifact` / `audio_path_of()`）、
  `infra/` 五件包装（`subprocess` / `cdp_bridge` / `cookies` / `ytdlp` / `ffmpeg`）
- **Task 6** · 抖音 Adapter（V1 §7.1 / §7.2 / §7.3 三条陷阱的落点）：
  `platforms/douyin/urls.py`（sec_uid 三种形状 + **抖音系域名闸门** +
  `as_http_url`/`require_http_url` + 中文计数法解析）、`listing.py`（主页与滚动两份页面 JS、
  占位符注入后**检查没有残留**、`sec_uid_mismatch` 与"网格没渲染"两种红分开）、
  `media.py`（cookie 阶梯落地 + 详情 JS 问播放直链 + `.part`→`os.replace` 落盘）、
  `adapter.py`（`DouyinAdapter` + `@register("douyin")` + `_Pacer` 按 `per_minute` 上闸）、
  `tests/contracts/_doubles.py`（`FakeBridge` / `FakeYtDlpRunner`）与
  `tests/fixtures/douyin/` 七份页面模拟结果、`tests/unit/test_import_layers.py`
  （六个入口各起子进程验导入顺序）
- **Task 7** · B站 Adapter（V1 §7.13 / §7.14 / §7.15 / §7.16 / §7.21 的落点）：
  `platforms/bilibili/urls.py`（`mid` / `bvid` 提取 + b23.tv 短链判定 + 协议相对地址补齐）、
  `listing.py`（`--flat-playlist` 输出解析、外部浏览器清单、`x/web-interface/view` 元数据）、
  `media.py`（三档 cookie 阶梯 + **未合并 DASH 分片按文件名配对**，绝不扫目录）、
  `subtitles.py`（`x/player/v2` 字幕轨 → `Transcript`，"没问到"与"确实没有"分开）、
  `adapter.py`（`BilibiliAdapter` + `@register("bilibili")`）；
  `infra/pacing.py`（`RatePacer`，两个适配器共用一份节流）；
  `tests/contracts/_doubles.py` 加 `flat_playlist` 记录；
  `tests/fixtures/bilibili/` 九份，其中四份是 2026-09-22 打真接口抓回来的响应
  （其余在文件里用 `_comment` 标了合成原因）。`tests/unit/test_import_layers.py` 的入口加到六个。
- **Task 10** · 前端脚手架 + 孟菲斯设计令牌（`frontend/` 原先只有一个 `.gitkeep`）：
  Vite 8.3 + React 19.3 + TS 5.9（`strict` + `noUncheckedIndexedAccess` +
  `exactOptionalPropertyTypes`）+ Tailwind v4（`@tailwindcss/vite`，vite 中间代理只绑回环）+
  Vitest 5（jsdom + Testing Library，覆盖率门槛 70 写在 `vite.config.ts` 与 CI 同一处）+
  eslint 10 flat config / stylelint 16 / prettier 3；
  `src/styles/tokens.css`（`@theme` = **唯一真源**，ADR-0013）、`patterns.css`（§6 五种图案 +
  `.bg-pattern-*` 工具类 + `.page-*` 一页一种）、`globals.css`（§8 组件类：卡/按钮/徽章/输入/开关 +
  §7 的跳点加载与 `prefers-reduced-motion`）；
  `tokens.json` 是单向投影（`npm run tokens`），`src/lib/tokens.ts` 从它读动效值并把
  `cubic-bezier(...)` 解析成四个数（读不懂就抛，不静默没有缓动）；
  四个孟菲斯基件（`PlatformBadge` / `HardShadowCard` / `PatternBackground` / `MemphisButton`）+
  令牌对照页 `src/dev/TokenSheet.tsx`；
  20 条用例里最要紧的是 `src/styles/tokens.spec.ts`：把 `ui-tokens.md` 的 YAML 源解出来
  逐条核 CSS、核 `tokens.json` 逐字节等于投影、核图案与色板一致（四条破坏做过变异验证）
- **Task 11** · 前端 API 层 + SSE + stores + 路由外壳：
  `src/api/client.ts`（唯一出口：非 2xx 一律 `ApiError`，422 的数组 detail 展开成 `loc: msg`，
  响应不是 JSON 时留原文前 160 字 —— `response.json()` 会消耗 body，所以错误分支只读一次 `text()`）、
  `src/api/keys.ts`（query key 单处定义）、`src/api/json-schema.ts`（`ui:hidden` / `ui:advanced`
  是契约键，所以类型里就有；`resolveRef` 解 `$defs` 一层）、5 个 hook 文件（URL/动词/请求体
  逐条有用例钉，包括「跟踪开关发的是 `tracking` 不是 `is_tracking`」与「转写稿 404 是正常态、
  500 不是」）；`src/api/schema.d.ts` 由 `npm run gen:api` 从 **`docs/specs/openapi-snapshot.json`**
  生成（不需要后端在跑），同源由 `src/api/schema.spec.ts` 双向核；生成物进 `.prettierignore`
- **Task 11** · `src/events/useTaskEvents.ts`：`/api/events` 订阅。后端每帧都带
  `event: <type>`，`EventSource.onmessage` **一帧都收不到**，所以逐类型 `addEventListener`；
  服务端不发 `id:`，重连由前端带 `since=<最后一条已收到事件的时间戳>` 重开（退避 0.5→10s），
  缓冲区有界 500，`status`/`error` 交回界面。`EVENT_TYPES` 与 Python `EventType`
  由用例逐字核（漏一个名字的症状是那种事件静默收不到）。`docs/specs/event-schema.md §7`
  的样例同步改成能跑的形状（`docs/lessons.md` 经验 35）
- **Task 11** · stores 与外壳：`stores/settings.ts` 是 V1 §7.24"跟踪默认值只有一处"在前端的
  落点（`localStorage` 当外部输入处理：`"false"` 这种字符串不认，回到唯一默认值；
  写非布尔直接拒），`stores/ui.ts` 只管侧栏；`Layout`/`Sidebar`/`PageShell` +
  `App.tsx` 的 7 条 `HashRouter` 路由与 `/tokens` 开发页。侧栏的平台清单读自
  `/api/platforms`（**读不到要显示原因，不许渲染成安静的一片空**）

- **Task 8 · 运行时**：`tasks/definition.py`（`TaskDefinition` / `TaskContext` / `CancelToken`
  三个契约类型，放在 `tasks/` 因为 `AGENTS.md §2` 的契约表把它钉在那儿）、`core/task_registry.py`
  （12 个任务全登记、6 个真实现 + `DepsFactory` 按 `capabilities.needs_browser` 决定给不给桥客户端）、
  `core/task_runner.py`（`TaskRunner` 单次执行 + `TaskScheduler` 开关门/Semaphore/协作式取消）。
  六个 handler 里采集只写一份 `make_collect_handler(platform)` 注册两次（写两份就是"两处真相漂一次"）。
  清单先落盘、再广播 `task.finished`：反过来的话前端收到完成事件点进去清单是 404
- **Task 9 · API**：`main.py`（`create_app` 工厂 + `_lifespan`：初始化存储 → `ensure_dirs` →
  平台镜像同步 → `reap_orphans` → 配置订阅 → 每日事件清理）+ `api/v1/` 九个路由模块
  （health / config / creators / videos / transcripts / tasks / events / manifests / router）。
  公开前缀是 `/api`（与 `docs/specs` 的 Locked 契约一致）。异常映射：
  `NotFoundError`→404、`ConflictError`→409、`TaskRejected`→422、`PlatformError`→400、
  `StorageError`→500、校验失败→422
- **Task 14 · 契约测试基类**：`tests/contracts/test_platform_adapter.py` 的
  `PlatformAdapterContractTests`（11 条通用契约）+ 抖音/B站 两个实例子类；
  `test_contract_guard_index.py` 把 V1 §7 那 25 条陷阱逐条核到"有归属且用例真存在且真会跑"
- **Task 15 · `tools/migrate_from_v1.py`**：只读 V1 SQLite（`mode=ro` URI）→ 写 V2 主库 + `data/` 树。
  映射表见 `docs/specs/data-model.md §6`；媒体走 hardlink、跨卷退 copy；
  V1 内联的 `clean_transcript` 拆成 `transcripts` 行 + 磁盘 `speech-clean.txt`；
  `launcher-state/hidden-videos.json` 的墓碑翻成 `is_hidden`。幂等 + `.migration_state.json` 续跑
- **这四处的首次真数据验证在 2026-09-23 的全栈冒烟里**（临时目录，未动 live `data/`）：
  `creators=4 videos=21 transcripts=16 墓碑=2 媒体 link=21 copy=0 missing=0`；
  页面上点一次「跑一次」真的起了 `preflight`，事件顺序
  `task.started → manifest.written → task.finished` 与清单落盘都对，summary 说实话
  （`platforms_ok=0 degraded=2 tools_missing=ffmpeg, ffprobe`）

### Added（测试与门禁）
- 存储层测试 **554 条**（`tests/unit/storage/` 486 + `tests/unit/test_safe_filename.py` 68），
  全仓累计 650 passed、覆盖率 95.07%、`ruff` / `mypy --strict` 全绿
- 事件与清单测试 **51 条**（`test_event_bus.py` 28 + `test_manifest.py` 21），
  全仓累计 **701 passed**、覆盖率 **95.39%**、四关（format / check / mypy / 覆盖率门禁）全绿
- 平台契约与 infra 测试 **119 条**（`tests/unit/infra/` 4 个文件 + `tests/unit/platforms/`），
  全仓累计 **820 passed**、覆盖率 **94.04%**、四关全绿。
  全部不碰网络与真二进制：yt-dlp / ffmpeg / 桥 一律 mock，
  真机那一档留给 `@pytest.mark.real_network`（Task 6/7）
- `storage` fixture 参数化跑两档后端（`memory` = `create_all` / `file` = 真 Alembic），
  外加三条漂移看护：`check_schema_matches_migrations()`、`alembic upgrade/downgrade/upgrade` 往返、
  以及一条**验证漂移看护本身能发现漂移**的用例
- **Task 12 之一 · Preflight 页**（`/preflight`，四页里的第一页）：
  `usePreflight` 的三态分开渲染 —— 全绿 / 部分可用 / 有不可用的地方，外加
  "读不到"（显示 `ApiError` 原文）与"补发被挂起"两种非结果态，
  四种情况互相**不许**露出对方的文案。失败明细逐条给出 `platform` / `stage` /
  `error_kind` 与原文。`tools_missing` 单独不判红这件事在页面上说出来（要紧由平台决定），
  不然一栏红色工具清单会被读成"环境完了"。
  `lib/formatters.ts` 的 `parsePairs`/`parseNames` 拆的是 `tasks/preflight.py` 拼出来的字符串，
  所以用例同时钉生产者形状（`", ".join(f"{k}={v}")` 改了 → 前端红）。
  `PreflightSummary` 按 handler 实际写的 9 个键声明（路由返回的是自由 `dict`）。
  顺带修掉 `Sidebar` 同一条坑：`isPaused` 以前会一直显示"读取中…"（经验 36）
- **Task 12 之二 · Settings 页**（`/settings`）：表单**不手写**，字段、顺序、说明、
  哪些不许渲染全部来自 `/api/platforms/{name}/schema`。执行者是新的纯函数
  `lib/schema-form.ts`（`planForm` / `coerceValue` / `mergeDraft`，13 条用例）：
  - `ui:hidden` 的字段不渲染，但**必须原样送回 `PUT`**（那个端点收整份配置，
    少带一项就是让后端用默认值覆盖用户机器上的值）—— 这条单独有用例；
  - 被隐藏的项数在页面上说出来（"有 N 项后端不实现，已不显示"），不让它们凭空消失；
  - 整组隐藏的折叠组不渲染（§4 的实现期约定），部分隐藏的组照常出现并只留能用的；
  - `ui:order` 生效；`Path | None` 那种只有 `anyOf` 没有 `type` 的落到文本框而不是不渲染；
  - 空文本框送 `null` 不送 `""`，数字框送数字不送字符串（`docs/lessons.md` 经验 37）
- **Task 12 收尾 · Dashboard + Feed**（四页到此全落，前端 94 → **164 passed**）：
  - `components/shared/QueryState.tsx` —— "读数据的界面"那四态（挂起 / 读不到 / 还没有结论 /
    有数据）的唯一出口，四态**互斥**这条纪律从 Preflight 一处变成五页共用；
    `VideoRow` / `TaskTimeline` 同为总览与作品流共用，"运行状态 → 文案 + 色"搬到
    `lib/run-status.ts`（那张表是契约的投影，不该为了取它把 React 拉进 node 环境的用例）
  - **作品流**：虚拟滚动只画视口内 + overscan（60 条数据不画 60 个 DOM 节点，这条有用例）；
    筛选**点"查询"才发请求**（每敲一个字一个新 queryKey 就是对全库做一次 LIKE）；
    换筛选条件在同一次提交里退回第 1 页（不用 effect 补）；`placeholderData` 带着上一页数据进入
    新一轮请求时明说"显示的是上一页的结果"；隐藏＝打墓碑，在"只看已隐藏"那一档可以取消
  - **总览**：四个区块各自过 `QueryState`，一个端点坏了不牵连别的区块；平台卡片画的是
    **配置里的事实**（`enabled` / `implemented` 三种状态分开给），
    健康灯刻意**不画** —— `platforms.health_status` 在生产里没有任何写者（见下面的欠账）
  - `src/test/fixtures.ts` 是**工厂**不是三份写死的数组（条数由用例自己数回去）；
    `src/test/setup.ts` 补 `ResizeObserver` 桩：jsdom 没有它时 TanStack Virtual
    在量到容器尺寸前一条都不画，症状是"列表永远空白"，看起来像组件坏了
- **修掉两处"看得见、传得进来、什么都不做"**（都是这一轮做作品流/总览时撞出来的）：
  - `VideoFilter.hidden` 手写成 `boolean`，后端那个查询参数是 `visible|hidden|all` →
    作品流一改筛选就 422。而 `client.spec.ts` 与 `hooks.spec.tsx` **两处用例把
    `hidden=false` 当期望 URL 钉住了**（钉的是"前端自己发出的字符串"，从没跟契约比过）。
    现在类型从快照 `operations[...]` 派生，界面那份取值清单 `VIDEO_HIDDEN_MODES`
    与快照 enum **双向**核 → 经验 39
  - `cn()` 把 `border-memphis` 当同类冲突**吃掉**（`cn("border-memphis border-ink-black")`
    实测只剩 `"border-ink-black"`）：tailwind-merge 把任何 `border-<任意值>` 归进同一组，
    于是 §3 的"3px 黑边"在 Preflight 状态牌、Sidebar 导航块、Settings 表单组上**从未生效**。
    改名 `memphis-border`（与 `memphis-card/-btn/-badge/…` 同族，不进 Tailwind 命名空间），
    零使用且同坑的 `.border-badge` 一并删；纪律用例遍历 `globals.css` 的 layer，
    另留一条"反向证据"钉住改名原因 → 经验 38
- **Task 13 · VideoDetail / Creators / Tasks（V2.0 最小版，七页到此全落）**：前端 164 → **196 passed**
  - **作品详情**：元数据 + 口播稿，**没有内嵌播放器**（V2.1 的事）且页面上写明白 ——
    一块空白会被读成"坏了"而实际是"没做"；地址里那段不是数字时**一个请求都不发**并说明
    是地址的问题（404 该由后端回答）；口播稿的 404 是**正常态**（"还没有稿子"），
    500 才是失败，两者文案互斥且各有用例
  - **博主页**：`CreatorCard` + 跟踪开关（`PATCH` 字段名 `tracking`、值是真布尔、
    写入状态按行独立）+ 收录表单。**筛选用的平台与收录时指定的平台是两个状态**：
    合成一个的症状是按过 B站 筛选后，下一位抖音博主会被打上 `bilibili` 提交（有专门用例）
  - **任务页**：卡片墙 + 运行历史（含取消）+ 实时事件流。
    "能不能一键发起"**由那个任务自己的 `params_schema.required` 决定**而不是前端记一份清单：
    无必填 → "跑一次"提交 `{}`；只差 `url` → 链接框；其余 → 说清必填是什么、
    **不画一个必然 422 的按钮**。没有 `EventSource` 时那一栏红着说不可用，不安静
  - 又抓到一处手抄契约（第三例）：`useAddCreator` 声明 `api.post<Creator>`，
    而端点回的是 202 + `TaskAccepted`（注释还写着"立即回一个 Creator"）。
    这次配的是结构性看护而不是人肉翻快照：`schema.spec.ts` 新增一条**源码级扫描**，
    把每个 `api.<verb><T>(path)` 的 `T`（含 `X[]` / `Schemas["Y"]` / 别名）解析成 schema 名，
    与快照那个端点 2xx 的 `$ref` 比 —— 扫到 18 个可比调用点，
    自由 `dict[str, Any]` 的路由跳过并单独计数（`docs/lessons.md` 经验 41）
- **契约基类补到 `contract-tests.md §4` 承诺的形状**：通用用例 7 条 → 11 条
  （能力声明快照、能力与配置三个镜像字段一致、列表流式产出的形状与 `limit` 是上限、
  媒体产物说得出走了哪条路 + 兜底必留原文），钩子 2 个 → 6 个且全部 `@abstractmethod`；
  各平台自己写的 4 条重复用例删掉、值搬进 `expected_capabilities()`。
  §4 从"一段设想中的代码样例"改成两张受测的名单表，
  新看护 `test_contract_tests_section_4_is_the_abc_itself` **双向**核相等
- **修掉一整套契约用例每次全跑被跑两遍**：`test_contract_guard_index.py` 按名字 import 了
  `TestDouyinContract` / `TestBilibiliContract`，而 pytest 会把模块命名空间里（**含 import 进来的**）
  `Test*` 类当成本模块收集 —— `tests/contracts` 收集数 310 → 289，一条不少。
  纪律由 `test_this_module_binds_no_test_classes` 钉住（`docs/lessons.md` 经验 34）

### Added（V2.1 批量推进 · 2026-09-24 夜 → 2026-09-25）

- **T2.1 第三片** · 小红书契约测试：`tests/contracts/test_xiaohongshu_adapter.py`（191 条）+
  `TestXiaohongshuContract`（基类 11 条）+ 16 份合成 fixture 与其 README（明写"挡不住什么"）。
  四个模块覆盖率 23/35/37/33% → 98/100/100/99%
- **T2.2** · `platforms/youtube/`（urls/config/listing/media/adapter）+ 163 条用例，四平台齐；
  字幕（`fetch_subtitles`）是 V2 新兑现——V1 的 YouTube 路径没有这条码
- **ADR-0019** · 图文笔记的产物表示：`MediaArtifact.has_video` + `SingleFileArtifact.extra_paths`
- **ADR-0020** · `video_comments` 与 `video_metric_snapshots` 两张附属读数表（Alembic 0003）+ B 站评论抓取
- **ADR-0021** · `topics` 与 `drafts` 按 §2.8/§2.9 的预留契约落表（Alembic 0004）+ 6 个端点 + `pages/Topics.tsx`
- **ADR-0022** · preflight 把探测结论写回 `platforms` 镜像；`/api/platforms` 带出健康三列；总览画灯
- **ADR-0023** · 暗色版：`[data-theme="dark"]` 重指派三个中性色令牌的角色 + 新令牌 `--color-on-accent`
- **T5.2 / T5.3** · `core/analysis/{benchmark,draft}_engine.py`（V1 两张规则表逐字搬，
  "参数是装饰"的四处实测拆穿并写进 docstring）+ `GET /api/benchmark-analysis` +
  `POST /api/generate-draft-script`
- **T5.4** · V1 三份报告脚本搬进 `ported/reports/`（4114→5172 行，AST 比对 `diffcount 0`）+
  `tools/render_reports.py`：第一例**只走子进程、不 import 搬运区**的薄壳（ADR-0018）
- **T5.6** · `lib/theme.ts` + `ThemeSwitch`（亮/暗/跟随系统）+ `tokens.spec.ts` 里实现的
  WCAG 对比度判据（两个主题逐对量）
- **T5.7** · `tests/e2e/`：真 Chromium + 真 uvicorn 线程 + 假适配器，8 条关键流程。
  `make e2e` 从"调一个 Python playwright 没有的子命令"改成能跑的那条
- **T6.3 第二片** · `GET/PUT /api/schedule` + `POST /api/schedule/run-now`，改 cron 当场重排不重启；
  被环境变量盖住的键由配置层自己算（`scheduler_keys_shadowed_by_env`），接口不编镜像名单
- **T6.5** · `GET /api/videos/{id}/media`（HTTP Range + 媒体目录包含判定 + 容器白名单）+
  详情页 `<video>` 与逐句时间轴跳转
- **T6.6** · `/api/videos?sort=benchmark` 与博主页的「爆款回溯」面板
- **T6.8** · `CollectParams.metrics_only`（V1"仅采集数据"的真形状）
- **T4.7** · `pages/Workshop.tsx` 创作工坊：选对标 → 拆解与逐字稿（只读）→ 每 1.6 秒自动保存的
  编辑器 → 四视图（正文 / 分镜 / 提词器 / 截图包）。派生规则与保存判据住在 `lib/workshop.ts`
  （`beatsOf` / `promptLinesOf` / `shouldAutosave` / `isDirty`），`useAnalysis.ts` 是两个分析端点的
  第一批消费方，`template_key` 那份名单由 `schema.spec.ts` 与请求体枚举双向核。
  "截图包"那一格说的是"V2 没有这一步"，不是一个空网格

### Changed（同批）

- ~~`pyproject.toml` 的 `addopts` 加上 `-m "not real_network and not e2e"`~~
  —— **同日撤回，改为从根上解决**：那条排除是在遮盖症状（裸跑 `pytest` 被 e2e 污染 1604 errors），
  真因是 `sync_playwright` 会在主线程 asyncio 的"当前运行循环" ContextVar 里留一个活但不在跑的循环，
  之后每一次 `asyncio.run()` 都报"已在运行的循环里"。`tests/e2e/` 换成 `playwright.async_api`
  跑在 pytest-asyncio 自己那个循环上之后，**那条 `addopts` 排除整块删掉了**
  （同一进程 `pytest tests/e2e tests/integration` 实测 228 passed / 0 errors）
- `api/hooks/useTasks.ts`：`useRuns` 在"有 running"时轮询（e2e 发现的缺陷，见 Fixed）
- 排序与筛选类的用例从"样本式"改写成"关系式"：加一家平台或加一档排序不再需要挨个改用例

### Added（同日补完 · T4.1/T4.2 调用侧 + 截图包真数据源）

- **`PlatformAdapter.fetch_comments` / `fetch_metrics` + `Capabilities.supports_comments`**
  （ADR-0020 决定二，动的是 Locked 契约面）：走的是 `fetch_subtitles` 那条已验证的形状 ——
  方法人人有，能不能干活由能力声明决定，`supports_comments=False` 的三家返回 None 不抛。
  四家各自的如实回答：B站 两项全做（`view` 的 `stat` 出四项、`x/v2/reply` 出评论，
  评论的 `oid` 要数字 aid 所以先问一次 view）；小红书只有读数能做且只有一项（`likes`，
  其余留 NULL 并把 `{"only": ["like_count"]}` 写进 metadata）；抖音与 YouTube 抛，
  并在消息里点名该跑哪一条采集任务
- **`enrich_metrics` 任务**（`tasks/enrich_metrics.py`，注册表第 13 个、实现第 9 个）：
  挑活的判据是"一条快照都没有"而不是"缺某个固定窗口"（一条三年前的稿子永远等不到它的
  `24h`，按窗口挑会让它每轮被重挑、每轮零产出）；窗口由这一趟的时刻算
  （`window_for`，边界那一秒归已开始的那一档），`published_at` 为 NULL 或落在将来都算
  `manual` 不 `publish`；评论只在能力位为真的平台问，先问能力再分支，不 try/except 里猜
- **collect 顺手记一条 `publish` 快照**：用的是列表枚举已经带回的那几个计数，一跳网络都不发。
  四项全空是**跳过不是失败**（"这一家没回读数"是事实），写库挂了才记 `stage="metrics"`
  且不掀掉已经进库的作品；`summary` 多一栏 `publish_snapshots`
- **工坊「截图包」有了真数据源**（ADR-0024）：`POST /api/videos/{id}/shots` 用 ffmpeg 从
  **这条作品已落地的成片**按分镜时间点现截，`GET /api/videos/{id}/shots/{name}` 出图。
  产物不入库（可再生的派生物，记一份"上次截了哪些帧"就是第二个真源）、同步做完不起任务、
  写盘动作必须是 POST。`infra/ffmpeg.py` 长出 `extract_frames`/`frame_argv`/`ffmpeg_version`，
  `storage/files.py` 长出 `shots_dir`/`shot_file`（命名对时间点单射且确定，所以 `cached`
  不用记账也算得准）
- 测试：后端 +52（`enrich_metrics` 22 条 + collect 的快照 6 条 + 四家适配器那两条新契约的
  各自看护），前端 +13，`tests/integration/test_api_shots.py` 21 条 +
  `tests/unit/infra/test_ffmpeg_frames.py` 15 条，
  另加 `tests/integration/test_shots_real_ffmpeg.py`（`-m real_network`，真 ffmpeg 9.0.1）
- e2e：工坊页从 2 条变 3 条，其中一条真跑"收录博主 → 采集入库 → 工坊选中 → 点生成截图包 →
  **浏览器真的解码出 3 帧**"（断 `naturalWidth > 0`，不是断 DOM 里有个 `<img>`）。
  这条链的替身只有两处且都写清了：平台适配器假的（不出本机）、ffmpeg 是 tmp 里现写的假二进制

### Changed（同日补完）

- **`FailureRecord.stage` 多两个词**：`"metrics"` / `"comments"`。不复用 `store` ——
  清单按 stage 分组，"接口没给数"与"库写不进去"要做的动作不同，合成一格就是两类红混成一堆
- 出图端点那条"越界一律拒"的用例**重写而不是撤掉**：payload 改成由
  `os.path.relpath(诱饵, shots_dir)` 现算（原来是按猜的目录名埋的，指向一个不存在的路径，
  三道判据全拆掉它照样绿），并加了两段防空转前置（诱饵确实指得到 + 至少 N 条写法真的打到应用层）。
  变异实测：拆①+③ → 红；只拆① → 绿（③接住，证明它不是死代码）；只拆③ → 绿
  （③今天从 HTTP 打不到，另有一条直接测 `_is_under` 的用例）
- `platforms/xiaohongshu/adapter.py`：`fetch_metrics` 复用详情页这件事有一个 stage 副作用
  （那一步自己把失败报成 `media`）—— 换成 `metrics` 再抛，原文一字不动，
  否则清单会把它排到"去查 yt-dlp / 直链"那一格
- 文档：`platform-adapter.md`（两个新方法 + 新能力位 + 四家的 §4.x + `MetricReadings`）、
  `task-runner.md`（注册表那一格顺手对齐到今天的 13/9，原来停在 V2.0 的"12 里挑 6"）、
  `contract-tests.md §4.1`（新增那条通用契约）、ADR-0020 的"分期"改为已落

### 真机（Phase 3 · 2026-09-25 晚，用户登录后当场跑）

- **T3.1 抖音真采通**：`add_creator` → `douyin_collect` 落 2 条，`downloaded=2 / failed=0`，
  两条真 `media.mp4`（57.7 MB / 37.4 MB，`ffprobe` 认出 h264+aac）。§7.2 第一次在真数据上成立：
  `media_source` 全 `page_play_url`，`yt_dlp_error` 留着阶梯两档各自的 exit 1 原文
- **T3.4 转写闭环通**：`speech-clean.txt` 1402 字 / 35 句，`engine=sherpa_sense_voice`、
  `summary_method=local-extractive`、`content_summary` 有值（T1.2b 两列第一次吃真数据）；
  权重经 `SENSEVOICE_MODEL_DIR` 指 V1 目录，只读、不拷 233 MB
- **截图包在真 ffmpeg + 真媒体上验完**：4 帧全出（178–281 KB）、版本串带回
  `ffmpeg version 9.0.1-full_build`、第二次 `cached=true` 且一帧不重截、
  **四帧 sha256 互不相同**（真 seek 的证据，替身层给不出这一条）
- **T3.5 live 迁移执行完**：dry-run 与真跑逐格对账（`videos=19 + 已存在跳过=2 = 21`、
  `reference=21`、`missing=0`、exit 0）；按计划钉的 `--media-strategy reference`，
  没自作主张换成更省空间的 `hardlink`
- **B站 读数的真机一路已通**（顺路）：`fetch_metrics` 打真 `view`，
  `view=1428/like=49/reply=11/share=4` 落进 `72h` 快照，`coin`/`favorite` 进 metadata

### Fixed（真机那一批挖出来的）
### Fixed（真机那一批挖出来的 · 第二组）

- **`--cookies` 传给子进程的是相对路径**：`YtDlpRunner` 用 `cwd=dest_dir` 跑下载，
  而配置里的 `cookies_file` 是仓库根相对的 `data/cookies/<域>.txt`。存在性检查按我们自己的
  cwd 判（通过），yt-dlp 在别的 cwd 下看不见 → exit 1。**后果正好是 V1 §7.15 那一族**：
  阶梯最高那一档（登录档画质）静默失效、每次都退到匿名档，而这在合成测试里永远看不出
  （它们都在同一个 cwd 下跑）。阶梯这一层现在一律 `expanduser().resolve()` 之后再进 argv
- **`single_link` 把 `xsec_token` 压掉了**：handler 把用户粘的链接换成
  `canonical_video_url(platform, video_id)` 的同时连 query 参数一起丢了，
  于是小红书详情一律被站内跳去风控页，报出来的是 `login_wall_or_removed` ——
  与"笔记被删""登录态过期"三者同形。判定过程：同一条链接、同一个 token，
  在同一个浏览器里**手工导航能正常打开**（标题取得到），所以不是现网的锅。
  新增 `urls.note_token_of()`（`build_note_url` 的反方向，放同一层免得两处各认一次参数名），
  并有一条 build→extract 的往返断言盯着那个尾部 `=` 的双重编码
- **ADR-0019 的 `has_video` 从没出口到需要它的那一端**：字段只活在产物上，
  入库没写进 metadata、模型没暴露、播放器只能按"`media_path` 有没有"猜 ——
  图文笔记因此在详情页挂出一个**永不加载的黑播放器**，而那条 ADR 写这句判据的理由就是它。
  现在三段补齐（入库 / `models/video.Video` 上由 metadata 优先、后缀回落推导 / 播放器改问这一位），
  回落那一支与 `/api/videos/{id}/media` 的容器白名单**逐字对齐**并由一条看护钉住：
  两处各列一份就会漂，而漂的表征正好是"能播的不播"或"黑屏回来了"。
  界面文案也分开两种"没有"：图文是"文件在，只是放不了"，未采到是"没有可播的文件"
- **e2e 全跑到最后两条报"服务线程没退"，单跑每条都过**：根因不在测试而在 uvicorn 配置 ——
  优雅关闭没有上界，浏览器那条 keep-alive 不 FIN 它就一直等，把 `_Server.stop()` 的 30 秒
  join 拖爆。给 `timeout_graceful_shutdown=5`（**不是**把 join 调大，那只是往后挪撞线时间）。
  连跑两次各 10 passed / 35.5s 与 37.6s，对比出问题那次的 69s + 2 errors


- **B站 评论抓取从来没有成功过一次**：`parse_reply_rows` 读的身份键 `idstr` 与点赞键
  `like_count` **在真响应里都不存在**（现网是 `rpid_str`/`rpid` 与 `like`）—— 于是每一行都
  "没身份"而被正当规则丢掉，症状是 `comments_new: 0` 且 `failures` 为空，
  而同一条作品的快照里 `comment_count` 明明写着 11。键名是从合成 fixture 来的，
  所以"实现"与"看护它的测试"共享同一个错误、一起全绿。修完真机数字：`comments_new=3`，
  再跑一次 `0 新增 / 3 更新`
- 同一处的 `metadata_json` 读的 `liked`/`reply_tag`/`floor` 三个键也都不存在
  （那一列因此永远写成 `{}`），换成真有的 `up_action`/`root`/`parent`/`invisible`；
  `_author_id` 按 V1 生产代码的顺序补上顶层 `mid_str`/`mid`
- 新增两道看护，各自红什么都量过：① 真捕获 fixture（`tests/fixtures/bilibili/reply_page.json`）
  + "三行必须出三条草稿"；② **接口给了 N 行而解析出 0 条 → 抛**（"这是接口换了字段名，
  不是这条作品没人评论"）。变异实测：键名改回旧的 → 8 条红（含①）；只拆② → 恰好 1 条红

### Fixed（同日补完）

- `tests/e2e/conftest._stub_adapter` 造的产物写的是 `media_source="e2e_stub"`，
  而 `MediaSource` 那四个字面量里没有它 —— 也就是**e2e 里从来没有一条用例真的走到过
  `download_media`**，所以它一直没人发现（新加的那条长链一跑就炸："采集 failed +
  两条 download 失败"）
- `tasks/enrich_metrics.py`：库里有某平台作品而这一家今天没装配（平台被关掉 / 配置坏了）
  会让整轮崩在 `adapters.get()`，现在记一条 `metrics` 失败照常收尾
- `platforms/xiaohongshu/adapter.py`：`compact_number_to_int` 对"有字但不是数"抛的是裸
  `ValueError`（没有平台名、没有 stage），现在换成 `PlatformError` 并照抄原文
- `Makefile` 的 `e2e` recipe 从来说不通（`$(PLAYWRIGHT) test` 是 Node 那份 CLI，而本仓库装的是
  Python playwright）→ 改成 `$(PYTEST) -m e2e tests/e2e`

### Added（同日再补 · 平台总闸，ADR-0025）

- **`app.yaml` 多一段 `platform_control.enabled`（四家共用的总闸）**。语义是一道路闸：
  `可用 = 总闸 AND 这一家自己的 enabled`，**不改写任何一家自己的值** ——
  批量写四次那种实现在"重新打开总闸"时会把用户之前单独关掉的某家悄悄复原，
  而界面上再也分不出"你自己关的"与"被总闸盖住的"
  （`tests/unit/core/test_config.py` 里那条 `test_the_master_does_not_rewrite_…` 盯着这个）
- **`PlatformAvailability` 四态 + `resolve_platform_availability`**（`platforms/base.py`）：
  `available / own_off / master_off / absent`。四态而不是 bool 的理由是文案要说得出
  **是哪一道闸**挡的 —— 注册表原来那句「已被关掉（config/platforms.yaml 的 enabled: false）」
  在总闸关着时是谎话（文件里写的还是 `true`），会把人支去改一个本来就开着的字段。
  四个消费者（`/api/tasks` 的过滤、跑前那道门、平台注册表、cron 名单）全调这一个函数
- **`GET/PUT /api/platform-control`**：响应带 `shadowed_by_env`（env 压着 yaml 时逐键点名，
  与 `/api/schedule` 同一条纪律）。PUT 之后**当场重排采集 job**，不重启
- `GET /api/platforms` 根上多 `master_enabled`、每行多 `availability`；
  `GET /api/schedule` 多 `master_enabled`（默认名单是空的 = 所有启用的平台，
  于是关总闸会拿到"`effective_platforms` 空、`skipped_platforms` 也空"，
  cron 还写着 08:00 而一家都不排 —— 没有任何一栏解释原因，那一格是真洞）
- **设置页多一张 `PlatformControlCard`**：总闸开关 + 四行**只读**状态
  （开着 / 你自己关的 / 被总闸盖住的 / 没有配置对象）。四行不放开关是刻意的：
  放了就等于把"总闸 + AND"重新解释成"批量写四次"
- `frontend/src/lib/platform-state.ts`：那四句说法的唯一出处，总览页与设置页共用
  （两页各写一份判据迟早分叉，分叉症状是"总览页说已启用、设置页说被总闸关着"）
- 用例：`tests/unit/platforms/test_availability.py`（新，三处入口并排问同一件事 +
  四态穷举 + 总闸闭包现读现算）、`tests/integration/test_api_platform_control.py`（新，15 条：
  一次 PUT 之后四处口径同步、绕界面直接 POST 被拒且点名总闸、真 APScheduler 上 job 被重排）、
  `tests/unit/platforms/test_registry.py` +4（三种红各说一句，含**反向**断言）、
  `tests/integration/test_api_schedule.py`/`platform-control` 各一条防"恒印那句话"的反向用例、
  e2e 一条：真浏览器里翻总闸 → 任务页少掉采集卡片、四行全说"被总闸关着"、
  翻回来第一家仍是"已关闭"
- **15 条变异实测**（`.scratch/mutation/run_master_switch*.py`）：全部 KILLED。
  其中"PUT 里不重排采集 job"这一条第一轮**真的活了**（集成那一格 `apscheduler is None`，
  名单又每次现算），补了带真 `AsyncIOScheduler` 的用例才杀掉。
  第一轮另有三条 `ANCHOR-MISS`（锚点是照 `ruff format` 之前的排版抄的）与一条
  "变异本身是死代码"（把 `case "own_off":` 插到已有 case 后面）—— 两种"看起来验过了"
  记在 `docs/lessons.md`

### Changed（同批）

- `PlatformRegistry.__init__` 多一个**必填关键字参数** `master_enabled: Callable[[], bool]`。
  闭包而不是 bool 快照：传值也能让今天所有用例绿，症状是"设置页显示总闸已开、
  任务列表还是空的"要等重启。没有默认值：默认等于"新调用点忘记传就静默忽略总闸"
- `ConfigManager.is_enabled` 的语义从"这一家自己的开关"改成"这一家可用吗"（AND）。
  名字留着是因为四个调用方要的都是后者
- `ConfigManager.enabled_platforms()` 成为那一份**唯一**的"可用名单"，
  cron 排程、`/api/schedule` 的两栏、`/api/tasks` 的过滤都从它进货
- `TaskScheduler._gate_platforms` 的报错从一句盖住三种成因的「未启用或未实现」
  分家成三句（自家关的 / 被总闸盖住的 / 没有配置对象）
- e2e 那条"草稿式设置页要按保存"的用例，定位从"页面上第一个 switch"换成 `#f-enabled`：
  总闸那一格现在排在表单**上面**，而它是点了就写盘、没有草稿这一步 ——
  用 `.first` 会让那条用例悄悄改测另一个功能，且失败信息完全指不到成因

### Changed
- **`docs/adr/0011`：cookie 阶梯的顺序只有一处真源。**
  `DouyinConfig.ytdlp_cookie_priority` 删除 —— 它与 `DouyinAdapter.capabilities.cookie_variants`
  给的是**两个互相矛盾的顺序**（前者把浏览器档排在导出文件档之前），而 Protocol 写的是
  "实现必须遵守 `capabilities` 的顺序"。同批删除 `persist_play_url`
  （契约里没有任何能存放播放直链的字段 → 只有 `false` 合法的布尔不是配置）。
  V1 的 `DOUYIN_YTDLP_COOKIES_FILE` / `_FROM_BROWSER` 保留兼容，但降级成
  "这一档用哪个文件 / 哪个浏览器"的输入，不再决定顺序。
  影响：`config/platforms.yaml` 抖音一节、`docs/specs/config-schema.md §3.1/§6`、
  B站 的 `cookie_variant_order` 在 Task 7 按同一条处理。
- **`BilibiliConfig.list_strategy` 的取值从 `'api'` 收窄成 `'yt_dlp_flat' | 'external_manifest'`**
  （`docs/adr/0011` Task 7 追记）。设计文档说空间作品列表走公开 web-interface，
  实测 `x/space/wbi/arc/search` 匿名请求回的是 **HTML 风控页**而不是 JSON（它要 WBI 签名），
  而 V1 一直跑通的是 `yt-dlp --flat-playlist`。留一个没有实现路径的取值等于给前端
  渲染出一个点了没反应的选项 —— 与删 `persist_play_url` 是同一条判据。
  `config/platforms.yaml` 的 B站 一节同步。公开 web-interface 没有作废，
  角色换成逐条作品元数据 / 字幕轨 / 博主资料（三个接口都实测匿名 `code:0`）。
- **`BilibiliConfig.cookie_variant_order` 删除**（ADR-0011 第 4 条预告的收口）：
  它与 `BilibiliAdapter.capabilities.cookie_variants` 是同一份顺序写两遍。
  结构看护改成"配置模型里不许出现带 order/priority 的字段名"。
- **`MediaArtifact` 新增 `cookie_rung`**：V1 §7.15 要求"清单 note 必须写清是哪一档"
  （档位差别是画质不是能不能下），而原来唯一能放这句话的格子是 `yt_dlp_error`，
  于是一次**正常的匿名下载**带着一个非空 error，读的人先去找"哪一步失败了"。
  现在 `yt_dlp_error` 语义收窄回"只装失败原文"，档位走 `cookie_rung`；
  抖音的页面直链兜底那一路是 `None`（那条路不经过 yt-dlp，**不编一个假标签**）。
- `netscape_file_blocker` / `pick_exported_cookie_file` / `progress_from_ytdlp_line`
  三件从抖音私有实现提到 `infra/ytdlp.py`，两个平台共用一份
  （V1 §7.15 的原文教训就是"两家各写一份 cookie 阶梯，漂过一次"）。
- **`MediaArtifact.path` 的归属改了**（`platform-adapter.md §2.4` 就地加修订说明）：
  原写"相对 `data/`"，但 `AdapterDeps` 里没有 `FileStorage`，采集层算不出相对路径。
  现在适配器返回 `dest` 下的路径，`FileStorage.rel()` 归一化由入库层（Task 8）做。
- `infra/ytdlp.py` 不再**运行期**导入 `platforms.base`（`CookieVariant` 挪进 `TYPE_CHECKING`）。
  它只出现在注解位置，而这条反方向的边在 `platforms/__init__.py` 接上适配器之后
  会闭合成循环导入 —— 症状是"取决于谁先被 import"的红绿漂移（`docs/lessons.md` 坑 17）。
- `config/platforms.yaml` 拍平：顶层键即平台名，删掉 `platforms:` 外层与 `defaults:` 块；
  原 `defaults:` 的运行时项升格为 `app.yaml` 的真实字段。`xiaohongshu` / `youtube` 整段注释到 V2.1
- `Manifest` 新增 `error` 字段（任务级错误原文）；`FailureRecord.platform` 放宽为可空，
  `stage` 新增 `"task"` 取值 —— 同步修订 `docs/specs/task-runner.md §2.6`
- `logging.py`：`FileHandler` → `RotatingFileHandler`（消费 `rotate_max_bytes` / `rotate_backup_count`）；
  JSON 渲染改 `ensure_ascii=False`；stdout 自动重配 UTF-8；`get_logger` 返回类型改为 Protocol
- 设计文档按实施结果回改三处（都是 spec 那边不成立，不是实现偏离）：
  `data-model.md §3` 的 `attach_transcript()` 返回类型 `Transcript` → `TranscriptRecord`；
  `data-model.md §4` 删掉 `InMemoryStorage` 这一档实现（改成 `SqliteStorage.in_memory()`）；
  `event-schema.md §4/§6` 写明**全局事件只广播不落库**（`task_events.task_id` 是 NOT NULL）
  并补齐 `prune()` 的三条口径（`cancelled` 不享 3 倍保留、`running` 一条不删、`retention_days<1` 入口拒）
- `alembic.ini` 全文改写成纯 ASCII（中文注释会让整条 alembic 命令 `UnicodeDecodeError`，
  因为它用 `encoding="locale"` 读 = 中文 Windows 的 GBK）；中文理由挪进 `alembic/env.py` 与 `docs/lessons.md`
- `pyproject.toml` 的 ruff 豁免补 `TC002`：它与已豁免的 `TC001/TC003` 是同一条理由的另一半，
  漏掉会得到"同一个仓库一半文件把 import 挪进 `TYPE_CHECKING`、一半不挪"的分裂写法
- **14 个"渲染得出来、后端没人读"的平台配置字段标上 `ui:hidden`**（ADR-0012；
  不是 ADR-0011 那次"直接删"，因为这些模型是 `extra="forbid"` 而 `config/platforms.yaml`
  里这 14 个键全在 —— 删字段等于让那份文件走 `extra_forbidden`，起不来）：
  每个都补了 `Field(description=...)` 写明实际生效规则与真源（`FieldInfo` 才是进 schema 的那一份，
  紧跟赋值的 docstring 不进）。同时补上 `config-schema.md §4` 一直承诺却从没被标过的
  `ui:advanced`，并把 §3.2 代码块里 ADR-0011 说"已删除"却仍留着的 `cookie_variant_order` 对齐。
  看护是 `tests/unit/platforms/test_config_fields_have_readers.py`（双向棘轮，判据走 AST
  —— 散文不能当读取路径，实测 `bilibili.list_strategy` 就是被一句 docstring 放过去的）

### Fixed
- `ManifestBuilder.fail()` 静默丢弃异常原文（违反 V1 §1.3「不许吞错」）
- `get_logger` 的返回类型与运行期实际类型不符（`cast` 掩盖了 `BoundLoggerLazyProxy`）
- ruff 配置与中文文档冲突：130 条报错中 110 条是 RUF002 误判全角标点，改为逐条带原因豁免
- **四个 Repository 漏翻译 `IntegrityError`**（`PlatformRepository.delete` / `TaskRunRepository.start`
  / `EventRepository.append` / `ManifestRepository.record`）—— 症状是 API 层只能一律 500
  并给用户一屏 SQLAlchemy traceback，而它需要的是"该平台下还有 N 位博主"这种可行动的文案
- `PlatformRepository._as_json` 用 `default=str` 序列化 `Path`，在 Windows 上落成
  `data\cookies\x.txt`，与库内其余 `as_posix()` 路径**两套分隔符**（按前缀筛媒体会静默匹配不到）
- Alembic autogenerate 生成的迁移引用自定义类型 `UTCDateTime` 却不 import 它，
  `alembic revision` 成功而 `alembic upgrade` 当场 `NameError` —— 用 `env.py` 的 `render_item`
  钩子把它渲染成 `sa.DateTime()`（`UTCDateTime.impl is DateTime`，DDL 逐字相同）
- Alembic 1.20 的 `version_path_separator` 弃用警告 × `filterwarnings = ["error"]`
  = 每个文件库用例抛 `MigrationError`（内存库全绿，只有 `[file]` 档红）→ 改用 `path_separator`
- `infra/ffmpeg.probe_streams()` 让"ffprobe 没装"的 `LookupError` 冒出去，与它自己
  "看不懂时返回空列表"的契约冲突（V1 §7.21 的兜底要求"问不出来算有音频"）——
  后果是**一次已经下好的媒体被判成采集失败**，而这台机器 PATH 里本来就没有 ffprobe。
  现在就地消化成空列表 + 一条 debug 日志；`extract_audio` 那边照旧必须红
  （产出用户要的东西 vs 问一个问题，两种职责两种处置）
- V1 抄来的 `sec_uid` 解析器接受**任何域名**的 `/user/x`，于是
  `https://example.com/user/x` 能生成一个看起来完全合法的 `platform_id`
  （V1 §7.1 那条脏行的另一个入口）。加了一道抖音系域名闸门，
  相对路径（页面 JS 的 `a[href]`）照旧放过
- Alembic `env.py` 的 `dictConfig` 会冲掉 structlog 的 processor 链与 `RotatingFileHandler`
  （迁移是在 `setup_logging()` **之后**跑的）→ `configure_logger` 属性开关，CLI 仍装自己的日志
- 跨会话提醒：`CURRENT_SESSION` 原先住在 `storage/db.py`，使 `db.py` 无法在顶层 import
  repositories（循环），七个 Repository 被迫函数级导入 → 抽出叶子模块 `storage/session.py`
- **清单文件名 `<日期>-<时间>-<kind>.json` 同一秒会撞名**：两个并行的采集任务里，
  后一份 `os.replace` 会原子地、静默地盖掉前一份 —— DB 两条索引指向同一个文件，
  前一个任务的审计凭据消失且不报错 → `manifest_path()` 多收一个 `task_id`（改了 Locked 布局）
- `BaseRepository` 握着 sessionmaker **对象**，`storage.close()` 之后已经取出去的
  Repository 照样能发查询，症状是裸 `sqlite3.OperationalError`（连接池已 dispose）
  而不是本仓库的 `StorageError` → 改注入"给我一个 session"的工厂
- `manifest_writer` 的终态规则：spec §2.6 草图 `else: builder.succeed()` 会把 handler
  设的 `partial` 改成 `success`（绿灯 + `failures[]` 里躺着失败记录）。
  实际规则是"没人设过才补、异常压过 success、handler 的其它终态不动"
- `_path_from_line()` 认不出"已经下载过了"：匹配串按记忆写成 `has already downloaded: `
  （尾巴多个冒号），yt-dlp 从不这么写。症状是**库里有作品行、媒体列表为空**（坑 15）
- cookie 退档判据只认"cookie 读不出来"，风控的 412/352 被当死链直接判负 ——
  V1 §7.15 实测无 cookie 时 B站 枚举就是随机回这两个码，带导出 cookie 就过。
  拆成 `looks_like_cookie_failure`（文案/预检）与 `should_escalate_cookie_rung`（处置）（坑 16）
- `BridgeClient("https://")`（空主机）被接受，症状是所有请求"连不上"而不是配置少打了个 IP
- `CookieFreshness.looks_empty` 被一次批量编辑写坏成"文件存在即空壳"——
  返回类型仍是 bool、`mypy` 与 `ruff` 都不报，只有用例抓得到（经验 16）
- `run_subprocess` 的超时路径会丢掉已读到的 stderr 尾部：加 `_Lines` 累加器，
  边读边落一份，超时异常里带出"为什么这么慢"的唯一线索
- B站 `fetch_creator_profile()` 读 `payload["card"]`，而真形状嵌在 `data` 一层里
  （`{"code":0,"data":{"card":…}}`）。症状是"每位博主都没昵称"而**不报错**，
  第一次把真响应喂进解析层才红（坑 22）
- B站 `entries_to_cards()` 收到一条非 dict 的清单条目时抛
  `AttributeError: 'str' object has no attribute 'get'`。它是公开出口，
  外部清单那条路也喂它 —— 现在早退并跳过那一行，而不是让整位博主失败
- B站 枚举的两条红调了两次才对：先写成"空手时前一层已抛，判 `search_fallback_node_playwright`
  那一支是死代码"，改成捕获 `ListError` 之后又走偏 —— 那句真 412 被替换成了"未实现"。
  现在的分层是**硬失败原样抛**（保留风控原文），**只有空手**才分兜底/抽取失败两条红。
  看护 `test_a_hard_failure_keeps_its_text_even_with_the_fallback_switched_on`

## [0.1.0] — V2.0「骨架可用」（计划中）

待 V2.0 完成判据全部勾掉后发布。详见 [`ROADMAP.md`](ROADMAP.md)。
