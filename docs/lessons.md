# Lessons

> V1 的 25 条陷阱（`AGENTS.md §7`）在 V2 的命运，以及 V2 实施过程中新踩的坑。
> 每条都带**判据**（怎么验证它还在/没了）与**看护**（哪条测试或哪个结构性约束在守着）。

---

## 第一部分 · V1 §7 陷阱在 V2 的命运

V1 的 `AGENTS.md §7` 记录了 25 条踩出来的陷阱。V2 不是"重写一遍然后祈祷别再踩"，而是逐条判定：
**结构性消除**（设计上不可能再犯）/ **契约测试看护**（仍可能发生，但有测试守着）/ **V2 新增**（V1 没有的问题）。

完整映射表在 `docs/specs/contract-tests.md`。这里只记**结论**与**判据**。

### 结构性消除（9 条）

| V1 §7 | 陷阱 | V2 的解法 | 判据 |
|-------|------|----------|------|
| §7.1 | 抖音身份是 `sec_uid`，短链不含身份 | `DouyinAdapter.parse_creator_url()` 强制跟 302，返回 `CreatorRef(platform_id=sec_uid)`；`CreatorRef` 是 Pydantic model，`platform_id` 必填 | 单元测试：短链输入 → `CreatorRef.platform_id` 是 sec_uid 而非 URL |
| §7.4 | `upsert_video()` 整行覆盖，空标题折叠成默认值 | `VideoRepository.update_fields(id, VideoUpdatableFields(...))` 字段级更新，`VideoUpdatableFields` 是 TypedDict，只允许显式列出的字段 | mypy strict 拒绝 `update_fields(id, {"title": None})`；契约测试：部分字段更新后其他字段不变 |
| §7.5 | 转写落盘目录按平台不对称 | `FileStorage.transcript_path(video_id)` 统一路径（`data/transcripts/<video_id>/speech-clean.txt`），与平台无关 | 契约测试：四个平台的转写都落在同一目录结构 |
| §7.6 | `LocalCreatorStore` 第一个位置参数是 root 不是 path | V2 没有 `LocalCreatorStore`；`CreatorRepository` 通过 DI 注入 `db_session`，无路径参数 | 代码审查：`CreatorRepository.__init__` 签名只有 `session: AsyncSession` |
| §7.7 | `creators.json` 顶层是 list，`local_store` 里同名表是 dict | V2 只有一个数据源（SQLite），无 JSON 文件 | `data/` 目录里没有 `creators.json`；迁移脚本读 V1 的 JSON 后只写 SQLite |
| §7.10 | `/api/state` 之类"顺手猜的路由"不存在 | OpenAPI schema 自动生成，前端用 `openapi-typescript` 生成类型；不存在的路由在编译期就报错 | `make gen-api` 后 `frontend/src/api/generated/` 的类型与后端路由一一对应；前端调用不存在的路由 → TS 编译错误 |
| §7.11 | `videos` 表列名 `creator_id`，外部数据叫 `creator_platform_id` / `mid` | Pydantic model `VideoMeta` 统一字段名 `creator_platform_id`；`VideoRepository.create()` 内部映射到 `creator_id` 列；外部代码只见 `VideoMeta` | mypy strict：`VideoMeta` 的字段名固定；契约测试：四个平台的 `list_creator_videos()` 返回的 `VideoMeta` 字段名一致 |
| §7.12 | 预检 `sqlite` 检查的是飞书镜像库，不是主库 | V2 只有一个 SQLite（`data/hub.sqlite3`）；`/api/v1/preflight` 检查的是这个库 | 预检代码审查：`preflight.py` 里 `db_path` 是 `config.storage.db_path`，无分支 |
| §7.25 | 情报流"删除"= 删行 + 墓碑，少一半都不算数 | V2 没有墓碑文件；`videos.is_hidden` 列，`VideoRepository.hide(id)` 只更新这一列；`list_videos()` 默认过滤 `is_hidden=False` | 契约测试：`hide()` 后 `list_videos()` 不返回，但 `get_video(id)` 仍能取到；磁盘扫描不会复活（V2 无磁盘扫描） |

### 契约测试看护（16 条）

这些陷阱在 V2 仍可能发生（外部依赖、网络、子进程），但每条都有对应的契约测试守着。
测试名 + 层级 + 文件见 `docs/specs/contract-tests.md`。

