# ADR-0020: 评论与指标快照落成两张"附属读数"表，抓取的调用面走 Protocol 的可选成员

- **状态**：Accepted（决定一与决定二**都已落地**：表 + 四个适配器的 Protocol 实现 + `enrich_metrics` 任务 + collect 的 publish 快照，2026-09-25。下面的"分期"保留为过程记录）
- **日期**：2026-09-24
- **决策人**：用户 + Qoder（**待确认项**：本条动了 Locked 契约面 `PlatformAdapter`）
- **相关**：ADR-0004（适配器 Protocol）、ADR-0006（数据模型）、ADR-0018（搬运区）、
  `docs/plans/v2.1-migration-plan.md` T4.1 / T4.2、`docs/specs/data-model.md §2.11 / §2.12`

## 背景

盘点 V1→V2 的缺口时有两组数据是 V1 有、ROADMAP 原未排期的：

- **B站 评论**（`download_bili_following_latest.py::fetch_comments`，`x/v2/reply`，
  落到 V1 飞书侧的 `video_comments` 表）。V2 的迁移脚本对它的引用数是 0 —— 今天搬不进来。
- **多检查点指标快照**（V1 `video_metric_snapshots`：发布时 / 24h / 72h / 7d，
  外加 `--enrich-existing` 那条给存量视频补抓的路）。V2 只在 `videos` 上存了一组
  **插入那一刻**的计数，之后再没被更新过。

两者的共同点决定了方案的形状：**它们都不是独立实体**。评论只属于某一条作品，
快照也只是"同一条作品在另一个时刻的读数"。而它们要进主库，就一定会撞上两个既有契约面：
`data-model.md`（Locked）与 `PlatformAdapter`（Locked）。

## 决定一：两张表，都 `ON DELETE CASCADE`，都不进 Feed 的检索面

`video_comments` 与 `video_metric_snapshots`，键与含义见 `data-model.md §2.11 / §2.12`。
三条不是随手定的：

1. **评论的唯一键含 `platform_comment_id`，且它为空就当场拒收**。
   不这么做的话空串是一个**合法**的唯一键值：第一条"平台没给 id"的评论占住它，
   之后所有没 id 的评论全部撞上同一条，症状是"这一轮只抓到 1 条"而一条错误都没有。
   V1 的行为是整批重插（无唯一键），重复率随采集轮数涨 —— 这正是这一格要修的东西。
2. **快照的唯一键是 `(video_id, checkpoint)`，不含 `collected_at`**。
   含了就会让同一个窗口攒出一串读数，而"24h 那条算哪个数"没有答案，
   增长率曲线随重跑次数变长。重抓同一窗口 = 覆盖，被覆盖的那份的去向记在
   `metadata_json.overwrote` 里（不新加一列：它没有任何查询会用，只用来让人看见"这条被重写过"）。
3. **四项读数可空，且"空"不等于 0**。
   小红书公开主页的卡片上没有 `view_count`。写成 0 就得到一条"发布时 0 播放"的快照，
   下一轮算 `7d / publish` 时要除以它。NULL 的价值是**不参与计算**。
   配套的第二条：`put()` 拒绝四项全空的草稿 —— 一条空快照会冒充"这个窗口抓过了"，
   让 `video_ids_missing` 从此不再看这条作品：一次失败的抓取换来一个永久盲点。

`Alembic 0003`。与 0002 不同，这一份**没有需要手补的约束**：
`create_table` 会把 CHECK 随 metadata 一起下发，autogenerate 看得见；
0002 看不见是因为它要给**已存在的表**加 CHECK，而 SQLite 反射不出约束文本。
这条差别写在迁移的 docstring 里，免得下一个人照 0002 去"手补"一遍已经存在的约束。

## 决定二：抓取的调用面是 `PlatformAdapter` 的**可选成员**，不是新的发现机制

表有了，谁往里写是个契约问题。三个候选：

| 选项 | 代价 |
|---|---|
| **A. 加进 `PlatformAdapter`**（`fetch_comments` / `fetch_metrics`），像 `fetch_subtitles` 那样"方法人人有，能不能干活由 `capabilities` 决定" | 动 Locked 契约面；三家适配器（+ YouTube）都要实现，包括"这个平台没有这项能力"的显式回答 |
| B. handler 里 `getattr(adapter, "fetch_comments", None)` 结构式探测 | 不动契约，但"平台到底能不能干这件事"变成**运行期猜**：`capabilities` 那一层声明就白摆了，而 V1 §7 里"能力表是装的"这类教训不止一条 |
| C. 评论/快照各自一条**平台专属**的 handler（`bilibili_comments`），直接 import 平台模块 | 最省事，但 `tasks/collect.py` 的"两个平台共用一份 handler"这条纪律就此作废，V2.1 每加一家就要多一个 handler 文件 |

