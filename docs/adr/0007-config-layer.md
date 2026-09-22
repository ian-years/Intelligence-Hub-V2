# ADR-0007: 配置层

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q8、`docs/specs/config-schema.md`、ADR-0004（平台 Adapter）

## 背景

V1 的配置散落多处：

- `config/feishu-base-config.json`（飞书 token）
- `downloads/launcher-state/creators.json`（博主库，与 SQLite 双源）
- `downloads/launcher-state/hidden-videos.json`（墓碑）
- 各采集器的 argparse 参数（`--videos-per-creator` / `--external-browser-manifest` 等）
- 环境变量（`DOUYIN_YTDLP_COOKIES_FILE` / `BILI_YTDLP_COOKIES_FILE` / `SENSEVOICE_MODEL_DIR`）

痛点：
- 没有统一的配置 schema，每个脚本自己读
- 改配置要重启脚本（启动器自身驻内存，旧进程跑旧代码 —— V1 §4 那条经验）
- 没有"平台开关"概念，关掉一个平台要改代码或删博主
- 配置漂在文件里，运行时不知道

V2 要：
1. 统一配置入口（Pydantic Settings + YAML）
2. 平台开关由配置驱动（关掉 = 任务自动消失）
3. 配置只通过 API 写（避免"手改文件 vs 运行时不一致"）
4. JSON Schema 自动渲染前端表单

## 决定

### 加载优先级（低 → 高）

1. Pydantic Settings 内置默认值
2. `config/app.yaml`（全局）
3. `config/platforms.yaml`（每个平台）
4. 环境变量 `INTELLIGENCE_HUB_*`
5. CLI 参数（`--config-dir` / `--data-dir` / `--port` / `--platforms-enable douyin,bilibili`）

### 配置写入路径

**只允许通过 API 写**：

- `PUT /api/platforms/{name}/config` 是唯一写入入口
- 服务收到写入 → Pydantic 校验 → 原子写盘（`tempfile + os.replace`）→ 内存热加载 → publish `CONFIG_CHANGED` 事件
- **不允许"手改 YAML 文件等服务感知"**：避免 V1 那种"配置漂在文件里、运行时不知道"的坑
- 如果用户手改了 YAML，下次重启时会读到，但运行时不监听文件变化
- 配置文件仍写盘的原因：审计、备份、可进 git（敏感的 `feishu.yaml` 进 gitignore）

### 平台开关的全局影响

关掉一个平台时，前后端协同行为：

| 影响面 | 行为 |
|---|---|
| `/api/tasks` | 该平台的任务自动从列表消失（`TaskDefinition.platforms` 过滤） |
| `/api/tasks/{name}/run` | 提交时校验，平台关着直接 422 |
| 调度器 | 定时任务跳过该平台的博主 |
| Feed 页 | 默认隐藏该平台视频，但保留"显示已禁用平台的历史数据"切换 |
| 博主库 | 该平台博主置灰、不可编辑、不删数据 |
| Dashboard | 该平台卡片显示"已禁用"状态（灰色几何形状） |

### 关键 API

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/config/app` | 全局配置（含数据目录、版本） |
| PUT | `/api/config/app` | 改全局配置（部分字段需重启，响应里告诉前端） |
| GET | `/api/platforms` | 列所有平台 + 当前配置 + 健康状态 + capabilities |
| GET | `/api/platforms/{name}/schema` | JSON Schema（前端自动渲染表单） |
| PUT | `/api/platforms/{name}/config` | 改平台配置（含 enabled） |
| POST | `/api/platforms/{name}/healthcheck` | 主动触发健康检查 |

### 配置模型

```python
class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INTELLIGENCE_HUB_",
        env_nested_delimiter="__",
        yaml_file="config/app.yaml",
    )
    app: AppSection
    data: DataSection
    storage: StorageSection
    logging: LoggingSection
    scheduler: SchedulerSection
    cdp_bridge: BridgeSection
    asr: AsrSection
    http_client: HttpSection

class PlatformConfig(BaseModel):
    """每个平台的配置基类"""
    enabled: bool = True
    display_name: str
    cookies_file: Path | None = None
    use_cdp_bridge: bool = False
    media_strategy: Literal["yt_dlp", "page_play_url", "yt_dlp_with_fallback"]
    list_strategy: Literal["api", "browser_scroll", "yt_dlp_flat", "external_manifest"]
    videos_per_creator: int = Field(default=30, ge=1, le=200)
    rate_limit: RateLimitConfig
    advanced: dict[str, Any] = Field(default_factory=dict)   # 平台特有字段

class DouyinConfig(PlatformConfig):
    """抖音特有字段"""
    ytdlp_cookie_priority: tuple[str, ...]
    fallback_to_page_play_url: bool = True
```

每个平台 Adapter 的 `config_schema()` 返回对应的 Pydantic 模型，FastAPI 自动出 JSON Schema 给前端。

### 高级字段折叠

平台配置里有些字段（如 B站的 `cookie_variant_order`、抖音的 `ytdlp_cookie_priority`）是"高级"字段，新手不该看到。

- Pydantic 模型里用 `Field(json_schema_extra={"ui:advanced": True})` 标记
- 前端配置面板默认折叠"高级"区域，点击展开
- JSON Schema 里 `ui:advanced` 字段被前端识别，自动归类

## 后果

**好处**：
- 配置单一入口，运行时与文件一致（API 写入会同时更新两者）
- 平台开关由配置驱动，关掉平台 = 任务自动消失，无需改代码
- JSON Schema 自动渲染前端表单，添加新平台时前端零改动
- 高级字段折叠，新手不被复杂配置吓到

**代价**：
- "只通过 API 写"意味着用户不能直接编辑 YAML（要起服务）。缓解：服务起不来时可以临时手改 YAML，重启生效
- 配置变更要 publish 事件，前端要订阅 `CONFIG_CHANGED` 才能实时更新（增加一点复杂度）

**对 V3 的意义**：
- Pydantic 配置模型 = 配置契约。V3 重写时配置 schema 不变，前端配置面板可复用
- JSON Schema 是机器可读的契约，V3 即使换语言（Go/Rust），只要能出 JSON Schema，前端零改动
- `ui:advanced` 标记是前端约定，V3 沿用
