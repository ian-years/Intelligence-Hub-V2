# ADR-0010: ROADMAP 分阶段与 V1 → V2 迁移

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q10、ADR-0001（数据策略）、`ROADMAP.md`

## 背景

V2 是个大工程（10 节决策、7 份 spec、四平台移植、前端七页、迁移脚本），不可能一次性交付。要拆成可独立交付的里程碑，每个里程碑都能跑、能演、能停。

V1 已有真金白银的资产：博主库、历史媒体、口播稿、466+ 测试、四平台真机实测可达性（V1 §6 表）。V2 要保住这些资产，但不能让 V1 的历史数据绑架 V2 的 schema 演化。

## 决定

### 里程碑划分

**V2.0「骨架可用」** — 跑通端到端最小闭环：
- 后端：FastAPI + SQLite + EventBus + SSE + 配置层
- 平台：**抖音 + B站** Adapter 移植完毕（V1 §6 实测全通的两个，最有把握）
- 调度：6 个核心任务（preflight / douyin_collect / bilibili_collect / single_link / add_creator / postprocess）
- 前端：Dashboard + Feed + Settings 三页 + 孟菲斯设计令牌
- 测试：L0-L4 全绿 + coverage 达标 + V1 §7 契约测试映射
- DevEx：Makefile + pre-commit + CI
- 文档：10 ADR + 7 spec + AGENTS + README + CONTRIBUTING + architecture + lessons
- 迁移脚本：dry-run + 真机跑通一次

**为什么先做抖音 + B站**：V1 §6 表里这两个平台真机已实测全通；抖音的 `a_bogus` 兜底（§7.2）与 B站的 cookie 三档 + DASH 分片（§7.15/7.21）是 V1 最难的两条经验，先用 V2 接口表达清楚，剩下两个平台照葫芦画瓢。

**V2.1「四平台齐全 + 转写完整」**：
- 小红书 + YouTube Adapter
- ASR 引擎（sherpa-onnx SenseVoice）
- PostprocessTask 跨平台统一
- 字幕优先路径（B站 / YouTube）
- 前端 Video Detail / Creators / Tasks / Preflight 四页
- BackfillTask（爆款回溯）
- 真机烟雾测试

**V2.2「飞书 + 分析层 + 暗色」**：
- 飞书同步移植
- 爆款拆解引擎 + 脚本生成
- 话题 / 草稿表与前端页
- 报告生成
- 暗色模式
- E2E 测试覆盖关键流程

**V2.x 稳定后**：修 bug、性能调优、文档完善、等 V3 启动。

### V1 → V2 迁移脚本

**位置**：`tools/migrate_from_v1.py`

**行为契约**：

1. **只读 V1**：脚本以只读模式打开 V1 SQLite（`file:...?mode=ro` URI），不写 V1 任何文件
2. **幂等**：重复跑不会重复插入；用 `(platform, platform_id)` 与 `(platform, platform_video_id)` 做去重键
3. **可选媒体复制策略** `--media-strategy {copy|hardlink|symlink|reference}`：
   - `copy` — 复制文件到 V2 `data/media/`（最稳，吃磁盘）
   - `hardlink` — 硬链接（同盘 instant、省磁盘；Windows 需 `mklink /H` 或 `os.link`）
   - `symlink` — 软链接（跨盘可用，但 Windows 需管理员权限）
   - `reference` — 只在 V2 DB 里记 V1 的绝对路径（最快、零拷贝，但 V1 数据目录搬走就坏）
   - **默认 `hardlink`，失败自动降级 `copy`**
4. **进度可恢复**：写 `data/.migration_state.json` 记录上次跑到哪条，中断后 `--resume` 接着跑
5. **dry-run**：`--dry-run` 只打印将要做什么，不写盘
6. **回滚**：`--rollback` 删除 V2 库里所有 `migrated_from_v1=true` 的行（媒体文件不动，因为可能 hardlink 还指向 V1）

**迁移内容**：

