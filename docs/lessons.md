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

#### 坑 8 · `alembic.ini` 里的中文注释让所有 alembic 命令起不来（Task 3）

**现象**：`alembic revision --autogenerate` 直接抛
`UnicodeDecodeError: 'gbk' codec can't decode byte 0x82 in position 411`，
栈顶在 `config.read_config_from_persistent`。一条 `alembic` 命令都没跑起来过，
而仓库里其它地方中文用得好好的。

**根因**：Task 1 写 `alembic.ini` 时把中文的"为什么不配 sqlalchemy.url / 不挂 post_write_hooks"
注释写进了 ini。Alembic 读这个文件用的是 `encoding="locale"`
（`alembic/util/compat.py`），中文 Windows 的 locale 编码是 **GBK**，
而文件是 UTF-8。注释也是要被解析器读进去的 —— 它先读字节再判断哪行是注释。

**解法**：`alembic.ini` 全文改写成纯 ASCII 的说明，中文理由挪到 `alembic/env.py` 的
docstring（`.py` 永远按 UTF-8 读）和本文件。ini 顶部留一句显眼的
"THIS FILE MUST STAY PURE ASCII"，并写明为什么。

顺带一条同类教训：`rm` 临时库时别用 Git Bash 的 `$TEMP`。
它展开成 `/tmp`，而 Windows 上的 sqlite3 打不开这个路径 ——
报的是 `unable to open database file`，看起来像权限问题其实是路径。用仓库内 `.tmp/`。

**判据**：`.venv/Scripts/python.exe -m alembic upgrade head && -m alembic check`
→ `No new upgrade operations detected.`，退出码 0。

**看护**：`tests/unit/storage/test_migrations.py::test_alembic_ini_is_pure_ascii`
（直接读字节找 >0x7F）、
`test_alembic_ini_uses_path_separator_not_the_deprecated_alias`、
`test_alembic_ini_does_not_hardcode_a_url`。
后两条**必须走 configparser** 而不是子串搜索 —— 这个文件的注释里为了说明"为什么没配"
恰好写了 `sqlalchemy.url` 和 `version_path_separator` 两个名字，子串搜索会把注释读成配置。

#### 坑 9 · autogenerate 出来的迁移引用 `UTCDateTime`，`revision` 是绿的但 `upgrade` 当场 NameError（Task 3）

**现象**：`alembic revision --autogenerate` 成功、生成的文件能 import、
但 `alembic upgrade head` 在全新库上抛
`NameError: name 'UTCDateTime' is not defined`（`0001` 里所有时间列都写成
`sa.Column(..., intelligence_hub_v2.storage.schema.UTCDateTime(), ...)` 而没有 import）。

**根因**：`UTCDateTime` 是 `TypeDecorator` 子类。autogenerate 序列化列类型时
默认输出"完整点号路径"，它假设迁移文件里能引到那个名字 —— 裸 `DateTime` 能，
自定义类型不能。而**生成阶段不做任何校验**，所以这一步不红。

**解法**：在 `env.py` 装一个 `render_item` 钩子，把 `UTCDateTime` 渲染成 `sa.DateTime()`。
这不是绕过去：`UTCDateTime.impl is DateTime`，DDL 逐字相同（有断言钉着），
而时区语义由**运行时**的列类型决定，迁移文件里不需要知道那个装饰器。
加新 `TypeDecorator` 时同样要过这个钩子。

**判据**：`0001_initial_schema.py` 里 `grep -c UTCDateTime` → 0；
`alembic downgrade base` 后只剩 `alembic_version` 一张表，再 `upgrade head` 又 7 张表齐全。

**看护**：`tests/unit/storage/test_migrations.py` 的
`test_run_migrations_creates_every_table` / `test_downgrade_base_then_upgrade_head_roundtrips` /
`test_schema_matches_migrations`。**这类"生成时绿、执行时红"的东西只有真跑一遍 upgrade 才能发现**，
所以下面坑 10 那条双后端 fixture 不是奢侈品。

#### 坑 10 · Alembic 1.20 把 `version_path_separator` 改名，配上 `filterwarnings=["error"]` 得到一种只红一半的失败（Task 3）

**现象**：30 个用例报错，全在 `[file]` 档：
`MigrationError: ... No path_separator found in configuration; falling back to legacy splitting...`。
同一套代码、同一个 Repository，`[memory]` 档 30 条全绿。第一反应是"文件库 fixture 写坏了"。

