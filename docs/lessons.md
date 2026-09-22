# Lessons

> V1 的 25 条陷阱（`AGENTS.md §7`）在 V2 的命运，以及 V2 实施过程中新踩的坑。
> 每条都带**判据**（怎么验证它还在/没了）与**看护**（哪条测试或哪个结构性约束在守着）。

---

## 第一部分 · V1 §7 陷阱在 V2 的命运

V1 的 `AGENTS.md §7` 记录了 25 条踩出来的陷阱。V2 不是"重写一遍然后祈祷别再踩"，而是逐条判定：
**结构性消除**（设计上不可能再犯）/ **契约测试看护**（仍可能发生，但有测试守着）/ **V2 新增**（V1 没有的问题）。

完整映射表在 `docs/specs/contract-tests.md`。这里只记**结论**与**判据**。

### 结构性消除（9 条）

| V1 §7 | 陷阱 | V2 的解法 | 判据 |
|-------|------|----------|------|
| §7.1 | 抖音身份是 `sec_uid`，短链不含身份 | `DouyinAdapter.parse_creator_url()` 强制跟 302，返回 `CreatorRef(platform_id=sec_uid)`；`CreatorRef` 是 Pydantic model，`platform_id` 必填 | 单元测试：短链输入 → `CreatorRef.platform_id` 是 sec_uid 而非 URL |
| §7.4 | `upsert_video()` 整行覆盖，空标题折叠成默认值 | `VideoRepository.update_fields(id, VideoUpdatableFields(...))` 字段级更新，`VideoUpdatableFields` 是 TypedDict，只允许显式列出的字段 | mypy strict 拒绝 `update_fields(id, {"title": None})`；契约测试：部分字段更新后其他字段不变 |
| §7.5 | 转写落盘目录按平台不对称 | `FileStorage.transcript_path(video_id)` 统一路径（`data/transcripts/<video_id>/speech-clean.txt`），与平台无关 | 契约测试：四个平台的转写都落在同一目录结构 |
| §7.6 | `LocalCreatorStore` 第一个位置参数是 root 不是 path | V2 没有 `LocalCreatorStore`；`CreatorRepository` 通过 DI 注入 `db_session`，无路径参数 | 代码审查：`CreatorRepository.__init__` 签名只有 `session: AsyncSession` |
| §7.7 | `creators.json` 顶层是 list，`local_store` 里同名表是 dict | V2 只有一个数据源（SQLite），无 JSON 文件 | `data/` 目录里没有 `creators.json`；迁移脚本读 V1 的 JSON 后只写 SQLite |
| §7.10 | `/api/state` 之类"顺手猜的路由"不存在 | OpenAPI schema 自动生成，前端用 `openapi-typescript` 生成类型；不存在的路由在编译期就报错 | `make gen-api` 后 `frontend/src/api/generated/` 的类型与后端路由一一对应；前端调用不存在的路由 → TS 编译错误 |
| §7.11 | `videos` 表列名 `creator_id`，外部数据叫 `creator_platform_id` / `mid` | Pydantic model `VideoMeta` 统一字段名 `creator_platform_id`；`VideoRepository.create()` 内部映射到 `creator_id` 列；外部代码只见 `VideoMeta` | mypy strict：`VideoMeta` 的字段名固定；契约测试：四个平台的 `list_creator_videos()` 返回的 `VideoMeta` 字段名一致 |
| §7.12 | 预检 `sqlite` 检查的是飞书镜像库，不是主库 | V2 只有一个 SQLite（`data/hub.sqlite3`）；`/api/v1/preflight` 检查的是这个库 | 预检代码审查：`preflight.py` 里 `db_path` 是 `config.storage.db_path`，无分支 |
| §7.25 | 情报流"删除"= 删行 + 墓碑，少一半都不算数 | V2 没有墓碑文件；`videos.is_hidden` 列，`VideoRepository.hide(id)` 只更新这一列；`list_videos()` 默认过滤 `is_hidden=False` | 契约测试：`hide()` 后 `list_videos()` 不返回，但 `get_video(id)` 仍能取到；磁盘扫描不会复活（V2 无磁盘扫描） |

### 契约测试看护（16 条）

