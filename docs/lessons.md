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
但**没人会去看** —— 因为状态灯是绿的。

**根因**：草图把"退出方式"当成"状态的唯一来源"。实际有两个说话的人：
handler 知道"跑完了但有些 item 失败"，wrapper 只知道"这里没抛异常"。
`else:` 分支无条件覆盖，等于让信息少的一方否决信息多的一方。

**解法**：一条判据（`core/manifest.py::_may_settle`），三个方向：
- 没人设过 → 按退出方式补（这是契约二的结构性保证，必须保留）；
- handler 设了 `success` 但随后抛出异常 → **异常赢**（否则就是 §1.3 的"看起来在跑"）；
- handler 设了 `partial` / `failed` / `cancelled` / `timeout` → 不动。

**判据**：两条方向相反的用例必须同时在 —— `test_handler_set_partial_survives` 与
`test_success_then_raise_never_reports_success`。只留前一条会被改回草图那样还全绿，
只留后一条会被改成"异常永远覆盖一切"也全绿。

**看护**：上面两条 + `test_explicit_fail_in_handler_is_not_overwritten_by_the_raise`。
已回写 `task-runner.md §2.6`（原草图保留，旁边标出这一行有 bug）。

**可迁移的结论**：**设计文档里的代码片段是意图，不是成品**（经验 7 已经说过一次，
这次是它的新形态）：草图短、看着无害、抄过去就绿。凡是草图里有
`else: 设成某个具体值` 这种"无条件赋值"，先问"还有谁能比我更早地说这句话"。

#### 坑 15 · yt-dlp 的"已经下载过了"句式配了个不存在的冒号（Task 5）

**现象**：`_artifacts_from()` 认不出"这条媒体其实早就下好了"，测试断言
"三行输出认出三个文件"当场红成两个。

**根因**：我照记忆写的匹配串是 `"has already downloaded: "`（尾巴带冒号）。
yt-dlp 的真实句式是 `[download] media.mp4 has already been downloaded` —— 冒号在**前面**
（`[download] ` 之后），而且从不在结尾。
症状很值得记：**库里有作品行、媒体列表为空**，而排查的人会先去怀疑磁盘或权限。

**解法**：改成"前缀 + 结尾短语"两段式解析，两种措辞（`has already been downloaded` /
`has already downloaded`）都收；文件后缀仍是白名单，防止把 `[info]` 行认成产物。

**判据**：`test_artifacts_are_taken_from_yt_dlp_reported_paths_only`。

**看护**：`tests/unit/infra/test_ytdlp.py`。

**为什么不用 glob 扫目录**（同一处实现的决定）：V1 §7.21 扫 `*.mp4` 会把自己产出的
`postprocess/audio/part-001.m4a` 也认成源媒体，一条作品转两遍。

#### 坑 16 · 退档判据只认"cookie 读不出来"，风控的 412/352 被当成死链（Task 5）

**现象**：写阶梯测试时期望"412 会退到下一档"，实际只跑了一档就返回失败。

**根因**：`looks_like_cookie_failure()` 那张表（V1 §7.15 抄来的）只收了
**cookie 读取**类原文（`Could not copy Chrome cookie database` / `Failed to decrypt with DPAPI`）。
而 V1 实测的另一半是：**无 cookie 时 B站 枚举随机回 352/412**，带导出 cookie 就过 ——
那是"没带 cookie"的形状，不是"cookie 读不出来"。两类的处置动作相同（换一档再试），
**原因与人要做的动作完全不同**（一个去修 DPAPI，一个去带 cookie）。

**解法**：拆成两个函数。`looks_like_cookie_failure` 管文案与预检（该说什么话），
`should_escalate_cookie_rung` 管处置（要不要再试一档）= 读取失败 ∪ 风控原文。
合成一个的代价是"风控 412"被报成"cookie 读不出来"，于是人去找解密问题。

**判据**：`test_the_escalation_boundary` 参数化五例（412 / 352 / Fresh cookies /
Could not copy 会退档；Unsupported URL 不退）。

**看护**：`tests/unit/infra/test_ytdlp.py`。

#### 坑 17 · `infra` 运行期反引 `platforms`，只在"谁先被 import"时才炸（Task 6）

**现象**：把 `from ...douyin.adapter import DouyinAdapter` 接到
`platforms/__init__.py` 末尾之后，单跑 `tests/unit/platforms/` 全红：
`ImportError: cannot import name 'YtDlpResult' from partially initialized module
'intelligence_hub_v2.infra.ytdlp'`。而单跑 `tests/unit/infra/` 是**绿的**。

**根因**：`infra/ytdlp.py` 在运行期 `from platforms.base import CookieVariant`，
而导入 `platforms.base` 会**先执行父包 `platforms/__init__.py`** —— 那里现在要装适配器，
适配器又回头 import `infra.ytdlp`（还没执行完）：

```
infra.cookies → infra.ytdlp → platforms.base → platforms/__init__
  → douyin.adapter → infra.ytdlp（半成品）→ ImportError
```

平时看不见，是因为只要先从 `platforms.*` 进门，链条就不会闭合。
"取决于导入顺序"的坑在单进程测试套件里表现为**红绿随文件收集顺序漂移**。

**解法**：把边掰正，而不是挪接线点。`CookieVariant` 在本仓库只出现在注解位置
（dataclass 字段与函数签名），文件又有 `from __future__ import annotations`，
所以挪进 `TYPE_CHECKING` 是零成本的；infra 从此不再运行期依赖 platforms。

**为什么不在 `main.py` 里装配**：注册表要的是"**实现了哪些平台**在导入期就固定"。
如果适配器只在应用启动时才登记，`test_the_shipped_platform_schemas_have_no_adapters_yet_by_design`
那条快照就永远是绿的 —— 一个不会变红的看护等于没有看护。

**判据 / 看护**：`tests/unit/test_import_layers.py` —— 六个入口各起**一个子进程**
当第一个 import 跑一遍，外加"先从 infra 进门 / 先从 platforms 进门"两种顺序都要能读到
`PLATFORMS["douyin"]`。同进程内模块只加载一次，这类环**只有换进程才照得出来**。

#### 坑 18 · `probe_streams()` 让"ffprobe 没装"冒出去，一次已下好的媒体变成采集失败（Task 6）

**现象**：抖音 `download_media` 里加了一句 `has_audio_stream(path)`，11 条媒体用例
全红在 `LookupError: 找不到可执行文件 'ffprobe'`。

**根因**：`infra/ffmpeg.py` 的模块纪律是"缺二进制要如实报找不到可执行文件"
（`extract_audio` 那边对，还有专门用例钉着），但 `probe_streams` 照抄了同一条，
而它的契约是**另一个形状**：文档自己写着"看不懂时返回空列表"，
`has_audio_stream` 再按"问不出来算有音频"兜底（V1 §7.21）。
空列表这一档没覆盖 `LookupError`，于是"问不出来"的两个来源只有一个走得到兜底。
本机 PATH 里**真的没有 ffprobe**（V1 §7.19），所以任何调用方一碰就炸。

**解法**：`probe_streams` 就地消化 `LookupError` → 空列表 + 一条 debug 日志。
不对称是**有意**的，两边各有一条用例钉着：
`extract_audio` 缺 ffmpeg 必须红（它在产出用户要的东西），
`probe_streams` 缺 ffprobe 必须是"问不出来"（它在问一个问题）。
"这台机器没装 ffmpeg"该红的位置是 preflight，不是每一次 probe。

**看护**：`tests/unit/infra/test_ffmpeg.py::test_probe_treats_a_missing_binary_as_an_unanswerable_question`
＋ `tests/contracts/test_douyin_adapter.py::TestDownloadMedia::test_an_unprobeable_file_is_still_reported_as_having_audio`。