**根因**：两层叠起来才红。
① Alembic 1.20 弃用 `version_path_separator`，改名 `path_separator`，
**两种情况都发警告**（用旧名、或不配这个键）。
② `pyproject.toml` 里 `filterwarnings = ["error"]`（Task 1 为了"别把警告当无声"）
把这条警告升级成异常。
③ 只有文件库会真的跑迁移 → 只有 `[file]` 档撞上。

**解法**：`alembic.ini` 用 `path_separator = os`。
不改 `filterwarnings` —— 那条"警告即失败"的纪律换来的东西比这个坑值钱。

**判据**：`pytest tests/unit/storage -q` → `[memory]` 与 `[file]` 两档同数通过。

**看护**：`tests/unit/storage/conftest.py` 的 `storage` fixture 参数化本身
（两档跑同一批用例），加上坑 8 那节提到的 `test_alembic_ini_uses_path_separator_not_the_deprecated_alias`。

**可迁移的结论**：**凡是"只有第二条路径会红"的失败，先怀疑路径而不是业务代码**。
双后端 fixture 的价值就在这里 —— 少一档，这个坑会一路带到生产。

#### 坑 11 · `env.py` 的 `dictConfig` 会把 structlog 的处理器链冲掉（Task 3，被测试之外发现）

**现象**：没有现象 —— 这是读代码时抓到的。`alembic/env.py` 模板在模块顶层无条件
`logging.config.dictConfig(fileConfig)`，而 `SqliteStorage.initialize()` 是
**在 `setup_logging()` 之后**才跑迁移的。

**根因**：`dictConfig` 默认 `disable_existing_loggers=False`，但它会**重建 root logger 的 handler**
并覆盖已装的 formatter。structlog 的 `ProcessorFormatter` 链与 `RotatingFileHandler`
就在那一步被换掉：结果是"日志文件不再轮转、控制台不再是 key-value"，
而**跑迁移这件事本身看起来完全正常**。CLI 上反过来是想要的行为（要看见 alembic 的 INFO）。

**解法**：`env.py` 里用 `config.attributes.get("configure_logger", True)` 决定装不装日志；
`_alembic_config()`（程序内路径）显式置 `False`，CLI 保持默认 `True`。

**判据**：`-m alembic current` 的 stdout 里有 `INFO [alembic.runtime.migration] ...`（CLI 档生效）；
应用启动路径不覆盖 structlog。

**看护**：**没有自动化看护** —— 这是诚实的记录。它要验的是"别人的日志配置没被我冲掉"，
得同时装配 structlog 再跑一次迁移然后比对 handler，代价与收益不成比例。
`tests/unit/storage/test_migrations.py::test_file_storage_runs_migrations_on_initialize`
只保证跑迁移不炸。改动 `env.py` 或 `logging.py` 时**人肉看这一条**。

#### 坑 12 · Alembic 的 `compare_metadata` 看不见 CHECK 约束，于是"漂移看护"有个洞（Task 3）

**现象**：`check_schema_matches_migrations()` 返回 `[]`，但给 `HEALTH_STATUSES`
加一个取值，它**仍然**返回 `[]` —— 而实际后果是内存库放行、
生产库（`0001` 里那段字面量）当场拒收。

2026-09-22 用一次性脚本实测（两个 metadata，只差 CHECK 里多一个取值，
库按 A 建、比对目标给 B）：

```
CHECK 内容变化产生的差异: 无差异 —— compare_metadata 对它不可见
对照组（加一列）: ('add_column', None, 't', Column('extra', Integer(), ...))
```

对照组是**这条断言的信誉来源**：同一个函数、同一份库，加一列它看得见，
改 CHECK 它看不见。没有对照组的话，"它返回 []"既可能是"没漂移"也可能是"函数坏了"，
两者分不开 —— 而本仓库对这两者的处置完全相反。

**根因**：SQLite 反射不出 CHECK 约束的原始文本，所以 `env.py` 里
`compare_server_default=False`，`compare_check_constraints` 也没开。
这带来一个容易被忽略的推论：**"metadata 与迁移一致"这件事不覆盖约束的内容**。
而 enum 的取值清单在这个仓库里有两个定义点（`schema.py` 的常量 + 迁移文件里的字面量），
Task 3 之前还是**手抄**的四条。