这些陷阱在 V2 仍可能发生（外部依赖、网络、子进程），但每条都有对应的契约测试守着。
测试名 + 层级 + 文件见 `docs/specs/contract-tests.md`。

| V1 §7 | 陷阱 | V2 的看护 |
|-------|------|----------|
| §7.2 | 抖音对非浏览器客户端风控，yt-dlp 从来没产出过媒体 | `DouyinAdapter.download_media()` 的契约测试：mock yt-dlp 返回失败 → 验证走页面播放直链兜底 → 验证 `MediaArtifact` 的 `source` 字段是 `page_play_url` |
| §7.3 | Windows 上 yt-dlp 读不了 Chrome cookie 库 | `ytdlp_cookie_variants()` 的单元测试：三档顺序（导出文件 > browser > 匿名）；`looks_like_cookie_failure()` 认全 V1 记录的所有原文 |
| §7.8 | `safe_filename()` 不止换 `/`，还要处理 `..` / 结尾点空格 / Windows 设备名 | `FileStorage.safe_filename()` 的 hypothesis 测试：随机 Unicode 输入 → 输出不含 `/ \ ..`、不以点/空格结尾、不是 Windows 设备名 |
| §7.9 | SenseVoice 不产标点，`asr_sherpa.py` 按静音切句补 `。` | `asr.py` 的契约测试：mock sherpa-onnx 返回无标点文本 → 验证 `Transcript.segments` 每段以 `。` 结尾 |
| §7.13 | 技能脚本物理上有两份（仓库 + `~/.agents/skills/`） | V2 没有技能脚本；CDP 桥是 V1 遗产，V2 通过 `infra/cdp_bridge.py` 的 httpx client 调用，无脚本复制问题 |
| §7.14 | `tests/test_bilibili_browser_listing.py` 找不到生产者脚本时是 SkipTest 不是 fail | V2 的 B站 列表枚举走 CDP 桥（`BilibiliAdapter.list_creator_videos()`），无外部脚本依赖；契约测试 mock 桥的 `/evaluate` 响应 |
| §7.15 | B站 cookie 分两条路（枚举 + 媒体），两条都得带导出文件 | `BilibiliAdapter` 的契约测试：枚举时验证 yt-dlp argv 带 `--cookies`；下载时验证三档顺序；`cookie_variant_order` 配置项控制 |
| §7.16 | B站搜索兜底要 Node 版 playwright + `NODE_PATH` | V2 的 B站 列表枚举**不走搜索兜底**（走 CDP 桥的 space 页面滚动）；如果未来需要，`preflight` 检查 `node` + `NODE_PATH` + `playwright` 模块 |
| §7.17 | 别把常驻服务接在 `| head` 后面（BrokenPipeError） | V2 的后端用 uvicorn，日志走 structlog 到文件；开发期 `make dev` 用 honcho，不会管道断裂 |
| §7.18 | 桥 profile 的登录态是跨会话持久的 | `data/cdp-bridge-profile/` 是 V1 遗产，V2 复用同一份；`preflight` 检查桥的 `/health` + cookie 有效性 |
| §7.19 | "注册表里有 PATH" ≠ "进程拿得到"（ffmpeg） | `preflight` 用 `shutil.which()` 检查**当前进程**的 PATH，不读注册表；如果红，提示用户重启**启动方**（终端 / Qoder 宿主） |
| §7.20 | 桥的浏览器被人关掉后 formerly 会一直报绿 | V2 复用 V1 的桥（已修）；`infra/cdp_bridge.py` 的 `probe()` 用真往返（`/cookies`）判活；`/health` 503 时 `bridge_available()` 返回 True（自愈发生在第一条真请求） |
| §7.21 | B站媒体可能是未合并的 DASH 分片 | `BilibiliAdapter.download_media()` 返回 `MediaArtifact`（`SingleFile` | `VideoAudioPair`）；`postprocess` 任务用 `ffprobe` 检查音频流，无音频 → 报错而非静默丢 |
| §7.22 | 「抓取爆款 Top 5」必须按位扫描，跟踪开关只管整库/定时 | V2 的任务参数显式传 `creator_ids: list[str]`，不依赖"跟踪开关"；`fetch_creator_videos` 任务的 `creator_ids` 为空时报错而非退化全库扫描 |
| §7.23 | pytest 用时抖动有三倍 | V2 的 CI 门禁用**覆盖率**（80% / 90% / 70%），不用用时；`Makefile` 的 `test` target 不计时 |
| §7.24 | 「持续跟踪」的值必须是真布尔，且默认值只能有一处 | V2 的 `creators.is_tracking` 列是 `INTEGER CHECK (is_tracking IN (0, 1))`；`CreatorRepository.set_tracking(id, tracking: bool)` 入口 `isinstance(tracking, bool)` 断言；前端默认值由 `settings.ts` 的 Zustand store 决定一次 |

