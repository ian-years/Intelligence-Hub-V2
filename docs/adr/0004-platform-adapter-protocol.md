# ADR-0004: 平台适配器 Protocol

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q5、`docs/specs/platform-adapter.md`、`docs/specs/contract-tests.md`

## 背景

V1 四个采集器各自为政：

- `download_douyin_latest.py`、`download_bili_following_latest.py`、`download_xiaohongshu_latest.py`、`download_youtube_latest.py` 都是独立脚本，每个有自己的 argparse、自己的 stdout 末行 JSON 契约、自己的清单格式
- 启动器 `launcher_server.py` 用字典 `TASK_DEFS` 硬编码调度
- 添加新平台要改字典、加新脚本、改前端按钮
- 平台间没有共享契约，导致 V1 §7 那 25 条陷阱里很多是"每个平台各踩一遍"

V2 要：
1. 把"平台"抽成显式接口，配置驱动开关
2. 关掉一个平台 = 它的所有任务在调度层就被拒，前端按钮置灰
3. 添加新平台 = 写一个 Adapter 类 + 注册表加一行 + YAML 加一段，**不动调度器**
4. V3 重写时即使换语言（Go/Rust 通过 subprocess + JSON-RPC 接入），调度层零改动

## 决定

定义 `PlatformAdapter` Protocol（`src/intelligence_hub_v2/platforms/base.py`），所有平台采集器必须实现：

```python
class PlatformAdapter(Protocol):
    name: ClassVar[str]                    # "douyin" / "bilibili" / ...
    display_name: ClassVar[str]            # "抖音" / "B站" / ...
    capabilities: ClassVar[Capabilities]

    @classmethod
    def config_schema(cls) -> type[PlatformConfig]: ...

    def __init__(self, config: PlatformConfig, deps: AdapterDeps) -> None: ...

    async def healthcheck(self) -> HealthReport: ...
    async def parse_creator_url(self, url: str) -> CreatorRef: ...
    async def fetch_creator_profile(self, ref: CreatorRef) -> CreatorProfile: ...
    async def list_creator_videos(
        self, ref: CreatorRef, *, since: datetime | None, limit: int
    ) -> AsyncIterator[VideoMeta]: ...
    async def download_media(
        self, video: VideoMeta, dest: Path, *, on_progress: ProgressCallback | None
    ) -> MediaArtifact: ...
    async def fetch_subtitles(self, video: VideoMeta) -> Transcript | None: ...
```

**关键设计**：

1. **`Capabilities` 是声明式的**：

   ```python
   @dataclass(frozen=True, slots=True)
   class Capabilities:
       needs_browser: bool          # True → 调度器必须先确认 CDP 桥活
       needs_cookies: bool          # True → 必须先有 cookies 文件
       cookie_variants: tuple[str, ...]  # ("exported_file", "browser", "anonymous")
       supports_subtitles: bool     # B站/YouTube True，抖音/小红书 False
       supports_dash_split: bool    # B站 True（V1 §7.21）
       list_strategy: Literal["api", "browser_scroll", "yt_dlp_flat", "external_manifest"]
       media_strategy: Literal["yt_dlp", "page_play_url", "yt_dlp_with_fallback"]
   ```

   调度器据此决定"跑这个任务前要满足什么前置条件"，不需要每个 Adapter 自己写健康检查逻辑。

2. **`MediaArtifact` 是 union**：

   ```python
   MediaArtifact = SingleFileArtifact | VideoAudioPairArtifact
   ```

   解决 V1 §7.21 B站 DASH 未合并分片问题。`media_source` 字段强制记录 `'yt_dlp' / 'page_play_url' / 'dash_merged' / 'dash_split'`，`yt_dlp_error` 字段在兜底时强制保留原文（V1 §7.2 那条经验）。

3. **`list_creator_videos` 用 AsyncIterator**：流式更省内存（小红书滚动加载几百条时），调度器用 `async for` 消费，可以中途取消。

4. **`parse_creator_url` 是接口的一部分**：V1 §7.1 那条"抖音短链 302 才拿到 sec_uid"的逻辑统一进这里，**不能跳过**。

5. **依赖通过 `AdapterDeps` 注入**：

   ```python
   @dataclass
   class AdapterDeps:
       bridge: BridgeClient | None      # CDP 桥（needs_browser=True 时必填）
       cookies: CookieManager
       http: httpx.AsyncClient
       storage: Storage
       events: EventBus
       logger: structlog.BoundLogger
       config: PlatformConfig
   ```

   测试时换 mock，生产时 FastAPI DI 注入真实实现。

**插件注册**：显式注册表（`src/intelligence_hub_v2/platforms/registry.py`）：

```python
PLATFORMS: dict[str, type[PlatformAdapter]] = {
    "douyin": DouyinAdapter,
    "bilibili": BilibiliAdapter,
    "xiaohongshu": XiaohongshuAdapter,
    "youtube": YoutubeAdapter,
}
```

**不选 `importlib.metadata.entry_points`**：四个平台都是第一方代码，显式注册表类型安全、IDE 友好、测试好写。接口设计成 V3 想换成 entry_points 是 30 行的事，但 V2 不上因为收益小、调试难。

**配置驱动开关**：`config/platforms.yaml` 每个平台一段，`enabled` 字段是开关。前端配置面板从 `/api/platforms/{name}/schema` 拿 JSON Schema 自动渲染表单，保存时 `PUT /api/platforms/{name}/config` 校验后写回 YAML。

## 后果

**好处**：
- V1 §7 的平台特有陷阱被 `Capabilities` + `MediaArtifact` 字段强制保留（`media_source` / `yt_dlp_error` / `cookie_variants` / `supports_dash_split`）
- 添加新平台不动调度器
- 契约测试用抽象基类（`tests/contracts/test_platform_adapter.py`），V3 加新平台或重写老平台都自动跑同一套
- V3 即使把抖音采集器从 Python 重写成 Go/Rust（拿到更稳的 `a_bogus` 签名能力），只要还实现这套接口（通过 subprocess + JSON-RPC 也行），调度层零改动

**代价**：
- 接口设计要前瞻（V3 重写时不能动），所以 V2.0 阶段要多花时间打磨
- `AsyncIterator` 的消费方要小心（取消时要把 iterator 关掉，否则资源泄漏）
- `Capabilities` 的字段集会随平台演化扩展（如 V2.x 加 Instagram 时可能要加 `needs_oauth`），扩展走 ADR

**对 V3 的意义**：
- `PlatformAdapter` Protocol + `Capabilities` dataclass + Pydantic 配置模型 = V3 实现的**可执行规范**
- 契约测试套件 = V3 的可执行验收标准
- V3 加新平台或重写老平台，调度层零改动
