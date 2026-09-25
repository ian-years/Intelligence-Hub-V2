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

    # ---- 评论（可选能力，ADR-0020 决定二） ----
    async def fetch_comments(
        self, video: "VideoMeta", *, limit: int = 50, sort: str = "hot"
    ) -> list["VideoCommentDraft"] | None:
        """顶层评论草稿。`capabilities.supports_comments=False` 的平台**直接返回 None**，
        与 `fetch_subtitles` 同一条契约：抛会让"每条作品都先失败一次"变成常态。

        返回 `[]`（这条确实没评论）与返回 `None`（这一家不支持）必须可区分，
        所以声明了 True 的实现**不许**返回 None —— 那是声明与实现不一致，
        `tasks/enrich_metrics.py` 会把它记成一条失败而不是"零条"。

        `sort` 只认 `"hot"` / `"new"` 两个词：平台的数字排序档是实现细节，
        不许漏到 handler 与任务参数里。认不出的词要抛（"静默按 hot 排"会长得
        完全像"热评就这些"）。"""

    # ---- 读数补抓（人人有这个方法，能不能干活由实现如实回答） ----
    async def fetch_metrics(self, video: "VideoMeta") -> "MetricReadings":
        """再问一次这条作品当前的计数。**不返回窗口**：窗口由调用方按
        `taken_at - published_at` 算（`tasks/enrich_metrics.window_for`），
        同一个判断放两处迟早漂。

        只有两种合法答案：
        - 一项以上非空的 `MetricReadings`；
        - 抛 `PlatformError(stage="metrics")` 并带原文（"这一家的数只在签名接口里"）。

        **不许交回四项全 NULL 的读数**：那会被当成"这个窗口已经抓过了"，
        于是这条作品从此不再被 `video_ids_missing` 挑中 —— 一次失败换来一个永久盲点
        （同一判据见 `MetricSnapshotRepository.put` 的拒收）。
        四项全 0 **是**有效读数（"刚发出去真没人看"与"没给数"是两个答案）。

        不另开能力位：任何能枚举出作品计数的一方都能补抓一次读数；
        做不到是**错误**（要带原因），不是"不支持"（那是 None 的语义）。"""
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

    supports_comments: bool = False
    """B站 True（`x/v2/reply`），抖音/小红书/YouTube False。
    False 时 `fetch_comments` 直接返回 None，不抛。

    **带默认值的字段只能排在最后**：这是 `@dataclass(frozen=True, slots=True)`，
    无默认值的字段跟在有默认值的后面会当场 `TypeError`（实例化时，不是导入时）。
    这一格踩过：把 `supports_comments` 插在 `supports_dash_split` 前面，
    报出来的是"non-default argument follows default argument"，与平台无关，
    看的人第一反应是"我声明写错了"而不是"顺序错了"。
    新增能力位时往**末尾**加。"""
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

> `VideoCommentDraft` / `MetricReadings` 定义在 `models/engagement.py`
> （表侧的那两个兄弟 `VideoComment` / `MetricSnapshot` 与 CHECK 约束见
> `data-model.md §2.11 / §2.12`），本节只列适配器**说出口**的那些类型。
>
> **两条 import 惯例同时成立，各有方向**（照抄错方向不会红，只会漂）：
> - 平台适配器 import 数据模型时走 `models.*`（`models.creator` / `models.media` /
>   `models.video` / `models.engagement`），**不绕 `platforms.base`** —— 四家现在都是这样。
> - `platforms/base.py` 把这些名字 re-export 一份（在它的 `__all__` 里），服务对象是
>   **只看契约、不看实现** 的消费方：`tests/contracts/_doubles.py`、注册表测试、
>   以及 V3 那份"换语言重写时要对齐的清单"。
>
> 判据是方向不是位置：谁需要"协议的参数长什么样"就从 base 拿，
> 谁需要"这个平台自己要造一个读数对象"就从 models 拿。

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
    path: Path                  # 见下方"路径归谁算"（Task 6 修订）
    size_bytes: int
    media_source: Literal["yt_dlp", "page_play_url", "dash_merged"]
    yt_dlp_error: str | None = None    # **只装失败原文**（V1 §7.2）
    cookie_rung: str | None = None     # 实际用了哪一档 cookie（V1 §7.15；见 docs/adr/0011 追记）
    duration_seconds: float | None = None
    has_audio: bool = True
    has_video: bool = True
    extra_paths: tuple[Path, ...] = ()