### V2 新增（实施过程中踩的坑）

> 本节在 V2.0 实施过程中逐步填充。每条格式：
> **现象** → **根因** → **解法** → **判据** → **看护**

#### 坑 1 · ruff 的默认规则集把中文文档全判成"歧义 Unicode"（Task 1）

**现象**：`ruff check src/ tests/` 报 130 条错误，其中 110 条是 `RUF002 ambiguous-unicode-character-docstring`，
指认的全是中文全角标点（`，` `。` `：` `（`）。

**根因**：RUF001/002/003 的设计目标是防"用西里尔字母 а 冒充拉丁 a"这类同形字攻击。
中文全角标点和 ASCII 标点确实同形，但本仓库的 docstring / 注释 / 用户可见文案**一律中文**，
这条规则开着等于禁止写中文文档。

**解法**：在 `pyproject.toml` 里显式 ignore，**每条都写中文原因**，不做"一把梭关掉全部 lint"。
同一批还豁免了：
- `TC001` / `TC003`（不把导入挪进 `TYPE_CHECKING`）—— Pydantic v2 建类时求值注解，
  挪进去要么处处补 `model_rebuild()`，要么运行期 `NameError`。
- `UP046` / `UP047`（不用 PEP 695 的 `class X[T]`）—— `Generic[T]` 在 Pydantic 泛型模型上更久经考验。
- `N818`（异常名不强制 `Error` 后缀）—— `TaskCancelled` / `TaskRejected` 是
  `docs/specs/task-runner.md §2.5` 锁定的契约名，改动要走 ADR。
- `PLR0913` / `TRY003` / `S101` —— 参数多的构造函数、中文错误消息、测试里的 `assert`。

**判据**：`ruff check src/ tests/` 全绿；`pyproject.toml` 的 `ignore` 列表里**没有无注释的条目**。

**看护**：`tests/unit/core/test_config.py` 之外没有专门测试（这是配置约定）；
看护方式是代码审查 —— 往 `ignore` 里加东西必须带中文原因，否则打回。

#### 坑 2 · `ManifestBuilder.fail()` 静默丢掉错误原文（Task 2）

**现象**：`builder.fail(RuntimeError("读博主库失败"))` → `finalize()` 出来的 `Manifest`
里**没有任何地方**装着 "读博主库失败" 这句话。`failures[]` 也是空的。

**根因**：`fail()` 把消息存进了 `self._error`，但 `Manifest` 这个 Pydantic model
**根本没有 `error` 字段**，`finalize()` 也就没传。spec `task-runner.md §2.6` 的
`Manifest` 定义里同样没有 —— 这是**设计阶段的遗漏**，不是实施走样。
后果正好撞上 V1 §1.3 的红线（"不许吞错"）和 V1 §7.22 那次真实事故
（用户看到"任务失败 退出码 1"+ 一屏 traceback，清单里没有原因）。

**解法**：
1. `Manifest` 加 `error: str | None = None`（顶层任务级错误原文）。
2. `fail()` 里 `self._error = str(exc) or f"{type(exc).__name__}（无消息文本）"`
   —— 空消息的异常也要留下类名，`str(exc)` 返回 `""` 不等于"没有错误"。
3. `fail()` 顺手把带 `platform` / `stage` 属性的异常（`PlatformError`）
   自动追加一条 `FailureRecord`，这样"整个任务挂了"和"哪一步挂了"两个视角都在。
4. 配套放宽两处契约：`FailureRecord.platform` 改成 `str | None`
   （runner 级失败不归任何单一平台），`stage` 的 Literal 多一个 `"task"`。