**选 A**，理由不是"干净"，是**已有先例且那条先例正是为这件事造的**：
`supports_subtitles` 与 `fetch_subtitles` 就是这个形状 —— 方法在 Protocol 上人人都有，
`supports_subtitles=False` 的平台**返回 None 而不是抛**（`platform-adapter.md §2.4` 与
契约基类 `test_unsupported_subtitles_returns_none_not_raise` 都钉着它）。
评论与快照是完全同一类问题：能力是平台侧的属性，不是调用方该在运行期猜的东西。
选 B 的话，`Capabilities` 就退化成"给 UI 看的装饰"，而它是 ADR-0011 认定的**唯一真源**。

新增字段 `Capabilities.supports_comments: bool = False`（默认 False 让既有声明不破 ——
`dataclass(frozen=True)` 加**有默认值**的字段仍然可比较，三家现有快照的 `==` 不受影响）。
`fetch_metrics` 不另开能力位：任何能枚举出作品计数的一方都能补抓一次读数，
做不到的平台如实抛并带原文，那是错误而不是"不支持"。

## 分期（这一条是给下一个会话的，不是给评审的）

本条 ADR 落地时**只落了决定一与决定二的表/模型/仓库那一半**：

- 已落：两张表 + Alembic 0003 + `models/engagement.py` + 两个 Repository +
  `platforms/bilibili/comments.py`（抓取与解析，离线可验）。
- **待落**（2026-09-25 全部落完，逐条对号）：
  - `PlatformAdapter.fetch_comments` / `fetch_metrics` + `Capabilities.supports_comments`
    （`platform-adapter.md §2.1 / §2.2` 已同步）
  - **四家**各自的实现：B站 真做（`view` 的 `stat` + `x/v2/reply`，`aid` 那一跳见
    `§4.2`）、小红书只做读数（详情页 `likes` 一项，`§4.3`）、抖音与 YouTube **如实抛**
    并在消息里点名该跑的那条采集任务（`§4.1` / `§4.4`）
  - `tasks/enrich_metrics.py`（挑活 = `video_ids_missing`，窗口 = `window_for`）
  - `collect` 里"新作品顺手记一条 `publish` 快照"（用的是 `meta` 上已有的计数，
    **不再问一次适配器**；四项全空就跳过不记失败）

  没在写这份 ADR 的那一轮一起做，是因为**当时有三个 Agent 正在改这三个适配器与契约测试基类**，
  在别人的文件上动 Locked 契约的签名会把两边的工作都变成冲突。
  实现期另外发现的一条，值得留在这里：**`fetch_metrics` 不该有第三种答案**。
  写契约时想的是"能做就返回，不能做就抛"，实现时才看清中间那一档
  （返回四项全 NULL）必须显式禁掉 —— 它会顺着 `enrich_metrics` 走到 `put()` 被拒收，
  而拒收之前这一轮已经被当成"抓过了"记进清单，于是 `video_ids_missing` 从此不再看这条作品：
  **一次失败换来一个永久盲点**。所以现在三处都在挡它（适配器抛、任务再挡一次、仓库拒收），
  判据是同一条，写在 `platform-adapter.md §2.1` 与那两个方法的 docstring 里。

## 后果

- `videos` 那一行**不再是指标的唯一去处**：它是"插入那一刻的读数"，
  历史在 `video_metric_snapshots` 里。任何"这条作品现在多少赞"的查询要么改读快照，
  要么明确它读的是快照时刻 —— 这一条会在 V2.2 的分析层撞上，先记在这里。
- 两张表都进 `ALL_TABLES`，所以 `check_schema_matches_migrations()` 与
  `EXPECTED_CHECKS` 那两条既有看护会盯着它们（约束漂移会红，这是要的效果）。
- 评论与快照**不进导出**（T6.4 那 `/api/export` 目前只有 videos / creators）：
  评论是外部用户的发言，导出一份 CSV 发给别人是另一件事，需要单独决定。
