# Intelligence Hub V2 — Makefile
# 用法：make <target>；`make help` 列所有目标
# Windows 上需要 make（Git Bash 自带，或 `choco install make`）

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# 路径与变量
# ---------------------------------------------------------------------------
PYTHON       := uv run python
PYTEST       := uv run pytest
RUFF         := uv run ruff
MYPY         := uv run mypy
ALEMBIC      := uv run alembic
NPM          := npm --prefix frontend
PLAYWRIGHT   := uv run playwright
V1_ROOT      ?= E:/08-Codework/Intelligence-Hub
DATA_DIR     ?= data
PORT         ?= 8789
FRONTEND_PORT ?= 5173

# ---------------------------------------------------------------------------
# 安装与环境
# ---------------------------------------------------------------------------
.PHONY: install install-python install-frontend install-hooks install-playwright
install: install-python install-frontend install-hooks install-playwright  ## 装全套依赖

install-python:  ## uv sync（含 dev 与所有 extras）
	uv sync --all-extras --all-groups

install-frontend:  ## npm install
	$(NPM) install

install-hooks:  ## pre-commit install
	uv run pre-commit install
	uv run pre-commit install --hook-type commit-msg

install-playwright:  ## Playwright Chromium（E2E 用）
	$(PLAYWRIGHT) install chromium

# ---------------------------------------------------------------------------
# 开发
# ---------------------------------------------------------------------------
.PHONY: dev dev-backend dev-frontend run build
dev:  ## 同时起后端 :8789 + 前端 :5173（honcho）
	uv run honcho start

dev-backend:  ## 仅起后端（reload）
	uv run uvicorn intelligence_hub_v2.main:app --reload --host 127.0.0.1 --port $(PORT)

dev-frontend:  ## 仅起前端（Vite HMR）
	$(NPM) run dev -- --port $(FRONTEND_PORT)

run: build  ## 生产模式（构建前端 → 后端 serve 静态文件）
	uv run python -m intelligence_hub_v2.main

build:  ## 构建前端到 src/intelligence_hub_v2/web/
	$(NPM) run build

# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
.PHONY: test test-backend test-frontend test-real test-contracts e2e coverage ci-local
test: test-backend test-frontend  ## 全跑（不含 real_network / e2e）

test-backend:  ## pytest（L0-L4）
	$(PYTEST) -m "not real_network and not e2e"

test-frontend:  ## vitest（L5）
	$(NPM) run test -- --run

test-contracts:  ## 仅 L2 适配器契约测试
	$(PYTEST) tests/contracts/

test-real:  ## 真机烟雾测试（手动跑，要 cookie + 桥 + Chrome）
	$(PYTEST) -m real_network -v

e2e:  ## Playwright E2E（L6）
	$(PLAYWRIGHT) test

coverage:  ## 生成 HTML coverage 报告
	$(PYTEST) --cov=src/intelligence_hub_v2 --cov-report=html --cov-report=term-missing
	@echo "→ htmlcov/index.html"

ci-local:  ## 本地跑 CI 全套（断网时用）
	@echo "=== lint ===" && $(MAKE) lint
	@echo "=== test-backend ===" && $(MAKE) test-backend
	@echo "=== test-frontend ===" && $(MAKE) test-frontend
	@echo "=== build ===" && $(MAKE) build
	@echo "=== ✅ ci-local 全过 ==="

# ---------------------------------------------------------------------------
# Lint 与格式化
# ---------------------------------------------------------------------------
.PHONY: lint lint-python lint-frontend format format-python format-frontend typecheck
lint: lint-python lint-frontend  ## 全 lint

lint-python:  ## ruff check + mypy
	$(RUFF) check src tests tools
	$(MYPY) src

lint-frontend:  ## eslint + stylelint + tsc
	$(NPM) run lint
	$(NPM) run typecheck

format: format-python format-frontend  ## 全格式化

format-python:  ## ruff format + ruff check --fix
	$(RUFF) format src tests tools
	$(RUFF) check --fix src tests tools

format-frontend:  ## prettier --write
	$(NPM) run format

typecheck:  ## mypy + tsc
	$(MYPY) src
	$(NPM) run typecheck

# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
.PHONY: db-init db-upgrade db-downgrade db-current db-history db-revision
db-init:  ## 初始化数据库（跑所有迁移）
	$(ALEMBIC) upgrade head

db-upgrade:  ## 升级到最新
	$(ALEMBIC) upgrade head

db-downgrade:  ## 回退一版
	$(ALEMBIC) downgrade -1

db-current:  ## 当前版本
	$(ALEMBIC) current

db-history:  ## 迁移历史
	$(ALEMBIC) history --verbose

db-revision:  ## 生成新迁移：make db-revision m="add foo table"
	$(ALEMBIC) revision --autogenerate -m "$(m)"

# ---------------------------------------------------------------------------
# 数据迁移（V1 → V2）
# ---------------------------------------------------------------------------
.PHONY: migrate-v1 migrate-v1-dry migrate-v1-rollback
migrate-v1:  ## 从 V1 迁移数据（默认 hardlink 媒体）
	$(PYTHON) tools/migrate_from_v1.py --v1-root "$(V1_ROOT)" --media-strategy hardlink

migrate-v1-dry:  ## dry-run（不写盘）
	$(PYTHON) tools/migrate_from_v1.py --v1-root "$(V1_ROOT)" --dry-run

migrate-v1-rollback:  ## 回滚（删 migrated_from_v1=true 的行）
	$(PYTHON) tools/migrate_from_v1.py --rollback

# ---------------------------------------------------------------------------
# CDP 桥
# ---------------------------------------------------------------------------
.PHONY: bridge bridge-cookies
bridge:  ## 起 CDP 桥（默认打开抖音）
	$(PYTHON) cdp_bridge_server.py --open https://www.douyin.com/

bridge-cookies:  ## 从桥导出 cookie 到 data/cookies/
	$(PYTHON) tools/refresh_bridge_cookies.py

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
.PHONY: gen-api clean help
gen-api:  ## 从后端 OpenAPI 生成 TS 类型
	@echo "→ 后端必须起着（make dev-backend）"
	$(NPM) run gen:api

clean:  ## 删构建产物与缓存
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	rm -rf frontend/dist frontend/node_modules/.vite frontend/coverage
	rm -rf src/intelligence_hub_v2/web/static
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

help:  ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
