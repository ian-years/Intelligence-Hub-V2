# Spec: Platform Adapter Protocol

> **状态**：Locked（V2.0 起契约稳定，改动需走 ADR）
> **源文件**：`src/intelligence_hub_v2/platforms/base.py`
> **相关 ADR**：[0004](../adr/0004-platform-adapter-protocol.md)

这是 V2 → V3 的核心契约之一。**V3 重写实现时，这份 spec 不能动**；动了等于改 V3 的规范。

---

## 1. 总览

每个平台采集器（抖音 / B站 / 小红书 / YouTube / 未来扩展）实现 `PlatformAdapter` Protocol，声明 `Capabilities`，提供 Pydantic 配置模型。调度器只依赖 Protocol，不依赖具体实现。

```
┌──────────────────┐
│  TaskRegistry    │  ← 调度层只调 Protocol 方法
└────────┬─────────┘
         │ uses
         ▼
┌──────────────────┐
│ PlatformAdapter  │  ← Protocol（契约）
│   (Protocol)     │
└────────┬─────────┘
         │ implemented by
   ┌─────┼─────┬─────────┬──────────┐
   ▼     ▼     ▼         ▼          ▼
Douyin Bili  XHS      YouTube   <NewPlatform>
```

---

## 2. 核心类型

### 2.1 `PlatformAdapter` Protocol

```python
from typing import AsyncIterator, Callable, ClassVar, Protocol
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel

class PlatformAdapter(Protocol):
    """所有平台采集器必须实现的接口。V3 重写时，这就是契约。"""

    # ---- 类级元数据 ----
    name: ClassVar[str]
    """平台标识，全小写下划线，如 'douyin' / 'bilibili' / 'xiaohongshu' / 'youtube'。
    必须与 PLATFORMS 注册表的 key 一致，必须与 config/platforms.yaml 的 key 一致。"""

    display_name: ClassVar[str]
    """人类可读名称，如 '抖音' / 'B站'。前端展示用。"""

    capabilities: ClassVar["Capabilities"]
    """声明式能力。调度器据此决定前置条件（要不要起桥、要不要 cookie）。"""

    # ---- 构造 ----
    @classmethod
    def config_schema(cls) -> type["PlatformConfig"]:
        """返回 Pydantic 模型类，前端配置面板据此自动渲染表单。
        必须是 PlatformConfig 的子类。"""

    def __init__(self, config: "PlatformConfig", deps: "AdapterDeps") -> None:
        """deps 注入：CDP 桥客户端、cookie 管理器、HTTP 客户端、logger、event bus、storage。"""

    # ---- 健康检查 ----
    async def healthcheck(self) -> "HealthReport":
        """探测平台可达性、cookie 有效性、桥连通性。
        替代 V1 launcher 的 health_checks。
        必须如实报告，不许臆造成功（V1 §1.3）。"""

    # ---- 博主 ----
    async def parse_creator_url(self, url: str) -> "CreatorRef":
        """把用户粘贴的 URL 规范化成 CreatorRef。
        V1 §7.1 那条「抖音短链 302 才拿到 sec_uid」的逻辑统一进这里。
        实现必须：
        - 跟随重定向（短链 → 规范主页）
        - 提取平台原生 ID（不是 URL 里的东西）
        - 识别脏行（V1 is_http_url(platform_id) 那种）
        失败抛 PlatformError，带原文。"""

    async def fetch_creator_profile(self, ref: "CreatorRef") -> "CreatorProfile":
        """拉博主资料（昵称/头像/粉丝数）。
        V1 download_creator_profiles.py 的对应物。"""

    # ---- 视频列表 ----
    async def list_creator_videos(
        self,
        ref: "CreatorRef",
        *,
        since: datetime | None = None,
        limit: int = 30,
    ) -> AsyncIterator["VideoMeta"]:
        """流式产出视频元数据。
        AsyncIterator 而不是 list：小红书滚动加载几百条时省内存，
        调度器可以中途取消（用 aclose() 关 iterator）。
        since 不为空时只产出该时间之后的视频。
        limit 是上限，不是保证。"""

    # ---- 媒体下载 ----
    async def download_media(
        self,
        video: "VideoMeta",
        dest: Path,
        *,
        on_progress: "ProgressCallback | None" = None,
    ) -> "MediaArtifact":
        """下载媒体到 dest 目录。
        返回的 MediaArtifact 必须带：
        - media_source: 'yt_dlp' / 'page_play_url' / 'dash_merged' / 'dash_split'
        - yt_dlp_error: 兜底时 yt-dlp 的失败原文（V1 §7.2 那条经验，不许丢）
        on_progress 是 0.0~1.0 的回调，可空。
        实现必须遵守 capabilities.cookie_variants 阶梯（V1 §7.15）。"""

    # ---- 字幕（可选能力） ----
    async def fetch_subtitles(self, video: "VideoMeta") -> "Transcript | None":
        """有字幕优先字幕（B站/YouTube），没有返回 None 让调度器走 ASR。
        capabilities.supports_subtitles=False 的平台直接返回 None。"""
```

