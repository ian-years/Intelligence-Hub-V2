# ADR-0025: 平台总闸是一道路闸（AND），不是"把四家的开关批量写四次"

- **状态**：Accepted
- **日期**：2026-09-25
- **决策人**：用户 + Qoder
- **相关**：`docs/specs/config-schema.md §2.1 §5`、`docs/specs/platform-adapter.md §1`（注册表）、
  `docs/specs/task-runner.md §2.2`（跑前那道门）、ADR-0010（四平台分批）、ADR-0017（cron 段）、
  `AGENTS.md §2`（`PlatformConfig` 是锁定的契约面）

## 背景

用户的要求：「小红书，YouTube，抖音，哔哩哔哩这四个做一个全局开关，控制开还是关闭」。

这句话有两种读法，而它们的后果完全不同：

1. **四家在界面上各有一个开关** —— 仓库里已经有了：`PlatformConfig.enabled` 就是它，
   Settings 页按 `/api/platforms/{name}/schema` 自动渲染，`/api/tasks` 的过滤与跑前那道门
   都已经读它。如果是这个意思，本次不需要写任何代码。
2. **一个总闸同时管四家** —— 这才是缺的那一位。

确定为 2 之后，还剩两个必须回答的问题。

**"总闸关下去"这个动作，落到盘上是什么？**
最省事的实现是把四家的 `enabled` 批量写成 `false`。它有一个致命的副作用：
打开总闸时只能"四家全开"，于是**用户之前单独关掉的某一家会被悄悄复原**。
而且界面上再也分不出"你自己关的"与"被总闸盖住的" —— 这两种状态要的动作相反。

**关下去的那一刻，正在跑的任务怎么办？**
可选：打断（取消在跑的）、等它跑完再算关、只管新提交。

## 决定

### 一、新增 `app.yaml` 的 `platform_control.enabled`，语义是 AND

```
可用 = platform_control.enabled AND platforms.yaml 里这一家的 enabled
```

总闸**不改写任何一家自己的值**，两份配置各住各的文件、各由各自的端点写。
默认 `True`：新增一道闸门把现网所有平台静默关掉，比没有这道闸门更糟。
环境变量 `INTELLIGENCE_HUB_PLATFORM_CONTROL__ENABLED` 照既有优先级（`yaml < env`）压得住它，
而设置页必须把"被 env 压着"显式说出来（与 `scheduler` 段同一条纪律）。

### 二、四态而不是布尔：`PlatformAvailability`

`available / own_off / master_off / absent`，唯一定义与唯一算法在
`platforms/base.py::resolve_platform_availability`。四个消费者全部调它，
**不许在本地重写那道 AND**：

| 消费者 | 位置 | 关掉之后 |
|---|---|---|
| 任务列表过滤 | `core/task_registry.py::task_is_available` | 四家 `*_collect` 消失；`platforms=()` 的五个留下 |
| 跑前那道门 | `core/task_runner.py::TaskScheduler._gate_platforms` | 点名 POST 一样 422 |
| 平台注册表 | `platforms/registry.py::availability / enabled_platforms` | 采集名单空、`get()` 抛错且文案点名总闸 |
| cron 排程 | `ConfigManager.enabled_platforms()`（唯一一份可用名单） | 一条 job 都不排 |

为什么四态不能压成一个 bool：`master_off` 时 `platforms.yaml` 里写的还是 `enabled: true`。
原来注册表那句"已被关掉（config/platforms.yaml 的 enabled: false）"在这一刻是**谎话** ——
它会把人支去改一个本来就开着的字段，得到"我打开了，还是这一句"。
`absent` 排在最前：装配漏了一环不该被说成"谁关了开关"。

### 三、只管新提交，不动在跑的

闸门全部在**提交侧**（`submit` 之前）。已经在跑的那一轮跑完、清单落终态、事件照常发。
打断需要每条采集链上都有取消点，而 `add_creator` / `preflight` 声明的是
`cancellable=False` —— 一个"关掉但某些任务其实停不下来"的开关比"只管新提交"更会说谎。

### 四、`platforms=()` 的跨平台任务不受总闸影响（在列表与门这一层）

`preflight / single_link / add_creator / postprocess / enrich_metrics` 不属于任何一家，
关掉总闸它们仍然可提交。**但**它们中间真正去碰平台的那一步照样会被注册表挡住：

- `single_link` / `add_creator` 认链接走 `detect_platform` → `registry.enabled_platforms()` → 拒。
- `postprocess` 取字幕走 `registry.get(platform)` → 拒（本地 ASR 那一步不需要适配器，能跑）。

这不是遗漏，是"这一家不可用"的既有范围：今天把 douyin 的 `enabled` 写成 false，
同样这几条路会拒。总闸的定义是"四家都这样"，所以后果也这样。
**没有把总闸做成"整个系统不能用"**：任务列表、库、报告、工坊照常。

### 五、生效不需要重启，也不需要重建注册表

`PlatformRegistry` 收的是 `master_enabled: Callable[[], bool]`（**闭包，没有默认值**）。
`write_platform_control` 与 `write_scheduler` 一样**原地换共享 `AppConfig` 上的那个字段**，
所以三处持有者（AppState / TaskRunner / TaskScheduler）与那个闭包看见的是同一个新值。
传 bool 快照也能让今天所有用例绿，症状是"设置页显示总闸已开，任务列表还是空的"，要等重启。
这个参数**故意不给默认值**：默认等于"忘记传的新调用点静默忽略总闸"。

界面侧 `GET/PUT /api/platform-control` + `GET /api/platforms` 根上的 `master_enabled`
+ 每行的 `availability`；`GET /api/schedule` 也补了 `master_enabled`，
因为"`effective_platforms` 空、`skipped_platforms` 也空"是这一族里最难自己看出来的形状。

## 后果

**好**

- 一次翻转停掉四家的新采集，而每个人的"我本来就把 B站 关了"完好无损。
- 三处消费点共用一个谓词，接口、异常文案与界面说的是同一件事。
- 契约面（`PlatformConfig` 的字段名、`/api/platforms/{name}/schema` 的表单）一字未动：
  总闸住在 `app.yaml` 的**新段**里，走的是新端点。V3 换实现时该换的还是那一份 Protocol。

**代价 / 要注意**

- `master_enabled` 是必填关键字参数：`PlatformRegistry` 的每一个构造点都要表态（测试也要）。
  这是刻意的，但它让"随手 new 一个注册表"变贵了一点。
- 四态进了 `/api/platforms` 的响应（每行加 `availability`）。前端渲染状态的代码
  收敛到 `lib/platform-state.ts` 一处，两页共用 —— 加了第五态会让那里与
  `tests/unit/platforms/test_availability.py` 各红一次。
- `postprocess` 在总闸关掉时不能给"这一家"的作品取字幕（要适配器的路）。已记在
  `docs/progress/2026-09-25.md §7` 的待确认表里：如果希望"本地已有的东西照样处理"，
  那是一次**范围**改动（要把"可用"分成"可采"与"可解引用"两件事），不在本 ADR 内。
- `config/app.yaml` 里这一位是**注释掉**的（默认开）。写成 `enabled: false` 会变成
  "新克隆下来四家全不可用"，那条由用例看着。
