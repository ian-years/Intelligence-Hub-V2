# Architecture

> V2 的 C4 视图。Level 1（System Context）→ Level 2（Container）→ Level 3（Component）三层。
> 实现细节去 `docs/specs/`，决策依据去 `docs/adr/`。

---

## Level 1 · System Context

```
┌──────────────────────────────────────────────────────────────────────┐
│                          本机（开发者电脑）                            │
│                                                                       │
│   ┌─────────┐         ┌──────────────────────────┐                   │
│   │  人      │ ──────► │  Intelligence Hub V2     │                   │
│   │  浏览器  │ ◄────── │  127.0.0.1:8000 (API)    │                   │
│   └─────────┘         │  127.0.0.1:5173 (Dev UI) │                   │
│                       │  或 dist/ 静态资源         │                   │
│                       └────────────┬─────────────┘                   │
│                                    │                                  │
│                  ┌─────────────────┼─────────────────┐               │
│                  ▼                 ▼                 ▼               │
│         ┌────────────┐    ┌────────────┐    ┌────────────┐          │
│         │ CDP 桥      │    │ 外部进程    │    │  外网       │          │
│         │ Chrome       │    │ yt-dlp     │    │ douyin.com  │          │
│         │ 3457 (回环)  │    │ ffmpeg     │    │ bilibili    │          │
│         │ 用户已登录    │    │ sherpa-onnx│    │ xiaohongshu │          │
│         └────────────┘    │ node       │    │ youtube     │          │
│                           │ lark-cli   │    └────────────┘          │
│                           └────────────┘                              │
│                                                                       │
│   ┌────────────────────────────────────────────────────────────────┐│
│   │  V1 (E:/08-Codework/Intelligence-Hub/)  ← 只读，迁移期一次性使用 ││
│   └────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────┘
```

**边界与契约**：

- V2 **不**直接打开用户 Chrome，所有浏览器操作走 CDP 桥（3457）。桥是 V1 遗产，V2 复用同一份。
- V2 **不**直连四个平台域名，要么走 yt-dlp（带导出 cookie），要么走 CDP 桥里的页面上下文。
- V2 **只绑 127.0.0.1**，不暴露局域网；CDP 桥同样只绑回环（`--allow-non-loopback` 是明知故犯开关）。
- V1 的 `downloads/` 在迁移期**只读访问**（`file:...?mode=ro` URI），迁移完成后 V2 与 V1 再无耦合。

---