> **修订（2026-09-24，ADR-0019）—— `extra_paths` 与 `has_video`**：小红书有图文笔记
> （一条笔记 = 最多 18 张原图，没有视频轨），而这一族产物在今天**没有表达方式**。
> 契约只给了两个选项，都不诚实，所以扩了字段而不是新加一种 `kind`：
>
> - `extra_paths` —— 同一趟采集带回来的其他文件。图文笔记的主文件是第一张图，
>   第 2~N 张进这里；入库后落在 `videos.media_aux_paths_json`，**那一列的语义因此从
>   "DASH 音频轨"变成"同一条作品的其他产物文件"**（见 `data-model.md §2.3`）。
> - `has_video=False` —— 这个"媒体文件"根本不可播放。判据必须在字段上，不能靠后缀：
>   一张 3 MB 的 jpg 是真的躺在 `media_path` 里，喂给 `<video>` 只会得到一块**不报错的黑屏**
>   （T6.5 播放器因此先问这一位，再问 `media_path` 在不在）。
> - `size_bytes` 的口径同时变成"主文件 + `extra_paths` 之和"（生产方负责），
>   所以 `total_size_bytes()` 对单文件直接返回它、不做二次相加。
>
> 转写链不受这个扩张影响，靠的是 `has_audio` 而不是"aux 非空"：
> `audio_path_of()` 在 `has_audio=False` 时**抛**，`postprocess._audio_source()` 认
> `metadata_json.has_audio`。看护：`tests/unit/test_media_artifact_kinds.py`
> （联合成员从 `get_args(MediaArtifact)` 取，新加一种产物而不补入库分支 → 红）与
> `tests/unit/tasks/test_image_note_artifact_contract.py`。

> **修订（2026-09-22，Task 6）—— `path` 归谁算**：本 `path` 原写作"相对 `data/` 的路径"，
> 实施时**不成立**：`AdapterDeps` 里没有 `FileStorage`（`storage/` 在依赖图最底层，
> 适配器拿不到），而"相对 `data/`"这件事必须先知道数据根在哪。
> 现在的口径是：**适配器返回 `dest` 下的路径**（`dest` 由调用方给），
> 归一化成 `FileStorage.rel()` 是**入库那一层**（Task 8 的 handler）做的。
> 为什么不给 `AdapterDeps` 加一个 `files` 字段：只有"写库"需要相对路径，
> 采集本身不需要；为了省一次转换给所有适配器开一个 storage 形状的洞，
> 等于允许适配器偷偷算路径 —— 那正是 §7.5（转写目录按平台不对称）的成因形状。
> 判据：`tests/contracts/test_douyin_adapter.py::test_signed_play_url_never_reaches_the_artifact`
> 只断言产物里不含 URL，不断言它是相对路径。
> 这条同样适用于 `VideoAudioPairArtifact.video_path` / `audio_path`。

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

class MetricReadings(BaseModel):
    """**一次读数**，不带窗口（`fetch_metrics` 的输出，ADR-0020 决定二）。

    四项都可空，且"空"不等于 0（与 `video_metric_snapshots` 同一口径）：
    小红书公开页的卡片上没有 view_count，写 0 就得到一条"发布时 0 播放"的快照，
    下一轮算 `7d / publish` 时要除以它。
    """
    view_count: int | None = Field(default=None, ge=0)
    like_count: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    share_count: int | None = Field(default=None, ge=0)
    metadata_json: str = "{}"
    """平台给了但契约没有列去放的项（B站 的 coin / favorite）。"""

    def is_empty(self) -> bool:
        """四项全 NULL。**这不是"零互动"** —— 见 §2.1 `fetch_metrics` 那条判据。"""

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
    platform: str | None = None      # runner 级失败不归任何单一平台（V1 §7.22 那一族）
    stage: Literal[
        "parse_url", "list", "download", "transcribe", "store",
        "task",       # 不是某个 item 的某一步挂了，是任务本身挂了
        "metrics",    # 补读数（ADR-0020）
        "comments",   # 抓评论（同上）
    ]
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