**补一句（又是经验 16）**：这个 `NameError: name 'logger' is not defined`
是我给 `probe_streams` 补日志时**当场犯的**——那一轮只跑了 `pytest`，没先跑
`ruff check`。同一个错误 ruff 的 F821 直接就能指出来。**改完就跑门禁**，
攒到提交前只会让红点离原因更远。

#### 坑 19 · 在 Windows 上每建一个 httpx 默认真传输的客户端要 2.1 秒（Task 6）

**现象**：新写的 85 条抖音契约测试跑了 **125 秒**，而且几乎每条都均匀地占 1.4~4.5 秒
—— 连 `parse_creator_url("MS4w…")` 这种零 I/O 的用例也是。

**根因**：`make_deps()` 在 `http=None` 时默认建一个 `httpx.AsyncClient()`（默认真传输）。
实测这台机器上 `httpx.AsyncClient()` **每次都**要 2.1~3.0 秒
（不是进程内一次性开销；`trust_env=False` 一样慢，`ssl.create_default_context()` 本身
只要 168 ms，所以大头在 httpcore 那条路径上）。而
`httpx.AsyncClient(transport=httpx.MockTransport(...))` 是 **0 ms**。

**解法**：测试里的默认客户端换成 MockTransport。这条改动**顺手修掉了一个更要紧的问题**：
以前忘了传 `http=` 的用例会拿到一个能上真网的客户端，"离线契约测试"其实要看网络运气。
现在默认是"任何真请求当场炸"，测试的离线性质由构造保证。

**结果**：`tests/contracts` 125 秒 → 3.3 秒；全套 915 条 76 秒（带覆盖率）。

**对项目本身的那一半结论（还没落地，Task 9 记账）**：生产端**必须全应用共用一个
`AsyncClient`**（`AdapterDeps.http` 就是这个设计），不能每个适配器 / 每次桥调用各建一个 ——
在这台 Windows 机器上那不是"稍微慢一点"，是每建一次白付 2 秒。
`infra/cdp_bridge.py:BidgeClient` 在不注入客户端时会自己 lazy 建一个，
装配层要显式把共享 client 传进去。

#### 坑 20 · V1 的 `sec_uid` 解析器接受**任何域名**的 `/user/x`（Task 6 补的洞）

**现象**：给 `parse_creator_url` 写"脏输入"用例时，
`https://example.com/user/x` 被认成 sec_uid=`x` 的合法抖音主页，测试没红。

**根因**：V1 的 `_sec_uid_in_url()` 最后一条兜底是"路径以 `user/` 开头就取第二段"，
本意是给页面 JS 可能返回的相对 `href` 用的，但它对**带任意主机名的 URL 同样成立**；
`?sec_uid=` 那条 query 分支也没有域名约束。于是任何第三方站点的一条
`/user/anything` 链接都能生成一个"看起来合法"的 `platform_id`。
V1 里它的实际后果是"对标库里混进别的号的行"，靠人眼发现，所以一直没人修 ——
**V1 有这条不等于它对**。

**解法**：三条解析分支之前加一道域名闸门 `is_douyin_host()`
（`douyin.com` 子域 + `iesdouyin.com`；**无主机名的相对路径放过**，那正是这条兜底的存在理由）。

**踩到的第二个坑**：判据写成 `_DOUYIN_HOST.match(host)`，于是
`www.douyin.com` 因为开头的 `www.` 被判成"不是抖音"，**所有正常主页链接全部认不出**。
模式里的 `(?:^|\.)` 已经挡住了 `notdouyin.com` 这种仿冒，所以该用 `search`。
这条是契约测试当场抓出来的（8 条红），不是读代码读出来的。

**看护**：`tests/contracts/test_douyin_adapter.py::TestParseCreatorUrl`
（`test_unrecognisable_input_raises_parse_url_with_the_original_text[https://example.com/user/x]`
是这一条的正身；参数化里另外五例守的是"别修过头把合法链接也挡掉"）。

#### 坑 21 · 设计文档写着"列表走公开 API"，实测那个接口匿名回的是一个 HTML 风控页（Task 7）

**现象**：`platform-adapter.md §4.2` 与 Task 7 的计划草图都写着
B站 `list_strategy='api'`（博主作品列表走公开 web-interface）。
照这个假设动手前先用 `curl` 问了一次
`x/space/wbi/arc/search?mid=…&ps=10`，回的是 `<!DOCTYPE html>…`，不是 JSON。

**根因**：那个接口 2023 年起要 WBI 签名（`wbi/img_key` 派生的 mixin + 参数排序哈希）。
yt-dlp 的 `BiliSpaceVideo` 抽取器内部实现了签名，所以 V1 用
`yt-dlp --flat-playlist` 一直是通的；设计阶段把它记成了"公开 API"。
如果照 `'api'` 实现，等于在 V2 里重写一遍 yt-dlp 已经维护的那套签名 ——
对面改版时我们要跟着改，而 yt-dlp 社区也会改。**两处维护同一个签名算法**是纯负债。

**顺带量到的两件事**（都进了 fixtures 与用例）：
匿名 `yt-dlp --flat-playlist` 回 `Request is blocked by server (412)` —— V1 §7.15
那句原文在**今天**仍然成立，而且它正好在 `should_escalate_cookie_rung()` 的表里，
所以"带 cookie 的第一档先跑、风控才退档"这条链是真的；
`yt-dlp -J` 匿名能看到 15 条 formats（视频轨 `acodec=none` + 音频轨 `m4a`），
证实 §7.21 那对未合并分片是**默认形状**而不是偶发。

**解法**：`list_strategy` 取值收窄成 `yt_dlp_flat | external_manifest`，
公开 web-interface 保留它真正能做的角色（逐条 `view` / `player/v2` 字幕 / `card` 资料，
三个都实测匿名 `code:0`）。见 `docs/adr/0011` 的 Task 7 追记。

**一般化**：**照设计文档动手之前，先用一次真请求量一下对面**。
"接口叫什么名字"是可查的，"这个接口匿名能不能通、回什么形状"只能问它本人。

**看护**：`TestRealFixturesAreActuallyReal`（fixture 里标了"真样本"的东西必须真的可解析）
＋ `test_the_412_stderr_is_the_text_the_escalation_table_knows`。

#### 坑 22 · 两个 bug 都是"第一次跑真响应"抓出来的，而它们长得完全不像同类（Task 7）

**现象**：B站 适配器第一次对着真 fixture 跑，红了两个：

1. `fetch_creator_profile()` 读 `payload["card"]` → 永远 None →
   报"card 接口没有 card 字段"。真形状是 `{"code":0,"data":{"card":{…}}}` ——
   **B站 所有业务负载都嵌在 `data` 一层里**（view / player / card 三家一致）。
2. `entries_to_cards()` 收到清单里一条 `"not a dict"` →
   `AttributeError: 'str' object has no attribute 'get'`。
   `parse_dump_json_lines` 那一路确实筛过类型，但 `entries_to_cards`
   是**公开出口**，外部清单那条路也喂它。

**为什么它们值得单独记**：两个都**不可能**被类型检查或 lint 抓到
（`payload: dict[str, Any]` 里取什么是自由的；`Sequence[Mapping[str, Any]]` 的注解
在运行期什么都不是）。它们只在"喂进去的东西真长成那样"时现身。
凭印象编的 fixture 会让两个都安静地躲过去 —— 因为编的时候我就会写成
`{"card": {...}}`（我以为的形状）。

