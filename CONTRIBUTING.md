# CONTRIBUTING

V2 的开发约定。改代码前读这份。

---

## 开发环境

### 前置依赖

| 工具 | 版本 | 装法 |
|---|---|---|
| Python | 3.12+ | [python.org](https://python.org) / `pyenv` |
| Node | 20+ | [nodejs.org](https://nodejs.org) / `nvm` |
| uv | 最新 | `pipx install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| ffmpeg | 6+ | Windows: `winget install Gyan.FFmpeg`；macOS: `brew install ffmpeg` |
| Chrome | 最新 | 用于 CDP 桥 |
| make | 任意 | Windows 走 Git Bash 自带的；或 `choco install make` |
| pre-commit | 最新 | `uv tool install pre-commit`（已含在 `uv sync` 里） |

可选（飞书同步）：`lark-cli`（`npm install -g @larksuiteoapi/lark-cli`）。

### 一次性安装

```bash
git clone <repo> && cd Intelligence-Hub-V2
uv sync --all-extras              # Python 依赖 + dev 工具
npm --prefix frontend install     # 前端依赖
pre-commit install                # git hook
uv run playwright install chromium  # E2E 测试需要
```

### 跑起来

```bash
make dev          # 同时起后端 :8789 + 前端 :5173（推荐）
make test         # 全测试（不含真机）
make lint         # ruff + mypy + eslint + stylelint
make build        # 生产构建
```

详见 [`README.md`](README.md)。

---

## 工作流

### 分支与提交

- 主分支 `main` 始终绿（CI 全过 + coverage 达标）
- 功能分支 `feat/<slug>` / 修复分支 `fix/<slug>` / 文档分支 `docs/<slug>`
- 提交信息走 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`
- pre-commit 会自动跑 ruff format / mypy / eslint / prettier / detect-secrets，**不许 `--no-verify`**

### PR 流程

1. 开 PR 前本地跑 `make ci-local`（等价于 CI 全套）
2. PR 描述写清楚：动机、改动范围、测试方式、相关 ADR / issue
3. 改了契约层（`docs/specs/` 里那些）→ 必须配 ADR
4. 改了 schema → 必须有 Alembic 迁移 + `upgrade`/`downgrade` 测试
5. CI 全绿后 squash merge

### 文档纪律（每个 PR 都要）

- 有分量的决定 → `docs/adr/NNNN-<slug>.md`（编号不复用）
- 当日推进 → `docs/progress/YYYY-MM-DD.md`
- 新踩的坑 → `docs/lessons.md`
- 接口变更 → 改 `docs/specs/` 对应文档
- 里程碑勾选 → `ROADMAP.md` + `CHANGELOG.md`

---

## 代码风格

### Python

- `ruff format` + `ruff check`（自动修，pre-commit 兜底）
- `mypy --strict`（不允许 `Any` 逃逸，必须 `# type: ignore[code]` 时写理由）
- 类型提示全开：公共接口必须有完整签名
- Pydantic 模型用于所有外部数据（API 入参、配置文件、清单、事件）
- `typing.Protocol` 用于所有可替换的边界（PlatformAdapter / Storage / EventBus / BridgeClient）
- 异步优先：所有 I/O 走 `async`；CPU 密集型（ASR）走 `asyncio.to_thread()`
- 异常层次：`IntelligenceHubError` → `PlatformError` / `TaskError` / `StorageError` / `ConfigError` / `BridgeError`，不允许裸 `except:`，ruff `BLE001` 强制
- 日志走 `structlog`，结构化字段，不允许 `print()`

### TypeScript / React

- `eslint --fix` + `prettier`（pre-commit 兜底）
- `strict: true`，不允许 `any`（必须用时 `// eslint-disable-next-line` + 理由）
- 组件函数式 + hooks，不允许 class component
- 状态：本地 state 用 `useState`，跨组件用 Zustand，服务端数据用 TanStack Query
- 样式：**只用设计令牌**（`tokens.css` CSS 变量 / `tailwind.config.ts` 主题），**禁止硬编码颜色 / 间距 / 阴影**（Stylelint 兜底）
- 孟菲斯风格：粗黑边 3px、硬阴影 `6px 6px 0 ink-black`、直角、几何形状、亮色块
- API 类型从 OpenAPI 自动生成（`npm run gen:api`），**禁止手写 API 类型**

### SQL / Alembic

- 所有 schema 变更走 Alembic 迁移，**禁止手改 SQLite**
- 迁移必须可逆（`upgrade` + `downgrade` 都写）
- 字段加 `CHECK` 约束兜底（如 `is_tracking IN (0,1)`）
- 索引命名 `idx_<table>_<cols>`

---

## 测试

详见 [`docs/specs/contract-tests.md`](docs/specs/contract-tests.md)。

### 分层

- **L0 单元**：纯函数，无 I/O，`tests/unit/`
- **L1 仓库**：Repository + Alembic，SQLite `:memory:`，`tests/unit/storage/`
- **L2 适配器契约**：每个平台 Adapter 满足 Protocol，`tests/contracts/`（抽象基类，V3 复用）
- **L3 调度契约**：EventBus / 清单 / 取消 / 超时，`tests/integration/tasks/`
- **L4 API 集成**：FastAPI 端点 + OpenAPI snapshot，`tests/integration/api/`
- **L5 前端组件**：Vitest + Testing Library，`frontend/src/**/*.test.tsx`
- **L6 E2E**：Playwright，`tests/e2e/`，仅 main + 手动触发
- **L7 真机烟雾**：`@pytest.mark.real_network`，CI 不跑，本地手动

### Coverage 阈值

- 全局 ≥80%
- `src/intelligence_hub_v2/platforms/` 与 `tasks/` ≥90%
- 前端 ≥70%
- 不达标 CI 直接 fail

### 跑法

```bash
make test                       # L0-L5 全跑
make test-backend               # 仅 pytest
make test-frontend              # 仅 vitest
make e2e                        # L6 playwright
make test-real                  # L7 真机（手动）
make coverage                   # 生成 HTML 报告
```

**禁止**：
- `-k` 与显式 node id 混在一行（pytest 会静默 deselected）
- 跑 `pytest tests/` 不看 skip 明细（V1 §7.14 那条经验：SkipTest 不是 fail）
- 用用时做门禁（V1 §7.23：本机 35s ~ 107s 抖动）

---

## 添加新平台

1. 在 `src/intelligence_hub_v2/platforms/<name>/` 起一个新包
2. 实现 `PlatformAdapter` Protocol（参考 `douyin/adapter.py`）
3. 声明 `Capabilities`（needs_browser / needs_cookies / cookie_variants / supports_subtitles / supports_dash_split / list_strategy / media_strategy）
4. 写 `<Name>Config(PlatformConfig)` Pydantic 模型
5. 在 `platforms/registry.py` 注册：`PLATFORMS["<name>"] = <Name>Adapter`
6. 在 `config/platforms.yaml` 加默认配置
7. 在 `tests/contracts/test_<name>_adapter.py` 继承 `PlatformAdapterContractTests`，加平台特有契约用例
8. 前端 `<PlatformBadge>` 加几何形状与色板（`ui-tokens.md`）
9. 更新 `docs/specs/platform-adapter.md` 的"已实现平台"表
10. **不需要改调度器**——这是 V2 设计目标

---

## 添加新任务

1. 在 `src/intelligence_hub_v2/tasks/` 加一个 `<name>_task.py`
2. 定义 `Params(BaseModel)` 与 `async def runner(ctx: TaskContext, params: Params) -> TaskResult`
3. 在 `tasks/registry.py` 注册 `TaskDefinition(...)`
4. 写测试（L3：调度契约 / L4：API）
5. 前端任务卡片墙会自动出现（按 `TaskDefinition` 渲染）

---

## 调试

### 后端

```bash
# 起后端 + 调试日志
uv run uvicorn intelligence_hub_v2.main:app --reload --log-level debug

# 看 SQLite
uv run python -c "import sqlite3; c=sqlite3.connect('data/intelligence_hub.sqlite3'); ..."

# 看清单
ls data/manifests/ | tail
```

### 前端

```bash
npm --prefix frontend run dev      # Vite HMR
npm --prefix frontend run gen:api  # 重新生成 OpenAPI 类型
```

### CDP 桥

```bash
uv run python cdp_bridge_server.py --open https://www.douyin.com/
# 另一个终端
curl http://127.0.0.1:3457/health
```

V1 §7.17 那条经验仍然成立：**别把常驻服务接在 `| head` 后面**（管道断裂会让 handler 抛 `BrokenPipeError`）。重定向到文件：`> .tmp/bridge.out 2>&1 &`。

---

## 故障排查

| 症状 | 排查方向 |
|---|---|
| `make dev` 起不来 | 看 `:8789` 与 `:5173` 是不是被占（V1 §4 那条「双实例同端口并存」经验仍适用） |
| 平台采集 403 / 412 | cookie 失效，跑 `tools/refresh_bridge_cookies.py` 重导 |
| 抖音"未拿到媒体，已改用页面播放直链" | **常态而非故障**（V1 §7.2，抖音对非浏览器客户端风控） |
| B站采集失败 | 看是不是 cookie 三档全挂（V1 §7.15）；Chrome 开着会触发 `Could not copy Chrome cookie database` |
| 转写报 `does not contain any stream` | B站 DASH 未合并（V1 §7.21），不是 ffmpeg 没装 |
| 桥 `/health` 503 | 浏览器被人关了（V1 §7.20），跑一次真请求会自动重建 |
| 预检 ffmpeg 红 | 注册表 PATH 没传到进程（V1 §7.19），重启宿主 |

详见 V1 `AGENTS.md` §7 与 [`docs/lessons.md`](docs/lessons.md)。
