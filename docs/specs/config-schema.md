# Spec: Config Schema

> **状态**：Locked（V2.0 起契约稳定，改动需走 ADR）
> **源文件**：`src/intelligence_hub_v2/core/config.py` + `src/intelligence_hub_v2/platforms/<name>/config.py`
> **相关 ADR**：[0007](../adr/0007-config-layer.md)

配置层的 schema。前端配置面板按 JSON Schema 自动渲染表单，**V3 重写时这份 schema 是契约**。

---

## 1. 加载优先级

低 → 高：

1. Pydantic Settings 内置默认值
2. `config/app.yaml`（全局）
3. `config/platforms.yaml`（每个平台）
4. 环境变量 `INTELLIGENCE_HUB_*`（嵌套用 `__` 分隔，如 `INTELLIGENCE_HUB_APP__PORT=9000`）
5. CLI 参数（`--config-dir` / `--data-dir` / `--port` / `--platforms-enable douyin,bilibili`）

> **实施期修订（2026-09-22，Task 2）**，四条，详见 `docs/lessons.md` 坑 6 / 坑 7：
>
> 1. **YAML 不是通过构造函数传的**。计划里写的 `AppConfig(_yaml_file=path)` 不工作 ——
>    Pydantic v2 的 `__init__` 会**静默丢弃**下划线开头的私有属性，不报错。
>    实现改成 `ContextVar` + 自定义 `PydanticBaseSettingsSource`，公开入口是
>    `load_app_config(yaml_path=None, **cli_overrides) -> AppConfig`。
> 2. **`settings_customise_sources` 返回的元组顺序即优先级，第一个最高**：
>    `(init_settings, env_settings, _YamlDictSource)`。`dotenv` / `file_secret` 两个 source
>    故意不启用 —— 配置来源只有上面这五层，多一层就多一种排查成本。
> 3. **裸 `AppConfig()` 不读盘**，这是**故意的**（测试隔离，也避免 V1 §7.12 那类
>    "行为取决于 cwd"的坑）。要读盘必须走 `load_app_config()` 或 `ConfigManager.load()`。
>    看护：`test_bare_app_config_does_not_read_yaml`。
> 4. **`platforms.yaml` 是扁平结构**：顶层键就是平台名，**没有** `platforms:` 外层、
>    **没有** `defaults:` 块（设计阶段的示例文件是嵌套的，与 schema 不兼容，已改）。
>    平台级公共默认值由 `PlatformConfig` 的 Pydantic 字段默认值承担 —— 默认值只能有一处
>    （V1 §7.24 的同一条纪律）。原 `defaults:` 里的运行时项升格成 `app.yaml` 的真实字段：
>    `app.show_disabled_platform_history`、`scheduler.health_check_on_startup`、
>    `scheduler.health_check_interval_seconds`、`scheduler.task_timeout_seconds`。
>    注册表里没有实现的平台（`xiaohongshu` / `youtube`）在 YAML 里**整段注释掉**，
>    不留 `enabled: false` —— 出现了却没有实现等于对读者撒谎；
>    `_load_platforms()` 对未注册的顶层键**硬失败**（`ConfigError`）。

---

## 2. `AppConfig`

