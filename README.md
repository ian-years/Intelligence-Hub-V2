# Intelligence Hub V2

本机情报站。四平台（抖音 / B站 / 小红书 / YouTube）采集 → 本地 SQLite + 媒体 → ASR 出口播稿 → 看板与配置面板。

**V2 是对 V1（`E:/08-Codework/Intelligence-Hub`）的"重组 + 移植"重构**：把 V1 一堆顶层脚本与 `launcher_server.py` 字典调度重构成标准 Python 包 + FastAPI + React 工作台。**V1 工作区不动，V2 完全独立**。设计动机与决策依据见 [`docs/adr/`](docs/adr/)。

---

## 5 分钟跑起来

```bash
# 0. 前置：Python 3.12、Node 20+、ffmpeg、Chrome、uv（pipx install uv）
# 1. 拉代码 + 装依赖
git clone <this-repo> && cd Intelligence-Hub-V2
uv sync --all-extras
npm --prefix frontend install
pre-commit install

# 2. 配置：复制平台配置模板（默认四平台 enabled=true）
cp config/feishu.yaml.example config/feishu.yaml   # 可选，飞书同步用

# 3. 起开发环境（同时起后端 :8789 + 前端 :5173）
make dev

# 4. 打开 http://127.0.0.1:5173/
```

需要采集抖音 / 小红书时另起一个终端：

```bash
# 起 CDP 桥（V1 的桥，从 V2 仓库直接跑）
uv run python cdp_bridge_server.py --open https://www.douyin.com/
# 浏览器里人工登录一次，再导出 cookie
uv run python tools/refresh_bridge_cookies.py
```

V1 → V2 一次性数据迁移：

```bash
uv run python tools/migrate_from_v1.py \
    --v1-root "E:/08-Codework/Intelligence-Hub" \
    --media-strategy hardlink \
    --dry-run         # 先看会做什么，去掉这个标志才真写
```

---

## 当前状态

V2 项目正在 V2.0「骨架可用」里程碑实施中。当前进度看 [`ROADMAP.md`](ROADMAP.md)，推进日志看 [`docs/progress/`](docs/progress/)。

| 里程碑 | 范围 | 状态 |
|---|---|---|
| **V2.0** | FastAPI + SQLite + 抖音/B站 Adapter + 三页前端（Dashboard/Feed/Settings）+ 孟菲斯设计令牌 | 设计中（设计已锁定，实施待启动） |
| **V2.1** | 补齐小红书/YouTube Adapter + 转写 + Video Detail / Creators / Tasks / Preflight 页 + Backfill | 规划中 |
| **V2.2** | 飞书同步 + 爆款拆解 / 草稿 / 话题 + 暗色模式 + E2E 覆盖 | 规划中 |
| **V3** | 全量重写（接口契约不变，实现可弃） | 远期 |

V2 的契约清单（哪些是 V3 重写时不能动的）见 [`docs/architecture.md`](docs/architecture.md#v2-contract-listing)。

---

## 我要去哪里看什么

| 想知道 | 看哪 |
|---|---|
| 项目整体架构与契约 | [`docs/architecture.md`](docs/architecture.md) |
| 平台适配器接口 | [`docs/specs/platform-adapter.md`](docs/specs/platform-adapter.md) |
| 任务调度与事件 | [`docs/specs/task-runner.md`](docs/specs/task-runner.md) / [`docs/specs/event-schema.md`](docs/specs/event-schema.md) |
| 数据模型（SQLite schema） | [`docs/specs/data-model.md`](docs/specs/data-model.md) |
| 孟菲斯设计令牌 | [`docs/specs/ui-tokens.md`](docs/specs/ui-tokens.md) |
| 配置格式 | [`docs/specs/config-schema.md`](docs/specs/config-schema.md) |
| 测试契约映射 | [`docs/specs/contract-tests.md`](docs/specs/contract-tests.md) |
| 每条决策的"为什么" | [`docs/adr/`](docs/adr/) |
| 跨会话续上下文 / 接手项目 | [`AGENTS.md`](AGENTS.md) |
| V1 的坑在 V2 怎么消的 | [`docs/lessons.md`](docs/lessons.md) |
| 每日推进 | [`docs/progress/`](docs/progress/) |

---

## 工程化约定

- **Python 3.12**，类型提示全开（`mypy --strict`），所有公共接口走 `typing.Protocol` + Pydantic 模型
- **包管理** uv（lockfile 兼容 pip，无 Poetry）
- **代码风格** ruff format + ruff check，pre-commit 卡所有提交
- **依赖只通过 API 写入配置文件**（手改 YAML 不热加载，重启生效）
- **单一真源** SQLite，不再用 JSON 文件存业务状态
- **V1 的 25 条已知陷阱**（`Intelligence-Hub/AGENTS.md` §7）每一条要么结构性消除、要么有契约测试看护，详见 [`docs/lessons.md`](docs/lessons.md)

---

## 许可

私有项目，未对外授权。`data/` 与 `config/feishu.yaml` 含真实凭证与媒体，已在 `.gitignore`。