**解法**：两件。
① 约束文本改成**从常量生成**（`schema.py::_enum_check`），Python 侧只剩一处真相；
② 迁移文件那侧是历史事实不能重生成，于是加一条"从真库读 `sqlite_master`，
把 CHECK 里的取值集合与常量比对"的用例，并把**全部 11 条约束名**列成精确集合
（不是子集）—— 顺手也覆盖了"batch 模式重建表时约束静默消失"。

**判据**：`pytest tests/unit/storage/test_schema_types.py -q` → 18 passed；
其中 `test_the_migrated_db_has_exactly_the_checks_we_wrote` 断言的是集合相等。

**看护**：`tests/unit/storage/test_schema_types.py` 全文件。
另外 `test_the_platform_health_statuses_are_one_tuple_not_two` 盯住
`models/platform.HEALTH_STATUSES` 与 `storage.schema.HEALTH_STATUSES` 这对**不得不两份**的定义点
（V2.1 加健康状态时改一处不够）。

**还有一处没收口（记在这里，别当成已解决）**：同一个 CHECK 的**整条被删掉**时
`compare_metadata` 同样看不见（上面那个探针测的是"改内容"，删约束是它的兄弟情形），
所以现在靠的是 `EXPECTED_CHECKS` 那份**手写的 11 条清单**。
它比"没有看护"强（漏一条就红），但它自己也是第三处真相 ——
新加约束时必须同时往那份清单里加一条，否则红的是"集合不相等"而不是"你忘了改"。

#### 坑 13 · 两个任务在同一秒起跑，后一份清单会静默盖掉前一份（Task 4）

**现象**：`test_two_runs_get_two_files_and_two_index_rows` 期望两个任务两份清单文件，
实际只有**一份**，而 DB 里有两条索引都指向它。测试是绿的假象的反面 ——
它一次跑出来就是红的，而且是"少了一个文件"这种看不出后果的红。

**根因**：清单文件名按 Locked 的 `data-model.md §1` 生成：
`<8位日期>-<6位时间>-<kind>.json`。同一秒 + 同一个 `kind` = 同一个名字。
而 `all_platforms` 这个任务的存在理由就是让多个平台采集**并行**，
所以"同一秒两个 douyin_collect"不是极端情况，是常态。
写文件用的是"写 `.tmp` 再 `os.replace`"（这是为了防半截清单，本身没错），
于是第二个任务的 replace **原子地**覆盖掉第一个 —— 原子性在这里反而帮凶：
没有任何部分失败可供察觉。前一个任务的审计凭据消失，不报错。

**解法**：文件名加 `task_id` 段 → `20260922-120000-douyin_collect-<task_id>.json`。
时间戳仍在最前，"按名字排序 = 按时间排序"这个性质没动。
这是**改了一份 Locked 的目录布局**，所以同步回写了 `data-model.md §1` 并写了理由。

**判据**：`test_two_tasks_started_in_the_same_second_get_different_files`（`files.py` 层）+
`test_two_runs_get_two_files_and_two_index_rows`（`manifest_writer` 层）。

**看护**：上面两条。

**可迁移的结论**：**凡是"用时间当唯一键"的产物文件名都要问一句同一秒怎么办。**
V1 的清单用的正是这个名字（`downloads/manifests/<8位日期>-<6位时间>-<kind>.json`），
本机至今没撞过 —— 但**没撞过不是它安全的证据**，我并没有验证过 V1 的并发形状
（去数一下同时存活的采集进程才能下结论，这不值当）。
V2 这边是明确要并行（`all_platforms` + 平台级 Semaphore），所以必须在有测试的时候改掉。

#### 坑 14 · spec 草图里的 `else: builder.succeed()` 会把 `partial` 改成 `success`（Task 4）

**现象**：照 `docs/specs/task-runner.md §2.6` 的 `manifest_writer` 草图抄，
handler 里 `builder.partial({"downloaded": 1, "failed": 1})` 之后正常退出，
清单落到盘上会变成 `status: "success"`，`failures[]` 里那条失败记录还在，
但**没人会去看**——因为状态灯是绿的。

**根因**：草图把"退出方式"当成"状态的唯一来源"。实际有两个说话的人：
handler 知道"跑完了但有 item 失败"，wrapper 只知道"这里没抛异常"。
`else:` 分支无条件覆盖，等于让信息少的一方否决信息多的一方。

**解法**：一条判据（`core/manifest.py::_may_settle`），三个方向：
- 没人设过 → 按退出方式补（这是契约二的结构性保证，必须保留）；
- handler 设了 `success` 但随后抛出异常 → **异常赢**（否则就是 §1.3 的"看起来在跑"）；
- handler 设了 `partial` / `failed` / `cancelled` / `timeout` → 不动。