```python
from pydantic import BaseModel, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Literal


class AppSection(BaseModel):
    name: str = "Intelligence Hub V2"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    """只绑回环（V1 §1.2 硬约束）。改成 0.0.0.0 启动时打红色警告。"""
    port: int = Field(default=8789, ge=1, le=65535)
    cors_origins: list[str] = []
    openapi_url: str = "/openapi.json"
    docs_url: str | None = "/api/docs"
    redoc_url: str | None = None


class DataSection(BaseModel):
    dir: Path = Path("data")
    media_subdir: str = "media"
    manifests_subdir: str = "manifests"
    cookies_subdir: str = "cookies"
    logs_subdir: str = "logs"


class StorageSection(BaseModel):
    sqlite_file: str = "intelligence_hub.sqlite3"
    wal_mode: bool = True
    busy_timeout_ms: int = Field(default=5000, ge=100)
    event_retention_days: int = Field(default=30, ge=1)


class LoggingSection(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"
    file: str = "server.log"
    rotate_max_bytes: int = Field(default=52428800, ge=1048576)
    rotate_backup_count: int = Field(default=5, ge=1)


class SchedulerSection(BaseModel):
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    max_concurrent_per_platform: int = Field(default=2, ge=1)
    max_concurrent_global: int = Field(default=4, ge=1)

    # --- 定时采集（ADR-0017，2026-09-24 加）---------------------------------
    collect_cron: str | None = None       # 五段 cron；None = 不排任何采集
    collect_platforms: list[str] = []     # 空 = 所有**已启用**的平台
    collect_limit: int | None = Field(default=None, ge=1)  # None = 用平台的 videos_per_creator


> **定时采集那三键的形状校验分两层**（ADR-0017）：`core/config.py` 只验"五段 + 字符集"，
> 语义（`99 99 * * *` 这种越界值）由 `main._add_collect_jobs` 里的 `CronTrigger.from_crontab`
> 拒收并抛 `ConfigError`。**默认是 None**：装了 V2 的人不该在没同意的情况下
> 让服务定时拿他的登录态去动平台配额。`collect_platforms` 的名字按
> `PLATFORM_CONFIG_SCHEMAS` 校验（写错平台名 = 那个平台永远不被定时采集，而库里看不出来）。

class BridgeSection(BaseModel):
    url: HttpUrl = HttpUrl("http://127.0.0.1:3457")
    enabled: bool = True
    health_check_interval_seconds: int = Field(default=60, ge=10)
    restart_cooldown_seconds: int = Field(default=30, ge=5)


class AsrSection(BaseModel):
    engine: Literal["sherpa_sense_voice", "mlx_whisper", "faster_whisper"] = "sherpa_sense_voice"
    model_dir: Path | None = None
    num_threads: int = Field(default=4, ge=1)
    device: Literal["cpu", "cuda", "mps"] = "cpu"


class HttpSection(BaseModel):
    timeout_seconds: int = Field(default=30, ge=1)
    follow_redirects: bool = True
    max_retries: int = Field(default=3, ge=0)
    user_agent: str = "Mozilla/5.0 ..."


class PathsSection(BaseModel):
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    node: Path | None = None
    yt_dlp: Path | None = None


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INTELLIGENCE_HUB_",
        env_nested_delimiter="__",
        yaml_file="config/app.yaml",
        extra="ignore",
    )

    app: AppSection = AppSection()
    data: DataSection = DataSection()
    storage: StorageSection = StorageSection()
    logging: LoggingSection = LoggingSection()
    scheduler: SchedulerSection = SchedulerSection()
    cdp_bridge: BridgeSection = BridgeSection()
    asr: AsrSection = AsrSection()
    http_client: HttpSection = HttpSection()
    paths: PathsSection = PathsSection()
```

---

## 3. `PlatformConfig` 基类

```python
class RateLimitConfig(BaseModel):
    per_minute: int = Field(default=30, ge=1)
    per_creator_seconds: float = Field(default=1.0, ge=0.0)


class PlatformConfig(BaseModel):
    """所有平台配置的基类。每个平台继承它，加平台特有字段。"""

    enabled: bool = True
    """前端配置面板的开关。关掉 = 该平台所有任务自动禁用。"""

    display_name: str
    """人类可读名称。"""

    cookies_file: Path | None = None
    """Netscape 格式 cookie 文件路径。capabilities.needs_cookies=True 时必填。"""

    use_cdp_bridge: bool = False
    """是否走 CDP 桥。capabilities.needs_browser=True 时必须 True。"""

    media_strategy: Literal["yt_dlp", "page_play_url", "yt_dlp_with_fallback"]
    list_strategy: Literal["api", "browser_scroll", "yt_dlp_flat", "external_manifest"]

    videos_per_creator: int = Field(default=30, ge=1, le=200)

    rate_limit: RateLimitConfig = RateLimitConfig()

    advanced: dict[str, Any] = Field(default_factory=dict)
    """平台特有字段，前端默认折叠（用 ui:advanced 标记）。"""
```

