# ADR-0021: `topics` 与 `drafts` 按预留契约落表，但只落有人写的那两张；选题状态不进事件流

- **状态**：Accepted
- **日期**：2026-09-24
- **决策人**：用户 + Qoder（本条动了 Locked 契约面 `data-model.md §2.8 / §2.9`）
- **相关**：ADR-0006（数据模型与存储）、ADR-0020（同一族"给 Locked 模型加表"的手续）、
  ADR-0005（事件 schema，本条**没有**动它）、
  `docs/plans/v2.1-migration-plan.md` T5.5、`docs/specs/data-model.md §2.8 / §2.9`

## 背景

`data-model.md` 从 V2.0 起就把 §2.8 / §2.9 / §2.10 三节写成**预留契约**：列与含义已定，
但"V2.2 实施"，所以今天库里没有这三张表。T5.5 要交付的是"选题金矿页 + 草稿增删查改"，
它需要 §2.8 与 §2.9 —— 而 §2.8 那一节的代码块里其实躺着**两张**表（`topics` 与关联表
`video_topics`）。于是这一格真正要决定的不是"怎么建表"（照抄 DDL 就行），
而是三件抄不出来的事：

1. **§2.8 的两张表都建吗？** 选题与作品的连线由谁写、今天有没有那个写入口。
2. **部分更新的白名单里放什么？** 两张表都是"人手工编辑"的对象，而
   `update_fields()` 的字段集（V1 §7.4 那一族的解法）决定"哪一列绝不能被顺手抹掉"。
3. **改动的边界在哪？** `POST` 是同步写入还是起任务；变更要不要进事件流。

三条都不是风格问题：第 1 条决定库里会不会多一张永远 0 行的表，
第 2 条决定"改了正文之后稿子的来源不见了"这类事故是否可表示，
第 3 条决定界面要不要说一句假话。

## 决定一：只落 `topics` 与 `drafts`，`video_topics` 留在 spec 里等它的写入方

`video_topics` 今天的写入方数量是 **0**：选题与作品的连线是 V2.2 分析层
（T5.2 爆款拆解 / T5.4）的产出，那一批任务还没落。现在把它建出来，得到的是：

| 选项 | 后果 |
|---|---|
| **A. 只建两张有人写的表**（选这个） | §2.8 那一节的 DDL 保持原样不动（它是契约，不是实现清单），实施状态写在该节下面；关联表等写入方那格一起落 |
| B. 三张表一次建齐，"契约完整" | 库里多一张永远 0 行的表 + 一个 Repository 方法族 + 一份 OpenAPI。§6 末尾对 V1 那三列空字段的判据（"给它们预建空列等于给前端两个永远为空的字段，并把『这列由谁写』含糊掉"）**逐字适用于这里** |

选 A。**spec 那一节不删不改** —— 它是预留契约，V3 要照它建；漂的是实现进度，
而进度记在该节的「实施状态」里，不靠删 DDL 来表达。

同理 §2.10 `feishu_sync_state` 继续留在"没建"那一栏：飞书同步域没开工。

## 决定二：`topics` 的可更新集只有一个字段，`drafts` 有四个 —— 差在"哪一列是身份"

两张表都走 `update_fields()`（字段级更新，V1 §7.4 的结构性解法），但白名单不同：

- `TopicUpdatableFields = { description }`。**`name` 故意不在里面**：
  它是这张表唯一的身份判定（唯一键），改名字等于换一条选题，
  而"改名撞了已有的另一条"需要的是**合并**语义（被合并那条的描述去哪了），今天没有那个答案。
  放开 `name` 必须和合并语义同一格决定，不许从 `update_fields` 悄悄绕过去。
- `DraftUpdatableFields = { title, content, source_video_id, status }`。
  排除的只有 `id` 与 `created_at`：这篇稿子是哪一篇、什么时候起笔的，不能被改写。
  与 `is_hidden` / `is_tracking` 不同，`status` **没有**专门的 `publish()` 方法 ——
  那两个要走专门方法是因为它们要连带写别的列或发事件（V1 §7.24 / §7.25），
  `status` 在这里没有连带动作，多开一个入口只是多一处能写它的地方。

配套的两点：

