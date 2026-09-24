# ADR-0017: 定时采集放在配置里，形状是一个 cron 串

- **状态**：Accepted
- **日期**：2026-09-24
- **决策人**：用户 + Qoder
- **相关**：ADR-0007（配置层）、ADR-0012（没人读的配置字段标 `ui:hidden`）、
  `docs/specs/config-schema.md §3 scheduler`、`docs/plans/v2.1-migration-plan.md` T6.3、V1 `DailyScheduler`

## 背景

V2.0 的 APScheduler 里只有一个 job（每天清一次过期事件），`main._start_background_jobs`
的 docstring 明写着"cron 采集调度留到前端暴露那版"。T6.3 就是那一版。

V1 的做法是 `DailyScheduler`：一个 20 秒 tick 的循环 + 一份 `downloads/launcher-state/schedule.json`，
里面存"几点跑、跑哪些平台"。要搬的是**行为**，不是它的存储与循环方式。

要先定的是这份状态放哪儿、长什么样。三个候选位置各有代价：

| 位置 | 代价 |
|---|---|
| 新表 `schedules` | 一次迁移 + 第二份"这台机器怎么跑"的真相（与 `scheduler.*` 其余字段分家）；而且它只有几行 |
| `schedule.json` 那类侧文件（照 V1） | V2 里 `data/` 的写入口是有清单的，多一个自由格式文件就等于多一个没人校验的入口 |
| `config/app.yaml` 的 `scheduler` 段 | 要经 API 改就得走 `ConfigManager` 的原子写盘（已有）；Settings 页要渲染它（也已有） |

## 选项

**A. 照 V1：`daily_at: "08:00"` + `platforms: [...]`。**
简单，但"每天一次"只是需求的一种。V1 自己就撞上过"想一天跑两次 / 只想工作日跑"，
它的解法是往 `schedule.json` 里加字段。而 cron 一个字符串就覆盖完。

**B. `collect_cron` 一个五段串 + 可选的平台名单与 limit。** ← 选定

**C. 完全交给 APScheduler 的表达式（`{trigger: {hour: 8}, ...}` 这种 dict）。**
把第三方库的参数形状变成 V2 的配置契约 —— 换调度库时这份 YAML 就得跟着改，
而 YAML 是人手写的东西。

## 决定

`config/app.yaml` 的 `scheduler` 段加三个键，默认全部"不开"：

```yaml
scheduler:
  collect_cron: "0 8 * * *"        # 五段：分 时 日 月 周。默认 None = 不排任何采集
  collect_platforms: [douyin]      # 空/省略 = 所有已启用的平台
  collect_limit: 20                # 每位博主每次收几条；省略 = 用平台自己的 videos_per_creator
```

配套的决定：

1. **默认 `None`，不是"每天 08:00"**。装了 V2 的人不该在没同意的情况下，
   让服务定时拿他的登录态去动平台配额。
2. **写错红在启动，不红在第一次到点**。两层：
   `core/config.py` 校验形状（五段 + 字符集，`ValueError` → `ValidationError`，
   与这一段里每个既有字段的失败口径一致），`main._add_collect_jobs` 用
   `CronTrigger.from_crontab` 做语义校验（抛 → `ConfigError`，服务起不来）。
   两层都不省：只做形状校验会放过 `99 99 * * *`，症状是"每天到点什么都不发生"；
   只做语义校验就要把可选依赖 apscheduler 拖进 `core/config.py`，
   让"装没装这个包"改变"这份配置对不对"的答案。
3. **不查"这个平台的采集任务实现了没有"**。`collect_platforms` 已经被
   `PLATFORM_CONFIG_SCHEMAS` 校验过，而那张表里的两家都有已实现的采集任务 ——
   加两层今天跑不到的检查只会让人以为它们被验过了（本仓库踩过多次的那种假防护）。
4. **`max_instances=1` + `coalesce=True`**。一次采集可能几十分钟，跨过下一个触发点时
   不挡住就会有两个 job 同时扫同一批博主（白烧配额、互相撞 cookie 档位）。
   宁可并成一次。
5. **job 的回调必须是协程函数**。`AsyncIOExecutor` 把非协程的 job 丢进默认线程池，
   那里没有 running loop，`TaskScheduler.submit` 里的 `create_task` 当场炸 ——
   症状是"定时器响了、日志一条 error、任务永远不出现"。这条是集成用例
   `test_the_cron_job_actually_fires_and_submits_the_task` 抓出来的（它不 mock 时钟）。
6. **job id 是 `collect:<platform>`**，不是平台名：`/api/schedule` 要能区分采集 job 与
   `prune_events`，不能靠"猜哪些 id 是平台名"。

## 后果

- 定时采集的行为从此**完全由一份可进 git、可审计、原子写盘的 YAML 决定**，
  与 `scheduler.*` 其余字段同一个入口（`ConfigManager`）。
- **这一片没有 API 与 UI**：改 cron 现在要编辑文件 + 重启服务。第二片补
  `api/v1/schedule.py`（GET 列出 job 与下次触发时间 / POST 改配置 / `action: run_now`）
  与 Settings 那一块，届时"到点真起"的判据已经在这片就绪，UI 只是把同一个开关搬到界面上。
- 关着的平台即使写在 `collect_platforms` 里也不会被排（与"关掉平台 → 它的任务从
  `/api/tasks` 消失"同口径），但会留一条 `jobs.collect_platform_disabled` warning ——
  "配置里有、实际上没排"从配置本身看不出来。
- `collect_limit` 省略时落到平台配置的 `videos_per_creator`，**不在定时任务里另立一套默认值**
  （同一件事两个默认值就是漂移）。
- 事件清理那个 job 保持不变，与本决定无关。