> **实施期修订（2026-09-22，Task 2）**：`advanced` 的注解从 `dict[str, Any]` 改成 `Any`
> （默认值仍是 `default_factory=dict`）。原因是子类会把它**覆盖成强类型 model**
> （`DouyinAdvanced` / `BilibiliAdvanced`），而 Pydantic model 不是 `dict` 的子类型，
> mypy strict 会拒绝这个覆盖。放宽到 `Any` 之后，强类型由子类自己的注解保证，
> 基类不再假装它是 dict。前端仍然按 JSON Schema 渲染，取到的是子类的具体 schema。
>
> 另一处：`Capabilities` 是 `@dataclass(frozen=True, slots=True)`，
> **不进配置**（`docs/specs/platform-adapter.md`），改它要发版 —— 见 `docs/lessons.md` 经验 4。
>
> **第三处（2026-09-23，`docs/adr/0012`）**：上面 `use_cdp_bridge` 那句
> "`capabilities.needs_browser=True` 时必须 `True`" 今天**没有任何代码强制**——
> 装不装桥完全由 `needs_browser` 决定（`core/task_registry.py` 的 `DepsFactory`），
> 这个字段一行都没被读。所以它在两个平台子类里都标了 `ui:hidden`；
> **在基类标是无效的**，子类重新声明会换掉 `FieldInfo`（见 §4 第 2 条）。

### 3.1 `DouyinConfig`

> **修订（2026-09-22，Task 6，`docs/adr/0011`）**：原设计里这里有一个
> `ytdlp_cookie_priority: tuple[str, ...]`，把 cookie 阶梯的**顺序**写成了配置字段
> （`("env:...FROM_BROWSER", "env:...FILE", "file:cookies_file", "none")`）。
> 它与 `DouyinAdapter.capabilities.cookie_variants = ("exported_file", "browser", "none")`
> 是同一条阶梯的**两个互相矛盾的顺序**（前者把浏览器档排在导出文件档之前），
> 而 `PlatformAdapter.download_media` 的契约写的是"必须遵守 `capabilities` 的顺序"。
> 同时删掉 `persist_play_url`：契约里没有任何字段能存放播放直链，
> 一个只有 `false` 合法值的布尔不是配置，是没想清楚的化石。
>
> 现在的分工：**顺序只归 `Capabilities`**；配置与 V1 的 env 名只回答
> "导出文件档用哪个路径 / 浏览器档用哪个浏览器"。

```python
class DouyinConfig(PlatformConfig):
    media_strategy: Literal["yt_dlp_with_fallback"] = "yt_dlp_with_fallback"   # ui:hidden
    list_strategy: Literal["browser_scroll"] = "browser_scroll"                # ui:hidden
    use_cdp_bridge: bool = True                                               # ui:hidden

    ytdlp_cookies_from_browser: str | None = None
    """浏览器档的目标浏览器。默认 None = **没有浏览器档**（V1 §7.3：
    Windows 上这一档永远读不出来，而它的报错正好会触发退档，
    于是白扔一次子进程 + 把真原因混进 cookie 报错）。
    B站 那边默认 "chrome"，两边不同是有意的。"""

    fallback_to_page_play_url: bool = True
    """yt-dlp 失败时是否兜底到页面播放直链（V1 §7.2，常态）。"""

    advanced: DouyinAdvanced = Field(..., json_schema_extra={"ui:advanced": True})


class DouyinAdvanced(BaseModel):
    retry_max: int = Field(default=3, ge=0)                          # ui:hidden，V2.0 未实现
    retry_backoff_seconds: float = Field(default=2.0, ge=0.0)        # ui:hidden，同上
    request_timeout_seconds: int = Field(default=30, ge=1)           # ui:hidden，抖音侧没读
    max_video_duration_seconds: int | None = None                    # ui:hidden，collect 没过滤器
```

> **修订（2026-09-23，`docs/adr/0012`）**：上面 7 个 `ui:hidden` 是量出来的，不是装饰 ——
> 它们在代码里一行都没被读（抖音的重试/超时实际走 `DouyinAdapter` 里写死的
> `DIRECT_BUDGET_SECONDS` / `YTDLP_BUDGET_SECONDS`）。为什么不删字段：这些模型是
> `extra="forbid"`，而 `config/platforms.yaml`（这 7 个键都在里面）每次启动都要过校验 ——
> 删字段等于让那份文件走 `extra_forbidden`，症状从"表单上一个空开关"变成"升级后服务起不来"。
> 解封顺序见 ADR-0012。

