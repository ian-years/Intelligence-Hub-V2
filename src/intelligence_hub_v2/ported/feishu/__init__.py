"""V1 飞书同步全家的搬运区（ADR-0018、T5.1）。

六份文件按 V1 原样拷进来、保留 V1 的文件名（对账用）：`run_feishu_local_sync.py` 是管线入口，
`sync_feishu_base_to_local.py` / `sync_feishu_transcript_docs_to_local.py` 是它拉起的两个阶段，
`feishu_core.py` / `lark_cli_runtime.py` / `feishu_helpers.py` 是共用的访问层。

为什么这一族整个待在 `ported/` 而不是"顺手适配一半"：它们互相之间是**结构耦合**的 ——
阶段二的 `DB_PATH` 与语料表 DDL 在 import 期就取自阶段一，阶段一又拿 `feishu_core.CONFIG_PATH`
当配置真源。拆开后每一半都不再是 V1 那份东西，"未适配"这个标签也就失去了意义。

它们依赖的 V1 地面（`utils` / `platform_schema` / `durable_progress` /
`cross_platform_model` / `local_creator_store`）不专属飞书，放在 `../v1_shared/`。

规矩与豁免见 `ported/__init__.py` 与 `tests/unit/test_ported_boundary.py`；本包刻意不做
re-export，要用就写全路径。
"""