### 2.2 `Capabilities`

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True, slots=True)
class Capabilities:
    """平台能力声明。调度器据此决定前置条件。"""

    needs_browser: bool
    """True → 调度器必须先确认 CDP 桥活（/health 通）。
    抖音/小红书 True，B站/YouTube False。"""

    needs_cookies: bool
    """True → 必须先有 cookies 文件。"""

    cookie_variants: tuple[str, ...]
    """cookie 阶梯顺序，如 ('exported_file', 'browser', 'anonymous')。
    V1 §7.15 那条「B站 cookie 三档」的契约化。
    每个 variant 必须对应 download_media 的实现路径。"""

    supports_subtitles: bool
    """B站/YouTube True，抖音/小红书 False。"""

    supports_dash_split: bool
    """True → download_media 可能返回 VideoAudioPairArtifact。
    B站 True（V1 §7.21 未合并分片陷阱）。"""

    list_strategy: Literal["api", "browser_scroll", "yt_dlp_flat", "external_manifest"]
    """列表枚举策略。
    - api: 公开 web-interface（B站）
    - browser_scroll: 桥内页面 JS 滚动加载（抖音/小红书）
    - yt_dlp_flat: yt-dlp --flat-playlist（YouTube）
    - external_manifest: 吃外部浏览器清单（V1 bilibili-download 技能那条路）"""

    media_strategy: Literal["yt_dlp", "page_play_url", "yt_dlp_with_fallback"]
    """媒体下载策略。
    - yt_dlp: 纯 yt-dlp
    - page_play_url: 纯页面播放直链（抖音兜底）
    - yt_dlp_with_fallback: yt-dlp 失败 → 页面播放直链（抖音常态，V1 §7.2）"""
```

### 2.3 `AdapterDeps`

```python
from dataclasses import dataclass
import httpx
import structlog

@dataclass
class AdapterDeps:
    """注入给 Adapter 的依赖袋。测试时换 mock。"""

    bridge: "BridgeClient | None"
    """CDP 桥客户端。capabilities.needs_browser=True 时必填，否则可空。"""

    cookies: "CookieManager"
    """cookie 文件管理器，提供 Netscape 格式文件路径与导出能力。"""

    http: httpx.AsyncClient
    """全局 HTTP 客户端，已配置 UA / 超时 / 重试。"""

    storage: "Storage"
    """数据层，用于读写 creators / videos / transcripts。"""

    events: "EventBus"
    """事件总线，用于推进度事件。"""

    logger: structlog.BoundLogger
    """已绑定 platform=<name> 字段的 logger。"""

    config: "PlatformConfig"
    """平台配置（已校验）。"""
```

### 2.4 数据模型（Pydantic）

```python
from pydantic import BaseModel, Field, HttpUrl
from datetime import datetime
from pathlib import Path
from typing import Literal, Any

class CreatorRef(BaseModel):
    """博主引用（parse_creator_url 的输出）。"""
    platform: str
    platform_id: str            # sec_uid / mid / user_id / channel_id（统一字段名）
    profile_url: HttpUrl
    source_url: HttpUrl | None = None   # 用户原始粘贴的 URL（审计用）

class CreatorProfile(BaseModel):
    """博主资料（fetch_creator_profile 的输出）。"""
    ref: CreatorRef
    name: str
    avatar_url: HttpUrl | None = None
    follower_count: int | None = None
    bio: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

class VideoMeta(BaseModel):
    """视频元数据（list_creator_videos 的产出）。"""
    platform: str
    platform_video_id: str      # aweme_id / bvid / note_id / video_id
    creator_ref: CreatorRef
    title: str
    description: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    cover_url: HttpUrl | None = None
    webpage_url: HttpUrl
    extra: dict[str, Any] = Field(default_factory=dict)

