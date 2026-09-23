# ADR-0012: 没人读的配置字段标 `ui:hidden`，不删、也不只记账

- **状态**：Accepted
- **日期**：2026-09-23
- **决策人**：用户 + Qoder
- **相关**：ADR-0011（那条判据的出处）、ADR-0007（配置层）、
  `docs/specs/config-schema.md §3 / §4 / §5`、`tests/unit/platforms/test_config_fields_have_readers.py`、
  Task 12（Settings 页）

## 背景

ADR-0011 立了一条判据：**「一个前端能渲染出来、后端没有实现路径的配置取值，就是在给用户撒谎」**，
并用它删掉了 `ytdlp_cookie_priority` 与 `persist_play_url`。那条判据是**一次性用掉的** ——
删完之后没有任何东西阻止下一个死字段长出来。

2026-09-23 的审查轮第一次系统性地量了一遍（把每个平台配置模型展开到叶子字段，
逐字段在 `src/` 里找读取路径）。结果是**同一个仓库里 14 个字段有开关、没实现**：

| 字段 | 为什么是死的 | 实际生效的规则在哪 |
|---|---|---|
| `douyin.use_cdp_bridge`<br>`bilibili.use_cdp_bridge` | 没有任何代码读它 | 装不装桥由 `capabilities.needs_browser` 决定（`core/task_registry.py` 的 `DepsFactory`）。抖音勾上"不走桥"不会让桥不被使用，B站 勾上"走桥"不会去连 |
| `douyin.media_strategy`<br>`bilibili.media_strategy` | 单一取值的 `Literal` —— 只有 `false` 能用的布尔不是配置 | `capabilities.media_strategy`（ADR-0011） |
| `douyin.list_strategy` | 同上，单一取值 | `capabilities.list_strategy` |
| `bilibili.list_strategy` | **`_enumerate` 不按它分支**：走哪条路实际看的是「`external_browser_manifest_path` 有没有给、给了但没命中就回落」 | 所以写 `yt_dlp_flat` 也照样会用外部清单 —— 这比"没人读"更糟，它是**读出来一个和界面不符的行为** |
| `douyin.advanced.retry_max`<br>`douyin.advanced.retry_backoff_seconds`<br>`bilibili.advanced.retry_max`<br>`bilibili.advanced.retry_backoff_seconds` | V2.0 没有"同一条视频重试 N 次"这个循环：失败即原样失败，逐条重试在任务级 | 无（V2.1 的采集健壮性项） |
| `douyin.advanced.request_timeout_seconds` | 抖音侧没读；`BilibiliAdapter` 读的是**它自己**那个同名字段 | 抖音的预算是 `DIRECT_BUDGET_SECONDS`(300) / `YTDLP_BUDGET_SECONDS`(600)，那是**整段下载**的预算不是单次请求超时，把 30 接上去会把下载掐死 |
| `douyin.advanced.max_video_duration_seconds` | collect 里没有时长过滤器 | 无 |
| `bilibili.advanced.require_login_for_high_quality` | 没人读；画质由阶梯走到哪一档决定 | `capabilities.cookie_variants`（V1 §7.15） |
| `bilibili.prefer_subtitles` | V2.0 的 postprocess 只有字幕轨一条路，"优先"没有可选项 | ASR 在 V2.1 |

三件事是量的过程中撞出来的，比名单本身更要紧：

1. **守卫自己差点是假的。** `_SRC` 的相对层级写错一级（`parents[2]` 指进了 `tests/`），
   语料变成空串，于是 14 个字段变成"全部字段都是死的"。一个只会狂报红的守卫
   和红一次就被 `# noqa` 掉的守卫，最后效果一样。
2. **散文不能当读取路径。** 第一版判据是"`.字段名` 出现在 src 的文本里"。
   `platforms/bilibili/listing.py` 的**模块 docstring** 里有一句
   「走哪条由 `capabilities.list_strategy` 与配置决定」，于是 `bilibili.list_strategy`
   被判成"有人读"而躲过了检测 —— 而上面那张表说它在代码里一行都没被读。
   **最容易骗过这个判据的，恰好就是最容易骗过人的那种注释。**
