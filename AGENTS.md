# AGENTS.md — V2 仓库的 AI / 工程师入口

**新会话进来先读这份，再读 [`ROADMAP.md`](ROADMAP.md) 与最近一份 [`docs/progress/`](docs/progress/)。**

只写**读代码读不出来的东西**：契约、陷阱、实测结论、决策依据指针。
项目对外说明在 [`README.md`](README.md)，决策依据在 [`docs/adr/`](docs/adr/)，接口契约在 [`docs/specs/`](docs/specs/)。

V1（`E:/08-Codework/Intelligence-Hub`）的 `AGENTS.md` 是这份的前身，**V1 工作区一行不动**，V1 §7 那 25 条陷阱在 V2 的状态见 [`docs/lessons.md`](docs/lessons.md)。

---

## 0. 一句话架构

FastAPI（`src/intelligence_hub_v2/`，回环 `127.0.0.1:8789`）+ React/Vite 前端（`frontend/`，开发时 `:5173`，构建产物由 FastAPI serve）+ SQLite（`data/intelligence_hub.sqlite3`，WAL）+ 四平台 `PlatformAdapter` 实现（`src/intelligence_hub_v2/platforms/{douyin,bilibili,xiaohongshu,youtube}/`）+ CDP 桥独立服务（`src/intelligence_hub_v2/bridge/server.py`，回环 `127.0.0.1:3457`）+ sherpa-onnx ASR。

