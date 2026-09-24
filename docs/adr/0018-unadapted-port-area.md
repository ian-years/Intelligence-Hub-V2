# ADR-0018: 未适配的 V1 搬运代码住 `ported/`，并在那里被门禁明确豁免

- **状态**：Accepted
- **日期**：2026-09-24
- **决策人**：用户 + Qoder
- **相关**：`docs/plans/v2.1-migration-plan.md` §迁移策略原则（`策略:搬运 未适配`）、
  T4.3–T4.6 / T5.1–T5.4、AGENTS.md §1.3（不许臆造成功）、ADR-0009（测试与门禁）

## 背景

V2.1 计划里有八条任务标着 `策略:搬运 未适配`：**把 V1 脚本原样拷进 V2，只保证 import 得通，
不接入主流程**，将来需要时再按 V2 范式重构。用户 2026-09-24 已定调（V1 已废弃，无须保留其可运行性）。

涉及的 V1 源体量（`wc -l` 实测）：

| 目标 | V1 源 | 行数 |
|---|---|---|
| 飞书全家 | `run_feishu_local_sync.py` + `sync_feishu_base_to_local.py` + `sync_feishu_transcript_docs_to_local.py` + `feishu_core.py` + `lark_cli_runtime.py` + `feishu_helpers.py` | ≈5 700 |
| 报告生成 | `prepare_creator_analysis.py` + `render_creator_analysis_report.py` + `generate_creator_insight_dashboard.py` | ≈4 100 |
| 分析引擎 | `launcher/engine/benchmark_engine.py` + `draft_engine.py` | 547 |
| 主体匹配 | `cross_platform_model.py` | 399 |
| 断点日志 | `durable_progress.py` | 42 |

问题不在要不要搬，在**搬进来之后门禁怎么说谎**。本仓库现有三道硬门：

1. `pytest --cov-fail-under=80`（全局）、CI 另一道 `--include='platforms/*,tasks/*,tools/*' --fail-under=90`；
2. `mypy src tools`（`strict = true`）；
3. `ruff check`（select 里含 `BLE`/`S`/`TRY`/`EM`/`ANN`/`PL`/`RET`/`SIM`）。

V1 那份是平铺脚本：无类型注解、`except: pass`、`assert` 当运行期校验、`os.path` 混 `pathlib`。
按现状量过：整个 `src/` 现在 7 300 条可执行语句、覆盖 93.7%。**塞 5 700 行未测代码进去，
全局覆盖率立刻掉到 80% 线下**，唯一"让它绿"的办法是给这些脚本编一堆只为凑行数的用例 ——
那正是 AGENTS.md §1.3 禁止的形状（"看起来在跑"）。反过来，把它们排除在门禁之外而不写下来，
就是 `docs/lessons.md` 里那一族"文档说有看护而实际没有"的第五种形态。

所以这件事必须显式定：**哪些目录不受哪几道门，理由是什么，什么时候收回来**。

## 选项

**A. 按计划原定的位置散布**（`infra/feishu/`、`core/analysis/`、`platforms/bilibili/external_manifest.py`、`tools/*`）。
好处是"看起来已经 V2 化了"。代价是豁免要按路径写七八处，而 `platforms/**` 与 `tools/**`
落在 90% 那一档 —— 给未适配代码开这个口子，等于把那条更严的门禁变成装饰。
更糟的是**下一个读代码的人分不出哪块是适配过的**：`infra/feishu/` 与旁边适配过的
`infra/cookies.py` 长在一个目录里，只有翻文件头那行注释才知道。

**B. 一个专门的目录 `ported/`，豁免只写在这一处。**
读代码的人（和文件浏览器）一眼看到"这一整块是 V1 原样搬进来的"，
`ported/` 之外的一切照旧受全套门禁。代价是与原计划的路径不一致，且"以后搬回来"要多走一步。