3. **标记必须打在重新声明处。** 子类重新标注一个字段（`use_cdp_bridge: bool = False`）
   会换一个全新的 `FieldInfo`，父类上的 `json_schema_extra` 与 `description` **双双丢掉**
   （实测 `C.model_fields['x'].json_schema_extra is None`）。所以"在 `PlatformConfig`
   基类标一次，四个平台都生效"这个写法是不成立的，而且它失败得很安静 ——
   schema 里就是没有那个键，测试不查 schema 就没人知道。

另外顺手量到一条同形状的欠账：`config-schema.md §4` 写着前端按 `ui:advanced` 折叠高级字段，
而代码里**从来没有任何一处标过这个标记**（全仓库唯一用到 `json_schema_extra` 的地方是
`DouyinConfig` 的 `ui:order`）。文档承诺了一个不存在的契约键，第 N 次。

## 选项

**A. 删字段（ADR-0011 对 `persist_play_url` 做过的那件事）。**
不成立，理由是可执行的而不是修辞性的：这些模型都是 `extra="forbid"`，而
`config/platforms.yaml`（仓库自己那份，14 个键全在里面）**每次启动都要过校验**，
`write_platform_config` 又是按 `model_dump(mode="json")` 整段回写的。
现场验过：给 `DouyinAdvanced` 多喂一个键 → `extra_forbidden` / `Extra inputs are not permitted`；
删掉一个字段就是让上面那份 yaml 走同一条路径，症状从"表单上一个空开关"
变成"升级完服务起不来"。代价更大，不是更小。

**B. 只在看护测试里记账**（第一版真的这么写过：一个 `KNOWN_DEAD_FIELDS` 名单）。
名单躺在一个 Python 测试文件里，而撒谎的位置是**已经发布的 API** ——
`/api/platforms/{name}/schema` 今天就在吐这些字段，Task 12 的实现者是照 schema 渲染表单的，
他不会去读测试文件。等于把真相放在只有守卫看得见的地方。

**C. 全部标 `ui:hidden` + 在 `description` 里写清"实际生效规则"，看护改成双向棘轮。** ← 选定

## 决定

1. **14 个字段逐个标 `json_schema_extra={"ui:hidden": True}`**，标记打在**子类重新声明的那一处**
   （见「背景」第 3 条）。字段保留：老 yaml 能加载、`model_dump` 往返不变、
   将来实现它不用重新加一遍 schema。
2. **每个隐藏字段必须带 `Field(description=...)`，且 description 要说清三件事**：
   它现在不产生效果、真源在哪一处、以及什么条件下它有生效路径（"V2.0 未实现" / "V2.1"）。
   `description` 不是可选的润色 —— Pydantic **不把紧跟赋值的 docstring 写进 schema**，
   所以 docstring 对契约不可见，只有 `Field(description=...)` 会跟着 schema 出去。
   （V1 §7.15 那组"登录档 1772p vs 匿名 886p"的数字因此从 docstring 搬进了 description：
   搬进契约里比留在源码注释里更不可能丢。）
3. **`ui:advanced` 补进契约**：两个平台的 `advanced` 都标上，§4 那句话从"文档里的承诺"
   变成 schema 里真存在的键。看护单独一条断言盯住它（因为它正是"标记会被子类丢掉"那一类）。
4. **看护 = `tests/unit/platforms/test_config_fields_have_readers.py` 四条**，构成双向棘轮：
   - 可见字段必须有读取路径，**判据走 AST**（解析每个文件、把裸字符串语句换成 `None`、
     再 `ast.unparse`）—— 这样 docstring 与注释都不能冒充读取路径（「背景」第 2 条）。
     语料**按平台切**：`retry_max` 在 B站 被读一次不等于抖音那个也有人读。
   - 标了 `ui:hidden` 的字段**一旦有了读取路径就必须摘掉标记** —— 否则隐藏变成一个只进不出的抽屉，
     名单比现实悲观，下一个人会去修一个已经不存在的坑。
   - 隐藏字段必须有说清原因的 description。
   - `advanced` 必须有 `ui:advanced`。
