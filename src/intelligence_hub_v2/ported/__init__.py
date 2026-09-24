"""未适配的 V1 搬运区（ADR-0018）。

这里的文件是 `E:/08-Codework/Intelligence-Hub` 的脚本**原样搬进来**的：保留 V1 的结构、
V1 的报错文案、V1 的存储假设，只改到 `import` 得通为止。每一份的第一行是
`# ruff: noqa`，紧跟一行 `# TODO(v2-adapt): 为什么没适配 / 适配时要做什么`。

为什么不把它们放到"该在的位置"（`infra/feishu/`、`core/analysis/`）：那样下一个读代码的人
**分不出哪块是适配过的**。放在这一个目录里，文件名本身就是状态。
`git ls-files src/intelligence_hub_v2/ported` 就是这笔债的清单。

三条规矩，第一条有测试钉着（`tests/unit/test_ported_boundary.py`）：

1. **V2 主流程不许 import 这里**，唯一例外是显式列在那份测试里的薄壳
   （`tasks/feishu_sync.py`、`tools/*`）。薄壳在 `ported/` 之外，受全套门禁与 ≥90% 覆盖率
   约束；它的职责只有一件：把一次调用转交给搬运代码，并把结果如实记进清单。
2. **这里的东西不许进全局测量**：mypy 与 coverage 的豁免按目录写在 `pyproject.toml`
   （两处各自的注释里写着收回方式），lint 的豁免写在文件第一行。
3. **搬出去就是收回**。适配一块 → 文件移出本目录 → 三道门禁当场重新开始查它。
   不需要改任何配置，因此也不存在"忘了把豁免撤回来"这种债。

本包的 `__init__` 刻意**不做**任何 re-export：`from ...ported import feishu_core`
这种写法会让人以为它是 V2 设施的一部分。要用就写全路径，让"这是搬进来的"这件事
出现在每一处调用点的 import 语句里。
"""