**解法**：① 统一在 `_get_json()` 那层判 `code` 并交回整个 payload，取值处显式过 `data`；
② `entry_to_card()` 开头加 `isinstance` 早退，并把原因写在注释里
（"这条是被用例抓出来的"，不写成"防御性编程"）。

**看护**：`test_creator_profile_comes_from_the_real_card_response`、
`test_entries_to_cards_respects_limit_and_skips_junk`、
`test_malformed_track_items_are_skipped`（字幕轨那边同一族：非 dict / 缺 url /
`javascript:` 一律丢）。

#### 坑 23 · 一次**成功**的下载被写成"带着错误"（Task 7，改了契约字段）

**现象**：`test_merged_download_is_reported_as_yt_dlp_with_size` 红在
`assert artifact.yt_dlp_error is None`，而实际值是一句
"没有可用的导出 cookie，只能匿名或走浏览器档"。

**根因**：这是我自己写出来的语义冲突。§7.15 要求"清单 note 必须写清是哪一档"
（档位差别是画质，不写就没人知道这批视频为什么糊），而 `MediaArtifact`
里**只有一个**能放这句话的字段 —— `yt_dlp_error`。
第一次实现就把阶梯说明塞进去了，于是"成功但降级"与"失败"共用一个格子。
Task 14 那条通用契约用例（`media_source != yt_dlp` 时 error 必须非空）也会
被这个用法带偏：它会把"记录档位"变成"必须编一句错误"。

**解法**：一个字段只说一件事。`yt_dlp_error` 收窄回"**只装失败原文**"，
新增 `MediaArtifact.cookie_rung`（`YtDlpCookieVariant.label` 原文）。
抖音的页面直链兜底那一路 `cookie_rung=None` —— 那条路不经过 yt-dlp，
没有档位可记，**不编一个 `"page_context"` 假标签**（编了就会有人去查那条档不存在的路径）。
理由与影响写在 `docs/adr/0011` 的 Task 7 追记。

**一般化**：加字段之前先问"现有字段能不能同时回答两件事"。
如果答案是"能，但要靠读者自己分辨"，那就是不能。

**看护**：`test_a_clean_success_records_the_rung_not_an_error`（B站）、
`test_an_anonymous_download_still_says_which_rung_it_used`（B站）、
`test_yt_dlp_success_marks_the_source_and_keeps_error_none`（抖音补了 rung 断言）。

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
> `settings.ts` 那条已在 Task 11 落地（`frontend/src/stores/` + 4 条用例），见 `docs/specs/contract-tests.md §3` 的 §7.24 行。

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
#### 经验 16 · 批量脚本改完**立刻**重跑门禁，别攒到提交前

**现象**：`cookies.py` 里一处批量编辑把模块常量 `EMPTY_COOKIE_FILE_BYTES` 的定义丢了，
引用还在。我先跑了 `ruff format` 就去做别的事，最后是一条测试跑出
`NameError: name 'EMPTY_COOKIE_FILE_BYTES' is not defined` 才发现。

**根因不是工具瞎**（这次专门验了）：单独造一个"函数里引用了不存在的模块常量"的最小文件，
`ruff check` 回 `F821 Undefined name`，`mypy` 回 `Name "..." is not defined` —— **两个都抓得到**。
抓不到是因为我那一轮**根本没跑门禁**：批量改完只跑了 format。

**同一批里另一个工具真抓不到的**：`looks_empty` 被改坏成
`return self.exists and self.size_bytes is not None` —— 仍然是合法的 bool，
类型与 lint 全过，语义却从"空文件"变成"文件存在"。
所以两条教训要分开记：
① 门禁能抓的，别因为没跑而漏；② 门禁抓不到的（语义/方向错），只有**具体的用例**能抓，
所以那种地方要写"这条在盯什么"的注释（现在 `cookies.py` 里就有）。

**同一天第二次同类事故（就是写这条经验时发生的）**：用"按索引切片拼接"的脚本往
`lessons.md` 里插新章节，`s[:i] + new + s[j:]` 的 `i` 指到了**上一节**（坑 14）的标题，
于是把坑 14 整节替换掉了。`git diff` 才 3 行、`ruff`/`mypy` 完全不涉及 `.md`，
所以没有任何工具会红 —— 只有"数一下章节标题"会发现。
**结论**：批量改文档要么用带唯一上下文的精确替换（`Edit` 那一类），
要么改完立刻 `grep -c '^#### '` 对一遍数量。

**判据**：改完就跑 `ruff check` + `mypy`，再跑受影响的测试目录 —— 顺序不能倒过来。

**看护**：`test_freshness_does_not_call_a_real_file_empty`。

#### 经验 17 · "两处真相"的第三种形态：同一份顺序被写了两遍，而且互相矛盾（Task 6）

**现象**：`DouyinConfig.ytdlp_cookie_priority` 与 `DouyinAdapter.capabilities.cookie_variants`
都是"抖音的 cookie 阶梯"，前者是 `browser > exported_file > none`，
后者是 `exported_file > browser > none`。**两个都是活字段**：
前者进 `config/platforms.yaml`、进 JSON Schema、被前端渲染成表单；
后者被调度器读、被 `download_media` 的契约要求"必须照它走"。
实现只能挑一个，另一个当场变成撒谎的声明。

**根因**：前 16 条坑里的"两处真相"都是**同一份数据抄了两份**（V1 §7.7 双源、
§7.11 三种命名），这一条不一样：**两处都是权威，且给出不同的答案**。
这种形状没有任何工具会红 —— 类型对、schema 对、测试各测各的（配置测试验
"字段默认值是这个顺序"，适配器测试验"实现走了另一个顺序"），
两边全绿。症状要等到某天画质掉了对比才发现。

**解法（`docs/adr/0011`）**：一句话分工 —— **顺序归契约，资源归配置**。
阶梯*顺序*只在 `Capabilities`（它是 `ClassVar`，注册表不实例化就能读，
这正是 ADR-0004 定它的原因）；配置与 V1 的 env 名只回答
"导出文件档用哪个路径 / 浏览器档用哪个浏览器"。于是 `ytdlp_cookie_priority` 删除。
同一批删掉的还有 `persist_play_url`：`MediaArtifact` / `VideoMeta` / `videos` 表里
**没有任何字段能存放播放直链**，也就是这个开关唯一的合法实现是什么都不做 ——
一个只有 `false` 能用的布尔不是配置，是"这块还没想清楚"的化石。

**判据**：加平台 / 加字段时问一句 —— *这个信息在别处是不是已经有一份权威了？*
两份都是权威就必须当场收口，别指望以后有人发现。

**看护**：`test_douyin_cookie_ladder_order_is_not_a_config_field`（结构看护：
`DouyinConfig` 里不许再出现带 `priority`/`order` 的字段名 ——
加一个这种字段**不会**让任何取值用例变红，只有这一条抓得到）＋
`TestCookieLadder::test_order_comes_from_capabilities_and_starts_with_the_exported_file`＋
`test_capabilities_and_the_config_defaults_agree_on_strategies`（策略字段两处必须一致）。

#### 经验 18 · 平台给不出的字段就让它明着是 None，别造一个"看起来能过滤"的假象（Task 6）

**现象**：`PlatformAdapter.list_creator_videos(since=...)` 是契约里写好的增量参数，
但抖音主页网格的卡片 DOM 上**只有 id / 标题 / 点赞数，没有发布时间**。

**两种走歪的方式**，方向相反但都贵：

1. 拿 `updated_at`、页面里的"2小时前"文案或时间戳猜测去凑 `published_at` ——
   凑出来的值会让 `since` **看起来在工作**，而它筛掉的其实是随机一部分。
   看板上的症状是"这个博主的更新莫名少了一半"，最难查的那类。
