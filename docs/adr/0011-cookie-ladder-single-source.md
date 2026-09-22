# ADR-0011: cookie 阶梯只留一处真相

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：ADR-0004（平台 Adapter Protocol）、ADR-0007（配置层）、
  `docs/specs/platform-adapter.md §2.2`、`docs/specs/config-schema.md §3.1 / §6`、V1 §7.3 / §7.15

## 背景

Task 5 把 `Capabilities` 定成契约之后，抖音的 cookie 阶梯在**同一个仓库里存在两份，而且互不兼容**：

| 出处 | 内容 |
|---|---|
| `platforms/douyin/adapter.py` 要声明的 `Capabilities.cookie_variants` | `("exported_file", "browser", "none")` |
| `DouyinConfig.ytdlp_cookie_priority`（+ `config/platforms.yaml` 同款） | `browser > exported_file(env) > exported_file(配置) > none` |

`PlatformAdapter.download_media` 的 docstring 写的是「实现**必须遵守 `capabilities.cookie_variants`
的阶梯顺序**」（ADR-0004 定的措辞）。而配置里那份是**另一个顺序**。
两者都是"活的"字段：前者被 Settings 页与调度器读，后者进 YAML、进 JSON Schema、被前端渲染成表单。
写实现时只能挑一个，另一个就变成了**撒谎的声明** —— 界面上给用户排好的优先级，代码不按它走。

B站那边同形：`BilibiliConfig.cookie_variant_order` 与 `Capabilities.cookie_variants`
是**同一份顺序写两遍**（写漏一处就漂）。

`DouyinConfig.persist_play_url` 是另一类：spec §4.1 写着「CDN 播放直链是签名的、几小时后失效，
**别改成 true**」，而 `MediaArtifact` / `VideoMeta` / `videos` 表里**没有任何一个字段能存放播放 URL**。
也就是这个开关唯一的合法实现方式是什么都不做 —— 一个只有 `false` 能用的布尔，
是"我们还没想清楚这块"的化石，不是配置。

## 选项

**A. 让 `ytdlp_cookie_priority` 当唯一的阶梯，`Capabilities` 只声明"有哪几种档位"不声明顺序。**
不成立：`Capabilities` 是 `ClassVar`，注册表与调度器在**不实例化**时就要能读到完整声明
（ADR-0004 的理由），一个"有档位无顺序"的能力声明让 Task 8 无法判断"要不要先导出 cookie 再开跑"。
而且 `MediaArtifact` 的 `yt_dlp_error` 与档位标签都要按顺序记账。

**B. 保留两份，实现里取 `min(配置顺序, 声明顺序)`。**
把矛盾留在水下。两条顺序在测试里永远一致（测试只会喂一条），真出分歧时症状是
"某个平台某天开始走匿名档、画质掉了"—— V1 §7.15 那条最贵的坑就是这个形状。

**C. 顺序只归 `Capabilities`；配置与 env 只回答"这一档用哪个文件 / 哪个浏览器"；
`persist_play_url` 删掉。** ← 选定

## 决定

1. **`Capabilities.cookie_variants` 是阶梯顺序的唯一真源。**
   `infra.ytdlp.plan_cookie_variants(order, cookies_file=..., browser=...)` 收的就是它，
   它已经负责"这一档没有对应资源就跳过"（文件不在 → 跳，浏览器名没给 → 跳）。
2. **`DouyinConfig.ytdlp_cookie_priority` 删除。** 它承载的两个信息各归其位：
   - *顺序* → 由 `DouyinAdapter.capabilities.cookie_variants = ("exported_file", "browser", "none")` 声明。
   - *V1 的 env 名* → 在解析档位时读：`DOUYIN_YTDLP_COOKIES_FILE` 是"导出文件档用哪个路径"，
     `DOUYIN_YTDLP_COOKIES_FROM_BROWSER` 是"浏览器档的浏览器名"。env **不影响顺序**。
     spec §6 本来就写着 `DOUYIN_YTDLP_COOKIES_FILE → platforms.douyin.cookies_file`，
     但 `_build_platform_config()` 只吃 YAML、没接 env source（Task 2 的已知缺口），
     所以今天唯一能落地这条兼容的位置就是适配器。见「后果」里的收口项。
   - **默认"browser 档不存在"**：不给浏览器名就没有这一档，与 V1 一致（V1 只在 env 显式设置时才加
     `--cookies-from-browser`）。V1 §7.3 的实测结论是 Windows 上这一档**永远**读不出来
     （Chrome 开着 → `Could not copy Chrome cookie database`，关着 → `Failed to decrypt with DPAPI`），
     把它排在导出文件之前等于每条视频白扔一次子进程时间，
     而且它报出来的错正是"会触发退档"的那一类，顺序错了会掩盖真实原因。
3. **`DouyinConfig.persist_play_url` 删除。** 播放直链只在一次下载的生命周期里存在。
   看护：`test_signed_play_url_never_reaches_the_artifact`（产物对象里任何字段都不含 `http`）。
4. **B站的 `cookie_variant_order` 在 Task 7 用同一条规则处理**（那两个字段的值当前是一致的，
   所以它只是"同一份顺序写两遍"的漂移风险，不是已发生的矛盾；等 Task 7 落地时一并删）。
5. **只有表头的 cookie 文件不算"导出文件档"**（`CookieManager.freshness().looks_empty`）。
   这是 §7.15 那条坑的另一半：`--cookies <空壳文件>` 传出去 yt-dlp **不报错**，
   只是按匿名处理 → 用户以为拿了登录档。宁可退一档并如实说，也不要"看起来带了 cookie"。

## 后果

- 少一处真相：加平台时只需要在 `Capabilities` 里排一次序。
- `config/platforms.yaml` 的抖音一节少两个 key，前端表单少两个折叠项
  （JSON Schema 由模型生成，自动跟着变）。
- **`ytdlp_cookie_priority` 与 `persist_play_url` 是 V2.0 未发布字段，删它们不破坏任何用户配置**；
  V2.0 发布之后再动就是破坏性变更，要走 `extra="forbid"` 的迁移。
- **未收口项（记账，别忘）**：平台配置层的 env source 一旦在 `ConfigManager` 接上
  （spec §6 的 `DOUYIN_YTDLP_COOKIES_FILE → platforms.douyin.cookies_file`），
  适配器里那句 `os.environ.get(...)` 就该删掉，改读 `config.cookies_file`。
  跟进位置：`docs/lessons.md`「V2 新增」+ Task 9 的装配点。
- B站 Task 7 会碰到同一件事，届时按本 ADR 处理，不再单独开 ADR。
