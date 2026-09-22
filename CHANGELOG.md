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

### Added（测试与门禁）

- 存储层测试 **554 条**（`tests/unit/storage/` 486 + `tests/unit/test_safe_filename.py` 68），
  全仓累计 650 passed、覆盖率 95.07%、`ruff` / `mypy --strict` 全绿
- `storage` fixture 参数化跑两档后端（`memory` = `create_all` / `file` = 真 Alembic），
  外加三条漂移看护：`check_schema_matches_migrations()`、`alembic upgrade/downgrade/upgrade` 往返、
  以及一条**验证漂移看护本身能发现漂移**的用例

### Changed
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
- Alembic `env.py` 的 `dictConfig` 会冲掉 structlog 的 processor 链与 `RotatingFileHandler`
  （迁移是在 `setup_logging()` **之后**跑的）→ `configure_logger` 属性开关，CLI 仍装自己的日志
- 跨会话提醒：`CURRENT_SESSION` 原先住在 `storage/db.py`，使 `db.py` 无法在顶层 import
  repositories（循环），七个 Repository 被迫函数级导入 → 抽出叶子模块 `storage/session.py`

## [0.1.0] — V2.0「骨架可用」（计划中）

待 V2.0 完成判据全部勾掉后发布。详见 [`ROADMAP.md`](ROADMAP.md)。