**C. 干脆不搬，等功能真正要做时再一起适配。**
省掉豁免这件事，但违背用户的定调（他要的是"能力先进仓库，别让它在 V1 里烂掉"），
而且 T4.x 里有几条（评论、指标快照）确实要在 V2 主流程里跑，不可能"以后再说"。

**D. 全部适配后再入库。**
体量是 ≈10 000 行 + 配套用例，会把 V2.1 剩余工期整个吃掉，而其中大半（飞书全家）
在 V2 里今天根本没有调用方 —— 为一件没人做的事付适配成本。

## 决定

**选 B**，并加四条限制，使豁免是一笔**看得见数额的债**而不是一扇门：

1. **只有真的搬不动的才进 `ported/`。** 体量与"能不能顺手适配"无关而与"改结构会不会改变行为"
   有关：`cross_platform_model.py`（399 行，纯函数 + 一批可搬的用例）与 `durable_progress.py`
   （42 行）**按 V2 范式适配**，进 `core/`；飞书全家与报告生成三脚本（各自带独立镜像库、
   `lark-cli` 子进程协议、PipelineLock、自包含 HTML 生成）进 `ported/`。
   分析引擎两个（547 行、纯本地算法、要吃 V2 的库）**在 `core/analysis/` 适配**，
   但它们的**调用面**（`tasks/` handler、API 路由）照 V2 契约新写。
   判据一句话：**搬进 `ported/` 的东西不许被 V2 主流程 import**（下一条除外）。
2. **`ported/` 只能被 `ported/` 之内的东西引用，或被子进程调用。** 唯一例外是显式的薄壳
   入口（如 `tasks/feishu_sync.py`、`tools/render_reports.py`）：它们**在 `ported/` 之外**，
   因而受全套门禁与 90% 覆盖率约束，它们的职责就是把一次调用转交给搬运代码并把结果
   如实记进清单。凑数的用例在这里没有生存空间 —— 薄壳能断言的是"编排与失败原文"。
3. **豁免只写在三个地方，且逐条带收回条件**（`pyproject.toml` 的注释里就写）：
   `ruff.lint.per-file-ignores`、`mypy.overrides.ignore_errors`、`coverage.run.omit`。
   适配一块就从 `ported/` 移出去一块，**移动目录本身就是收回动作**，不需要另外记得改配置。
4. **每个文件顶部第一行是 `# TODO(v2-adapt): <为什么没适配> <适配时要做什么>`。**
   与本仓库"不许留着说不出名字的债"的纪律一致（AGENTS.md §5 那三栏同一族）。

## 后果

- 正面：门禁数字重新变成实话。`ported/` 之外，全局 ≥80%、`platforms+tasks+tools` ≥90%、
  mypy strict、ruff 全部照旧生效，且**没有为了搬代码而放宽任何一条**。
- 正面：`git ls-files src/intelligence_hub_v2/ported` 就是这笔债的准确清单，
  量得出大小，不必靠"我记得搬了点什么"。
- 代价：`ported/` 里的代码**不受静态检查**，抄进去什么就是什么 —— 包括 V1 的
  `assert` 运行期校验与裸 `except`。这是有意的：未适配的代码没人在跑，
  给它一个假的绿灯比给它一个红的 lint 更糟。
- 代价：与 v2.1 计划里那些目标路径不一致（`infra/feishu/` → `ported/feishu/` 等）。
  计划文档同步改，并在每条任务行记下这次改道。
- 风险：`ported/` 变成"谁都不想碰"的垃圾场。挡住它的是第 3 条那三个收回点，
  以及"主流程不许 import 它"这条 —— 一旦某块真要接入，第 2 条的薄壳就得受全套门禁，
  那时搬不动的借口就没了。
- 与 ADR-0009 的关系：那条定的是门禁的**值**（80/90/strict），本条定的是门禁的**作用范围**。
  两道门的门槛一个都没降，只是把"未适配"这件事从数字里挪到文件名里。