2. 因为凑不出来就把 `since` 实现成"没有发布时间就跳过" ——
   整轮作品一条都不进，而日志是干净的（**过滤器没错，是数据判不了**）。

**解法**：`published_at=None` 明着交出去，`since` 的口径写成
"判得了才跳过，判不了就放行"，并在 docstring 里点名"这一侧是弱过滤，
别把它当增量游标；真增量靠 `videos` 表按 `platform_video_id` 查重"（Task 8 的 handler 做）。
V1 走的本来就是第二条路（`store.video_exists(...)` 查重），所以这不是 V2 的退化，
是平台侧的限制被契约里一个看起来很正当的参数名掩盖住了。

**判据**：契约里每个"过滤 / 排序"参数都要能回答*这个平台靠什么字段实现它*；
答不出来就写进 docstring 并在测试里钉住"判不了时不许丢数据"。

**看护**：`test_since_cannot_drop_undated_cards`（判不了 → 放行，
＋ `test_a_real_publish_date_would_still_be_filtered`
（判得了 → 真判，否则上一条是句永不生效的空话）＋
`card_to_video_meta` 里 `duration_seconds=None` / `published_at=None` 的断言。

#### 经验 19 · 接口型适配器：先抓真响应再写解析层，凭印象编的 fixture 会让 bug 安静躲过去（Task 7）

**做法**（B站 适配器开工前）：`curl` 了四个公开接口，把回的东西裁一裁直接当 fixture — —
`x/web-interface/view`、`x/player/v2`、`x/web-interface/card` 都匿名可访问（`code:0`），
`yt-dlp --flat-playlist` 匿名那句 412 也存成真样本，`yt-dlp -J` 的 15 条 formats
证实了未合并分片是默认形状。拿不到的两样（非空 `subtitles[]` 与 flat-playlist 条目）
在文件里用 `_comment` 写明"合成"，并注明为什么合成、按什么口径合成。

**回报是当场可见的**：坑 22 那两个 bug（`data` 嵌套、非 dict 条目）都是
**第一次把真响应喂进解析层**时红的。凭印象编 fixture 的话它们一定躲过去 ——
我编的时候就会把它写成 `{"card": {...}}`，也就是我以为的形状。
这不是"测试写得好"，是**输入选对了**：测试只能证伪我想到要问的问题。

**顺带的三条纪律**：

1. fixture 里加一条 `TestRealFixturesAreActuallyReal`，断言"标了真样本的东西仍可解析"。
   它红了意味着**对面改了字段**，或当初那份其实是编的 —— 两种都要立刻知道。
2. 数值型字段**不要断言具体值**（粉丝数两次抓取就不一样）。断言形状与量级：
   `isinstance(fans, int) and fans > 10_000`。
3. 凭证与隐私：`player/v2` 的 `ip_info` 里是抓包机器的公网 IP，
   **进 fixture 之前删掉**；`data/cookies/*` 那种会话凭证永远不进。

**判据**：动任何"对面说了算"的形状（HTTP 接口、外部命令的 stdout、第三方文件格式）之前，
先问一次真的并把回答留下。设计文档能告诉你接口叫什么，不会告诉你它匿名能不能通。

**看护**：`tests/fixtures/bilibili/*` 每个文件的 `_comment` 与
`tests/contracts/test_bilibili_adapter.py::TestRealFixturesAreActuallyReal`。

### 实施阶段（V2.0）· Task 8（任务调度）

#### 坑 24 · 老坑 19（httpx 默认真传输 2.1 秒）在**测试替身**里又咬了一口（Task 8）

**现象**：Task 8 的 handler 测试第一版跑完 **37 秒 / 29 条**，每条均匀 ~2 秒。
不是逻辑慢，是 `make_deps()` 里 `httpx.AsyncClient()` 用了默认真传输 ——
每建一个就去碰一次系统证书存储（坑 19 记的是适配器测试，这次是运行器/handler 测试）。

**为什么又踩**：坑 19 的解法是"注入 MockTransport"，但那是记在**适配器**测试的上下文里的。
写 `TaskContext` 的假 `AdapterDeps` 时我没往那想，因为 handler 测试"根本不发请求" ——
可 httpx 的 2.1 秒发生在**构造期**，不在请求期。"这里不用网络"不等于"这里建客户端不要钱"。

**解法**：假 deps 一律 `httpx.AsyncClient(transport=httpx.MockTransport(...))`。
37 秒 → 4.7 秒，用例数不变。

**判据**：任何测试里 `new` 一个 `httpx.AsyncClient` 而没有注入 transport 的，都是坑 19 的复发点。
建客户端要钱这件事与"这段代码会不会发请求"无关。

#### 经验 20 · 调度层把计划里的"两个文件 / 一个字段表"收成了"一个函数 / 两个契约旗标"（Task 8）

三处"照计划抄会留下第二处真相"的地方，都当场改了并回写：

1. **collect 一份实现，两平台复用**。计划列了 `douyin_collect.py` + `bilibili_collect.py` 两个文件。
   两平台采集流程逐步骤相同、差异全在适配器里 —— 抄两份是四十行近似复制，
   正是 V1「B站 与抖音各写一份 cookie 阶梯，漂过一次」的形状。收成 `make_collect_handler(platform)`。
2. **`TaskContext` 补 `files`**。`platform-adapter.md §2.4` 说媒体路径归一化归 handler，
   但 `§2.3` 的 `TaskContext` 字段表里没有 `FileStorage`，而 `AdapterDeps` 也没有。
   不在这里补，handler 就只能自己再拼一遍 `data/` 路径 —— 那是 `FileStorage` 之外第二处路径真源。已回写 `§2.3`。
3. **`TaskDefinition` 补 `implemented`**。12 个任务登记、6 个实现。没有这个旗标，
   未实现的会进 `/api/tasks` 变成一个点了报 500 的按钮 —— 和"注册了没实现等于对前端撒谎"同判据。
   `task_is_available` 与跑前门都过这一关。已回写 `§2.2`。

**还有一条时序不是巧合**：`TaskRunner` 的 `task.finished` 事件**在 `manifest_writer` 出块之后**才发，
所以广播顺序是 `task.started → manifest.written → task.finished`。反过来发会得到
"前端收到完成、点进去清单 404"。清单是权威源，必须先于"我完成了"落盘 —— 看护在
`test_success_writes_terminal_run_and_events`（断言的就是这个事件顺序）。


### 实施阶段（V2.0）· Task 9（API 层）

#### 坑 25 · `PUT /config` 改完开关，关掉的平台照样能被采（Task 9）

**现象**：`available_task_names` 读 `ConfigManager`（活的），`/api/tasks` 立刻反映"抖音关了"；
但 `TaskScheduler._gate` 读的是它自己 `__init__` 时 `dict(configs)` 的快照，`registry.enabled_platforms()`
同理 —— 于是 `POST /tasks/douyin_collect/run` 照样放行，一个"在界面上关掉"的平台还能被采。
`reload_platform()` 只换 manager 内存里那份，**没有回流到这三个握快照的组件**。

**根因**：配置有三个运行期消费者（注册表 / 依赖袋 / 调度器），各自在装配时 snapshot 了一份，
`ConfigManager` 改的是自己那份。这是"两处真相"的又一种形态：**写入口只有一个（manager），
读入口却有四个**，改了写口没人通知读口。没有工具会红 —— 类型对、`/api/tasks` 也对，
只有"关掉还能采"这个行为错，而它藏在门控代码里。