> `capabilities` 里那个 `supports_comments` 是 V2.1 T4.1 加的位（ADR-0020 决定二）。
> **四家里只有 B站 True**，但"能补读数"与"能抓评论"是两件事：
> 读数一侧 B站（`view` 的 `stat`）与小红书（详情页的 likes 一项）能做，
> 抖音与 YouTube 如实抛（理由在各自 §4.x 与那两个 `fetch_metrics` 的 docstring 里）。
| 抖音 | `douyin` | needs_browser=True, needs_cookies=True, cookie_variants=('exported_file','browser','none'), supports_subtitles=False, supports_dash_split=False, list_strategy='browser_scroll', media_strategy='yt_dlp_with_fallback', supports_comments=False | V2.0 |
| B站 | `bilibili` | needs_browser=False, needs_cookies=True, cookie_variants=('exported_file','browser','anonymous'), supports_subtitles=True, supports_dash_split=True, list_strategy='yt_dlp_flat', media_strategy='yt_dlp', supports_comments=True | V2.0 |
| 小红书 | `xiaohongshu` | needs_browser=True, needs_cookies=True, cookie_variants=('exported_file','browser'), supports_subtitles=False, supports_dash_split=False, list_strategy='browser_scroll', media_strategy='yt_dlp', supports_comments=False | V2.1 |
| YouTube | `youtube` | needs_browser=False, needs_cookies=False, cookie_variants=('none',), supports_subtitles=True, supports_dash_split=False, list_strategy='yt_dlp_flat', media_strategy='yt_dlp', supports_comments=False | V2.1 T2.2（已注册）|

---

## 4. 平台特有契约

### 4.1 抖音

实现：`src/intelligence_hub_v2/platforms/douyin/`（Task 6 落地）。
分四个模块，各自守一条线：`urls.py`（§7.1 的纯解析）、`listing.py`（页面 JS 与解析）、
`media.py`（§7.2 兜底 + §7.3 阶梯落地）、`adapter.py`（装配与 Protocol）。

- **`parse_creator_url` 必须跟随 302**：`v.douyin.com/<code>/` 短链不含身份，必须跟一次 302 才能拿到规范主页与 `sec_uid`（V1 §7.1）
- **`download_media` 默认走兜底**：抖音对非浏览器客户端做风控（`a_bogus` 签名），yt-dlp 从来没有产出一片媒体，`yt_dlp_with_fallback` 是常态而不是故障（V1 §7.2）。`MediaArtifact.media_source` 几乎总是 `'page_play_url'`，`yt_dlp_error` 必须保留 yt-dlp 的失败原文
- **CDN 播放直链是签名的、不带 cookie，几小时后失效**：不要把 URL 当持久数据
  （所以契约里根本没有能放它的字段，`docs/adr/0011` 顺手删掉了配置里那个
  `persist_play_url` 开关 —— 一个只有 `false` 合法的布尔不是配置）
- **身份是 `sec_uid`，不是 URL 里的东西**
- **只认作品网格里的链接**（`[data-e2e="user-post-list"]`），整页扫 `a[href*="/video/"]`
  会在网格没渲染时把别人的作品算到本博主头上**而且报成功**（V1 实测踩过）。
  网格为空是 `ok:false` + 原文 → `ListError`，**不是**空列表