5. **解封清单（Task 12 开工时按这个顺序还债）**：
   | 顺序 | 字段 | 实现它的正确形状 |
   |---|---|---|
   | 1 | `*.list_strategy` | 让 `_enumerate` **真的按字段选路**（外部清单没命中时回落哪条由字段说，而不是由"路径给了没"猜）。这是唯一一个当前**读出来行为与界面不符**的簇，优先级最高 |
   | 2 | `*.use_cdp_bridge` | 二选一：让它成为 `needs_browser` 之外的**否决位**（并改 `DepsFactory`），或者承认它永远是capabilities 的镜像并把**字段从模型里删掉**（那时删除才成立，因为同时删 yaml 键 + 走一次配置迁移） |
   | 3 | `bilibili.advanced.require_login_for_high_quality` | `False` = 允许匿名档；`True` = 阶梯走完仍拿不到登录档就**如实失败**，不静默交一批糊的（V1 §7.15 的后半句） |
   | 4 | `douyin.advanced.max_video_duration_seconds` | collect 循环里一个纯过滤（`VideoMeta.duration_seconds` 上界），不碰网络，可离线测 |
   | 5 | `*.advanced.retry_max` / `retry_backoff_seconds` | 需要先决定"重试的是整条阶梯还是单个请求"，且只能真机验 —— 归 V2.1 |
   | 6 | `bilibili.prefer_subtitles` | ASR（V2.1）落地时第一个解封：`False` = 跳过字幕轨强制走 ASR |
   | 7 | `*.media_strategy` | **大概永远不会解封**：单一取值的 Literal 是文档不是配置，V2.1 也不会有第二个抖音媒体策略。真要留就该从配置里挪进 `Capabilities` 单独表达 |
6. **`config-schema.md §3.1 / §3.2` 的代码块同步**：那两个块里还留着
   `cookie_variant_order` 与 `list_strategy: Literal['api']` —— ADR-0011 的"已删除"
   写在块**上方的修订注记**里，块本身没动。契约文档里"注记说删了、示例还在"就是
   第二处真相，一并改。

## 后果

- `/api/platforms/{name}/schema` 的 JSON 多出 `ui:hidden` / `ui:advanced` 键与 14 段 description；
  Task 12 的表单渲染器必须实现"遇到 `ui:hidden: true` 不渲染"，这是它**第一次**成为契约而不是设想。
- **隐藏 ≠ 无效被消除**：yaml 里写这些键仍然合法、仍然被存、仍然什么都不做 —— description 说了。
  这是有意的兼容代价，比"升级后起不来"便宜。
- 全部字段整组被隐藏时（`douyin.advanced` 四个全隐藏）会留下一个空折叠组：
  前端约定"组内叶子全隐藏就不渲染这个组"，写进 `docs/specs/config-schema.md §4`。
- 这个守卫的粒度是"字段名在代码里出现过一次属性访问"，**不校验语义**：
  把 `config.retry_max` 读进一个局部变量然后丢掉，它照样绿。它防的是"新增一个没人读的字段"
  这个具体形状，不防"读了但没用"。后者仍然只能靠用例（解封清单里每一条都要求配一条改变行为的用例）。
- 判据要按平台切语料，因此**新平台接入时必须重跑一次**这个测试：`xiaohongshu` / `youtube`
  落地后它们的字段会立刻进同一张网（V1 §7.24 那种"开关没人读"的坑正是新平台最容易长的）。
- 记 `docs/lessons.md` 经验 32（散文冒充读取路径）与经验 33（子类重新标注丢掉 schema 标记）。