**解法**：`AppState.apply_platform_config(name, cfg)` 是唯一"把新配置推到三处 + invalidate 实例"的入口。
lifespan 注册它为 `reload_platform` 的订阅者；`PUT` 路由 reload 后也显式调一次（测试不跑 lifespan 时对确定）。

**判据**：任何"改了权威源、但下游有缓存快照"的地方，都要有一个**单一**的"推送到所有快照"入口，
不能靠每个写路径各 remember 一部分。写用例时要**同时断言权威源和缓存快照都变了** ——
`test_put_config_persists_and_gates` 既看 `/api/tasks` 消失，也看 `scheduler._configs["douyin"].enabled`
变 False；只断前者就会把这条 bug 放回代码里而全绿。

#### 经验 21 · 绕开这版 starlette 的 `TestClient`（它要 `httpx2` 且 import 即弃用告警）

**现象**：想跑真 ASGI lifespan 做端到端，`from fastapi.testclient import TestClient` 当场
`ModuleNotFoundError: httpx2` → 再 `StarletteDeprecationWarning`，在 `filterwarnings=["error"]` 下直接红。

**处置**：API 测试不跑 lifespan —— fixture 里手动 `storage.initialize()` + 预置平台镜像行 +
`httpx.ASGITransport` 打路由（lifespan 的活单独在 `test_app_lifespan` 用直接调
`build_components` / `_sync_platform_mirror` 覆盖）。**且写配置的用例一律走 tmp 目录那份
`platforms.yaml`**，绝不 `PUT` 到仓库 `config/`（那会真改文件）。

**顺带**：SSE 生成器**先 subscribe 再 replay** —— 原写法 replay 在前，消费者在 replay 阶段
`aclose()` 会让 `finally` 引用一个还没绑定的 `subscription`（`NameError`）。顺序调正后 `finally`
一定拿得到句柄，还顺手补上"追历史期间刚好跑完"那条 live 事件。

### 实施阶段（V2.0）· Task 14（契约测试基类）

#### 经验 22 · "抽公共基类"不等于"把两套大测试拆开重接"

**计划说的**：`docs/plans` line 466 —— "Task 14 抽基类时把抖音那 91 条里通用的部分上移"。
照字面做 = 把 `test_douyin_adapter.py`(1181 行) / `test_bilibili_adapter.py`(1114 行) 拆开，
通用用例上移进基类，两套大测试改成继承。**风险**：200+ 条正在绿的用例重接一遍，拼错就是几百红；
**收益**：上移后断言内容一个字没多（通用契约本来两套里各有一份）。

**实际做法**：基类照样交付（`PlatformAdapterContractTests`，是 V3 的可复用资产），
但**不动**两套大测试 —— 加两个薄子类复用它们已有的 `make_adapter`/`make_video` helper，
让真适配器过一遍通用契约。重复的只是**测试代码**（不是行为真源），且
`test_contract_guard_index.py` 盯着"每条 §7 点名的看护用例还在"。

**判据**：计划里的"上移/重构测试"是**意图**（要一个可复用的契约基类），不是唯一实现路径。
当照抄会拿"几百条绿用例的风险"去换一个"内容不变"的重排时，交付同一个基类、换一条不碰既有测试的路。
—— 与经验 7（"计划里的代码是意图不是成品"）同源，只是这次意图藏在一句"届时把通用部分上移"里。


### 实施阶段（V2.0）· V2.0 审查轮（Task 15 救活 + 契约回写）

> 触发方式：Task 10-13 开工前先审已完成的一半（4 个切片并行评审 + 门禁复跑）。
> 这一节的四条都来自"绿灯底下查出来的东西"，所以每条都写了**它当时为什么是绿的**。

#### 经验 23 · fixture 的 DDL 忠实不够，**值**也必须来自源系统的真写入路径

**现象**：Task 15 交付时写的是"按 V1 `local_store.py` 真实 schema 造的库验过行为"。
本轮把 fixture 的取值换成 V1 真会写的那一种（`normalize_platform()` 的出口是显示名 `抖音`，
不是 slug `douyin`），迁移立刻在**第一条博主**上 `StorageError: FOREIGN KEY constraint failed`。

**根因**：DDL 是逐字抄的（这一点当时的评审也复核过、确实没问题），但**值是照着被测代码的
假设写的**。而 `creators.platform` 是 `ForeignKey("platforms.name")`、V2 那边存 slug ——
于是"翻译平台词汇"这件迁移**唯一真正要做的功能**，恰好是那份 fixture 唯一测不到的一件事。
结构忠实 + 取值同源于被测代码 = 一份自证的空壳。

**解法**：`V1_PLATFORM_TO_SLUG` 一处词汇表；`_creator_draft()` / `_video_target()` 认不出就
记一条带原值的错误并**跳过该行**（不猜平台、也不替它建 `platforms` 镜像行 —— 那会给下次
启动的 `prune_unknown` 埋雷）；`platforms` 镜像由脚本自己补，`enabled=False`
（开关的权威源是 `platforms.yaml`，迁移不许顺手打开任何平台）。

**判据**：为**第三方系统**造 fixture 时，每个字段的取值都要能指出"源系统里哪一行代码会写出
这个值"。指不出来的，就是照着自己代码的假设编的。测试模块 docstring 里那张
`抄 V1 xxx.py:NN` 的表就是这个用处，并且因此**禁止**从被测模块 import 那些常量
（那等于用被测代码的假设验证被测代码）。

**看护**：`tests/integration/test_migrate_from_v1.py::test_v1_display_names_land_as_v2_slugs`
+ `test_migration_seeds_the_platform_rows_it_needs`。本文件的 fixture 模块级 storage
刻意**不预置** `platforms` 行（`tests/conftest.py` 那个会预置，一预置就替脚本把 FK 前置
条件做完了）。

#### 经验 24 · 注释与 spec 里承诺的防护，用之前先 grep 一遍

**现象**：这一轮 grep 出四处"文档说有、代码里没有"的防护 ——

| 位置 | 承诺 | 实际 |
|---|---|---|
| `infra/cdp_bridge.py:85` | 「预检会把非回环报成 degraded」 | 全 `src/` 没有这个检查；`tasks/preflight.py` 一次都没提桥 |
| `task-runner.md §2.2` | 「调度器跑前检查 requires，缺了直接拒」 | `_gate_platforms()` 只看 `implemented` + `enabled` |
| `storage/files.py:338` | 「任务结束时整个删掉」 | 没有任何代码路径删它 |
| `AGENTS.md §3` / `config-schema.md §6` 等四处 | `INTELLIGENCE_HUB_DATA_DIR` 能挪数据根 | 静默无效（生效的是 `..._DATA__DIR`） |

**根因**：这些句子写的是**打算**，交付之后读起来像**已经做了**。V1 §7.20 那一族的形状
就是"绿的是看板，不是机器"，只不过这次的看板是文档。

**解法**：本轮补了 workdir 清理与数据根 env 别名；`requires` 与桥回环**没有**补实现，
而是把"目前不生效 + 为什么 + 落点"写回 spec（`task-runner.md §2.2` 的实施期现状块），
宁可让文档承认缺一块，也不留一句会让人据此下结论的假话。

**判据**：引用某处防护的注释/spec，必须能指出实现它的那一行；指不出来的改成"应当"或删掉。
反向同理：**改了行为要回头删掉那句承诺**（本轮 `_maybe_attach_transcript` 就顺手删了
`dry_run` 分支里已经走不到的记账代码）。

