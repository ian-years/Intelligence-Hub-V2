"""V1 全家共用的地面（不是飞书专属，所以不和 `feishu/` 混在一层）。

`utils.py`（时间文本 / `safe_filename` / 原子写 JSON / 子进程 UTF-8 环境）、
`platform_schema.py`（飞书列名与平台词表）、`durable_progress.py`（先落盘后打屏）、
`cross_platform_model.py`（跨平台主体匹配 + 多维表格字段规格）、
`local_creator_store.py`（独立 JSON 博主库）。

其中两份是**已知的重复**，各自的 `# TODO(v2-adapt)` 里写着 V2 的对应物：
`cross_platform_model.py` → `core/identity.py`（T4.3 已适配），
`durable_progress.py` → `core/durable_progress.py`（T4.5 已适配）。
本区不 import 那两个 core 模块，也不被它们 import —— 收回动作是"把文件搬出 `ported/`"，
不是"让两边互相引用"（ADR-0018 决定 3）。
"""
