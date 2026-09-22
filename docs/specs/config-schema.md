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

### 3.1 `DouyinConfig`

```python
class DouyinConfig(PlatformConfig):
    media_strategy: Literal["yt_dlp_with_fallback"] = "yt_dlp_with_fallback"
    list_strategy: Literal["browser_scroll"] = "browser_scroll"
    use_cdp_bridge: bool = True

    ytdlp_cookie_priority: tuple[str, ...] = (
        "env:DOUYIN_YTDLP_COOKIES_FROM_BROWSER",
        "env:DOUYIN_YTDLP_COOKIES_FILE",
        "file:cookies_file",
        "none",
    )
    """yt-dlp cookie 优先级阶梯（V1 §7.3）。"""

    fallback_to_page_play_url: bool = True
    """yt-dlp 失败时是否兜底到页面播放直链（V1 §7.2，常态）。"""

    advanced: DouyinAdvanced = DouyinAdvanced()

class DouyinAdvanced(BaseModel):
    retry_max: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0.0)
    request_timeout_seconds: int = Field(default=30, ge=1)
    max_video_duration_seconds: int | None = None
```

### 3.2 `BilibiliConfig`

```python
class BilibiliConfig(PlatformConfig):
    media_strategy: Literal["yt_dlp"] = "yt_dlp"
    list_strategy: Literal["api"] = "api"
    use_cdp_bridge: bool = False

    cookie_variant_order: tuple[Literal["exported_file", "browser", "anonymous"], ...] = (
        "exported_file", "browser", "anonymous",
    )
    """cookie 三档阶梯（V1 §7.15）。"""

    ytdlp_cookies_from_browser: str | None = "chrome"
    external_browser_manifest_path: Path | None = None
    prefer_subtitles: bool = True

    advanced: BilibiliAdvanced = BilibiliAdvanced()

class BilibiliAdvanced(BaseModel):
    require_login_for_high_quality: bool = True
    dash_split_handling: Literal["auto", "merge", "keep_split"] = "auto"
    retry_max: int = 3
    retry_backoff_seconds: float = 2.0
    request_timeout_seconds: int = 30
    search_fallback_node_playwright: bool = False
```

### 3.3 `XiaohongshuConfig` / `YoutubeConfig`

类似，详见 `src/intelligence_hub_v2/platforms/<name>/config.py`。

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

前端用 `react-jsonschema-form` 或自渲染表单。`ui:advanced` 标记（通过 `Field(json_schema_extra={"ui:advanced": True})`）让前端把高级字段折叠。

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
    await event_bus.publish(Event(
        type=EventType.CONFIG_CHANGED,
        payload=ConfigChangedPayload(
            scope="platform",
            platform=name,
            changed_fields=diff_fields(old, validated),
            requires_restart=False,
        ).model_dump(),
    ))

    return PlatformConfigResponse(config=validated, health=...)
```

**纪律**：
- **不允许"手改 YAML 文件等服务感知"**：运行时不监听文件变化
- 配置文件仍写盘的原因：审计、备份、可进 git
- 敏感字段（token）走 `config/feishu.yaml`（gitignore），不进 `platforms.yaml`

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
| `DOUYIN_YTDLP_COOKIES_FILE` | `platforms.douyin.cookies_file`（兼容 V1） |
| `BILI_YTDLP_COOKIES_FILE` | `platforms.bilibili.cookies_file`（兼容 V1） |
| `BILI_YTDLP_COOKIES_FROM_BROWSER` | `platforms.bilibili.ytdlp_cookies_from_browser`（兼容 V1） |

V1 的环境变量名保留兼容，但 V2 内部统一走 `INTELLIGENCE_HUB_*`。

---

## 7. V3 重写时的契约

V3 即使换语言（Go/Rust）：

1. `AppConfig` / `PlatformConfig` Pydantic 模型的 JSON Schema 不变
2. `config/app.yaml` / `config/platforms.yaml` 字段名不变
3. `/api/platforms/{name}/schema` 返回的 JSON Schema 不变
4. 环境变量映射不变

→ 前端配置面板零改动，用户的 YAML 配置文件可直接复用。