**判据**：两条方向相反的用例必须在 —— `test_handler_set_partial_survives` 与
`test_success_then_raise_never_reports_success`。只留前一条会被改回草图那样还全绿，
只留后一条会被改成"异常永远覆盖一切"也全绿。

**看护**：上面两条 + `test_explicit_fail_in_handler_is_not_overwritten_by_the_raise`。
已回写 `task-runner.md §2.6`（原草图保留，旁边标出这一行有 bug）。

**可迁移的结论**：**设计文档里的代码片段是意图，不是成品**（经验 7 已经说过一次，
这次是它的新形态）：草图短、看着无害、抄过去就绿。凡是草图里有
`else: 设成某个具体值` 这种"无条件赋值"，先问"还有谁能比我更早地说这句话"。

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

> **Task 3 的后续（2026-09-22）**：Alembic 那条加了，而且**必须加两条**才够 ——
> `check_schema_matches_migrations()` 对 CHECK 约束是瞎的（坑 12），
> 所以除了漂移看护本身，还要一条直接读 `sqlite_master` 比对约束取值与约束名清单的用例
> （`tests/unit/storage/test_schema_types.py`）。
> 只做前者会留下一个"看起来有两处看护、实际只有一处"的错觉。
> `settings.ts` 那条仍在待办池，Task 10/11 落前端时做。

### 实施阶段（V2.0）· Task 3（存储层）

#### 经验 11 · 漏翻译异常是一**整类** bug，写测试时能一次性捞出好几个

**现象**：给 `platforms` 写删除用例时，`creators` 还指着这个平台，
期望拿到 `StorageError`（API 层要映射成"该平台下还有 N 位博主"），
实际甩出来的是裸 `sqlalchemy.exc.IntegrityError` + 一屏 SQL 回显。
顺着同一条思路回头审其它 Repository，又找到两处：
`TaskRunRepository.start()` 撞主键、`EventRepository.append()` / `ManifestRepository.record()`
指向不存在的任务 —— **同一个形状的漏口一共四个**。

**根因**：`BaseRepository._translate_integrity()` 是有的，但它是**可选调用**：
每个写方法都得自己记得 `try/except IntegrityError`。漏一个不会红，
只会在真实用户手上变成 500 页面。V1 §7 那句"看起来在跑"就是这个机制。

**解法**：把这一类当成**批量审计项**而不是逐个 bug ——
凡是有 `insert()` / `delete()` / 会撞约束的写方法，一律过一遍。
`affected_rows()` / `inserted_id()` 两个 helper（见经验 12）顺手把
`rowcount` 的读法也统一了，同一个道理。

**判据**：`tests/unit/storage/` 里 8 条断言"抛的是本仓库异常族"的用例全绿，
其中 `test_delete_is_translated_when_creators_still_reference_it` 匹配的是
`外键不成立` 这句**翻译后的**文案，不是 SQLite 原文。

**看护**：`tests/unit/storage/test_{platforms,creators,videos,task_runs,events,manifests}_repo.py`
各自的 NotFound / Conflict / 外键分支。**V3 换存储实现时这一批是行为契约**：
它认的是异常类型与文案，不是 SQL。

#### 经验 12 · 一个 `ContextVar` 放错模块，代价是七个"看不出为什么"的函数级 import

**现象**：`db.py` 里 `_Repositories.__init__()` 把七个 Repository 全 import 在函数体里，
旁边一句"避免循环导入"。ruff 为此专门有 `PLC0415`，于是每条都要人肉判断该不该豁免 ——
七个豁免注释，而注释本身还得解释一个只有读懂双向依赖才看得懂的问题。

**根因**：`CURRENT_SESSION` 这个 `ContextVar` 同时被两侧需要：
`db.py` 的 `transaction()` 要 set/reset，`repositories/base.py` 的 `_scope()` 要读。
它住在 `db.py` 里，`db → repositories → db` 就是环，函数级 import 是唯一的解法之一。
但"唯一"是错的：**它该住在叶子模块**。

**解法**：抽出 `storage/session.py`，只放这个 ContextVar 和它的说明。
依赖变成单向的 `session.py ← repositories/base.py ← db.py`，
七个函数级 import 直接提到顶层。`db.py` 仍然 re-export
`CURRENT_SESSION`，所以公开路径没变（V3 的契约名不动）。