- **`fetch_metrics` 抛而不是交回空读数**（T4.2）：抖音的计数只在页面上下文里给
  （作品详情接口要签名与页面态），V2 没有那条离线可验的路。空读数会被当成
  "这个窗口抓过了"，于是这条作品从此不再被补抓 —— 一个永久盲点。
  消息里点名 `douyin_collect`（它会把当次枚举到的读数记成 publish 快照）。
- **`sec_uid` 解析有一道域名闸门**（V2 补的，V1 没有）：`douyin.com` 子域 + `iesdouyin.com`
  之外一律认不出。V1 的"路径以 `user/` 开头就取第二段"对任意站点成立，
  于是 `https://example.com/user/x` 会生成一个看起来合法的 `platform_id`（坑 20）
- **`since=` 在抖音这一侧是弱过滤**：网格卡片上没有发布时间，`published_at` 是 None。
  口径是"判得了才跳过，判不了就放行"；真增量靠 `videos` 表按 `platform_video_id` 查重
  （经验 18）
- **cookie 阶梯的顺序只在 `capabilities` 里**（`docs/adr/0011`）；配置与 V1 的
  `DOUYIN_YTDLP_COOKIES_FILE` / `_FROM_BROWSER` 只决定"这一档用哪个文件 / 哪个浏览器"。
  浏览器档**默认不存在**（V1 §7.3：Windows 上那一档永远读不出来，而它的错会触发退档）。
  **只有表头的 cookie 文件不算导出文件档**（§7.15 的另一半：它会让整条链路静默退成匿名档）

### 4.2 B站

- **cookie 三档阶梯**（V1 §7.15）：`exported_file > browser > anonymous`，每档画质不同（登录档 1772p@59.94，匿名档 886p@29.97）。`download_media` 必须按 `capabilities.cookie_variants` 顺序尝试，**清单 note 必须写清是哪一档**
- **DASH 未合并分片**（V1 §7.21）：yt-dlp 合并成功只留 `<名字>.mp4`，没做成留 `<名字>.f<格式号>.mp4` + `<名字>.f<格式号>.m4a`。`MediaArtifact` 用 `VideoAudioPairArtifact` 表达。转写层认音频轨
- **列表枚举走 `yt-dlp --flat-playlist`**（`docs/adr/0011` 的 Task 7 追记：
  设计文档原来写的 `'api'` 实测不成立 —— `x/space/wbi/arc/search` 匿名请求回 HTML 风控页），
  但无 cookie 时随机回 `Request is rejected by server (352)` /
  `Request is blocked by server (412)`，**同一台机器上一条过一条不过**，
  所以**枚举与下载两条路都得带导出 cookie**（2026-09-22 本机匿名实测复现了那句 412）
- **逐条作品的元数据与字幕轨走公开 web-interface**（实测匿名可访问）：
  `x/web-interface/view`（标题/时长/发布时间/`pages[].cid`）、
  `x/player/v2`（字幕轨列表）、`x/web-interface/card`（博主资料）。
  B站 把业务失败编在 HTTP 200 里（`{"code":-404,...}`），**只看状态码会把"不存在"当成功**
- **`since=` 在 B站 这一侧是实过滤**（与抖音相反）：网格那边没有发布时间，
  所以要逐条问 `view`。因此实现口径是"**传了 `since` 才发这些请求**"——
  没要求按时间过滤时不为填字段多发 N 个请求（一位博主 30 条 × 20 位 = 600 个）
- **字幕优先**：`fetch_subtitles` 命中时 `PostprocessTask` 跳过 ASR
- **评论接口的 `oid` 要数字 `aid`，不是 `bvid`**（T4.1）：给 bvid 会回
  `code=-400 请求错误` 而 **HTTP 是 200**，那句报错长得像"接口挂了"，实际是参数身份不对。
  所以 `fetch_comments` 先问一次 `view` 拿 `aid`；**拿不到 aid 就当场抛**，
  不去发那个注定失败的请求（否则清单里留下的原因是"评论接口失败"，方向错一整条）