## Level 2 · Container

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Intelligence Hub V2 进程                           │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Frontend (Vite + React 18 + TS)                              │  │
│  │  ─────────────────────────────────────────                    │  │
│  │  • 7 页面：Dashboard / Feed / Video Detail / Creators /       │  │
│  │    Tasks / Settings / Preflight                                │  │
│  │  • TanStack Query (REST) + Zustand (UI state)                  │  │
│  │  • SSE 客户端（useTaskEvents hook）                            │  │
│  │  • 构建产物：frontend/dist/，由后端 StaticFiles 挂载            │  │
│  │  • 开发期：Vite Dev Server (5173) 代理到后端 (8000)            │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │ HTTP / SSE                            │
│                              ▼                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Backend (FastAPI + uvicorn)                                   │  │
│  │  ─────────────────────────────────────                         │  │
│  │  API Routes（/api/v1/*）                                        │  │
│  │    • creators / videos / transcripts / tasks / events /       │  │
│  │      config / health / preflight / manifests                  │  │
│  │                                                                │  │
│  │  Core Services                                                 │  │
│  │    • ConfigManager（热重载 + 校验 + ConfigChanged 事件）       │  │
│  │    • TaskRunner（in-process async + Semaphore + CancelToken） │  │
│  │    • EventBus（asyncio.Queue multicast + SQLite 持久化）      │  │
│  │    • Scheduler（APScheduler，cron / interval）                │  │
│  │    • PlatformRegistry（显式注册表，name → adapter class）     │  │
│  │                                                                │  │
│  │  Platform Adapters（Protocol 实现）                            │  │
│  │    • DouyinAdapter / BilibiliAdapter /                         │  │
│  │      XiaohongshuAdapter / YoutubeAdapter                       │  │
│  │                                                                │  │
│  │  Storage Layer                                                 │  │
│  │    • Repository (creators / videos / transcripts /            │  │
│  │      task_runs / manifests / events)                           │  │
│  │    • File Storage（media / cookies / manifests）              │  │
│  │                                                                │  │
│  │  Infra                                                         │  │
│  │    • SQLAlchemy 2.0 Core + aiosqlite（WAL）                    │  │
│  │    • structlog（JSON 日志）                                    │  │
│  │    • httpx.AsyncClient（共享连接池）                           │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │ subprocess / CDP                      │
│                              ▼                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  External Processes（子进程或 HTTP 桥）                         │  │
│  │  ───────────────────────────────────────                       │  │
│  │  • yt-dlp（媒体下载，B站/YouTube/抖音兜底）                    │  │
│  │  • ffmpeg / ffprobe（音频抽取、流探测）                        │  │
│  │  • sherpa-onnx（本地 ASR，SenseVoice）                         │  │
│  │  • node（yt-dlp-ejs 挑战、B站搜索兜底 playwright）             │  │
│  │  • lark-cli（飞书同步，可选）                                  │  │
│  │  • CDP 桥（127.0.0.1:3457，Playwright 常驻 Chrome）            │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Persistence                                                    │  │
│  │  ─────────────                                                  │  │
│  │  data/                                                          │  │
│  │    • hub.sqlite3（主库，SQLAlchemy Core + Alembic）             │  │
│  │    • media/<platform>/<creator>/<video>.{mp4,m4a,...}          │  │
│  │    • manifests/<date>-<time>-<kind>.json                       │  │
│  │    • cookies/*.txt（桥导出的 Netscape 格式）                    │  │
│  │    • transcripts/<video_id>/speech-clean.txt                  │  │
│  │    • logs/app.log（structlog JSON）                             │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

**部署形态**：单进程。`make dev` 用 honcho 起后端 + 前端两个子进程；生产用 `uvicorn` 直接 serve 后端，前端构建产物挂在 `frontend/dist/`。

---

## Level 3 · Component（Backend 内部）

### 3.1 请求生命周期

```
HTTP Request
     │
     ▼
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│ FastAPI Route│───►│  Dependency  │───►│  Service /  │
│ (api/v1/*.py)│    │  Injection   │    │  Repository │
└─────────────┘    └──────────────┘    └──────┬──────┘
                                              │
                  ┌───────────────────────────┼───────────────────┐
                  ▼                           ▼                   ▼
          ┌─────────────┐            ┌─────────────┐    ┌─────────────┐
          │ SQLAlchemy  │            │ EventBus    │    │ Platform    │
          │ Core (async)│            │ .publish()  │    │ Adapter     │
          └─────────────┘            └──────┬──────┘    └──────┬──────┘
                                            │                  │
                                            ▼                  ▼
                                     ┌──────────┐      ┌──────────────┐
                                     │ SSE      │      │ subprocess / │
                                     │ Broadcast│      │ httpx / CDP  │
                                     └──────────┘      └──────────────┘
```

### 3.2 任务执行流（TaskRunner）

```
POST /api/v1/tasks/run
     │
     ▼
┌──────────────┐    ┌──────────────────┐    ┌────────────────┐
│ TaskRegistry │───►│ TaskDefinition   │───►│ TaskContext    │
│  .get(kind)  │    │  (handler fn)    │    │  (deps + args) │
└──────────────┘    └──────────────────┘    └───────┬────────┘
                                                    │
                                                    ▼
                                          ┌──────────────────┐
                                          │ TaskRunner       │
                                          │  .submit()       │
                                          │  ─ asyncio.Task  │
                                          │  ─ Semaphore     │
                                          │  ─ CancelToken   │
                                          └────────┬─────────┘
                                                   │
                  ┌────────────────────────────────┼────────────────────────────────┐
                  ▼                                ▼                                ▼
          ┌─────────────┐               ┌────────────────┐               ┌─────────────┐
          │ EventBus    │               │ manifest_writer│               │ Repository  │
          │ TASK_STARTED│               │  (ctx manager) │               │ task_runs   │
          │ TASK_PROGRESS│              │  强制写终态     │               │ .create()   │
          │ TASK_*      │               └────────┬───────┘               │ .update()   │
          └─────────────┘                        │                       └─────────────┘
                                                 ▼
                                       ┌──────────────────┐
                                       │ Manifest 落盘     │
                                       │ data/manifests/  │
                                       │ + DB 同步        │
                                       └──────────────────┘
```

### 3.3 平台适配器调用（以「抓取某博主最新作品」为例）

```
TaskHandler(fetch_creator_videos)
     │
     ▼
PlatformRegistry.get("douyin")  →  DouyinAdapter(deps)
     │
     ├─► adapter.parse_creator_url(url)       →  CreatorRef
     ├─► adapter.fetch_creator_profile(ref)   →  CreatorProfile
     ├─► adapter.list_creator_videos(ref, since, limit)
     │       │  AsyncIterator[VideoMeta]
     │       │  （内部：CDP 桥 → 页面 JS → 解析）
     │       ▼
     │   for video in iter:
     │       ├─► adapter.download_media(video, dest, on_progress)
     │       │       │  yt-dlp subprocess（带导出 cookie）
     │       │       │  失败 → 页面播放直链兜底
     │       │       ▼
     │       │   MediaArtifact (SingleFile | VideoAudioPair)
     │       │
     │       ├─► adapter.fetch_subtitles(video)  →  Transcript | None
     │       │       （B站有字幕，抖音/小红书没有）
     │       │
     │       └─► repository.videos.create(...) + .transcripts.create(...)
     │
     └─► EventBus.publish(VIDEO_INGESTED) × N
```

### 3.4 配置热重载

```
PUT /api/v1/config/platforms/douyin
     │
     ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ Pydantic 校验    │───►│ 原子写入          │───►│ ConfigManager    │
│ DouyinConfig     │    │ platforms.yaml   │    │  .reload()       │
│                  │    │ (.tmp → rename)  │    │  ─ 通知订阅者    │
└──────────────────┘    └──────────────────┘    └────────┬─────────┘
                                                          │
                                                          ▼
                                                ┌──────────────────┐
                                                │ EventBus         │
                                                │ CONFIG_CHANGED   │
                                                └────────┬─────────┘
                                                          │
                              ┌───────────────────────────┼───────────────────────────┐
                              ▼                           ▼                           ▼
                      ┌──────────────┐           ┌──────────────┐           ┌──────────────┐
                      │ TaskRunner   │           │ Scheduler    │           │ SSE Clients  │
                      │ 重读平台开关  │           │ 重建定时任务  │           │ 前端刷新设置  │
                      └──────────────┘           └──────────────┘           └──────────────┘
```

### 3.5 模块边界（`src/` 包结构）

```
src/intelligence_hub_v2/
├── api/                      # FastAPI routes（薄，只做 HTTP ↔ Service 翻译）
│   ├── v1/
│   │   ├── creators.py
│   │   ├── videos.py
│   │   ├── transcripts.py
│   │   ├── tasks.py
│   │   ├── events.py         # SSE endpoint
│   │   ├── config.py
│   │   ├── health.py
│   │   └── manifests.py
│   └── deps.py               # Depends() 工厂
├── core/                     # 与平台/HTTP 无关的核心服务
│   ├── config.py             # ConfigManager + Pydantic Settings
│   ├── task_runner.py        # TaskRunner + CancelToken + Semaphore
│   ├── task_registry.py      # TASKS 字典 + TaskDefinition
│   ├── event_bus.py          # EventBus 实现
│   ├── scheduler.py          # APScheduler wrapper
│   └── manifest.py           # ManifestBuilder + manifest_writer ctx manager
├── platforms/                # 每个平台一个子包
│   ├── base.py               # PlatformAdapter Protocol + 数据类型
│   ├── registry.py           # 显式注册表
│   ├── douyin/
│   │   ├── adapter.py
│   │   ├── config.py
│   │   ├── listing.py        # 页面 JS 模板
│   │   └── media.py          # yt-dlp + 兜底
│   ├── bilibili/
│   ├── xiaohongshu/
│   └── youtube/
├── tasks/                    # TaskHandler 实现
│   ├── fetch_creators.py
│   ├── fetch_videos.py
│   ├── postprocess.py
│   ├── preflight.py
│   └── migrate_v1.py
├── storage/                  # Repository + File Storage
│   ├── db.py                 # SQLAlchemy engine + session factory
│   ├── repositories/
│   │   ├── base.py           # Repository Protocol
│   │   ├── creators.py
│   │   ├── videos.py
│   │   ├── transcripts.py
│   │   ├── task_runs.py
│   │   ├── manifests.py
│   │   └── events.py
│   ├── files.py              # media / cookies / manifests 落盘
│   └── migrations/           # Alembic env.py + versions/
├── infra/                    # 外部进程 / HTTP / 桥
│   ├── subprocess.py         # asyncio.create_subprocess_exec wrapper
│   ├── cdp_bridge.py         # httpx client for 3457
│   ├── ytdlp.py              # yt-dlp runner + cookie variants
│   ├── ffmpeg.py
│   └── asr.py                # sherpa-onnx wrapper
├── models/                   # Pydantic models（跨层共享的数据类型）
│   ├── creator.py
│   ├── video.py
│   ├── transcript.py
│   ├── task.py
│   ├── event.py
│   ├── manifest.py
│   └── config.py
├── logging.py                # structlog 配置
├── errors.py                 # 自定义异常层级
└── main.py                   # FastAPI app factory + lifespan
```

**依赖方向**（严格单向）：

```
api/ ──► core/ ──► platforms/ ──► infra/
  │        │           │            │
  │        │           │            ▼
  │        │           │        storage/
  │        │           │            ▲
  │        │           └────────────┘
  │        ▼
  └──► models/  （所有层都可以依赖 models，models 不依赖任何层）
```

- `models/` 是纯数据，无业务逻辑，可被任何层引用。
- `storage/` 只被 `core/` 和 `platforms/` 引用；`api/` 不直接碰 Repository（通过 Service 层）。
- `infra/` 只被 `platforms/` 引用；外部进程的封装在这里收口。
- `api/` 是最外层，只做 HTTP ↔ Service 翻译，不含业务逻辑。

### 3.6 前端模块边界（`frontend/src/`）

```
frontend/src/
├── main.tsx                  # 入口
├── App.tsx                   # Router + Layout
├── api/                      # openapi-typescript 生成的 client + 手写 hook
│   ├── generated/            # 自动生成，不手改
│   └── hooks/                # useCreators / useVideos / useTasks / ...
├── events/                   # SSE 客户端
│   └── useTaskEvents.ts
├── stores/                   # Zustand
│   ├── ui.ts                 # 侧边栏、抽屉、模态
│   └── settings.ts           # 用户偏好（localStorage）
├── pages/                    # 7 个页面
│   ├── Dashboard.tsx
│   ├── Feed.tsx
│   ├── VideoDetail.tsx
│   ├── Creators.tsx
│   ├── Tasks.tsx
│   ├── Settings.tsx
│   └── Preflight.tsx
├── components/               # 可复用组件
│   ├── memphis/              # Memphis 风格基础组件
│   │   ├── PlatformBadge.tsx
│   │   ├── HardShadowCard.tsx
│   │   └── PatternBackground.tsx
│   ├── ui/                   # shadcn/ui（Radix Primitives）
│   └── shared/               # 业务组件（CreatorCard / VideoRow / TaskTimeline / ...）
├── styles/
│   ├── tokens.css            # CSS 变量（从 docs/specs/ui-tokens.md 生成）
│   ├── patterns.css          # SVG 背景
│   └── globals.css           # Tailwind 入口
└── lib/
    ├── utils.ts
    └── formatters.ts         # 日期、时长、文件大小
```

**数据流**：

```
API (TanStack Query) ──► Pages / Components
                              ▲
SSE (useTaskEvents) ──────────┘
                              │
Zustand (UI state) ───────────┘
```

- 服务器状态走 TanStack Query（自动缓存、重试、失效）。
- UI 状态（侧边栏展开、抽屉打开）走 Zustand。
- SSE 事件通过 `useTaskEvents` hook 广播，组件订阅感兴趣的事件类型。

---

## 跨切面关注

### 错误处理

- 自定义异常层级在 `src/intelligence_hub/errors.py`。
- 业务异常（`PlatformError` / `CookieExpiredError` / `BridgeUnavailableError`）→ HTTP 4xx/5xx + JSON body。
- 任务执行中的异常 → `manifest_writer` 强制写终态（`status: failed`），不向上抛。
- 前端：TanStack Query 的 `onError` + 全局 toast。

### 日志

- `structlog` JSON 输出到 stdout + `data/logs/app.log`。
- 每条日志带 `task_run_id` / `platform` / `creator_id` 上下文（通过 `structlog.contextvars`）。
- 前端不消费日志，只消费 SSE 事件。

### 安全

- 后端只绑 `127.0.0.1`。
- CDP 桥只绑回环。
- `config/feishu.yaml` 与 `data/cookies/` 进 gitignore，pre-commit `detect-secrets` 兜底。
- SQL 全部参数化（SQLAlchemy Core 天然如此）。
- 删除文件用精确路径，禁止通配符 `rm`。

### 可观测性

- `/api/v1/health` → 后端存活。
- `/api/v1/preflight` → 外部依赖检查（ffmpeg / yt-dlp / node / CDP 桥 / ASR 模型 / 平台 cookie）。
- `/api/v1/events` (SSE) → 实时事件流。
- `data/manifests/*.json` → 任务终态审计。
- `data/logs/app.log` → 结构化日志。

---

## 与 V1 的对照

| 维度 | V1 | V2 |
|------|----|----|
| 后端 | 标准库 `http.server` + 子进程脚本 | FastAPI + in-process async + subprocess（外部二进制） |
| 前端 | 原生 JS（无构建） | React 18 + TS + Vite |
| 配置 | 散落的环境变量 + 硬编码 | Pydantic Settings + YAML + API 写入 + 热重载 |
| 存储 | SQLite（整行覆盖 upsert） + JSON 文件（creators.json） + 磁盘扫描 | SQLite（字段级 update）+ 文件（media/manifests/cookies） |
| 任务 | 子进程 + 尾行 JSON 契约 | in-process async + Manifest context manager + EventBus |
| 事件 | 无（前端轮询） | SSE + EventBus + SQLite 持久化 |
| 平台开关 | 跟踪开关（`is_tracking`）+ 硬编码 | 配置驱动（`platforms.yaml` 的 `enabled`）+ 前端 UI |
| 测试 | pytest（466 passed）+ node --test（20 pass） | pytest（L0-L7 分层 + 覆盖率门禁）+ Vitest + Playwright |
| 文档 | AGENTS.md（陷阱集）+ README + HANDOFF + TESTING | ADR × 10 + specs × 7 + ROADMAP + AGENTS + CHANGELOG + CONTRIBUTING |
| 数据目录 | `downloads/`（永不入库） | `data/`（永不入库） |

V2 不是 V1 的"重写"，是**重新设计契约 + 按契约实现**。V1 的 25 条陷阱里 9 条被结构性消除（如 `update_fields()` 取代整行覆盖），16 条由契约测试看护（详见 `docs/specs/contract-tests.md`）。

---

## V3 的预留

V2 的所有契约（Protocol / Pydantic models / SQLite schema / OpenAPI / Event schema / Design tokens）都是 V3 的**可执行规范**。V3 可以：

- 换语言（Rust / Go）重写后端，只要实现同一份 OpenAPI + Event schema。
- 换前端框架（Svelte / Vue），只要消费同一份 API。
- 换存储（PostgreSQL），只要保持 Repository Protocol。
- 换任务模型（Celery / Temporal），只要保持 Manifest 终态契约。

契约测试（`docs/specs/contract-tests.md` 的 `PlatformAdapterContractTests` 抽象基类）是 V3 的验收门禁：新实现必须通过同一套测试。
