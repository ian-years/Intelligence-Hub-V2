# ADR-0006: 数据模型与存储层

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q7、`docs/specs/data-model.md`

## 背景

V1 的数据层有几处债务（详见 V1 `AGENTS.md` §7）：

- §7.4 `local_store.upsert_video()` 是**整行覆盖**语义，传空标题会被折叠成 `"精选自媒体作品"`，部分字段 upsert 会抹掉真实标题
- §7.7 `creators.json`（顶层是 list）和 SQLite `creators` 表两个源，双源不一致
- §7.11 `videos` 表里叫 `creator_id`，外部数据源叫 `creator_platform_id` / `mid`，三种命名
- §7.12 预检里的 `sqlite` 检查的是飞书镜像库 `feishu-base.sqlite3`，不是主库 `local.sqlite3`
- §7.24 跟踪开关默认值倒置（`!!undefined === false`），从感知栏粘贴收录的博主静默不进日更
- §7.25 删视频要双删——库行 + `hidden-videos.json` 墓碑，散落两处

V2 要一次性收口：单一真源 SQLite、字段级更新、墓碑内化、统一命名、Alembic 版本化迁移。

## 决定

### 单一真源 = SQLite，文件只放二进制产物

```
data/
  intelligence_hub.sqlite3             # 主库（WAL 模式）
  manifests/                           # 清单 JSON
  media/<platform>/<creator_name>/<video_id>-<safe_title>/
    media.mp4 / media.f137.mp4 + media.f140.m4a / cover.jpg / metadata.json / transcript/
  cookies/                             # Netscape 格式，yt-dlp 直读
  cdp-bridge-profile/                  # Chrome 用户数据
  asr-models/                          # SenseVoice 模型（gitignore）
  logs/
```

媒体路径在 DB 里存**相对路径**（相对 `data/`），整个数据目录可以搬走、备份、迁移。

### Schema（SQLAlchemy Core + Alembic）

完整 schema 见 `docs/specs/data-model.md`。关键决策：

1. **`creators`**：唯一身份 = `(platform, platform_id)`，废 V1 的 `creators.json`。`is_tracking` 用真布尔 + `CHECK (is_tracking IN (0,1))`（V1 §7.24 看护）。
2. **`videos`**：墓碑内化为 `is_hidden BOOLEAN + hidden_at + hidden_reason`，废 `hidden-videos.json`（V1 §7.25）。所有查询走 `list_visible()`，自动过滤。
3. **`transcripts`**：独立表，`video_id` 是 PK + FK ON DELETE CASCADE。`text_path` 统一存 `speech-clean.txt` 路径，废 V1 §7.5 的"按平台不对称"。
4. **`task_runs` + `task_events` + `manifests`**：任务历史与事件流持久化。
5. **`platforms`**：配置来自 YAML，运行态镜像一份方便查询（含 `health_status` / `health_checked_at`）。
6. **`feishu_sync_state`**：废 V1 独立 `feishu-base.sqlite3`，状态收进主库（V2.2 实施）。
7. **统一字段名**：所有外部数据源的 `creator_platform_id` / `mid` 都映射到 `platform_id`（V1 §7.11 解决）。

### Repository 层（彻底解决 V1 §7.4 整行覆盖）

```python
class VideoRepository(Protocol):
    async def get(self, id: int) -> Video | None: ...
    async def find_by_platform_id(self, platform: str, platform_video_id: str) -> Video | None: ...
    async def insert(self, video: VideoDraft) -> Video: ...

    # 关键：字段级更新，不再"整行覆盖"
    async def update_fields(
        self, id: int, **fields: Unpack[VideoUpdatableFields]
    ) -> Video: ...
    """只更新传入的字段，其他字段保留原值。TypedDict 限定可更新字段集。"""

    async def hide(self, id: int, reason: str) -> Video: ...
    async def unhide(self, id: int) -> Video: ...
    async def list_visible(self, *, filters: VideoFilters, page: Page) -> PagedResult[Video]: ...
```

**`update_fields` 实现纪律**：
- 用 SQLAlchemy Core `update()` 语句，**只 SET 传入的列**
- TypedDict + mypy 限定哪些字段可更新（`title` 可，`platform` 不可）
- 单元测试覆盖"部分字段更新不能抹掉其他字段"（V1 §7.4 回归看护）

### Storage 抽象

```python
class Storage(Protocol):
    creators: CreatorRepository
    videos: VideoRepository
    transcripts: TranscriptRepository
    task_runs: TaskRunRepository
    events: EventRepository
    manifests: ManifestRepository
    platforms: PlatformRepository

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]: ...
    async def close(self) -> None: ...
```

实现：`SqliteStorage`（生产，aiosqlite + SQLAlchemy 2.0 async）+ `InMemoryStorage`（测试，SQLite `:memory:`）。

### Alembic 迁移

- `alembic/versions/0001_initial_schema.py` 起
- CI 必须跑 `alembic upgrade head` + `alembic downgrade base` + `alembic upgrade head`，确保可逆
- V1 → V2 数据迁移走 `tools/migrate_from_v1.py`，**不用 Alembic**（Alembic 只管 V2 内部 schema 演化）

### V2.0 范围

只移植核心三件套（creators / videos / transcripts）+ 任务调度 + 配置面板。`topics` / `drafts` / `feishu_sync` 留 V2.1+。

## 后果

**好处**：
- V1 §7.4 整行覆盖 → `update_fields()` 字段级更新 + 回归测试
- V1 §7.7 双源 → 废 `creators.json`，单一 SQLite
- V1 §7.11 三种命名 → 统一 `platform_id`，schema 强制
- V1 §7.12 预检主库错位 → 只有一个库，无可错位
- V1 §7.24 跟踪开关 → `CHECK` 约束 + Pydantic 入口校验
- V1 §7.25 墓碑散落 → 内化为 `is_hidden` 列，所有查询走 `list_visible()` 自动过滤
- 媒体路径相对存储 → V3 换数据目录、上对象存储（S3/MinIO）时，只需要重写 Storage 实现

**代价**：
- SQLAlchemy Core 比 V1 直接 `sqlite3.connect()` 重，但换来类型安全与迁移工具
- Alembic 迁移要写 upgrade + downgrade，前期投入大
- 单一 SQLite 在高并发写场景下有锁竞争（缓解：WAL 模式 + 任务级 Semaphore 限流）

**对 V3 的意义**：
- Schema = 数据契约。V3 换 PostgreSQL 也能用 Alembic 迁移过去（SQLAlchemy Core 跨方言）
- Repository Protocol = 实现契约。V3 重写存储层时，业务代码零改动
- 媒体文件路径全是相对的 → V3 换数据目录、上对象存储，只需要重写 Storage 实现
