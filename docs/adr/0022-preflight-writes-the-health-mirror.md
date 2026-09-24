# ADR-0022: preflight 把探测结果写回 `platforms` 镜像，Dashboard 因此可以画健康灯

- **状态**：Accepted
- **日期**：2026-09-24
- **决策人**：用户 + Qoder（**待确认项**：这一条改变了 preflight 的性质）
- **相关**：ADR-0005（TaskRunner 与 EventBus）、ADR-0006（数据模型）、
  `docs/lessons.md` 经验 40（"Dashboard 为什么故意不画健康灯"）、
  `docs/specs/data-model.md §2.1`、`ROADMAP.md` 待办池第一条、
  `docs/plans/v2.1-migration-plan.md` T6.7

> 编号说明：`AGENTS.md §3` 把 0014 预留给了 `_check_requires`，`ROADMAP` 待办池那句
> "需 ADR-0014" 是写的时候没对齐这两处。本条按"编号不复用、新决定往后排"取 0022。

## 背景

`platforms` 表有三列 `health_status` / `health_checked_at` / `health_detail`。
`PlatformRepository.set_health()` 写好了，`AGENTS.md` 也写着"由 preflight / 采集任务写"，
但**生产里没有任何调用方** —— 只有测试在调它。结果：

- 三列恒为 NULL（`_sync_platform_mirror` 的 upsert 刻意不碰健康三列，避免把配置镜像
  与健康历史混成一次写）；
- Dashboard 因此**故意不画**那三盏灯（`docs/lessons.md` 经验 40）：画出来会永远灰着
  "从没检查过"，而一个永远灰的灯比没有灯更糟 —— 它会让人以为"系统在看这件事而它说没问题"；
- 但"从没检查过"同时也是**事实上的谎**：preflight 每一轮都真的探了三个平台，
  结论只活在一条 run 的 summary 里，要翻运行历史才看得到。总览页答不了"现在到底行不行"。

要修它就得先定一件事：**preflight 还是不是纯只读探测。**

## 选项

| 选项 | 代价 |
|---|---|
| **A. preflight 跑完把每个平台的结论写回镜像** | preflight 变成"会写库、状态跨重启留下"的探测。它的幂等性、"随便点一下不会有副作用"这个直觉都没了 |
| B. Dashboard 改读最近一条 preflight run 的 summary | 不写库，保住只读；代价是"平台健康"这件事的真相散在清单 JSON 里，每次渲染要解析一条清单，而清单的 schema 是审计格式不是查询格式 |
| C. 保持现状，把那句注释改成实话（"生产无写者"） | 最省，但等于承认这三列是死字段，而它们本来是为总览页留的 |

**B 看着最干净，其实是最贵的一种**：它把"当前状态"存成"最近一次审计记录"，
两者在时间上不是一回事。V1 后来就是这么长的 —— 看板去翻最新清单，
而那条清单可能是三天前的，界面却不区分。本仓库在 §7.20 上栽过一次
（"三连未登录"其实是桥早死了，绿灯来自一个没人更新的缓存）。

**A 的问题不在"会写库"**（`add_creator` 也写库，没人觉得它脏），
在于**留下一个会变旧的答案**。所以 A 只有连着"过期怎么办"一起定才成立。

## 决定

**选 A，并附三条**：

1. **写这一笔的是 handler，不是 runner、不是 EventBus 订阅者。**
   与 `add_creator` 落库、`collect` 落库同一层：handler 是"这次任务干了什么"的执行者，
   健康结论就是它产出的一部分。放 runner 会让所有任务都背上这个副作用；
   放订阅者会让"探测"与"记账"之间的因果跨过一个异步边界，红的时候说不清是谁没写。
2. **只有真的探过的平台才写。**
   `health_status='unknown'` 是"探了但判不出来"，它是合法结论、要写；
   **没探过的平台一个字节都不写**（`enabled_platforms()` 之外的平台保持 NULL）。
   把"没探"写成 `unknown` 是把两种不同的"没有"合并成一种，
   而这正是 `PlatformConfig` 那边"空串 vs None"用过的同一个教训。
3. **`checked_at` 必须与灯同时出现在界面上，灯不许单独出现。**
   过期不是"错"，是**必须让人看见的信息**。总览页那一格写的是
   「B站 降级 · 2 分钟前」，不是只有个黄色方块。
   `health_check_on_startup` 默认开着（`scheduler.enabled` 为真时），
   所以重启会自己刷一次；这是"灯不会永远灰"的机制保证，不是运气。

`is_healthy` 的口径不变：只有显式 `ok` 算健康（V1 §7.20）。这一条由
`PlatformRecord.is_healthy` 与 `HealthReport.is_healthy` 两处共用同一判据守着，
本条不动它。

## 后果

- `repositories/platforms.py` 里那句"由 preflight / 采集任务写"从纸面变成实话 ——
  本条**只让 preflight 写**。采集任务写不写？不写：采集时的失败原文进清单的
  `failures[]` 更合适，而且采集成功不代表 healthcheck 会绿（它探的是桥与 cookie），
  两处写同一列必然出现"采集把 unreachable 抹成 ok"。
- preflight 不再是纯只读：测试里跑一次它会留下三行 UPDATE。
  看护 `tests/integration/test_platform_health_mirror.py` 反而因此**第一次**能验
  "探测之后镜像列非 NULL"这件事 —— 在只读的版本里那条用例是写不出来的。
- 状态跨重启保留：重启后灯先亮上次的结论，直到启动预检跑完。这一小段"旧灯期"是
  本条接受的代价，`checked_at` 就是为它准备的（那一瞬间界面写的是"3 天前"）。
- Dashboard 从此**要**画那三盏灯。不画就是这三列的新版本"文档说有而实际没有"。
  看护把这件事钉成一个双向判据：镜像里有结论而界面读不到 → 红（见
  `frontend/src/pages/dashboard.spec.tsx` 里"没有 checked_at 就不画灯"那条）。
- 与 ADR-0012（没人读的配置字段标 `ui:hidden`）是同一个方法论的两面：
  **有写者没读者**、**有读者没写者**，都是债；本条补的是前者。