**判据**：`db.py` 里函数体内的业务包 import 归零 ——
`grep -n "^    from intelligence_hub_v2" src/intelligence_hub_v2/storage/db.py`
只剩第 53 行那一条，而它在 `if TYPE_CHECKING:` 块里，
是**分层**要求（`storage` 不许运行期往 `core` 引），不是循环导入的补丁。这两个"缩进的 import"
长得一样、理由完全不同，看代码时别混。
`ruff check src/` 干净，且 `src/` 里 `# noqa: PLC0415` 计数为 0（豁免只剩 `alembic/env.py` 两条，各带原因）。

**看护**：`test_transaction_does_not_leak_into_sibling_tasks`
（并发两个 `transaction()` 互不串写 —— 抽模块之后这条仍然是唯一能证明隔离性的用例）。

**可迁移的结论**：**看到"一堆延迟导入 + 一句解释循环"时，先问那个共享符号能不能搬走**。
循环通常是放置位置的症状，不是架构的本质。

#### 经验 13 · 不要给"我以为的操作系统行为"写测试

**现象**：给 `safe_filename` 写 Windows 保留名用例时，我顺手加了一条"终判据"：
拿 `CON` 建目录、往里写文件，断言 `OSError` —— 用来证明"加 `_` 前缀不是洁癖"。
跑出来三条全红：**建得出来，也写得进去，读回来还对**。

