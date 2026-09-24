# Intelligence Hub V2 — Makefile
# 用法：make <target>；`make help` 列所有目标
# Windows 上需要 make —— **Git Bash 并不自带**（2026-09-23 在本机 Git for Windows 实测：
# PATH 里没有 make.exe）。没装就照 ci-local 的 recipe 逐条直接跑并自己打退出码；
# 别用 `make ... | tail`，那之后 $? 是 tail 的。装的话：`choco install make`。

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# 路径与变量
# ---------------------------------------------------------------------------
PYTHON       := uv run python
PYTEST       := uv run pytest
RUFF         := uv run ruff
MYPY         := uv run mypy
COVERAGE       := uv run coverage
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

# **必须 --factory**：main.py 没有模块级 `app`（是 create_app 工厂），
# 写 `main:app` 会报 "Attribute app not found" —— 这条原来就写错了（review P1-8）。
dev-backend:  ## 仅起后端（reload）
	uv run uvicorn --factory intelligence_hub_v2.main:create_app --reload --host 127.0.0.1 --port $(PORT)

dev-frontend:  ## 仅起前端（Vite HMR）
	$(NPM) run dev -- --port $(FRONTEND_PORT)

# 用 console script（pyproject: intelligence-hub = main:cli），**不是** `python -m
# intelligence_hub_v2.main`——那没有任何 __main__ 守卫，跑了不做事、退出码 0，
# 是"命令看着在、其实从没跑通"（2026-09-24 review P1-8 修掉）。
run: build  ## 生产模式（构建前端 → 后端 serve 静态文件）
	uv run intelligence-hub

build:  ## vite build 到 frontend/dist（main.py 的 _mount_frontend 读的就是这一份）
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
	$(PYTEST) --cov=src/intelligence_hub_v2 --cov=tools --cov-report=html --cov-report=term-missing
	@echo "→ htmlcov/index.html"

# 与 ci.yml 的**后端那一半**对齐：以前这个 target 不跑覆盖率门禁、也不跑 alembic，
# 于是"make ci-local 全绿"可以和 CI 红同时成立（而验收判据 1 写的就是它）。
ci-local:  ## 本地跑 CI 的后端全套（前端存在时再带上前端）
	@echo "=== lint-python ===" && $(MAKE) lint-python
	@echo "=== alembic 可逆（判据 11）===" && $(MAKE) db-roundtrip
	@echo "=== test-backend ===" && $(MAKE) test-backend
	@echo "=== coverage 门禁（全局 ≥80 / platforms+tasks ≥90）==="
	$(PYTEST) -m "not real_network and not e2e" -q
	$(COVERAGE) report --fail-under=80
	$(COVERAGE) report --include='src/intelligence_hub_v2/platforms/*,src/intelligence_hub_v2/tasks/*,tools/*' --fail-under=90
	@if [ -f frontend/package.json ]; then 	  echo "=== lint-frontend + test-frontend + build ==="; 	  $(MAKE) lint-frontend && $(MAKE) test-frontend && $(MAKE) build; 	else 	  echo "（frontend/package.json 不在 —— Task 10-13 未开工，前端三项跳过）"; 	fi
	@echo "=== ✅ ci-local 全过 ==="

# ---------------------------------------------------------------------------
# Lint 与格式化
# ---------------------------------------------------------------------------
.PHONY: lint lint-python lint-frontend format format-python format-frontend typecheck db-roundtrip snapshot-api
lint: lint-python lint-frontend  ## 全 lint

lint-python:  ## ruff check + ruff format --check + mypy
	$(RUFF) check src tests tools
	$(RUFF) format --check src tests tools
	$(MYPY) src tools

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
	$(MYPY) src tools
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
# 迁移可逆性 与 OpenAPI 快照
# ---------------------------------------------------------------------------
# 路径要问 Python 自己要：Git Bash 的 /tmp 是 MSYS 视图，本机原生 Python 打不开它
# （症状是一句看不懂的 `unable to open database file`）。
db-roundtrip:  ## alembic upgrade → downgrade base → upgrade → check（验收判据 11，跑在临时库上）
	@db=$$($(PYTHON) -c "import tempfile,os;print(os.path.join(tempfile.gettempdir(),'ih-roundtrip-$$.sqlite3').replace(os.sep,'/'))"); \
	export INTELLIGENCE_HUB_STORAGE__SQLITE_URL="sqlite:///$$db"; \
	$(ALEMBIC) upgrade head && $(ALEMBIC) downgrade base && $(ALEMBIC) upgrade head && $(ALEMBIC) check; \
	rc=$$?; rm -f "$$db" "$$db"-wal "$$db"-shm; \
	if [ $$rc -eq 0 ]; then echo "✓ 可逆，且 schema 与 ORM 一致"; fi; exit $$rc