- **`stat` 才是读数的地方**：`view` 的顶层没有 `view_count`，四项嵌在 `stat` 里。
  读顶层会永远拿到 NULL，于是"补抓一次"静默变成"抓了个空的"
  （`reply`=评论数、`share`=转发数，`coin`/`favorite` **不当作** share：
  收藏与转发回答的是两个问题，这一格最容易被人"反正都是个位数级互动"地合并）
- `sort` 的两个契约名落成 B站 的 `2`（按赞）与 `0`（按时）；认不出的名字抛，不"当作 hot"

### 4.3 小红书

- **全走桥**：列表枚举与详情都要桥内页面 JS 注入（V1 §7 契约三：占位符不加引号）
- **登录态是会话级的**：cookie 过期后媒体会 403，需要重新在桥里登录
- **`supports_comments=False`**：评论区是另一条签名接口（`/api/sns/web/v2/comment/page`），
  V2 没有实现路径，所以声明位保持 False 且 `fetch_comments` 返回 None ——
  "声明了能力但没有实现"等于对调度器撒谎（`Capabilities` 的判据）
- **`fetch_metrics` 只能给一项**（详情页的 `likes`，T4.2）：其余三项留 NULL 并把
  `{"only": ["like_count"]}` 写进 `metadata_json`。这一家与抖音的差别不在"要不要实现"，
  在**这条路是否已经存在**：详情页本来就要为下载去访问且离线可验（假桥 double），
  顺手回一个赞数是同一次访问的副产品
- 复用详情页带来一个 stage 副作用：那一步自己把失败报成 `media`。`fetch_metrics`
  会换成 `metrics` 再抛，**原文一字不动** —— 否则清单会把它排到"去查 yt-dlp / 直链"那一格

### 4.4 YouTube

- **`fetch_metrics` / `fetch_comments` 都如实回答"不做"**（T4.2）：单条 `-J` 确实能回
  view/like，但那是为"补一个数"再跑一次几秒到几十秒的完整解析，
  而同一次采集的 flat-playlist 已经带回这些计数。所以抛出的那句里点名
  `youtube_collect` —— **"做不到"要连着"该做什么"一起给**，否则拿到的是一句没有下一步的红。
  评论要走 InnerTube 那套未公开接口，V2 没有需要它的验收，声明位保持 False。
- **纯 yt-dlp**，不需要桥，也**不带任何 cookie**：`cookie_variants=('none',)` 是真实档位
  （`plan_cookie_variants` 给出的那一档 argv 为空），不是"没有阶梯"。
  所以 V1 §7.15 那一族（枚举与下载都要带导出 cookie）在这一族不可能发生。
- **本机网络不可达时如实失败**（V1 §6）：`healthcheck()` 的 `network` 一格回
  `unreachable` 并带 httpx 原文；`yt_dlp` 缺一格在这里是 `unreachable` 而不是 `degraded`
  （没有第二条媒体路）。配了 `proxy` 时探活与 yt-dlp 走同一个代理。
- **`--recent-days` 的语义进 `since`**：窗外丢弃、窗内保留、**日期缺失的条目保留**并计数
  （V1 "不编造时间，按原顺序取"）。判"缺失即丢"会让整位博主空手，两个判法的后果不同。
- **未合并的 DASH 分片 = 失败**：`supports_dash_split=False` 的兑现方式是判失败并点名
  ffmpeg，而不是挑一条交出去（V1 的 `glob("*.mp4")` 恰好会交出**纯视频轨**）。
- **字幕真的去取**（`supports_subtitles=True`）：`--skip-download --write-auto-subs` 落一份
  `.vtt` 到专用临时目录再解析；**确认没有轨**才回 None，跑失败要抛。
- **`yt-dlp-ejs` 是 yt-dlp 的 JS 挑战插件，跑时要调 node**（V1 §4.1）：
  探到 node 才加 `--js-runtimes node`；`advanced.require_node` 决定缺它时红不红。
- **V1 的 `SHOW_YOUTUBE` 环境变量没有搬**：V2 用平台开关（`enabled`）天然覆盖那一格。

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