**看护**：（当时欠的两条已补，2026-09-23）桥回环 = `tests/unit/infra/test_cdp_bridge.py::test_non_loopback_bridge_urls_are_rejected_at_construction`（参数化含 `127.0.0.1.evil.com` 这种要 DNS 才看得出来的写法）；`prune_unknown` 与 UPDATE 路径的翻译 = `tests/unit/storage/test_integrity_translation.py`（memory / file 双后端各跑一遍，因为翻译上提到了 `BaseRepository._scope()`）。

#### 经验 25 · 计划表的漏报，和虚报一样贵

**现象**：`--dry-run` 打印「未写任何东西」的同时，实测在目标目录建出
`intelligence_hub.sqlite3` + `media/ manifests/ cookies/ logs/ tmp/`。另一处它报
`hidden墓碑=0`，而真 V1 库上确实有 2 条墓碑会命中 —— 因为"隐藏"那一步在 `dry_run`
分支里被跳过了，只有写库那条路径会给 `report.hidden` 加一。

**根因**：预演的输出是**给人决定要不要按回车看的**。两个方向的错都会要命：虚报让人以为
会做更多，漏报让人以为这次不会碰任何东西 —— 而"被删过的作品会不会复活"恰好是那个人
唯一关心的问题。

**解法**：dry-run 不再 `ensure_dirs()`、用内存库（那条路径根本不碰 storage）；
所有投影项（videos / transcripts / hidden）统一走 `_project_dry_run()`，与真跑共用
`_transcript_worth_moving()` / `_tombstone_hit()` 两个谓词。

**判据**：dry-run 报告的每一项，要么与真跑由**同一处代码**算出来，要么有独立用例钉住
"预演值 == 真跑值"。断言"库里行数为 0"证明不了"磁盘没被动过"。

**看护**：`test_dry_run_creates_no_database_and_no_directories`（扫目标树）、
`test_dry_run_projects_the_tombstones_it_would_apply`。

#### 经验 26 · 缺 `py.typed` 让"四关全绿"只对 `src/` 成立

**现象**：`uv run mypy tools` 一跑就是 6 条 `import-untyped`（包没带 PEP 561 标记），
于是 `tools/migrate_from_v1.py` 那 400+ 行**从来不在类型门禁里**。补上标记后立刻暴露
两处真问题：`VideoDraft(**metrics)` 的字典展开会让 mypy 放弃校验该构造调用的**其余**参数，
以及两个跨循环复用的变量名（`draft`、`created`）在两个循环里类型不同。

**根因**：门禁的目标清单（`mypy src`）与"这次交付了什么"（`tools/` 是交付物）不同步；
而 `make lint-python` 里 ruff 带 `tools`、mypy 不带 —— 看命令的人以为两边都过了。

**解法**：加 `src/intelligence_hub_v2/py.typed`；`mypy src tools` 两条一起跑；
计数列改成逐个显式传参。

**判据**：每加一个**可执行交付物**（`tools/`、下一轮要移植的 `cdp_bridge_server.py`），
同时把它加进 mypy 与 coverage 的目标清单。否则它的"绿"是没测过的绿。

**看护**：门禁本身（`Makefile:lint-python` 与 `ci.yml` 都要含 `mypy src tools`）。
注意 `tools/` 仍不在 coverage 的 `source` 里 —— 那一条还没做。

**已做完**（第二轮）：桥回环校验、`IntegrityError` 上提到 `_scope()`、抖音 §7.21 回归、
三条不设防的用例、`platforms.enabled` 镜像同步、`ci.yml` 解锁、`tools/` 进 mypy。
下面这几条仍然欠着：

1. `_check_requires` 落地（读一次 preflight 的结论，不要再开第二套探测真源）。
2. `ConfigManager.write_platform_config` 未 `load()` 时会抹掉其他平台段（`config.py:599-608`），
   以及首次 PUT 抹掉 `platforms.yaml` 的 96 行注释。
3. 6 个"前端能渲染、后端零代码路径"的配置字段 —— 删或实现都要走 ADR（`PlatformConfig` 是冻结契约）。
4. `tools/` 进 coverage 的 `source`（`mypy` 已进，覆盖率还没）。
5. 契约测试基类补 `contract-tests.md §4` 点名的三条通用用例（`capabilities_match_expected` /
   `list_creator_videos` 必填字段 / 兜底带 `source`+`error`），或按 ADR 改 §4。
6. SSE 真机断连验证；DNS 重绑定要不要设 Host 白名单（要一个决定 + 一段 ADR）。

---

### 实施阶段（V2.0）· 审查轮 第二组（门禁真跑起来才看见的东西）

> 做法：把上面那份"还欠的"按会不会咬到人排序，逐条 TDD 修。
> 这一组最值钱的不是修了什么，而是**第一次把门禁本身跑通**之后露出来的四件事。

#### 经验 27 · 门禁的工具版本要与解析出来的工具同版本，否则它是常红而没人看见

**现象**：`pre-commit` 把 ruff 钉在 `v0.7.4`，项目解析到 `0.16.8`。0.7.x 认不得本项目
`select` 里的 `TC001`，于是 `ruff check` 与 `ruff format` 两个 hook **每次 exit 2**，
报的还是"TOML parse error at pyproject.toml line 103" —— 一句跟代码无关的错。
`name-tests-test` 也一直红（它要求测试目录下每个 `.py` 都叫 `test_*.py`，于是
`tests/contracts/_doubles.py` 这套共享替身被判"命名错误"）；`detect-secrets` 更是一直红 ——
`--baseline .secrets.baseline` 指向一个从来没存在过的文件。

**根因**：CI 的 `lint` job 内容是 `npm --prefix frontend ci` + `pre-commit`，而前端还没开工
→ 这个 job **从没跑到过 ruff 那一步**。一个从没跑过的 job 会一直保持它第一天红着的形状。

**解法**：hook rev 顶到 `v0.16.8`、`pyproject` 下限也顶到 `ruff>=0.16.8`（两边不许漂）；
生成 `.secrets.baseline`（实测 0 条候选）；命名 hook 加 `exclude: '(^|/)(_|conftest\.py)'`；
`ci.yml` 的 `lint` 拆成 `lint-python`（Python 四道，不碰 node）与 `precommit`，
`test-backend: needs: lint-python`。

**判据**：**任何门禁都要在本地完整跑过一次并亲眼看到 exit 0**，"配好了"不算配好了。

**看护**：`uv run pre-commit run --all-files` 本轮首次 exit=0；`make ci-local` 已与 CI 的
后端那一半对齐（补上了它以前不跑的覆盖率门禁与 alembic 往返）。

#### 经验 28 · 只在"整仓一起跑"时成立的断言，是一会说谎的看护

**现象**：把 §7 看护索引改成"比对本次运行收集到的 nodeid"之后，单跑
`pytest tests/contracts/test_contract_guard_index.py` 立刻红 —— 那一刻 session 里只有
这个文件的 20 个用例。

**根因**：收集范围随调用方式变，我把它当成了全局事实。这类看护平时绿、单跑红，
正是"看板上是绿的"那个病形状的镜像。

**解法**：改用 AST 看目标文件：函数在不在、外层类叫不叫 `Test*`、有没有
`skip/skipif/xfail`、body 里有没有一条**非恒真**断言。这四条都是
"名字还在但它已经不跑了"的真实形态，且与运行范围无关。

**判据**：看护不许依赖"别人也会一起跑"这个前提；写完**单独跑一次那个文件**。

**看护**：`test_every_mapped_guard_will_actually_run`。实测把 §7.4 的一条指向改成
只存在于另一个文件的名字，它会响（`… 里没有 xxx（改名或删掉了）`）。
**它仍然抓不到的**：把断言写成逻辑恒真但"看起来像真断言"的空壳 —— 那要靠变异验证。