5. 适配器可以抛任意 stage 字符串（V1 §7.22 那次抛的是 `creators`），
   `_coerce_stage()` 认不出来就归到 `"task"`，**原文仍然留在 `error` 里**，不丢信息。

**判据**：`pytest tests/unit/test_manifest_builder.py -q` → 20 passed。
其中 `test_fail_preserves_error_text_verbatim` 断言原文逐字相等，
`test_fail_with_empty_message_exception` 断言空消息也留下类名。

**看护**：`tests/unit/test_manifest_builder.py`（20 条），
外加 `test_schema_version_is_locked` 锁住 `schema_version == "2.0"`
（改 Manifest 结构必须显式升版本号，不许静默漂）。

#### 坑 3 · `logging.py` 用 `FileHandler`，配置里的轮转参数是摆设（Task 2）

**现象**：`LoggingSection` 有 `rotate_max_bytes=52428800` / `rotate_backup_count=5`，
但 `setup_logging()` 里建的是 `logging.FileHandler` —— 两个配置项**一次都没被读过**。

**根因**：Task 1 先写了 `LoggingSection`（照着 spec 抄字段），Task 2 补测试时
覆盖率报告指出 `logging.py` 只有 25%，写测试才发现字段和实现是断开的。
后果：常驻服务跑几周，`server.log` 无限长，几个 GB 起步，
而且 V1 §7.17 那条"别把常驻服务的输出接在管道后面"的经验在 V2 里换成文件后同样致命。

**解法**：换 `RotatingFileHandler(log_file, maxBytes=rotate_max_bytes,
backupCount=rotate_backup_count, encoding="utf-8")`，
`setup_logging()` 签名加两个 keyword-only 参数。
`encoding="utf-8"` 必须显式传 —— Windows 上默认编码是 GBK，中文日志会 `UnicodeEncodeError`。

**判据**：`test_file_handler_is_rotating` 断言 handler 类型 + `maxBytes` + `backupCount` 三个值都对得上。

**看护**：`tests/unit/test_logging_setup.py::test_file_handler_is_rotating`。
这条测试的价值不在"测轮转"，在**测配置项真的被消费了** —— 加配置字段时必须同时加一条这样的测试。

#### 坑 4 · JSON 日志把所有中文转义成 `\uXXXX`（Task 2）

**现象**：`{"event": "\u59dc\u80e1\u8bf4"}`。博主昵称、视频标题、平台返回的错误原文
全部变成转义序列。

**根因**：`structlog.processors.JSONRenderer` 默认 `serializer=json.dumps`，
而 `json.dumps` 默认 `ensure_ascii=True`。

**解法**：`functools.partial(json.dumps, ensure_ascii=False)` 传给 `JSONRenderer(serializer=...)`。
代价是 stdout 必须能编码中文，所以配套写了 `_ensure_utf8_stream()`：
V1 那条「Windows 上所有命令都要 `-X utf8`」的纪律（V1 AGENTS.md §4）
在 V2 里改成**进程自己负责** —— `stream.reconfigure(encoding="utf-8", errors="replace")`。
`errors="replace"` 是兜底：宁可日志里出现一个 `?`，也不要因为打日志把服务搞崩。
没有 `reconfigure` 的流（pytest 的 capsys、重定向到 StringIO）原样返回。

**判据**：`test_unicode_survives_json` 断言 `"\u" not in raw`（不只是断言解析回来相等 ——
解析回来永远相等，那条断言抓不到这个 bug）；
`test_ensure_utf8_stream_reconfigures_when_possible` 断言真的传了 `errors="replace"`。

**看护**：`tests/unit/test_logging_setup.py`（4 条编码相关用例，含两条 fallback 分支）。

#### 坑 5 · `get_logger` 的返回类型是谎话，`cast` 把它压过去了（Task 2）

**现象**：注解写 `-> structlog.stdlib.BoundLogger`，实现写
`cast("structlog.stdlib.BoundLogger", structlog.get_logger(...))`。
mypy 全绿，但运行期 `isinstance(logger, structlog.stdlib.BoundLogger)` 是 **False**。