| V1 §7 | 陷阱 | V2 的看护 |
|-------|------|----------|
| §7.2 | 抖音对非浏览器客户端风控，yt-dlp 从来没产出过媒体 | `DouyinAdapter.download_media()` 的契约测试：mock yt-dlp 返回失败 → 验证走页面播放直链兜底 → 验证 `MediaArtifact` 的 `source` 字段是 `page_play_url` |
| §7.3 | Windows 上 yt-dlp 读不了 Chrome cookie 库 | `ytdlp_cookie_variants()` 的单元测试：三档顺序（导出文件 > browser > 匿名）；`looks_like_cookie_failure()` 认全 V1 记录的所有原文 |
| §7.8 | `safe_filename()` 不止换 `/`，还要处理 `..` / 结尾点空格 / Windows 设备名 | `FileStorage.safe_filename()` 的 hypothesis 测试：随机 Unicode 输入 → 输出不含 `/ \ ..`、不以点/空格结尾、不是 Windows 设备名 |
| §7.9 | SenseVoice 不产标点，`asr_sherpa.py` 按静音切句补 `。` | `asr.py` 的契约测试：mock sherpa-onnx 返回无标点文本 → 验证 `Transcript.segments` 每段以 `。` 结尾 |
| §7.13 | 技能脚本物理上有两份（仓库 + `~/.agents/skills/`） | V2 没有技能脚本；CDP 桥是 V1 遗产，V2 通过 `infra/cdp_bridge.py` 的 httpx client 调用，无脚本复制问题 |
| §7.14 | `tests/test_bilibili_browser_listing.py` 找不到生产者脚本时是 SkipTest 不是 fail | V2 的 B站 列表枚举走 CDP 桥（`BilibiliAdapter.list_creator_videos()`），无外部脚本依赖；契约测试 mock 桥的 `/evaluate` 响应 |
| §7.15 | B站 cookie 分两条路（枚举 + 媒体），两条都得带导出文件 | `BilibiliAdapter` 的契约测试：枚举时验证 yt-dlp argv 带 `--cookies`；下载时验证三档顺序；`cookie_variant_order` 配置项控制 |
| §7.16 | B站搜索兜底要 Node 版 playwright + `NODE_PATH` | V2 的 B站 列表枚举**不走搜索兜底**（走 CDP 桥的 space 页面滚动）；如果未来需要，`preflight` 检查 `node` + `NODE_PATH` + `playwright` 模块 |
| §7.17 | 别把常驻服务接在 `| head` 后面（BrokenPipeError） | V2 的后端用 uvicorn，日志走 structlog 到文件；开发期 `make dev` 用 honcho，不会管道断裂 |
| §7.18 | 桥 profile 的登录态是跨会话持久的 | `data/cdp-bridge-profile/` 是 V1 遗产，V2 复用同一份；`preflight` 检查桥的 `/health` + cookie 有效性 |
| §7.19 | "注册表里有 PATH" ≠ "进程拿得到"（ffmpeg） | `preflight` 用 `shutil.which()` 检查**当前进程**的 PATH，不读注册表；如果红，提示用户重启**启动方**（终端 / Qoder 宿主） |
| §7.20 | 桥的浏览器被人关掉后 formerly 会一直报绿 | V2 复用 V1 的桥（已修）；`infra/cdp_bridge.py` 的 `probe()` 用真往返（`/cookies`）判活；`/health` 503 时 `bridge_available()` 返回 True（自愈发生在第一条真请求） |
| §7.21 | B站媒体可能是未合并的 DASH 分片 | `BilibiliAdapter.download_media()` 返回 `MediaArtifact`（`SingleFile` | `VideoAudioPair`）；`postprocess` 任务用 `ffprobe` 检查音频流，无音频 → 报错而非静默丢 |
| §7.22 | 「抓取爆款 Top 5」必须按位扫描，跟踪开关只管整库/定时 | V2 的任务参数显式传 `creator_ids: list[str]`，不依赖"跟踪开关"；`fetch_creator_videos` 任务的 `creator_ids` 为空时报错而非退化全库扫描 |
| §7.23 | pytest 用时抖动有三倍 | V2 的 CI 门禁用**覆盖率**（80% / 90% / 70%），不用用时；`Makefile` 的 `test` target 不计时 |
| §7.24 | 「持续跟踪」的值必须是真布尔，且默认值只能有一处 | V2 的 `creators.is_tracking` 列是 `INTEGER CHECK (is_tracking IN (0, 1))`；`CreatorRepository.set_tracking(id, tracking: bool)` 入口 `isinstance(tracking, bool)` 断言；前端默认值由 `settings.ts` 的 Zustand store 决定一次 |

### V2 新增（实施过程中踩的坑）

> 本节在 V2.0 实施过程中逐步填充。每条格式：
> **现象** → **根因** → **解法** → **判据** → **看护**

（暂无，待实施）

---

## 第二部分 · V2 设计与实施过程中的经验

### 设计阶段（2026-09-22）

#### 经验 1 · ADR 先行，避免"边写边改"