### 3.2 `BilibiliConfig`

> **修订（2026-09-22，Task 7，`docs/adr/0011`）**：这里的
> `cookie_variant_order: tuple[CookieVariant, ...]` 已删除 —— 它与
> `BilibiliAdapter.capabilities.cookie_variants` 是同一份顺序写两遍。
> 同时 `list_strategy` 的取值从 `'api' | 'external_manifest'` 收窄成
> `'yt_dlp_flat' | 'external_manifest'`：空间列表那个接口匿名回 HTML 风控页，
> 留一个没有实现路径的取值等于给前端渲染出一个点了没反应的选项。

```python
class BilibiliConfig(PlatformConfig):
    media_strategy: Literal["yt_dlp"] = "yt_dlp"
    list_strategy: Literal["yt_dlp_flat", "external_manifest"] = "yt_dlp_flat"  # ui:hidden，见下
    use_cdp_bridge: bool = False                                               # ui:hidden

    ytdlp_cookies_from_browser: str | None = "chrome"
    external_browser_manifest_path: Path | None = None
    prefer_subtitles: bool = True                                              # ui:hidden：ASR 在 V2.1

    advanced: BilibiliAdvanced = Field(..., json_schema_extra={"ui:advanced": True})


class BilibiliAdvanced(BaseModel):
    require_login_for_high_quality: bool = True    # ui:hidden：画质由 cookie 阶梯走到哪一档决定
    dash_split_handling: Literal["auto", "merge", "keep_split"] = "auto"
    retry_max: int = 3                             # ui:hidden，V2.0 没有重试循环
    retry_backoff_seconds: float = 2.0             # ui:hidden，同上
    request_timeout_seconds: int = 30              # 有人读：适配器每一段预算都用它
    search_fallback_node_playwright: bool = False
```

> `media_strategy` 同样是 `ui:hidden`（单一取值的 Literal 是文档不是配置，真源
> `capabilities.media_strategy`）。`list_strategy` 那一条比"没人读"更糟：
> `BilibiliAdapter._enumerate` 选路看的是「`external_browser_manifest_path` 给了没、
> 给了但没命中就回落」，**不看这个字段** —— 界面上写 `yt_dlp_flat`、实际照样用外部清单。
> 它是 ADR-0012 解封清单的第 1 位。
>
> `cookie_variant_order` 已经不在这个模型里了（ADR-0011 的 Task 7 追记：它与
> `capabilities.cookie_variants` 是同一份顺序写两遍）。但**这个代码块当时没跟着改**，
> 于是"注记说删了、示例还在"成了第二处真相 —— 2026-09-23 一并对齐。

### 3.3 `XiaohongshuConfig` / `YoutubeConfig`

类似，详见 `src/intelligence_hub_v2/platforms/<name>/config.py`。
**新平台接进来会自动进 ADR-0012 那张网**（守卫遍历 `PLATFORM_CONFIG_SCHEMAS` 的全部成员）：
每个字段要么有人读、要么按 §4 的三条约定标 `ui:hidden` + 写 description。
V1 §7.24「跟踪开关没人读」那类坑最容易长在正是新平台的第一个版本。

---

## 4. JSON Schema 生成

每个 `PlatformConfig` 子类自动出 JSON Schema：

```python
@router.get("/api/platforms/{name}/schema")
async def get_platform_schema(name: str) -> dict:
    adapter_cls = PLATFORMS[name]
    config_cls = adapter_cls.config_schema()
    return config_cls.model_json_schema()
```

前端用 `react-jsonschema-form` 或自渲染表单。两个标记都由字段自己的
`Field(json_schema_extra=...)` 带出来：

| 标记 | 前端必须怎么做 | 谁在守 |
|---|---|---|
| `ui:advanced: true` | 把这一组默认折叠（不是不渲染） | `test_advanced_group_carries_the_collapse_marker` |
| `ui:hidden: true` | **不渲染这个字段**（值仍在 yaml 里，加载与回写照旧） | `test_every_visible_field_has_a_reader` |