**根因**：`structlog.get_logger()` 实际交出来的是 `BoundLoggerLazyProxy`
（延迟到第一次调用才绑定真实 logger）。它**满足** `structlog.BoundLogger`
这个 Protocol（有 `bind` / `info` / ...），但**不是** `stdlib.BoundLogger` 的实例。
`cast` 的作用是"让 mypy 闭嘴"，不是"让类型变对" —— 用它压一个真实的类型不匹配，
等于把 bug 从编译期挪到运行期。

**解法**：返回类型改成 Protocol `structlog.BoundLogger`，`cast` 保留
（Protocol 与 LazyProxy 之间 mypy 推不出来），但这次 cast 的方向是**对的**。
函数 docstring 里写明"为什么不是 stdlib.BoundLogger"，防止下一个人再改回去。

**判据**：`test_get_logger_satisfies_the_bound_logger_protocol` 同时断言两件事 ——
Protocol 要求的 8 个方法都 `callable`，**且** `not isinstance(logger, structlog.stdlib.BoundLogger)`。
后半句是这条测试的全部价值：它锁住的是"没有标错"，不是"能用"。

**看护**：`tests/unit/test_logging_setup.py::test_get_logger_satisfies_the_bound_logger_protocol`。

#### 坑 6 · 计划里的 `AppConfig(_yaml_file=path)` 根本不工作（Task 2）

**现象**：实施计划 Task 2 写的测试模式是 `AppConfig(_yaml_file=yaml_file)`，
照抄进测试后**静默失败** —— `_yaml_file` 始终是 `None`，没有报错。

**根因**：Pydantic v2 的 `BaseModel.__init__` 会**丢弃**私有属性（下划线开头）的传入值，
不报错、不警告。`private_attr` 只能靠 `model_post_init` 或默认工厂赋值。
所以"通过构造函数传 YAML 路径"这条路在 Pydantic Settings 上是死的。

**解法**：换成 `ContextVar` + 自定义 `PydanticBaseSettingsSource`：
```python
_YAML_DATA: ContextVar[dict[str, Any] | None] = ContextVar("_YAML_DATA", default=None)

def load_app_config(yaml_path: Path | None = None, **cli_overrides: Any) -> AppConfig:
    token = _YAML_DATA.set(read_yaml_mapping(path))
    try:
        return AppConfig(**cli_overrides)
    finally:
        _YAML_DATA.reset(token)
```
`settings_customise_sources` 返回 `(init_settings, env_settings, _YamlDictSource)`
—— **顺序即优先级，第一个最高**（这点实测确认过，文档里写得含糊）。
`dotenv` / `file_secret` 两个 source 故意不启用：V2 的配置来源只有
defaults < YAML < env < CLI 四层，多一层就多一种"为什么这个值是这样"的排查成本。

**判据**：`test_priority_ladder_*` 三条用例分别验 yaml>default、env>yaml、cli>env。

**看护**：`tests/unit/core/test_config.py`（40 条）。
另有一条**反向**用例 `test_bare_app_config_does_not_read_yaml` ——
裸 `AppConfig()` 不读盘是**故意的**（测试隔离，也避免 V1 §7.12 那类"行为取决于 cwd"的坑），
将来有人"顺手加个便利"就会被这条测试拦住。

**连带坑**：`ContextVar` 的默认值不能写 `default={}`（ruff B039：可变默认值会被所有
未 set 的上下文共享）。改成 `default=None` + 模块级 `_EMPTY` 哨兵 + `_yaml_data()` 访问器。

#### 坑 7 · 设计阶段产出的 `platforms.yaml` 和 schema 对不上（Task 2）

**现象**：`ConfigManager().load()` 直接抛
`ConfigError: platforms.yaml 里有本构建不支持的平台: defaults, platforms`。

**根因**：设计阶段写 YAML 时用了嵌套结构（顶层一个 `platforms:` 键，
外加一个 `defaults:` 块给所有平台兜底）；而 `docs/specs/config-schema.md`
和实施计划假设的是**扁平结构**（顶层键就是平台名）。两边都没错，但谁也没验过。
`_load_platforms()` 的"未注册平台硬失败"逻辑（本来是为了防拼错平台名）
恰好把这个漂移抓了出来 —— 如果它当初写成"忽略未知键"，这个 bug 会一直活到首次启动。