**所有边界都是 `typing.Protocol` + Pydantic 模型**，V3 重写时换实现、不换契约。契约清单见 [`docs/architecture.md`](docs/architecture.md#v2-contract-listing)。

Python 3.12，uv 管包，ruff + mypy strict 卡风格，pytest + Vitest + Playwright 分层测试。

---

## 1. 三条硬约束（V1 §1 延续，违反就等于返工）

1. **`data/` 永不入库**。里面有 `cookies/*.txt`（有效会话凭证）、`intelligence_hub.sqlite3`、`config/feishu.yaml`（真实 token）、CDP 浏览器 profile、真实博主的视频与口播稿、ASR 模型（233 MB）。`.gitignore` 已经排除整个目录与敏感配置，**不要为了"方便复现"往里加例外或反向放开**。
2. **CDP 桥只绑回环**。它会在你已登录的浏览器里执行任意 JS，暴露到局域网等于交出账号。`bridge/server.py` 默认拒绝非回环地址（`make_server`，用例 `test_the_bind_guard_refuses_a_non_loopback_host_unless_told_otherwise`），`--allow-non-loopback` 是明知故犯的开关，别写进默认配置。客户端那一侧同样只认回环：`BridgeClient` 连非回环地址直接抛（`infra/cdp_bridge.py::_is_loopback_host`，字面判定不查 DNS）。
3. **不许臆造成功**。缺依赖（whisper / yt-dlp / lark-cli / 桥没起 / 网络不通）时的正确行为是：如实失败、把原因写进清单和事件流，**不要**把 `failed` 记成 0、不要把 dry-run 的产物当真数据、不要为了让看板变绿而伪造 `transcript_status`。V1 的历史问题就是"看起来在跑"。

补充：SQL 全部参数化（SQLAlchemy Core 天然如此）；React 天然 HTML 转义，后端模板（如有）走 Jinja2 autoescape；昵称/标题/评论/口播稿都是**外部输入**；删除文件用精确路径，禁止通配符 `rm`；所有用户输入的路径走 `safe_filename()` + 限制在 `data/` 下。

---

## 2. 契约层（V3 重写时不能动的部分）

这些是 V2 → V3 的"接口"。改它们等于改 V3 的规范，要走 ADR。

| 契约 | 文件 | 说明 |
|---|---|---|
| 平台适配器 Protocol | `src/intelligence_hub_v2/platforms/base.py` + [`docs/specs/platform-adapter.md`](docs/specs/platform-adapter.md) | `PlatformAdapter` / `Capabilities` / `MediaArtifact` / `VideoMeta` / `CreatorRef` 等 |
| 任务定义与事件 | `src/intelligence_hub_v2/tasks/definition.py` + [`docs/specs/task-runner.md`](docs/specs/task-runner.md) / [`event-schema.md`](docs/specs/event-schema.md) | `TaskDefinition` / `TaskKind` / `Event` / `EventType` / `Manifest` |
| 数据 schema | `src/intelligence_hub_v2/storage/schema.py` + Alembic + [`docs/specs/data-model.md`](docs/specs/data-model.md) | 所有表与字段 |
| 配置 schema | `src/intelligence_hub_v2/core/config.py` + [`docs/specs/config-schema.md`](docs/specs/config-schema.md) | `AppConfig` / `PlatformConfig`（每个平台一份 Pydantic 模型） |
| 设计令牌 | `frontend/src/styles/tokens.css` + `frontend/tokens.json` + [`docs/specs/ui-tokens.md`](docs/specs/ui-tokens.md) | 孟菲斯色板 / 形状 / 排版 / 图案 / 动效 |
| OpenAPI | FastAPI 自动出，`/openapi.json` | 前端 `openapi-typescript` 据此生成 TS 类型 |
| 契约测试套件 | `tests/contracts/` + [`docs/specs/contract-tests.md`](docs/specs/contract-tests.md) | L2 抽象基类 + V1 §7 25 条陷阱映射 |

实现层（V3 可弃）：所有 `*_adapter.py` 内部、`SqliteStorage`、FastAPI 路由具体实现、React 组件、APScheduler、structlog 配置。

---

## 3. 目录与职责

| 路径 | 职责 |
|---|---|
| `src/intelligence_hub_v2/main.py` | FastAPI 应用入口（lifespan / DI / 路由挂载） |
| `src/intelligence_hub_v2/api/` | 路由层（tasks / platforms / config / events / creators / videos / transcripts / health） |
| `src/intelligence_hub_v2/core/` | 配置加载、日志、异常、DI、`safe_filename`、URL 解析等基础设施 |
| `src/intelligence_hub_v2/events/` | EventBus（asyncio.Queue 多播 + SQLite 持久化 + SSE 桥接） |
| `src/intelligence_hub_v2/platforms/` | `base.py`（Protocol）+ `registry.py`（显式注册表）+ 四平台子包 |
| `src/intelligence_hub_v2/storage/` | SQLAlchemy Core schema + Repository + Alembic 迁移 |
| `src/intelligence_hub_v2/tasks/` | TaskDefinition / TaskRegistry / runner / context / 清单上下文管理器 |
| `src/intelligence_hub_v2/asr/` | sherpa-onnx 封装（SenseVoice + 静音切句补标点） |
| `src/intelligence_hub_v2/bridge/` | CDP 桥服务（`server.py`：Playwright + Chrome 持久化 profile，只绑 `127.0.0.1:3457`；**客户端**在 `infra/cdp_bridge.py`） |
| `frontend/` | React + Vite + TS 源；`frontend/src/pages/` 七页，`components/memphis/` 自定义组件，`styles/tokens.css` 设计令牌 |
| `tools/migrate_from_v1.py` | V1 → V2 一次性迁移脚本（只读 V1 SQLite） |
| `tools/refresh_bridge_cookies.py` | 从桥导出 Netscape cookie 到 `data/cookies/<域名>.txt`（走 `CookieManager.refresh_from_bridge`，渲染器只有那一处；T0.2，2026-09-24） |
| `tests/contracts/` | L2 平台适配器契约测试抽象基类 |
| `tests/{unit,integration,e2e}/` | L0-L1 / L3-L4 / L6 测试 |
| `docs/adr/` | 架构决策记录（0001~0015，背景/选项/决定/后果。**0014 已预留**给 `_check_requires` 那道闸，新决定从 0016 起 —— 编号不复用） |
| `docs/specs/` | 接口契约文档 |
| `docs/progress/YYYY-MM-DD.md` | 每日推进日志 |
| `docs/lessons.md` | V1 → V2 移植经验 |
| `config/` | YAML 配置（`app.yaml` / `platforms.yaml` / `feishu.yaml`） |
| `data/` | 运行时数据目录（gitignore，由 `INTELLIGENCE_HUB_DATA_DIR` 控制） |

---

## 4. 跑起来

```bash
# 装依赖（首次）
uv sync --all-extras
npm --prefix frontend install
pre-commit install

# 开发模式（同时起后端 :8789 + 前端 :5173）
make dev

# 单独跑后端（入口是**工厂**：main.py 里没有模块级 `app`，写 `main:app` 会报
# "Attribute app not found"）
uv run uvicorn --factory intelligence_hub_v2.main:create_app --reload --port 8789

# 单独跑前端
npm --prefix frontend run dev

# 生产构建（vite build → 后端 serve 静态文件，单端口 :8789）
make build
uv run intelligence-hub        # console script = main:cli；`python -m ...main` 没有 __main__，跑了不做事

# 测试
make test                    # 全跑（不含 real_network）
make test-backend            # 仅 pytest
make test-real               # 真机烟雾（手动跑）

# 数据迁移（一次性）
uv run python tools/migrate_from_v1.py --v1-root "E:/08-Codework/Intelligence-Hub" --dry-run
```

**Windows 上 Python 命令都要 `-X utf8` 或确保控制台 UTF-8**（GBK 会把中文打崩）。
**但代码里读文件一律显式 `encoding="utf-8"`**，不许依赖默认编码：本机
`locale.getpreferredencoding()` 是 `cp936`，"带着 `-X utf8` 跑出来的一条绿"守不住任何东西
（2026-09-24 有两条用例就是这样，换一跑就红）。看护 `tests/unit/test_encoding_discipline.py`，
见 `docs/lessons.md` 经验 51。
**`make` 不是自带的**：本机 Git for Windows 里没有 `make.exe`（Makefile 头注释那句"Git Bash 自带"
2026-09-23 实测不成立）。没装 make 时照 `ci-local` 的 recipe 逐条直接跑，
并且**自己打退出码** —— `make ci-local 2>&1 | tail -60` 报的是 `tail` 的 0。

**改了任何代码都要重启 `make dev`**：uvicorn `--reload` 只盯 `src/`，前端 Vite HMR 只盯 `frontend/src/`，配置文件改了要重启后端（运行时不监听文件变化，只通过 API 写）。

---

## 5. 已知陷阱（V1 → V2 移植过程踩出来的，会持续补充）

V1 那 25 条陷阱（`Intelligence-Hub/AGENTS.md` §7）在 V2 的状态分三类：

- **结构性消除**（V2 设计让它不可能再发生）：§7.4 整行覆盖、§7.6 LocalCreatorStore 参数、§7.7 双源、§7.10 路由靠记忆、§7.11 三种命名、§7.12 预检主库错位、§7.17 head 接常驻服务、§7.23 用时抖动门禁、§7.25 墓碑散落
- **契约测试看护**（行为保留，测试守住）：§7.1 sec_uid、§7.2 yt-dlp 必失败、§7.3 Windows cookie、§7.5 转写路径、§7.8 safe_filename、§7.9 SenseVoice 按静音切句补标点、§7.13 技能脚本漂移、§7.14 SkipTest、§7.15 B站 cookie 三档、§7.16 Node playwright、§7.18 桥重建沿用同一个 profile、§7.19 进程 PATH 与注册表一致（`core/runtime_env.py`）、§7.20 桥死了报绿、§7.21 B站 DASH、§7.24 跟踪开关
- **说得出名字但今天没看护**（别当成"已经守住了"）：§7.22 按位扫描（V2.1 的 Backfill，即 T4.x 补录那一族）

> 这三栏由 `tests/contracts/test_contract_guard_index.py` 逐条核：§7.1–§7.25 每条必须有归属、
> 表里点名的用例必须真的存在且真的会跑、本节的"结构性消除"那一行必须与测试里的分桶一致。
> 改任何一栏都要同步改那边，否则是一次红 —— 文档说"有看护"而实际没有，是本仓库踩过两次的坑。
- **V2 新引入的待观察项**：见 [`docs/lessons.md`](docs/lessons.md) 的「V2 新增」节（实施过程中持续补充）

每条对应测试见 [`docs/specs/contract-tests.md`](docs/specs/contract-tests.md)。

---

## 6. 改代码之前的自检清单

- 动的是 `PlatformAdapter` 实现？→ 契约测试基类的用例都过了？`Capabilities` 声明对了？媒体 artifact 带 `media_source` 与失败原文？
- 动的是 `TaskDefinition`？→ `requires` 写全了？`platforms` 写对了（关掉平台时这个任务自动消失）？清单走了上下文管理器（强制终态）？
- 动的是 SQLAlchemy schema？→ 写了 Alembic 迁移？`upgrade` + `downgrade` 都跑过？字段加了 CHECK 约束（如 `is_tracking IN (0,1)`）？
- 动的是 Repository？→ `update_fields()` 是字段级而不是整行覆盖？TypedDict 限定可更新字段集？
- 动的是 Pydantic 模型？→ JSON Schema 自动渲染前端表单还能用？字段加了 `Field(description=...)`？
- 动的是前端组件？→ 用了设计令牌而不是硬编码颜色 / 间距？孟菲斯风格（粗黑边、硬阴影、几何形状）保持一致？
- 动的是配置？→ 默认值在 Pydantic Settings 里？YAML 字段名与 Pydantic 一致？敏感字段（token）不进 git？
- 动的是 API？→ OpenAPI 自动出了？前端类型重新生成了（`npm run gen:api`）？SSE 事件 schema 没破？
- 声称"修好了"？→ 有没有真机跑通的命令 + 数字？没有就在 `docs/progress/` 写"未验证"。

---

## 7. 文档纪律（每会话都要做）

- **每个有分量的决定** → `docs/adr/NNNN-<slug>.md`（编号不复用）
- **每日推进** → `docs/progress/YYYY-MM-DD.md`（今天动了什么 / 卡在哪 / 下一步），即使没进展也写一行
- **新踩的坑** → `docs/lessons.md` 追加
- **接口变更** → 改 `docs/specs/` 对应文档 + 走 ADR
- **里程碑完成** → 改 `ROADMAP.md` 勾选 + `CHANGELOG.md` 追加 + git tag

每会话结束时，**至少更新 `docs/progress/` 与 `ROADMAP.md`**。这是跨会话续上下文的唯一保障。
