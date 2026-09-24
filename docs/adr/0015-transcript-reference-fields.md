# ADR-0015: 参考材料两字段进 `transcripts`，不放 `videos`

- **状态**：Accepted
- **日期**：2026-09-24
- **决策人**：用户 + Qoder
- **相关**：ADR-0006（数据模型与存储层）、ADR-0010（里程碑与迁移）、
  `docs/specs/data-model.md §2.4 / §6`、`docs/plans/v2.1-migration-plan.md` T1.2 / T1.2b、V1 §1.3

> 编号说明：0014 已被 `_check_requires` 那道闸预留（`ROADMAP.md` 待办池与 `task_registry.py`
> 的注释都点着它）。编号不复用，所以这条是 0015。

## 背景

T1.2（2026-09-24）把 V1 的三份 `postprocess_*.py` 统一成一个 handler，其中"抽取式参考材料"
那一半**只落了磁盘**：`transcript/reference.md`。库里没有它的位置。

于是 V1 → V2 的迁移今天会**静默丢掉两列**。`tools/migrate_from_v1.py` 里
`content_summary` / `key_points` 这两个名字的引用数是 **0** —— V1 的 `videos` 表有这两列，
V2 的 `videos` 与 `transcripts` 都没有，映射表（`data-model.md §6`）里没有这一行，
所以搬运脚本连"丢了两列"都不会记一条错误。

本机 V1 库实测（2026-09-24，`downloads/local.sqlite3` 以 `mode=ro` 打开，21 行）：

| 事实 | 数字 |
|---|---|
| `content_summary` 非空 | 13 / 21 |
| `key_points` 非空 | 11 / 21 |
| `transcript_status='已转写'` | 16 / 21 |
| **有摘要但没有稿子**（`content_summary` 非空且未转写） | **0** |
| 有要点但没有稿子 | **0** |
| `user_pain_points` / `expandable_topics` / `representative_comments` 非空 | **0 / 0 / 0** |
| `content_summary` 长度 | 最长 2982 字（`key_points` 最长 924） |
| 分布 | 抖音 11 行全部有摘要 + 要点；B站 10 行里 2 行有摘要、0 行有要点 |

三条最后一列值得单独说：`user_pain_points` 与 `expandable_topics` 是 T1.2 计划里点名的
"另两列"，**实测一条都没有**。它们是 V2.2 分析层（爆款拆解 / 脚本生成）的**输出**，
V1 那条本地链路从来不产。给它们预建空列 = 给前端两个永远为空的字段，
并且把"这两列由谁写"这件事永久含糊掉（V1 §1.3 那一族）。

## 选项

**A. 放 `videos`（V1 的位置）。**
V1 是一张扁平表：`transcript_status`、`clean_transcript`、`content_summary` 全在 `videos` 上。
照搬最省事，但把 V1 的形状请回来，而且立刻撞上一个不变量：
V2 里重跑转写是常态（换引擎档位、补字幕轨），`TranscriptRepository.attach()` 是
**整行删了再插**。摘要如果在 `videos` 上，稿子换成另一份之后旧摘要还留在原地 ——
库里就出现"摘要对应的是上一份稿子"，而**任何一处都看不出来**。
要修就得让 `attach()` 去清另一张表的列（跨表写、且必须记得清），
正是 `AGENTS.md §6` 那条"改 A 必须记得改 B"的形状。

**B. 放 `transcripts`，只两列。**
摘要与要点是一份稿子的两个派生字段，与 `char_count` / `sentence_count` / `segments_json` 同类；
`attach()` 整行替换天然带走它们。两列都是 `TEXT NULL`，不需要 CHECK。
问题在于**来源**：V1 那 13 条 `content_summary` 实测**不是**V2 这一族的东西 ——
它是 `# 标题` 开头、1459~2982 字的整篇改写（V1 `launcher_server` 有四个写入口：
整篇 `transcript_text`、原稿前 6 行、磁盘上的 summary 文件、小红书笔记正文），
而 V2 的 `extractive_reference()` 有 `MAX_SUMMARY_CHARS=600` 的上限、且只搬运原文片段。
两种质量完全不同的东西共用一列而不标来源，看板上分不出"这条摘要能不能当参考"。

**C. 放 `transcripts`，三列：两列内容 + `summary_method`。** ← 选定

## 决定

`transcripts` 加三列，全部可空（`NULL` = 没做过，不用空串撒谎）：

```sql
content_summary  TEXT,   -- 摘要正文
key_points       TEXT,   -- 逐行 "- 句子"，与 V1 同格式
summary_method   TEXT,   -- 'local-extractive' / 'v1-imported'，CHECK 枚举
```

- **`attach()` 整行替换 → 摘要与稿子同源**是这条决定的承重墙。不需要"重转写时记得清摘要"，
  因为它根本不在那张表上。
- **`summary_method` 是凭据，不是元数据**。它答的是"这段摘要是谁产的、能信到什么程度"，
  与 `transcripts.engine` 对稿子干的是同一件事。V2 自己产的写 `local-extractive`
  （`ReferenceMaterial.summary_method` 早就是这个值），从 V1 搬来的写 `v1-imported`
  且**正文原样保留**（不截到 600、不改写 —— 迁移不许编辑数据）。
- 迁移的落点在 `_maybe_attach_transcript()`：V1 侧"摘要非空"的行全部落在"稿子也搬"的行上
  （上面那个 0），所以今天不丢；但这条**不能靠运气**，脚本里补一条判断 ——
  V1 行有摘要却换不来 V2 的 `transcripts` 行时，如实记一条 `report.errors`，
  不把丢列说成迁移成功。
- **明确不做**：`user_pain_points` / `expandable_topics`（0/21，V2.2 分析层的输出）、
  `representative_comments`（0/21，属 T4.1 评论域）。等真有写者再走一条 ADR 加列。

## 后果

- 正例：`SELECT content_summary, key_points FROM transcripts WHERE video_id = ?` 一次拿到，
  不需要 join `videos`；前端 `/api/videos/{id}/transcript` 多三个字段，一次请求同源。
- **字幕那条路从此也有摘要**。`reference.md` 原来只在 ASR 分支产出（T1.3 之后字幕成了第一优先，
  那 B站 有轨作品的两列就永远是空的）。这一格把 `_store()` 改成两条路共用同一次
  `extractive_reference()`，`MIN_TRANSCRIPT_CHARS` 那道反幻觉闸同样适用
  （几条字的轨不配换来"候选句"）。
- 反例/代价：**将来放 `videos` 级派生字段（V2.2 的爆款拆解分数）不在这条的覆盖里** ——
  那些东西不属于任何一份稿子，到时候要么 `videos` 加列、要么新开 `analysis` 表，另走 ADR。
- 代价：`CHECK` 枚举意味着 V2.2 接生成式摘要时要一次迁移来放宽它。这是仓库既有风格
  （`TRANSCRIPT_ENGINES` 同形），换来的是"写错一个标签就红在写盘那一刻"。
- `data-model.md §2.4` 是 Locked 契约 → 同步改 SQL 块与 §6 映射表；
  `schema.py` 与 Alembic `0002` 的一致性由既有的 `test_schema_matches_migrations` 钉。
  **不改 0001**：0001 已经在本机的 `data/intelligence_hub.sqlite3` 上跑过了。
- `TranscriptDraft` / `TranscriptRecord` 两个 Pydantic 模型加字段（契约里"采集层产出"的
  `Transcript` **不动** —— 摘要不是适配器产出的，是 handler 从正文派生的，
  所以 `platform-adapter.md` 那份 Locked 契约不受影响）。