**解法**：
1. `platforms.yaml` 拍平：顶层键 = 平台名，删掉 `defaults:` 块。
2. `defaults:` 里的内容不是丢掉，而是**升格成 schema 的真实字段**挪进 `app.yaml`：
   `app.show_disabled_platform_history`、`scheduler.health_check_on_startup`、
   `scheduler.health_check_interval_seconds`、`scheduler.task_timeout_seconds`（9 个 TaskKind 各一条）。
   平台级的公共默认值由 `PlatformConfig` 的 Pydantic 字段默认值承担 ——
   **默认值只能有一处**（V1 §7.24 的同一条纪律）。
3. `xiaohongshu` / `youtube` 两节**整段注释掉**并写明 V2.1 启用，
   而不是留着 `enabled: false`：注册表里没有实现却在配置里出现，等于对读者撒谎。

**判据**：三条**漂移看护**测试（读的是仓库里真实发货的 `config/*.yaml`，不是 tmp_path 造的）：
- `test_shipped_config_files_load` —— 发货配置必须能被 `ConfigManager` 加载。
- `test_shipped_config_has_no_unregistered_platform_sections` —— 顶层键必须在 `PLATFORM_CONFIG_SCHEMAS` 里。
- `test_shipped_platforms_yaml_survives_write_roundtrip` —— `write_platform_config()` 写回去再读回来必须等价。

**看护**：`tests/unit/core/test_config.py` 末尾的"发货配置漂移看护"一节。
这三条是**本仓库里唯一读真实 config/ 的测试**，改 YAML 不改 schema 就会红。

---

## 第二部分 · V2 设计与实施过程中的经验

### 设计阶段（2026-09-22）

#### 经验 1 · ADR 先行，避免"边写边改"

**现象**：V1 的架构决策散落在 `AGENTS.md` / `HANDOFF.md` / 代码注释里，新人（包括未来的自己）要拼凑。

**解法**：V2 在写任何代码之前先写 10 份 ADR，每份记录**背景 / 选项 / 决定 / 后果**。后续实施时如果遇到"当初为什么这么设计"的疑问，先翻 ADR。

**判据**：`docs/adr/` 有 10 份文件，每份都有 Status / Date / Deciders / Related 字段。

**看护**：`CONTRIBUTING.md` 的"如何提议架构变更"一节要求新 ADR。

#### 经验 2 · 契约测试是 V3 的验收门禁

**现象**：用户明确说"V3 还是希望完全重构"，V2 的设计要为 V3 服务。

**解法**：`docs/specs/contract-tests.md` 定义了 `PlatformAdapterContractTests` 抽象基类，V3 的新实现（哪怕换语言）必须通过同一套测试。契约测试不测实现细节，只测**输入输出 + 不变量**。

**判据**：四个平台的 adapter 测试都继承 `PlatformAdapterContractTests`；CI 的 `test-backend` job 跑这些测试。

**看护**：`Makefile` 的 `test` target 包含契约测试；覆盖率门禁（platforms/ 90%）。

#### 经验 3 · 结构性消除 > 契约测试 > 文档提醒

**现象**：V1 的 25 条陷阱里，有些是"设计缺陷"（如整行覆盖 upsert），有些是"外部依赖"（如 yt-dlp 读不了 Chrome cookie）。

**解法**：优先级是**结构性消除**（设计上不可能再犯）> **契约测试看护**（仍可能发生但有测试守着）> **文档提醒**（最后手段）。V2 的 9 条结构性消除都是"换 API"或"换数据模型"，让错误用法在编译期/类型检查期就失败。

**判据**：`docs/specs/contract-tests.md` 的映射表，每条陷阱都标注了"结构性消除"或"契约测试看护"。

**看护**：代码审查时，如果发现某条陷阱只有"文档提醒"没有测试，要求补测试或改设计。

#### 经验 4 · 配置驱动 vs 硬编码的边界

**现象**：用户要求"四个平台做成配置类型，可以在前端页面的配置里面打开或者关闭"。

