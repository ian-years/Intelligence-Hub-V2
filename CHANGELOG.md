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

### Changed
- `config/platforms.yaml` 拍平：顶层键即平台名，删掉 `platforms:` 外层与 `defaults:` 块；
  原 `defaults:` 的运行时项升格为 `app.yaml` 的真实字段。`xiaohongshu` / `youtube` 整段注释到 V2.1
- `Manifest` 新增 `error` 字段（任务级错误原文）；`FailureRecord.platform` 放宽为可空，
  `stage` 新增 `"task"` 取值 —— 同步修订 `docs/specs/task-runner.md §2.6`
- `logging.py`：`FileHandler` → `RotatingFileHandler`（消费 `rotate_max_bytes` / `rotate_backup_count`）；
  JSON 渲染改 `ensure_ascii=False`；stdout 自动重配 UTF-8；`get_logger` 返回类型改为 Protocol

### Fixed
- `ManifestBuilder.fail()` 静默丢弃异常原文（违反 V1 §1.3「不许吞错」）
- `get_logger` 的返回类型与运行期实际类型不符（`cast` 掩盖了 `BoundLoggerLazyProxy`）
- ruff 配置与中文文档冲突：130 条报错中 110 条是 RUF002 误判全角标点，改为逐条带原因豁免

## [0.1.0] — V2.0「骨架可用」（计划中）

待 V2.0 完成判据全部勾掉后发布。详见 [`ROADMAP.md`](ROADMAP.md)。