class SingleFileArtifact(BaseModel):
    """单文件媒体（合并后的 mp4 / 抖音兜底片）。"""
    kind: Literal["single_file"] = "single_file"
    path: Path                  # 相对 data/ 的路径
    size_bytes: int
    media_source: Literal["yt_dlp", "page_play_url", "dash_merged"]
    yt_dlp_error: str | None = None    # 兜底时 yt-dlp 的失败原文（V1 §7.2）
    duration_seconds: float | None = None
    has_audio: bool = True
    has_video: bool = True

class VideoAudioPairArtifact(BaseModel):
    """DASH 未合并分片（V1 §7.21 B站陷阱）。"""
    kind: Literal["video_audio_pair"] = "video_audio_pair"
    video_path: Path            # .f<格式号>.mp4
    audio_path: Path            # .f<格式号>.m4a
    video_size_bytes: int
    audio_size_bytes: int
    media_source: Literal["dash_split"] = "dash_split"
    yt_dlp_error: str | None = None
    duration_seconds: float | None = None

MediaArtifact = SingleFileArtifact | VideoAudioPairArtifact
"""媒体下载产物。Pydantic 判别联合，按 kind 字段区分。
转写层认 audio：SingleFileArtifact 走 has_audio，VideoAudioPairArtifact 走 audio_path。"""

class TranscriptSegment(BaseModel):
    start_seconds: float
    end_seconds: float
    text: str

class Transcript(BaseModel):
    """口播稿（fetch_subtitles 的输出，或 ASR 引擎的输出）。"""
    engine: Literal["sherpa_sense_voice", "bilibili_subtitle", "youtube_subtitle", "manual"]
    language: str | None = None
    text: str
    char_count: int
    sentence_count: int
    segments: list[TranscriptSegment] = Field(default_factory=list)
    text_path: Path | None = None       # 落盘路径（相对 data/）

class HealthReport(BaseModel):
    """healthcheck 的输出。"""
    platform: str
    status: Literal["ok", "degraded", "unreachable", "unknown"]
    detail: str | None = None
    checked_at: datetime
    components: dict[str, Literal["ok", "degraded", "unreachable"]] = Field(default_factory=dict)
    """子组件状态，如 {'cookies': 'ok', 'bridge': 'unreachable', 'network': 'ok'}"""

class FailureRecord(BaseModel):
    """清单里的失败记录。"""
    platform: str
    stage: Literal["parse_url", "list", "download", "transcribe", "store"]
    video_id: str | None = None
    creator_id: str | None = None
    error: str                  # 原文，不许吞错（V1 §1.3）
    error_kind: str | None = None   # 异常类名
    timestamp: datetime

ProgressCallback = Callable[[float], None]
"""进度回调，0.0~1.0。"""
```

### 2.5 异常层次

```python
class IntelligenceHubError(Exception):
    """所有 V2 异常的基类。"""

class PlatformError(IntelligenceHubError):
    """平台层异常。"""
    def __init__(self, platform: str, stage: str, message: str, *, cause: Exception | None = None):
        self.platform = platform
        self.stage = stage
        self.cause = cause
        super().__init__(f"[{platform}/{stage}] {message}")

class CookieError(PlatformError):
    """cookie 缺失或失效。"""

class BridgeError(PlatformError):
    """CDP 桥不可用或浏览器死了。"""

class MediaDownloadError(PlatformError):
    """媒体下载失败（所有 cookie 档位都试过）。"""

class ListError(PlatformError):
    """列表枚举失败。"""