#### 经验 29 · ruff ≥0.16 会格式化 Markdown 里的 Python 围栏

**现象**：第一次完整跑 pre-commit，`ruff format` 报"14 files reformatted"，其中 6 份是
`docs/adr/*.md` 与 `docs/plans/v2.0-implementation.md` —— 文档里的 Python 代码样例被按
100 列重排、`import` 拆成一行一个、类 docstring 后补空行。本地那条
`ruff format --check src tests tools` 根本碰不到这些文件，所以这事只在 CI 这一侧发生。

**根因**：ruff 现在接受任意文本文件并把 ```python 围栏当代码；pre-commit 的 ruff hook
默认按 `types: [text]` 粗筛，于是 `docs/` 被喂了进去。而计划文件是**历史事实**，
重排它等于改记录。

**解法**：两个 ruff hook 都加 `files: \.pyi?$`，把本地门禁与 pre-commit 的范围钉成同一份。

**判据**：**改写型 hook 的作用范围必须与本地等价命令逐字相同**；不一样就迟早出现
"本地绿 / CI 重排了我的文档"。

**看护**：`pre-commit run --all-files` 后 `git status docs/` 不再冒出无关改动（本轮实测如此）。

#### 经验 30 · 生成物要程序自己写文件，不能吃 shell 重定向

**现象**：`python -c "print(json.dumps(...))" > openapi-snapshot.json` 在 Git Bash 下落的是
**GBK** 字节（回头 `json.load` 报 `UnicodeDecodeError: 0xd0`）；换成 Python 内部写文件之后
又是 **CRLF**（`write_text` 在 Windows 翻译 `\n`），于是 `mixed-line-ending` 每次来擦。
CI 在 Linux 上生成的是 LF —— 而快照比对的全部意义就是两边字节一致。
同一类问题还有一次：临时库路径我写了 Git Bash 的 `/tmp`，本机原生 Python 打不开，
症状是一句看不懂的 `unable to open database file`，`make db-roundtrip` 第一版死在这。

**解法**：统一 `pathlib.Path(...).write_text(doc + chr(10), encoding='utf-8', newline=chr(10))`
（Makefile 与 `ci.yml` 同一段）；临时路径一律问 Python 要（`tempfile.gettempdir()`）。

**判据**：跨平台产物显式 `encoding=` + `newline=`；路径由被调语言自己解析，不要借 shell 的视图。

**看护**：实测两次生成逐字节一致、与提交的快照一致，文件 `CRLF: 0 / LF: 2122`。

#### 经验 31 · 测试全注入 `state=`，等于没测生产启动路径

**现象**：`create_app()` 不带 `state=` 时，`build_components` 里
`InProcessEventBus(events=storage.events)` 在 `storage.initialize()` **之前**取仓库句柄，
抛 `SqliteStorage 还没 initialize()`。而 `cli()` 用 uvicorn factory 模式指的就是
`create_app` —— 也就是说 `uv run intelligence-hub`（验收判据 2）**根本起不来**。
`test_app_lifespan.py` 那条"装配"用例是先 `await storage.initialize()` 再
`build_components(...)`，所以这条路一次都没被覆盖。

**根因**：Repository 只握"取 session 的工厂"，构造它不需要连接池 —— 门放错了位置：
放在"拿句柄"上，等于要求调用方先异步初始化才能装配，而"工厂在启动前构造 app"正是生产形态。
真正的门在第一次查询（`_require_sessionmaker`，那条文案还顺带盖住 `close()`）。

**解法**：`_Repositories` 在 `SqliteStorage.__init__` 建好，`initialize()` / `close()` 不再动它；
两条断言"属性本身就抛"的老用例按新语义改写（意图保留：**不许拿到半初始化的东西静默凑合**，
只是判据从"访问属性"挪到"发查询"）。

**判据**：**生产入口本身要有一条用例走一次**（`create_app(config_dir=tmp)` + `app.openapi()`）。
凡是"测试注入 X、生产自己造 X"的形状，两边构造顺序不一致时只有生产会炸。

**看护**：`test_create_app_boots_without_an_injected_state` 与
`test_accessors_resolve_before_initialize_but_queries_do_not`（都是先红后绿）。

---

#### 经验 32 · "有没有人读这个字段"的判据，会被一句注释满足

**现象**：写 ADR-0012 那条守卫（每个配置字段要么有人读、要么标 `ui:hidden`）时，
判据是"`.字段名` 出现在 `src/` 的文本里"。跑出来的名单**少了** `bilibili.list_strategy` ——
而 `BilibiliAdapter._enumerate` 其实完全不按它选路（看的是 `external_browser_manifest_path`
给没给）。它在 `platforms/bilibili/listing.py` 的**模块 docstring** 里被"读"到了一次：
「走哪条由 `capabilities.list_strategy` 与配置决定」。

同一轮里还有一个反向的自伤：`_SRC` 的相对层级写成 `parents[2]`（指进了 `tests/`），
语料变成空串，于是 14 个字段变成"全部字段都是死的"。

**根因**：这两条是同一件事的两面 —— **守卫的判据是文本，而代码里最像"读取路径"的文本是注释**。
一个只会狂报红的守卫和红一次就被 `# noqa` 掉的守卫最后效果一样；
一个能被散文满足的守卫则稳定地把最要命的那类谎言（说明与行为不符）放过去。

**解法**：判据走 AST —— 每个文件 `ast.parse`，把裸字符串语句换成 `None`，再 `ast.unparse`
（注释与 docstring 同时消失），然后才在剩下的纯代码上匹配属性访问。
语料另有一处要按平台切：`retry_max` 在 B站 被读一次不等于抖音那个也有人读。

**判据**：写"某样东西有没有被使用/被实现"这类守卫时，先问**散文能不能满足它**。
能，就先补一个"把散文剥掉"的步骤，再谈名单。
并且：**新写一条守卫，第一件事是让它对一个已知的真死字段变红**（这条是靠
`bilibili.list_strategy` 现场发现的，不是靠想）。

**看护**：`tests/unit/platforms/test_config_fields_have_readers.py` 四条（先红后绿）。

---

#### 经验 33 · 子类重新声明字段，会静默丢掉父类的 schema 标记和 description

**现象**：打算"在 `PlatformConfig` 基类标一次 `ui:hidden`，四个平台都生效"。
实测（`docs/adr/0012` 的「背景」第 3 条）：子类写 `use_cdp_bridge: bool = False`
之后 `C.model_fields['use_cdp_bridge'].json_schema_extra is None`、`.description is None` ——
两个都丢，而且丢得很安静：JSON Schema 里就是没有那个键，不看 schema 没人知道。

**根因**：重新标注等于换一个全新的 `FieldInfo`，Pydantic 不从父类合并 `metadata`
/ `json_schema_extra` / `description`。而本仓库每个平台的 `config.py`
**就是要重新声明**这些字段（把 `MediaStrategy` 收窄成单值 `Literal`、改默认值），
所以"在基类标"这个写法在这份代码里恰好必然失效。

**解法**：标记一律打在**重新声明的那一处**（`DouyinConfig` / `BilibiliConfig` 自己），
并把"`ui:advanced` 在每个平台的 `advanced` 上真的存在"写成一条独立断言 ——
它就是专门盯这个失效模式的，因为 `advanced` 每个平台都重新声明。

**判据**：任何"契约键靠 Pydantic 元数据带出去"的东西（`description`、`json_schema_extra`、
后续可能的 `examples`），都要**从最终子类的 schema 里查**，不是从定义处查。
定义处看到的那句，可能在真实 schema 里根本不存在。同理：**紧跟赋值的 docstring 不进 schema**，
只有 `Field(description=...)` 会 —— 所以对前端可见的解释只有一种写法。

