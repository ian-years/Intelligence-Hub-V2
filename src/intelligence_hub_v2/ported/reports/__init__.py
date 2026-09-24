"""V1 报告生成三脚本的搬运区（ADR-0018、T5.4）。

三份按 V1 原样拷进来、保留 V1 的文件名（对账用），合计 4 114 行：

- `prepare_creator_analysis.py` —— 从 V1 的飞书镜像库准备"内容分析输入包"（确定性 JSON）。
- `render_creator_analysis_report.py` —— 把 AI 手写的报告 JSON 校验后渲染成自包含离线 HTML。
- `generate_creator_insight_dashboard.py` —— 直接从镜像库出"旧内容情报看板"单文件 HTML。

为什么整块留在 `ported/` 而不是"顺手适配一半"：三份都绑在 V1 那座**独立镜像库**的
`*_readable` 视图与 `downloads/` 那套目录/文件名协议上，改取数就等于改产物内容 ——
"未适配"和"适配过一半"在这里分不开。它们的调用面在 `tools/render_reports.py`（薄壳，
在本包之外，受全套门禁）。

形状说明：V1 的三份首行是 shebang，所以 ruff 的文件级豁免统一落在第 2 行，
`# TODO(v2-adapt)` 从第 3 行起（见 `tests/unit/test_ported_boundary.py`）。
`ROOT = Path(__file__).resolve().parent` 按计划**原地保留**，因此它在 V2 里指向本包目录
而不是仓库根 —— 所有默认路径（`ROOT/"downloads"/…`、`ROOT/"outputs"/…`）都因此失去意义，
薄壳一律传**绝对路径**。为了 import 得通改动的只有两处：`from utils import …` 换成
`ported/v1_shared/utils` 的全路径，其余顶层只有常量与 `sys.path.insert`（V1 形状，未动）。

本包刻意不做 re-export，也不被 V2 主流程 import（规矩见 `../__init__.py`）。
"""