| V1 来源 | V2 目标 | 字段映射 | 备注 |
|---|---|---|---|
| `local.sqlite3:creators` | `creators` | `platform_id`/`name`/`avatar_url`/`follower_count`/`profile_url`/`is_tracking`/`metadata_json` | V1 `creator_platform_id` 与 `mid` 统一映射到 V2 `platform_id` |
| `local.sqlite3:videos` | `videos` | 见 spec | 整行覆盖陷阱（§7.4）不会发生，因为 V2 是新建 |
| `local.sqlite3:transcripts` 或 `videos.transcript_*` | `transcripts` | `engine`/`language`/`char_count`/`text_path` | V1 转写路径按平台不对称（§7.5），迁移时统一 |
| `creators.json` | 合并到 `creators`（去重） | V1 双源（§7.7） | 以 SQLite 为准，JSON 仅补 SQLite 没有的字段 |
| `downloads/manifests/*.json` | `manifests` + `task_runs`（best-effort） | V1 manifest 字段不全（§2 已知遗留），尽力解析 | 解析失败的 manifest 跳过并记日志，不阻塞迁移 |
| `downloads/launcher-state/hidden-videos.json` | `videos.is_hidden=true` | V1 §7.25 墓碑内化 | 匹配键沿用 V1 那套（platform_video_id / record_id 尾段） |
| `downloads/{videos,douyin,bilibili,xiaohongshu,youtube}/**` | `data/media/<platform>/...` | 按 media_strategy 处理 | V1 路径结构基本兼容 V2，迁移成本主要在 DB 字段 |
| `downloads/cookies/*.txt` | `data/cookies/*.txt` | 直接复制（小文件） | cookie 是凭证，复制完检查权限 |
| `downloads/cdp-bridge-profile/` | **不迁移** | 让用户在 V2 重新登录一次 | profile 与 Chrome 版本绑定，跨版本可能坏 |
| `downloads/asr-models/` 或 `data/asr/` | **不迁移** | V2 配置里指向 V1 路径即可 | 233 MB 模型，复制浪费 |
| `feishu-base-config.json` | `config/feishu.yaml` | 字段映射 + 转 YAML | V2.2 才会用到 |
| `feishu-base.sqlite3` | **不迁移**（V2.2 重新同步） | V2 把飞书状态收进主库，旧镜像库废弃 |

**新增列**：V2 所有迁移过来的行带 `migrated_from_v1=true` + `v1_id=<原 V1 主键>`（在 `metadata_json` 里），方便审计与回滚。

**迁移完成判据**：
- 脚本退出码 0
- 打印汇总表：「creators 迁了 N 条 / videos 迁了 M 条 / transcripts 迁了 K 条 / manifests 解析失败 X 条 / 媒体文件 hardlink Y 条 + copy Z 条」
- V2 服务起得来、Feed 页能看到迁移过来的作品、随便点一条能播放
- 这条要**真机跑过一次**才算完，写进 `docs/progress/`

## 后果

**好处**：
- 每个里程碑都能独立交付、独立验证
- 先做最有把握的两个平台（抖音 + B站），降低 V2.0 风险
- 迁移脚本只读 V1，零风险
- 默认 hardlink 策略省磁盘（V1 媒体可能几十 GB）
- 幂等 + 可恢复 + dry-run + rollback，操作安全

**代价**：
- V2.0 阶段只有两个平台，小红书 / YouTube 用户要等 V2.1
- 迁移脚本是一次性投入（约 500 行），但 V3 启动时可复用同构脚本（V2 → V3）
- hardlink 跨盘不可用（自动降级 copy，但用户要知道）

**对 V3 的意义**：
- V3 启动时，V2 的里程碑划分模式可复用（V3.0 骨架 / V3.1 补齐 / V3.2 高级）
- V2 → V3 迁移脚本与 V1 → V2 同构，可复用大部分代码
- V2.0 / V2.1 / V2.2 三轮真机验证后，接口契约已稳定，V3 重写时风险低