1. **"空白文本"这条规则只有一处定义**（`models/draft.py:assert_non_blank_text`），
   `DraftInput`（POST）与 `api/v1/drafts.py:DraftUpdate`（PATCH）都调它。
   为什么必须这样：部分更新手里只有一两栏，凑不出一份合法的 `DraftInput`，
   所以"复用模型校验"这条路走不通；各写一遍的失败方式是改了其中一半，
   症状是同一个输入有时 422 有时 200（然后列表上多出一条只有空白的草稿，
   而它的 `updated_at` 还显示"刚刚改过"）。看护是
   `tests/integration/test_topics_drafts.py::test_patch_rejects_blank_text_from_the_same_single_rule`。
2. **`TopicDraft` 拒收空名并 strip**。`name` 是唯一键而空串在 UNIQUE 里合法 ——
   这与 `video_comments.platform_comment_id` 那一坑同形（ADR-0020 决定一第 1 条），
   区别是那里库里也没有 CHECK，所以模型层是**唯一**一道闸。

## 决定三：`POST` 是同步写入回 201；变更不进事件流

- 新建选题/草稿**不起任务**：不需要跟 302、不需要拉资料、不需要网络，
  是一次本地 `INSERT`。起任务的唯一后果是界面要说一句"已排队"而它其实已经写完了 ——
  那是 §1.3 的"看起来在跑"换了个方向：把已经做完的事演成没做完。
  对照：收录博主回 202 + `task_id` 是对的，因为它真的要跑一段网络流程。
- **不加 `EventType` 取值**。选题/草稿的变更今天没有订阅者（Feed 与任务页读的是
  `task_events`），而 `EventType` 的取值集合是 `docs/specs/event-schema.md`（Locked）里的枚举，
  加一项要有消费它的人。写了没人读的日志，与 V1 §1.3 批过的"看起来在跑"是同一种东西。
  将来做"选题 → 草稿 → 发布"的看板时，那一格连带订阅方一起决定。

## 后果

- **`EXPECTED_CHECKS` 多了一条 `drafts_status_enum`**，同时进了
  `test_migrated_check_lists_exactly_the_model_constants` 的参数表（DB 的 CHECK 取值
  ↔ `DRAFT_STATUSES`）与新增的 `test_the_draft_statuses_are_one_tuple_not_two`
  （`DraftStatus` 这个 Literal ↔ `DRAFT_STATUSES`）。三条边都要有人守：
  只比前两条的话，漂开的是 Python 层那一侧，症状是"某个状态能过校验却写不进去"。
- 两张表进 `ALL_TABLES`，于是 `check_schema_matches_migrations()` 与迁移往返
  （判据 11）从 0004 起盯着它们；`ALL_TABLES` 的顺序按"平台 → 内容 → 选题/草稿 → 任务"排，
  **不是** spec 的章节顺序，那件事写在了它自己的 docstring 里，免得下一个人"顺手对齐编号"。
- Alembic 0004 **全程 autogenerate、没有手补约束**：`ck_drafts_status_enum` 随 `create_table`
  下发，autogenerate 看得见（0002 那次看不见是因为它给已存在的表加 CHECK）。这条链的差别
  在 0003 与本份迁移的 docstring 里各写了一遍 —— 重复是故意的，因为踩它的时机是"下一次加约束"。
- `data-model.md §3 / §4`（Repository Protocol 与 `Storage` 抽象的成员清单）
  **本条没有同步**：它们从 ADR-0020 起就已经不含 `video_comments` / `video_metric_snapshots`，
  再加两项不会让它变对，只会把两次漂移混进同一份 diff。它是一份待办的既有漂移，
  记在这里，归下一次动 §3/§4 的人一起收。
- 前端 `api.del` 是这一版新加的第一个 DELETE 调用方（`client.ts` 之前只有 GET/POST/PUT/PATCH）。
  204 无 body 由 `request()` 统一翻成 `undefined`，所以删除成功与否**不看响应内容**，
  只看重取之后的列表 —— 与 §决定三 那句"界面不许说假话"同一条纪律。
- `topics` 与 `drafts` 都**不进导出**（`/api/export` 仍只有 videos / creators）：
  与 ADR-0020 对评论的判据同源，外部可见的导出面要单独决定。
