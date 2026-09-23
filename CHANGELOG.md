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