**现象**：V1 的架构决策散落在 `AGENTS.md` / `HANDOFF.md` / 代码注释里，新人（包括未来的自己）要拼凑。

**解法**：V2 在写任何代码之前先写 10 份 ADR，每份记录**背景 / 选项 / 决定 / 后果**。后续实施时如果遇到"当初为什么这么设计"的疑问，先翻 ADR。

**判据**：`docs/adr/` 有 10 份文件，每份都有 Status / Date / Deciders / Related 字段。

**看护**：`CONTRIBUTING.md` 的"如何提议架构变更"一节要求新 ADR。

#### 经验 2 · 契约测试是 V3 的验收门禁

**现象**：用户明确说"V3 还是希望完全重构"，V2 的设计要为 V3 服务。

**解法**：`docs/specs/contract-tests.md` 定义了 `PlatformAdapterContractTests` 抽象基类，V3 的新实现（哪怕换语言）必须通过同一套测试。契约测试不测实现细节，只测**输入输出 + 不变量**。

**判据**：四个平台的 adapter 测试都继承 `PlatformAdapterContractTests`；CI 的 `test-backend` job 跑这些测试。

**看护**：`Makefile` 的 `test` target 包含契约测试；覆盖率门禁（platforms/ 90%）。

#### 经验 3 · 结构性消除 > 契约测试 > 文档提醒

**现象**：V1 的 25 条陷阱里，有些是"设计缺陷"（如整行覆盖 upsert），有些是"外部依赖"（如 yt-dlp 读不了 Chrome cookie）。

**解法**：优先级是**结构性消除**（设计上不可能再犯）> **契约测试看护**（仍可能发生但有测试守着）> **文档提醒**（最后手段）。V2 的 9 条结构性消除都是"换 API"或"换数据模型"，让错误用法在编译期/类型检查期就失败。

**判据**：`docs/specs/contract-tests.md` 的映射表，每条陷阱都标注了"结构性消除"或"契约测试看护"。

**看护**：代码审查时，如果发现某条陷阱只有"文档提醒"没有测试，要求补测试或改设计。

#### 经验 4 · 配置驱动 vs 硬编码的边界

**现象**：用户要求"四个平台做成配置类型，可以在前端页面的配置里面打开或者关闭"。

**解法**：`platforms.yaml` 的 `enabled` 字段控制平台开关；`ConfigManager` 热重载后通知 `TaskRunner` / `Scheduler` / SSE 客户端。但**不是所有东西都配置化**：
- 平台的 `name` / `display_name` / `capabilities` 是代码常量（改这些要发版）。
- 平台的 adapter 实现是代码（配置只能开关，不能换实现）。
- 任务定义的 `kind` / `handler` 是代码（配置只能调参数，不能换 handler）。

**判据**：`config/platforms.yaml` 只有 `enabled` / `cookie_variant_order` / `media_strategy` / 超时 / 并发数这类**运行时可调**的字段。

**看护**：`docs/specs/config-schema.md` 的"什么该配置化"一节；代码审查时拒绝"把平台名写进配置"这类 PR。

#### 经验 5 · 文档五件套（ADR + specs + ROADMAP + progress + lessons）

**现象**：用户要求"把要做的事情和进展，经验等记录下来，以免上下文丢失信息"。

**解法**：
- `docs/adr/` — 架构决策（为什么这么做）。
- `docs/specs/` — 契约规范（接口长什么样）。
- `ROADMAP.md` — 里程碑与完成标准（做到哪了）。
- `docs/progress/YYYY-MM-DD.md` — 每日进展日志（今天做了什么）。
- `docs/lessons.md` — 经验与陷阱（踩了什么坑）。

每个会话结束前必须更新 `progress/` 和 `ROADMAP.md`。

**判据**：`docs/progress/` 有今天的文件；`ROADMAP.md` 的"当前状态"一节是最新的。

**看护**：`CONTRIBUTING.md` 的"会话结束前的检查清单"。

### 实施阶段（V2.0）

> 本节在 V2.0 实施过程中逐步填充。

（暂无，待实施）

---

## 附录 · 如何新增一条经验

1. 在对应部分（V1 §7 映射 / V2 设计 / V2 实施）新增一节。
2. 格式：**现象** → **根因** → **解法** → **判据** → **看护**。
3. 如果是 V1 §7 的陷阱，更新 `docs/specs/contract-tests.md` 的映射表。
4. 如果是结构性消除，说明"为什么设计上不可能再犯"。
5. 如果是契约测试看护，给出测试名 + 层级 + 文件。
6. 提交时在 commit message 里引用本文件的章节（如 `docs(lessons): add V2 implementation lesson #3`）。