三条实现期约定（都是量出来的，不是设想）：

1. **组内叶子全被 `ui:hidden` 时不渲染这个组** —— 否则 `douyin.advanced`（四个字段今天全隐藏）
   会渲染成一个点开以后什么都没有的折叠条。
2. **标记必须打在子类重新声明的那一处。** 子类写 `use_cdp_bridge: bool = False` 会换一个全新的
   `FieldInfo`，父类上的 `json_schema_extra` 与 `description` **双双丢掉**（实测），
   而且丢得很安静：schema 里就是没有那个键。
3. **`ui:hidden` 的字段必须带 `Field(description=...)` 说明"为什么没有效果 + 真源在哪 + 什么时候有"。**
   紧跟赋值的 docstring **不进 JSON Schema**（Pydantic 只吃 `Field(description=...)`），
   所以对契约不可见；隐藏而不解释等于把撒谎从表单挪进注释缺失。

判据与"为什么不干脆删字段"见 `docs/adr/0012`；现存 14 个隐藏字段的名单与**解封顺序**
就在 ADR-0012 的表里（`bilibili.list_strategy` 排第一：它是唯一一个读出来行为与界面不符的）。

---

## 5. 配置写入 API

```python
@router.put("/api/platforms/{name}/config")
async def update_platform_config(
    name: str,
    new_config: dict,
    config_service: ConfigService = Depends(get_config_service),
) -> PlatformConfigResponse:
    # 1. Pydantic 校验
    adapter_cls = PLATFORMS[name]
    config_cls = adapter_cls.config_schema()
    validated = config_cls.model_validate(new_config)

    # 2. 原子写盘（tempfile + os.replace）
    await config_service.write_platform_config(name, validated)

    # 3. 内存热加载
    config_service.reload_platform(name)

    # 4. publish CONFIG_CHANGED 事件
    await event_bus.publish(
        Event(
            type=EventType.CONFIG_CHANGED,
            payload=ConfigChangedPayload(
                scope="platform",
                platform=name,
                changed_fields=diff_fields(old, validated),
                requires_restart=False,
            ).model_dump(),
        )
    )

    return PlatformConfigResponse(config=validated, health=...)
```

**纪律**：
- **不允许"手改 YAML 文件等服务感知"**：运行时不监听文件变化
- 配置文件仍写盘的原因：审计、备份、可进 git
- 敏感字段（token）走 `config/feishu.yaml`（gitignore），不进 `platforms.yaml`

> **实施期修订（2026-09-23）· 上面那条"可进 git"要配两个限定。**
>
> 1. **注释活不过第一次 `PUT`。** `yaml.safe_dump` 不保留注释，实测首写就抹掉
>    `platforms.yaml` 顶部与逐字段的 96 行说明。**决定：不为此引入 `ruamel.yaml`**
>    （多一个运行时依赖，换来的是一份"人写的注释与代码谁更新"长期不一致的文件）。
>    字段说明的活真相改成 JSON Schema 的 `Field(description=...)` —— 前端本来就是从
>    `/api/platforms/{name}/schema` 拿 schema 渲染表单，说明写在那里才可能被读到、
>    也才可能被测到；`platforms.yaml` 降级成"数据 + 一段会消失的注释"，
>    这一点已写进该文件自己的头部。
> 2. **"重写不会误伤别的段"以前是假的。** `_current_platforms_dump` 以前只按内存重建，
>    于是两种情况会真删数据：未 `load()` 的 manager 写一次（其他平台段整个消失），
>    以及 `load()` 之后有人往文件里加了段（那一段被抹）。现在改成
>    "盘上现有内容打底 + 内存覆盖自己那几段"，并且未 `load()` 直接 `ConfigError`。
>    看护：`tests/unit/core/test_config.py::test_write_without_load_refuses_...` 与
>    `test_write_preserves_a_section_added_to_disk_after_load`。
>
> 还没做的一小项：`reload_platform()` 写 `self._platforms[name]` 时没拿 `_write_lock`，
> 与并发 `write_platform_config` 交错时可能把刚重载的值又推回盘上（低频，记在 lessons 待办）。

---

## 6. 环境变量映射

