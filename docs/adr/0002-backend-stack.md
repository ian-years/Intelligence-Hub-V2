# ADR-0002: 后端工程化栈

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q3

## 背景

V1 是「Python 3.10 写法、无框架、无包管理」，顶层一堆脚本 + `launcher/` + `tests/`，全靠标准库。这套写法在 V1 阶段够用，但 V2 要：

1. 走「标准工程化项目规范」
2. 平台层做成可配置插件
3. 为 V3 全量重写留下**可执行的接口契约**

V1 的痛点：
- 没有 schema 校验，配置漂在 YAML/JSON 文件里靠运行时 `dict.get()` 读
- `launcher_server.py` 用字典 `TASK_DEFS` 硬编码任务调度
- 子进程 stdout 末行 JSON 当结果（脆弱、易破）
- 没有 OpenAPI，前端类型靠手抄
- 没有迁移工具，schema 演化靠 `CREATE TABLE IF NOT EXISTS`

## 决定

采用以下技术栈（一次性定下来，避免后面零碎换）：

| 层 | 选型 | 理由 |
|---|---|---|
| Web 框架 | **FastAPI + uvicorn** | Pydantic 模型即配置/接口的可执行 schema，自带 OpenAPI（V3 重写时这就是规范），async 让"四平台并发采集"从子进程编排降级成 `asyncio.gather` |
| 包管理 | **uv** | lockfile 兼容 pip，比 poetry 快 10-100×，2026 年事实标准 |
| 类型/Lint/Format | **ruff + mypy strict** | ruff 一个工具吃掉 black + isort + flake8；mypy strict 卡类型 |
| 测试 | **pytest + pytest-asyncio + coverage + hypothesis + respx + pytest-vcr** | 标准 + property-based + HTTP mock + 真实响应回放 |
| 配置 | **Pydantic Settings + YAML + JSON Schema** | 人写 YAML，机器校验，前端配置面板可自动生成表单 |
| Python 版本 | **3.12** | V1 已实测全绿 |
| 数据层 | **SQLAlchemy 2.0 Core + Alembic** | 不用 ORM（避免 V1 `local_store.py` 那种"整行覆盖"陷阱）；Alembic 版本化迁移 |
| 任务调度 | **APScheduler** | 单机够用，无需 Redis |
| 日志 | **structlog** | 结构化 JSON 日志，前端可直接渲染 |
| HTTP 客户端 | **httpx** | async + sync 双模，替代 requests |
| 子进程 | **asyncio.create_subprocess_exec** | 替代 V1 `subprocess.run`，stdout 走异步流读 |
| 依赖注入 | **FastAPI Depends()** | 平台适配器通过 DI 注入，测试时换 mock |
| SSE | **sse-starlette** | 任务事件流推送 |
| ASGI 服务器 | **uvicorn[standard]** | 含 uvloop + httptools |

**不选**：
- 保持 V1 stdlib 风格 → 工程化收益打折，Pydantic / OpenAPI / async 都得自己造
- Django → 太重，单机工作台用不上 admin/auth/ORM 那一整套
- Flask → 同步、无 schema 验证、无 OpenAPI 自动生成，相比 FastAPI 没优势
- Poetry → 比 uv 慢一个量级，lockfile 格式非标准

## 后果

**好处**：
- Pydantic 模型 = 数据契约的源代码版本，V3 即使换 TypeScript/Go/Rust，也能从 OpenAPI / JSON Schema 自动生成对应类型
- Alembic 迁移历史 = schema 演化记录，V3 接手时知道"为什么字段长这样"
- structlog JSON = V3 可以直接复用日志解析工具
- async 调度让"同时跑四个平台采集"从子进程编排降级成 `asyncio.gather`，调度层大幅简化
- FastAPI 的 OpenAPI 自动出 → 前端类型用 `openapi-typescript` 一键生成 → 前后端契约一致

**代价**：
- 学习曲线：V1 是 stdlib，V2 引入 SQLAlchemy / Pydantic / FastAPI / async，对纯 stdlib 派开发者有上手成本
- 依赖体积：装完整套依赖约 500 MB（含 yt-dlp / playwright / sherpa-onnx）
- async 调试比 sync 复杂（堆栈深、Task 取消语义需要熟悉）

**对 V3 的意义**：
- V3 即使把后端从 Python 换成 Go/Rust，只要还出 OpenAPI，前端类型与契约测试套件可复用
- V3 即使换 PostgreSQL，SQLAlchemy Core + Alembic 跨方言，迁移路径清楚

**风险**：
- uv 比 poetry 新（2024 发布），生态稳定性 poetry 更久经考验。回退方案：uv lockfile 兼容 pip，必要时可切回 pip-tools / poetry，迁移成本约半天