**看护**：同上文件的 `test_advanced_group_carries_the_collapse_marker` 与
`test_hidden_fields_explain_themselves`（后者读的正是 schema 可见的 `field.description`）。

---

#### 经验 34 · 把 `Test*` 类 import 进另一个测试文件，等于把整套用例再跑一遍（Task 14 收口）

**现象**：给 `contract-tests.md §4` 补看护时算测试数目对不上，于是逐目录 `--collect-only` 数。
`tests/contracts/test_contract_guard_index.py` 收集出 **28 个 item**，而这个文件自己只写了
6 条测试 —— 另外 22 个是 `TestDouyinContract` / `TestBilibiliContract` 的用例被**第二次**收集
（11 条 ×2）。改成只 import 模块之后：本文件 7 个 item（6 + 新增那条），
`tests/contracts` 目录的收集数从 **310 掉到 289**，一条用例都不少，只是不再跑两遍。

**根因**：那两行 `from tests.contracts.test_platform_adapter import TestDouyinContract, ...`。
pytest 的模块收集器看的是**模块命名空间里以 `Test` 开头的类**，导入进来的与定义在本文件的
一视同仁。所以"为了断言 `issubclass(...)` 而把类 import 过来"这个看起来很正常的写法，
效果是整套契约每次全跑被跑两遍，而且第二遍挂在索引看护这个文件名下 ——
失败时 pytest 报的是 `test_contract_guard_index.py::...::test_healthcheck_...`，
读的人会去查那个文件，而它跟这条用例毫无关系。

**解法**：只 `import <module> as _abc`，用 `_abc.TestDouyinContract` 访问；
类名不进本模块命名空间。`PlatformAdapterContractTests` 不以 `Test` 开头，
按名字 import 无害，留着是为了读 `_abc_members()` 时不绕。

**判据**：**测试文件之间不要按名字 import `Test*` 类**，要复用就 import 模块。
凡"数目对不上"都要当场算平 —— 这次如果按"多了几个就认了"处理，
这 21 个重复收集会一直留在 CI 时间里，而且下次谁看失败信息都会找错文件。

**看护**：`test_this_module_binds_no_test_classes`（AST 查本文件模块级有没有绑 `Test*` 名字，
并**先断言 `_abc` 里确实有两个可被重复收集的类** —— 否则这条绿灯可能只是没东西可查）。

---

#### 经验 36 · `isPending` 不是"马上就出来"：窗口不在前台时 react-query 会挂起补发（Task 12）

**现象**：`vite preview` + in-app browser（没有可见表面）里，侧栏平台块停在"读取中…"，
而控制台明明有两次 `404 /api/platforms`。同一份代码在 jsdom 里 1 秒后就变成
"读不到：配置层还没起来"。我第一版写下的结论是"大概率是重试窗口 + 隐藏标签页 timer 节流" ——
**那是猜的，而且猜错了一半**，所以这条经验连那次错判一起记。

**量到的事实**（一条条在页面上读出来的，不是推的）：
`navigator.onLine === true`、`document.visibilityState === "hidden"`、
`/api/preflight` 发出去了 1 次并返回 404、之后没有第 2 次。
把 react-query 的状态打出来是 `isPending: true` + **`isPaused: true`** ——
`fetchStatus` 停在 `paused`，因为 focusManager 认为窗口没聚焦，**补发被挂起**。

**根因**：`isPending` 一个布尔被界面同时用来表达三件事 —— "还没开始"、"正在飞"、
"被挂起，不会自己动"。第三种在无人值守的界面里最阴：它看起来像在等结果，
其实永远等不到，而那块区域下面什么都不显示。

**解法**：界面分开三态。`Sidebar` 与 `Preflight` 都加了 `isPaused` 分支，
文案说清"第一次请求已经发出去、重试要等页面回到前台"，并明确写
**"这一栏不是环境没问题"**。`isPending && !isPaused` 才是"读取中"。

**判据**：**任何"读数据"的界面都要问 `isPaused`**，不能只看 `isPending` / `isError`。
另外：一条 UI 结论必须由页面自己说话（读 `innerText` / `getComputedStyle`）来定案 ——
拿"我以为的浏览器行为"当结论，就是这条经验原来的样子。

**看护**：`src/components/shared/layout.spec.tsx` 的
`补发被挂起时说「被暂停」，不说「读取中…」`（用 `onlineManager.setOnline(false)` 造挂起态）
与 `src/pages/preflight.spec.tsx` 的
`补发被挂起时说「被暂停」，既不说正在探测也不说全绿`。

---

#### 经验 35 · 契约文档里的代码样例也得能跑：`EventSource.onmessage` 一帧都收不到（Task 11）

**现象**：`docs/specs/event-schema.md §7` 给的前端样例是
`es.onmessage = (e) => setEvents((prev) => [...prev, JSON.parse(e.data)])`。
照它写实现，界面上的事件流会**永远是空的**，而且不报错：连接是开的、后端在发、
`onmessage` 一次都不触发。

**根因**：`EventSource` 只把**没有** `event:` 字段的帧交给 `onmessage`。
而 V2 后端每一帧都带 `event: <type>`（`api/v1/events.py` 里
`return {"event": event.type.value, "data": ...}`）。具名事件必须逐个
`addEventListener(type, …)`。同一段样例里还有第二处不成立：
"自动重连后从 `since=` 拉回放" —— `EventSource` 的自动重连只会复用**同一个 URL**，
不会补任何查询参数，所以 `since` 必须由前端自己重开连接时带上。

**为什么会写成这样**：样例是**在实现之前**写进契约文档的，而且从来没被执行过一次。
文档里的代码比文档里的散文更容易骗人 —— 它看起来已经是实现，读者（和抄它的人）
不会再去验证。（同一族的另一例：`§4` 那份设想中的抽象基类，见经验 34 附近那几条。）

**解法**：`src/events/useTaskEvents.ts` 按 `types` 全集逐个注册监听器，自己管重连
（退避 + 带 `since=<最后一条已收到事件的时间戳>`），并把 `status` / `error` 交回界面；
`§7` 那段样例改写成能跑的形状，并注明"为什么原来那行收不到东西"。

**判据**：**契约文档里的代码样例，要么被执行过，要么就标成"形状示意"**。
写样例的人负责给它配一条用例 —— 这次配的是"具名帧进得来 + 未命名帧进不来"两条断言，
后者专门钉住"为什么不能只挂 `onmessage`"，否则下一个人还会觉得 `onmessage` 更简单。

**看护**：`src/events/useTaskEvents.spec.ts`（9 条：逐类型注册、首连不带 `since` /
重连带、断线时 `status=reconnecting` 且 `error` 有人看见、没有 `EventSource` 时
`offline` 而不是静默、`types` 决定注册集合与 query、缓冲区有界 500、卸载后不再重连，
外加 `EVENT_TYPES` 与 Python `EventType` 一字不差 —— 漏一个名字的症状就是那种事件静默收不到）。

---

## 附录 · 如何新增一条经验

1. 在对应部分（V1 §7 映射 / V2 设计 / V2 实施）新增一节。
2. 格式：**现象** → **根因** → **解法** → **判据** → **看护**。
3. 如果是 V1 §7 的陷阱，更新 `docs/specs/contract-tests.md` 的映射表。
4. 如果是结构性消除，说明"为什么设计上不可能再犯"。
5. 如果是契约测试看护，给出测试名 + 层级 + 文件。
6. 提交时在 commit message 里引用本文件的章节（如 `docs(lessons): add V2 implementation lesson #3`）。