**解法**：`platforms.yaml` 的 `enabled` 字段控制平台开关；`ConfigManager` 热重载后通知 `TaskRunner` / `Scheduler` / SSE 客户端。但**不是所有东西都配置化**：
- 平台的 `name` / `display_name` / `capabilities` 是代码常量（改这些要发版）。
- 平台的 adapter 实现是代码（配置只能开关，不能换实现）。
- 任务定义的 `kind` / `handler` 是代码（配置只能调参数，不能换 handler）。

**判据**：`config/platforms.yaml` 只有 `enabled` / `cookie_variant_order` / `media_strategy` / 超时 / 并发数这类**运行时可调**的字段。

**看护**：`docs/specs/config-schema.md` 的"什么该配置化"一节；代码审查时拒绝"把平台名写进配置"这类 PR。

#### 经验 5 · 文档五件套（ADR + specs + ROADMAP + progress + lessons）

**现象**：用户要求"把要做的事情和进展，经验等记录下来，以免上下文丢失信息"。

**解法**：
- `docs/adr/` — 架构决策（为什么这么做）。
- `docs/specs/` — 契约规范（接口长什么样）。
- `ROADMAP.md` — 里程碑与完成标准（做到哪了）。
- `docs/progress/YYYY-MM-DD.md` — 每日进展日志（今天做了什么）。
- `docs/lessons.md` — 经验与陷阱（踩了什么坑）。

每个会话结束前必须更新 `progress/` 和 `ROADMAP.md`。

**判据**：`docs/progress/` 有今天的文件；`ROADMAP.md` 的"当前状态"一节是最新的。

**看护**：`CONTRIBUTING.md` 的"会话结束前的检查清单"。

### 实施阶段（V2.0）

> 本节记录**跨任务的方法论**，具体的坑在上面"V2 新增"一节。

#### 经验 6 · spec 缺字段时补 spec，不要绕过去

**现象**：Task 2 发现 `Manifest` 装不下任务级错误原文（坑 2）。

**当时可以选的三条路**：
1. 把错误塞进 `summary["error"]`（`dict[str, int | str]` 勉强能装字符串）—— **不用改 spec，但是撒谎**：
   `summary` 的语义是计数，前端会拿它渲染统计卡片。
2. 塞进 `failures[]` 造一条假记录 —— **破坏 `FailureRecord` 的语义**（它是"逐条 item 的失败"，
   不是"整个任务的失败"），V1 §7.22 那次事故里恰恰就是这两层混在一起，看不出真因。
3. 给 `Manifest` 加 `error` 字段，同时在 spec 里记一笔 —— 改了契约，但改得**诚实**。

**决定**：走第 3 条。所有实施期对 spec 的偏离都记在本文件"V2 新增"一节，
带**根因**和**判据**，让 V3 重写时能直接看到"V2 的 spec 哪里是错的"。

**判据**：本文件的"V2 新增"一节里，每条坑都能回答"spec 原本怎么写的 / 为什么不够 / 改成什么了"。

**看护**：`CONTRIBUTING.md` 的会话结束清单里加一条 ——
"如果实施中偏离了 spec，是否在 `docs/lessons.md` 记了？"

#### 经验 7 · 计划里的代码是**意图**，不是成品；每条都要真跑一遍

**现象**：实施计划 `docs/plans/v2.0-implementation.md` 里的代码块，照抄进仓库后有 3 处直接不工作：
- `AppConfig(_yaml_file=...)` —— Pydantic 静默丢弃（坑 6）。
- `setup_logging()` 的 handler 装配 —— 配置字段没被消费（坑 3）。
- `get_logger` 的返回类型 —— 运行期不是那个类（坑 5）。

**根因**：写计划时没有可执行环境，代码块是"照着我理解的 API 写出来的"，
不是"跑通过的"。这不是计划的缺陷 —— 计划的价值在于**任务边界、依赖顺序、验收标准**，
不在于每行代码都对。

**解法**：严格执行 TDD 循环（写失败测试 → 确认失败 → 实现 → 确认通过），
**不许跳过"确认失败"那一步**。坑 5 就是靠这一步抓到的：
测试先写成 `isinstance(logger, structlog.stdlib.BoundLogger)` 断言为真，跑出来是红的，
才逼出"那到底该标什么类型"这个问题。