```

**纪律**：
- 所有平台层异常必须带 `platform` 与 `stage`，进清单 `failures[]`
- 网站侧错误（403/412/352/风控）必须带原文，不允许"看起来在跑"（V1 §1.3）
- 不允许裸 `except:`，ruff `BLE001` 强制

---

## 3. 已实现平台

| 平台 | name | capabilities | 里程碑 |
|---|---|---|---|
| 抖音 | `douyin` | needs_browser=True, needs_cookies=True, cookie_variants=('exported_file','browser','none'), supports_subtitles=False, supports_dash_split=False, list_strategy='browser_scroll', media_strategy='yt_dlp_with_fallback' | V2.0 |
| B站 | `bilibili` | needs_browser=False, needs_cookies=True, cookie_variants=('exported_file','browser','anonymous'), supports_subtitles=True, supports_dash_split=True, list_strategy='api', media_strategy='yt_dlp' | V2.0 |
| 小红书 | `xiaohongshu` | needs_browser=True, needs_cookies=True, cookie_variants=('exported_file','browser'), supports_subtitles=False, supports_dash_split=False, list_strategy='browser_scroll', media_strategy='yt_dlp' | V2.1 |
| YouTube | `youtube` | needs_browser=False, needs_cookies=False, cookie_variants=(), supports_subtitles=True, supports_dash_split=False, list_strategy='yt_dlp_flat', media_strategy='yt_dlp' | V2.1 |

---

## 4. 平台特有契约

### 4.1 抖音

- **`parse_creator_url` 必须跟随 302**：`v.douyin.com/<code>/` 短链不含身份，必须跟一次 302 才能拿到规范主页与 `sec_uid`（V1 §7.1）
- **`download_media` 默认走兜底**：抖音对非浏览器客户端做风控（`a_bogus` 签名），yt-dlp 从来没有产出一片媒体，`yt_dlp_with_fallback` 是常态而不是故障（V1 §7.2）。`MediaArtifact.media_source` 几乎总是 `'page_play_url'`，`yt_dlp_error` 必须保留 yt-dlp 的失败原文
- **CDN 播放直链是签名的、不带 cookie，几小时后失效**：不要把 URL 当持久数据
- **身份是 `sec_uid`，不是 URL 里的东西**

### 4.2 B站

- **cookie 三档阶梯**（V1 §7.15）：`exported_file > browser > anonymous`，每档画质不同（登录档 1772p@59.94，匿名档 886p@29.97）。`download_media` 必须按 `capabilities.cookie_variants` 顺序尝试，**清单 note 必须写清是哪一档**
- **DASH 未合并分片**（V1 §7.21）：yt-dlp 合并成功只留 `<名字>.mp4`，没做成留 `<名字>.f<格式号>.mp4` + `<名字>.f<格式号>.m4a`。`MediaArtifact` 用 `VideoAudioPairArtifact` 表达。转写层认音频轨
- **列表枚举走公开 web-interface**，但无 cookie 时随机回 `Request is rejected by server (352)` / `Request is blocked by server (412)`，**同一台机器上一条过一条不过**，所以必须带导出 cookie
- **字幕优先**：`fetch_subtitles` 命中时 `PostprocessTask` 跳过 ASR

### 4.3 小红书

- **全走桥**：列表枚举与详情都要桥内页面 JS 注入（V1 §7 契约三：占位符不加引号）
- **登录态是会话级的**：cookie 过期后媒体会 403，需要重新在桥里登录

### 4.4 YouTube

- **纯 yt-dlp**，不需要桥
- **本机网络不可达时如实失败**（V1 §6），不要伪造
- **`yt-dlp-ejs` 是 yt-dlp 的 JS 挑战插件，跑时要调 node**

---

## 5. 添加新平台的步骤

1. 在 `src/intelligence_hub_v2/platforms/<name>/` 起新包
2. 实现 `PlatformAdapter` Protocol（参考 `douyin/adapter.py`）
3. 声明 `Capabilities`
4. 写 `<Name>Config(PlatformConfig)` Pydantic 模型
5. 在 `platforms/registry.py` 注册：`PLATFORMS["<name>"] = <Name>Adapter`
6. 在 `config/platforms.yaml` 加默认配置
7. 在 `tests/contracts/test_<name>_adapter.py` 继承 `PlatformAdapterContractTests`，加平台特有契约用例
8. 前端 `<PlatformBadge>` 加几何形状与色板（`ui-tokens.md`）
9. 更新本文件的"已实现平台"表
10. **不需要改调度器**

---

## 6. V3 重写时的契约

V3 即使把某个平台的实现从 Python 重写成 Go/Rust（通过 subprocess + JSON-RPC 接入），只要：

1. 还实现 `PlatformAdapter` Protocol（RPC 层包一个 Python shim 即可）
2. `Capabilities` 声明不变
3. 配置 Pydantic 模型不变
4. `MediaArtifact` / `VideoMeta` / `Transcript` 等数据模型不变
5. 异常带 `platform` / `stage` / 原文

→ 调度层零改动，前端零改动，契约测试套件零改动。