**根因**：`CON` 这类保留名在 Win32 路径解析里的处理**随版本与路径写法而变**
（`\\?\` 前缀、长短路径、是否经 shell）。我写的是"我记得的行为"，不是这台机器的行为。
这类测试比没测试更糟：它带着"已经用操作系统验证过了"的口吻，
下一个人就不会再去查。

**解法**：撤掉那条断言，换成把**实测结果本身**写进用例的 docstring
（`test_windows_reserved_names_are_still_prefixed_even_though_this_machine_allows_them`），
并把保留前缀的理由改成诚实的那个：别的消费方（资源管理器、备份工具、某些杀软、
别的 Windows 版本）可能仍按设备名解析，而我们**无法在本机验证那批消费方**。
看护落在函数层（前缀确实加了）与幂等性上。

**判据**：`tests/unit/test_safe_filename.py` → 68 passed
（9 条 hypothesis 属性 + 16 个定向测试函数，参数化展开成 68 条；
其中 `test_result_is_usable_as_a_real_path` 是真建目录真写文件 ——
那条是**可移植**的不变式，不含任何"Windows 会拒绝 X"的猜测）。

**看护**：上面那个文件。另外记一句反直觉的：**这一轮 hypothesis 只找到 1 个 counterexample，
而且是"我的断言写错"**（`safe_filename("", max_length=1)` 返回兜底字面量 `"unnamed"`，
7 个字符 > 预算 2 —— 兜底值本来就不受长度预算约束）。
另外三个错的是我手写的定向断言（`.._` 不是穿越、`。` 不该被剥、`"///"` → `"___"` 不走兜底），
跟 hypothesis 无关。

**所以属性测试的真实价值在这批里不是"抓到实现 bug"**，而是两样别的东西：
① 它把"我认为的边界"变成可执行的句子，写的时候就得想清楚判据（那条 `max_length` 就是
写出来才发现说不圆的）；② 幂等性与"结果永远是单段路径"这类**没法逐条列举**的性质，
只有生成器能覆盖。抓到实现 bug 是偶得的红利，不是这层的 KPI。

#### 经验 14 · 依赖注入的对象 vs 工厂：一个"close 之后还能用"的洞

**现象**：给 `EventBus.replay()` 写"库出真错时要往外抛、不许变成空列表"这条用例时，
我 `storage.close()` 之后再调 `bus.replay(...)`，期望 `StorageError` ——
拿到的是裸 `sqlite3.OperationalError`，SQL 语句连参数都回显出来了。

**根因**：`SqliteStorage.close()` 把 `self._sessionmaker` 置回 None，
但 Repository 在构造时就**握着那个对象引用**了，置 None 只是让 `storage` 自己不再给，
拦不住已经拿到手的引用。于是"关掉了"与"还能用"同时成立 —— 而且后者拿到的是驱动层异常。

**这为什么会在生产里发生**：Task 9 的 lifespan 会 `close()` storage，
而那一刻完全可能还有一个任务正在往事件流里写日志（`EventBus` 正是长期持有
`storage.events` 的那个消费者）。症状是关服期间一屏看不懂的 traceback，
而不是"存储已关闭"这一句能判断的话。

**解法**：`BaseRepository.__init__` 收 `Callable[[], AsyncSession]`（`SqliteStorage.new_session`），
不再收 sessionmaker 对象。关闭后工厂里的 `_require_sessionmaker()` 抛本仓库的 `StorageError`，
文案同时覆盖两种情况（"还没 initialize()，或已经 close()"）。

**判据**：`test_repositories_fail_our_way_after_close` —— 故意先把 Repository 取出来
（`held = storage.platforms`），再 close，再调用。
**必须先把引用拿走**，不然测的是 `storage.platforms` 这个属性而不是那个洞。

**可迁移的结论**：注入**对象**等于把一份快照塞进构造器，注入**工厂**才是把状态判断留在原地。
凡是"生命周期比被注入者短"的东西（连接池、engine、client），都该注工厂。

#### 经验 15 · 双后端 fixture 的迁移要一次性摊销，且别在 auto 模式下用 session 作用域

**现象**：Task 3 结束时全套 650 用例 56 秒；Task 4 加完 701 用例后我先量到 **502 秒**。
一半时间确实是我在实现里绕了路（下面第一条），另一半是这台机器的 I/O 状态
（V1 §7.23 早就记过"同一套代码用时抖三倍"，这次抖得更多）。

**两处真开销**（各自可验证，不靠猜）：

1. `run_migrations()` 在本机 ~0.7s：cProfile 指到 23 条 DDL 落盘、`_exec` 累计 520ms，
   不是 alembic 导入的锅。`[file]` 档 240 多个用例各跑一遍 = 纯浪费。
   → 全会话用同一条 `run_migrations()` 建一个**模板库**，每个用例 `shutil.copy` 一份
   （100 KB 复制，亚毫秒），`initialize(migrate=False)` 打开它。
   省的是重复，**不是覆盖**：起点仍是真迁移产物，迁移写错照样全红；
   "空库从头建 / 可不可逆 / initialize 会不会真去迁移"三件事在 `test_migrations.py`
   里各有不用模板的专责用例。
2. `test_result_is_usable_as_a_real_path`（hypothesis + 真建目录真写文件）300 例 = 9.6s。
   它的判据是"这个名字操作系统收不收"，50 个随机形状已经够，其余交给纯函数属性。

**踩到的平台细节**：`asyncio_mode = "auto"` 会把**每个** fixture 包成协程，
所以一个 `scope="session"` 的 fixture 会要求 session 事件循环，
而依赖它的 function 作用域 async fixture 就直接报
`ScopeMismatch`（错误还只点名 `storage`，不点名你新加的那个 —— 找起来很费劲）。
解法不是去配 `asyncio_default_fixture_loop_scope = "session"`（那会让所有 function
用例共用一个循环，`_Subscriber` 之类的 loop 绑定对象会互相污染），
而是**用函数作用域 fixture + 进程内缓存**：`tmp_path_factory` 本身是 session 作用域的，
可以在函数 fixture 里安全地建一个跨用例存活的目录。

**判据（都是这台机器上实测的同一批用例，不是估算）**：
全套 `pytest tests/`（带覆盖率门禁）**502.88s → 292.24s**，701 passed 不变；
`tests/unit/storage tests/unit/core` 577 条改后 **180.91s** 全绿。
剩下的 ~0.3s/用例主要是文件型 SQLite 的冷打开与杀软扫描，属于环境而不是代码
（`[memory]` 档的 setup 也要 0.41s —— 那已经是"在内存里建 7 张表"的成本）。

**纪律**：改完之后**不要用测试用时当门禁**（V1 §7.23 原文）。
门禁是用例数与红绿；用时只用来定位"哪一处明显是我加的"。

---

## 附录 · 如何新增一条经验

1. 在对应部分（V1 §7 映射 / V2 设计 / V2 实施）新增一节。
2. 格式：**现象** → **根因** → **解法** → **判据** → **看护**。
3. 如果是 V1 §7 的陷阱，更新 `docs/specs/contract-tests.md` 的映射表。
4. 如果是结构性消除，说明"为什么设计上不可能再犯"。
5. 如果是契约测试看护，给出测试名 + 层级 + 文件。
6. 提交时在 commit message 里引用本文件的章节（如 `docs(lessons): add V2 implementation lesson #3`）。