| 环境变量 | 配置路径 |
|---|---|
| `INTELLIGENCE_HUB_DATA_DIR` | `data.dir` |
| `INTELLIGENCE_HUB_APP__PORT` | `app.port` |
| `INTELLIGENCE_HUB_LOGGING__LEVEL` | `logging.level` |
| `INTELLIGENCE_HUB_CDP_BRIDGE__URL` | `cdp_bridge.url` |
| `SENSEVOICE_MODEL_DIR` | `asr.model_dir`（兼容 V1） |
| `SHERPA_ONNX_MODEL_DIR` | `asr.model_dir`（兼容 V1） |
| `DOUYIN_YTDLP_COOKIES_FILE` | `platforms.douyin.cookies_file`（兼容 V1）**目前由适配器读**，见下 |
| `DOUYIN_YTDLP_COOKIES_FROM_BROWSER` | `platforms.douyin.ytdlp_cookies_from_browser`（兼容 V1）**目前由适配器读** |
| `BILI_YTDLP_COOKIES_FILE` | `platforms.bilibili.cookies_file`（兼容 V1） |
| `BILI_YTDLP_COOKIES_FROM_BROWSER` | `platforms.bilibili.ytdlp_cookies_from_browser`（兼容 V1） |

V1 的环境变量名保留兼容，但 V2 内部统一走 `INTELLIGENCE_HUB_*`。

> **实施期修订（2026-09-23）· 上面第一行曾经是完全纸面的。**
>
> `AppConfig` 的 `env_nested_delimiter="__"`，所以嵌套路径的**通用写法是双下划线**
> （表里其余每一行都是这个形状）。`INTELLIGENCE_HUB_DATA_DIR` 用的是单下划线，
> 按通用写法它**根本不映射到 `data.dir`** —— 而 `AGENTS.md §3`、`data-model.md §1`、
> `config/app.yaml` 四处都写的是这一种。后果不是难看：这是"把一次性脚本和测试
> 挡在 live `data/` 之外"的机制，而它静默失效、失效方向是**朝生产数据敞开**。
>
> 修法是给它一个显式别名表，而不是去改四处文档：
> `core/config.py:ENV_FLAT_ALIASES` + `_FlatEnvAliasSource`。规则：
> - 表里的扁平名**只有列出来的才生效**，不是"单下划线全局也认"（否则会发明第二套命名法）；
> - 两种写法同时出现时**嵌套那种优先**（它更具体，也是通用写法）；
> - 空串视为未设置，与 env 的一般语义一致。
>
> 要再加一条扁平别名，就改 `ENV_FLAT_ALIASES` 一处；看护在
> `tests/unit/core/test_config.py`（含"别名不许波及其他 section"那条）。
>
> 另：`alembic` 侧还有两条**没进这张表**的承重变量 ——
> `INTELLIGENCE_HUB_STORAGE__SQLITE_URL`（`alembic/env.py:69`，`0001` 的 docstring 让 CI 用它）
> 与 `INTELLIGENCE_HUB_ALEMBIC_DIR`（`storage/db.py:112`）。§7 声明这张表对 V3 冻结，
> 所以要么补进来，要么 V3 会照着缺一维的表重写。

> **实施现状（Task 6，`docs/adr/0011`）**：`_build_platform_config()` 只吃 YAML，
> 平台配置这一层**还没有接 env source**（上面三行 `DOUYIN_*` / `BILI_*` 的映射目前是纸面的）。
> 抖音的兼容行为此落地在 `platforms/douyin/media.py:resolve_cookie_ladder()` 里读 `os.environ`。
> **收口项**：等平台配置层接上 env source，把那句 `os.environ.get` 删掉改读配置 ——
> 否则同一件事有两处真相。跟进记账见 `docs/lessons.md`「V2 新增」。

---

## 7. V3 重写时的契约

V3 即使换语言（Go/Rust）：

1. `AppConfig` / `PlatformConfig` Pydantic 模型的 JSON Schema 不变
2. `config/app.yaml` / `config/platforms.yaml` 字段名不变
3. `/api/platforms/{name}/schema` 返回的 JSON Schema 不变
4. 环境变量映射不变

→ 前端配置面板零改动，用户的 YAML 配置文件可直接复用。