# 必须由 Python 自己写文件，不能吃 shell 重定向：本机 Git Bash 下 `python -c ... > x.json`
# 会按控制台代码页落盘（实测写出 GBK 字节，回头 json.load 直接 UnicodeDecodeError）。
# CI 那一步用同一个写法，两边字节才可能一致 —— 快照比对的全部意义就在这。
# newline=chr(10) 不是洁癖：Windows 上 write_text 默认会把 LF 写成 CRLF，
# 于是本地生成的快照带 CRLF（pre-commit 的 mixed-line-ending 会来擦），
# 而 CI 在 Linux 上生成的是 LF。
snapshot-api:  ## 重新生成 docs/specs/openapi-snapshot.json（改了路由就要跟着提交）
	@$(PYTHON) -c "import json, pathlib; from intelligence_hub_v2.main import create_app; \
	doc = json.dumps(create_app().openapi(), indent=2, sort_keys=True, ensure_ascii=False); \
	pathlib.Path('docs/specs/openapi-snapshot.json').write_text(doc + chr(10), encoding='utf-8', newline=chr(10))"
	@echo "→ docs/specs/openapi-snapshot.json（CI 拿它做契约 diff，漂了就红）"

# ---------------------------------------------------------------------------
# 数据迁移（V1 → V2）
# ---------------------------------------------------------------------------
.PHONY: migrate-v1 migrate-v1-dry migrate-v1-rollback
# ADR-0010 要求的 `--media-strategy` 与 `--rollback` **还没实现**（脚本只有
# --v1-root/--config-dir/--dry-run/--no-resume）。以前这里传一个不存在的 flag，
# 结果是 `error: unrecognized arguments` + exit 2 —— 命令看着在、其实从没跑通过。
# 媒体现在固定 hardlink→copy；补齐这两个开关（或改 ADR）之前不要把 flag 加回来。
migrate-v1:  ## 从 V1 迁移数据（媒体 hardlink，跨卷退 copy）
	$(PYTHON) tools/migrate_from_v1.py --v1-root "$(V1_ROOT)"

migrate-v1-dry:  ## dry-run（不写盘）
	$(PYTHON) tools/migrate_from_v1.py --v1-root "$(V1_ROOT)" --dry-run

# migrate-v1-rollback: 目标先不挂 —— `--rollback` 未实现（ADR-0010 行为契约 6 的欠账）。
# 需要一个能安全撤销的入口再挂回来；在那之前"回滚"只能手工按 metadata_json 里的
# migrated_from_v1 标记删，而那件事没有看护，不该做成一键。

# ---------------------------------------------------------------------------
# CDP 桥
# ---------------------------------------------------------------------------
.PHONY: bridge bridge-cookies
bridge:  ## 起 CDP 桥（Playwright + Chrome 持久化 profile，只绑 127.0.0.1:3457）
# 首次跑要带窗口：`make bridge` 弹一个 Chrome，人工扫码登录一次；
# 登录态存在 data/cdp-bridge-profile/（凭证，不入 git），之后可以 `--headless` 后台跑。
# 加参数：$(PYTHON) -X utf8 -m intelligence_hub_v2.bridge.server --headless
	$(PYTHON) -X utf8 -m intelligence_hub_v2.bridge.server

bridge-cookies:  ## 从桥导出 Netscape cookie 到 data/cookies/（凭证，不入 git）
# 前提：桥在跑（`make bridge`）而且人已经在那个 Chrome 里登录过一次。
# 桥里没有该域的 cookie 时这条 target 红着退出，不会落一个只有表头的空文件
# （那种文件被 --cookies 传出去之后 yt-dlp 不报错，只是匿名 —— V1 §7.15）。
	$(PYTHON) -X utf8 tools/refresh_bridge_cookies.py

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