**判据**：每个 Task 的提交都带测试，且 `pytest` 输出里能看到测试数在涨
（Task 1：15 条 → Task 2：96 条）。

**看护**：`superpowers:test-driven-development` skill；
CI 的覆盖率门禁（总 80% / core 90% / platforms 90% / api 70%）。

#### 经验 8 · lint 配置要在**第一个任务**就校准，不要攒到最后

**现象**：Task 1 结束时 `ruff check` 报 130 条。如果按"先写完再统一修"的节奏，
这 130 条会和后面 15 个任务的产出混在一起，届时已经分不清哪些是"该改的代码"哪些是"该改的规则"。

**解法**：Task 1 当场处理完 —— 能自动修的 `--fix`，
该豁免的写进 `pyproject.toml` 并**逐条附中文原因**（坑 1）。
之后每个任务的 `ruff check` 都是干净的，新增的报错一定是新代码的真问题。

**判据**：Task 1 的提交（`8c5f51a`）里 `ruff check` / `mypy src/` 双双全绿。

**看护**：`Makefile` 的 `lint` target；pre-commit 钩子。

#### 经验 9 · 覆盖率报告是**找断开的线**的工具，不是分数

**现象**：Task 2 的测试写完，`pytest --cov` 显示 `logging.py` 25%、`models/manifest.py` 82%。
补测试的过程中挖出坑 2、坑 3、坑 4、坑 5 —— **四个真 bug，全是"配置字段/类型注解和实现断开"**。

**根因**：这两处代码是 Task 1 照 spec 写的，当时没有消费方，所以"写了但没接上"看不出来。

**解法**：把覆盖率报告当**待办清单**用，而不是当分数用。
低于 90% 的模块先看 `Missing` 那一列的行号 —— 未覆盖的分支往往正是"没人调用过"的那条线。
反过来，100% 覆盖也不代表没 bug（坑 5 的类型谎话在补测试前覆盖率也是够的）。

**判据**：Task 2 结束时 `logging.py` / `models/manifest.py` 都是 100%，总覆盖率 96.02%。

**看护**：`pyproject.toml` 的 `--cov-fail-under=80`；CI 上传 coverage 到 codecov。

#### 经验 10 · "读真实发货文件"的测试是漂移的唯一有效看护

**现象**：坑 7 那个 `platforms.yaml` 与 schema 不兼容的问题，
在 40 条配置测试全绿的情况下仍然存在 —— 因为**所有测试都用 `tmp_path` 造 YAML**，
没有一条读仓库里真实的 `config/platforms.yaml`。

**根因**：单元测试的隔离性（不碰真实文件）和"发货配置是否正确"这个需求天然冲突。
tmp_path 测的是"代码能处理各种 YAML"，不是"我们发的那份 YAML 是对的"。

**解法**：专门开一节"发货配置漂移看护"，三条测试用
`REPO_ROOT = Path(__file__).resolve().parents[3]` 读真实的 `config/*.yaml`。
它们不测代码逻辑，测的是**仓库自身的自洽性**：
schema 改了 YAML 没改 → 红；YAML 里加了未注册的平台 → 红；写回读不等价 → 红。

**判据**：`test_shipped_config_files_load` / `test_shipped_config_has_no_unregistered_platform_sections`
/ `test_shipped_platforms_yaml_survives_write_roundtrip` 三条常绿。

**看护**：这三条测试本身。**同样的手法后面还要用**：
前端发货的 `settings.ts` 默认值 vs 后端 schema 默认值（V1 §7.24 的"默认值只能有一处"），
以及 Alembic 迁移链 vs SQLAlchemy 元数据（Task 3 要加）。

---

## 附录 · 如何新增一条经验

1. 在对应部分（V1 §7 映射 / V2 设计 / V2 实施）新增一节。
2. 格式：**现象** → **根因** → **解法** → **判据** → **看护**。
3. 如果是 V1 §7 的陷阱，更新 `docs/specs/contract-tests.md` 的映射表。
4. 如果是结构性消除，说明"为什么设计上不可能再犯"。
5. 如果是契约测试看护，给出测试名 + 层级 + 文件。
6. 提交时在 commit message 里引用本文件的章节（如 `docs(lessons): add V2 implementation lesson #3`）。
