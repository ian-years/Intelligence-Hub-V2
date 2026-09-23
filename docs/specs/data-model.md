# Spec: Data Model

> **状态**：Locked（V2.0 起 schema 稳定，改动走 Alembic 迁移 + ADR）
> **源文件**：`src/intelligence_hub_v2/storage/schema.py` + `alembic/versions/`
> **相关 ADR**：[0006](../adr/0006-data-model-and-storage.md)

SQLite schema 的契约。V3 换 PostgreSQL 时，这份 schema 是迁移目标。

---

## 1. 文件布局

```
data/                                  # 由 INTELLIGENCE_HUB_DATA_DIR 控制，默认 ./data/
  intelligence_hub.sqlite3             # 主库（WAL 模式）
  intelligence_hub.sqlite3-wal         # WAL 日志
  intelligence_hub.sqlite3-shm         # 共享内存
  manifests/                           # 清单 JSON（审计 + 离线分析）
    20260922-120000-douyin_collect-<task_id>.json
  media/
    douyin/<creator_name>/<video_id>-<safe_title>/
      media.mp4                        # 主媒体（合并后）
      media.f137.mp4 + media.f140.m4a  # 或 DASH 未合并分片（B站）
      cover.jpg
      metadata.json                    # 平台原始 metadata（审计用）
      transcript/
        speech-clean.txt
        segments.json
    bilibili/...
    xiaohongshu/...
    youtube/...
  cookies/                             # Netscape 格式，yt-dlp 直读
    douyin.com.txt
    bilibili.com.txt
    xiaohongshu.com.txt
  cdp-bridge-profile/                  # Chrome 用户数据（桥的持久化 profile）
  asr-models/                          # SenseVoice 模型（gitignore）
  logs/
    server.log
  tmp/                                 # 任务专属临时目录
    <task_id>/
  .migration_state.json                # V1→V2 迁移进度（仅迁移期间存在）
```

**媒体路径在 DB 里存相对路径**（相对 `data/`），整个数据目录可以搬走、备份、迁移。

> **实施期修订（2026-09-22，Task 4）· 清单文件名多了 `task_id` 段。**
> 原来锁的是 `<8位日期>-<6位时间>-<kind>.json`。**同一秒起跑的两个同 kind 任务会撞名**：
> 后写的那份 `os.replace` 静默盖掉前一份，DB 里两条索引指向同一个文件，
> 而文件内容是后那一份 —— 前一个任务的审计凭据凭空消失且不报错。
> `all_platforms` 这个任务的存在就是为了让它们并行，所以这不是理论风险。
> 看护：`test_two_tasks_started_in_the_same_second_get_different_files`（`files.py` 层）与
> `test_two_runs_get_two_files_and_two_index_rows`（`manifest_writer` 层，第二条就是它抓出来的）。
>
> 时间戳仍是 **UTC** 且仍在最前面，所以"按文件名排序 = 按时间排序"这个性质没变；
> 前缀段不变意味着 V1→V2 迁移脚本（Task 15）搬老清单时只需在尾巴上补 `task_id`。

---

## 2. Schema

### 2.1 `platforms`

```sql
CREATE TABLE platforms (
    name              TEXT PRIMARY KEY,           -- 'douyin' / 'bilibili' / ...
    enabled           BOOLEAN NOT NULL,
    config_json       TEXT NOT NULL,              -- 配置快照（Pydantic 模型 dump）
    health_status     TEXT,                       -- 'ok'/'degraded'/'unreachable'/'unknown'
    health_checked_at TIMESTAMP,
    health_detail     TEXT,
    CHECK (enabled IN (0, 1)),
    CHECK (health_status IS NULL OR health_status IN ('ok','degraded','unreachable','unknown'))
);
```

配置真源在 `config/platforms.yaml`，运行态镜像一份到这张表方便查询（含健康状态）。

### 2.2 `creators`

```sql
CREATE TABLE creators (
    id              INTEGER PRIMARY KEY,
    platform        TEXT NOT NULL REFERENCES platforms(name) ON DELETE RESTRICT,
    platform_id     TEXT NOT NULL,                -- sec_uid / mid / user_id / channel_id（统一字段名）
    name            TEXT NOT NULL,
    avatar_url      TEXT,
    follower_count  INTEGER,
    profile_url     TEXT NOT NULL,
    is_tracking     BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    UNIQUE(platform, platform_id),
    CHECK (is_tracking IN (0, 1))
);
CREATE INDEX idx_creators_platform ON creators(platform);
CREATE INDEX idx_creators_tracking ON creators(is_tracking);
```

**唯一身份 = `(platform, platform_id)`**。废 V1 的 `creators.json` 双源（V1 §7.7）。
**`is_tracking` 必须真布尔** + `CHECK` 约束兜底（V1 §7.24）。

### 2.3 `videos`

```sql
CREATE TABLE videos (
    id                   INTEGER PRIMARY KEY,
    platform             TEXT NOT NULL,
    platform_video_id    TEXT NOT NULL,           -- aweme_id / bvid / note_id / video_id
    creator_id           INTEGER REFERENCES creators(id) ON DELETE SET NULL,
    title                TEXT NOT NULL,
    description          TEXT,
    published_at         TIMESTAMP,
    duration_seconds     REAL,
    view_count           INTEGER,
    like_count           INTEGER,
    comment_count        INTEGER,
    share_count          INTEGER,
    media_path           TEXT,                    -- 主文件相对路径
    media_source         TEXT,                    -- 'yt_dlp'/'page_play_url'/'dash_merged'/'dash_split'
    media_aux_paths_json TEXT NOT NULL DEFAULT '[]',  -- DASH 分片等辅助文件（JSON 数组）
    cover_path           TEXT,
    metadata_json        TEXT NOT NULL DEFAULT '{}',
    is_hidden            BOOLEAN NOT NULL DEFAULT FALSE,
    hidden_at            TIMESTAMP,
    hidden_reason        TEXT,
    created_at           TIMESTAMP NOT NULL,
    updated_at           TIMESTAMP NOT NULL,
    UNIQUE(platform, platform_video_id),
    CHECK (is_hidden IN (0, 1)),
    CHECK (media_source IS NULL OR media_source IN ('yt_dlp','page_play_url','dash_merged','dash_split'))
);
CREATE INDEX idx_videos_creator ON videos(creator_id);
CREATE INDEX idx_videos_platform_published ON videos(platform, published_at DESC);
CREATE INDEX idx_videos_visible ON videos(is_hidden, published_at DESC);
CREATE INDEX idx_videos_created ON videos(created_at DESC);
```

**墓碑内化为列**（V1 §7.25 解决）：`is_hidden` + `hidden_at` + `hidden_reason`，废 `hidden-videos.json`。所有查询走 `list_visible()`，自动过滤。

**`creator_id` 用 `ON DELETE SET NULL`**：删博主不删视频，视频变成"孤儿"但仍可查（`creator_id IS NULL`）。

### 2.4 `transcripts`

```sql
CREATE TABLE transcripts (
    video_id        INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    engine          TEXT NOT NULL,                -- 'sherpa_sense_voice'/'bilibili_subtitle'/'youtube_subtitle'/'manual'
    language        TEXT,
    char_count      INTEGER NOT NULL,
    sentence_count  INTEGER NOT NULL,
    text_path       TEXT NOT NULL,                -- 相对 data/，统一 'media/.../transcript/speech-clean.txt'
    segments_json   TEXT,                         -- JSON 数组 [{start_seconds, end_seconds, text}, ...]
    created_at      TIMESTAMP NOT NULL
);
```

**`text_path` 统一**（V1 §7.5 解决）：废"按平台不对称"的转写目录。

### 2.5 `task_runs`

```sql
CREATE TABLE task_runs (
    id                     TEXT PRIMARY KEY,      -- UUID
    task_name              TEXT NOT NULL,
    kind                   TEXT NOT NULL,
    status                 TEXT NOT NULL,         -- 'running'/'success'/'partial'/'failed'/'timeout'/'cancelled'
    params_json            TEXT NOT NULL,
    config_snapshot_json   TEXT NOT NULL,
    started_at             TIMESTAMP NOT NULL,
    ended_at               TIMESTAMP,
    summary_json           TEXT,
    manifest_path          TEXT,
    error_text             TEXT,
    progress               REAL NOT NULL DEFAULT 0.0,
    CHECK (status IN ('running','success','partial','failed','timeout','cancelled')),
    CHECK (progress >= 0.0 AND progress <= 1.0)
);
CREATE INDEX idx_task_runs_started ON task_runs(started_at DESC);
CREATE INDEX idx_task_runs_status ON task_runs(status);
CREATE INDEX idx_task_runs_name ON task_runs(task_name);
```

### 2.6 `task_events`

```sql
CREATE TABLE task_events (
    id           INTEGER PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    timestamp    TIMESTAMP NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX idx_task_events_task_time ON task_events(task_id, timestamp);
CREATE INDEX idx_task_events_type_time ON task_events(type, timestamp);
```

事件 schema 见 `event-schema.md`。

**保留策略**：已完成任务的事件保留 30 天（`storage.event_retention_days`），失败任务保留 90 天。

### 2.7 `manifests`

```sql
CREATE TABLE manifests (
    id              INTEGER PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    schema_version  TEXT NOT NULL,                -- '2.0'
    file_path       TEXT NOT NULL,                -- 相对 data/
    written_at      TIMESTAMP NOT NULL,
    content_json    TEXT NOT NULL                 -- 完整清单内容（冗余，方便查询）
);
CREATE INDEX idx_manifests_task ON manifests(task_id);
CREATE INDEX idx_manifests_written ON manifests(written_at DESC);
```

**双写**（文件 + SQLite）：前端历史列表查 SQLite，详情/审计查文件。

### 2.8 `topics`（V2.2 实施）

```sql
CREATE TABLE topics (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT,
    created_at   TIMESTAMP NOT NULL
);

CREATE TABLE video_topics (
    video_id     INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    topic_id     INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, topic_id)
);
```

### 2.9 `drafts`（V2.2 实施）

```sql
CREATE TABLE drafts (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    source_video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'draft',   -- 'draft'/'published'/'archived'
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    CHECK (status IN ('draft','published','archived'))
);
```

### 2.10 `feishu_sync_state`（V2.2 实施）

```sql
CREATE TABLE feishu_sync_state (
    record_kind      TEXT NOT NULL,               -- 'video'/'creator'/'transcript'
    local_id         INTEGER NOT NULL,
    feishu_record_id TEXT NOT NULL,
    synced_at        TIMESTAMP NOT NULL,
    payload_json     TEXT NOT NULL,
    PRIMARY KEY (record_kind, local_id)
);
```

废 V1 独立 `feishu-base.sqlite3`，状态收进主库。

---

> **实施期修订（2026-09-23，Task 3 收口）· §2 的 DDL 抄本与 `storage/schema.py` 差四处，以本条为准。**
>
> 代码 ↔ 迁移两侧的一致性有 `check_schema_matches_migrations()` 和
> `test_the_migrated_db_has_exactly_the_checks_we_wrote` 守着；**spec 与代码之间没有守卫**，
> 这一条就是去补那一处漂移（AGENTS §6：「动的是 SQLAlchemy schema？→ spec 同步了吗」）。
>
> 1. **§2.4 `transcripts` 实有三条 CHECK，§2 一条都没写**：`ck_transcripts_engine_enum`
>    （engine ∈ `TRANSCRIPT_ENGINES`）、`ck_transcripts_char_count_nonneg`、
>    `ck_transcripts_sentence_count_nonneg`。engine 那条是 V1→V2 迁移脚本撞出来的：
>    provenance 只能走自由格式的 `metadata_json`，**不许占用枚举列**（§6 的映射因此而定型）。
> 2. **§2.5 `task_runs` 是三条 CHECK 不是两条**：多出的 `ck_task_runs_terminal_has_ended_at`
>    （`status = 'running' OR ended_at IS NOT NULL`）是 **V1 §2 契约二的 DB 半边** ——
>    它让"声称 success 却没有 ended_at"这一状态在库里根本不可表示。
>    §2 对此一字未提，是四处差异里最不该漏的一处。
> 3. **§2.3 / §2.5 / §2.7 写的五个索引带 `DESC`，实际全是 ASC**（见
>    `alembic/versions/0001_initial_schema.py` 与 dump 出的 DDL：
>    `CREATE INDEX idx_videos_visible ON videos (is_hidden, published_at)`）。
>    排序方向由查询侧的 `ORDER BY` 决定，索引方向不承重；写 ASC 是因为 SQLite 对
>    混合方向索引起不来，别让下一个人以为改回来是免费的。
> 4. **§2.2 / §2.3 的 `UNIQUE(platform, platform_id)` 写成了表约束，实际是命名唯一索引**：
>    `uq_creators_platform_platform_id`、`uq_videos_platform_platform_video_id`。
>    语义等价，但 V3 照 §2 逐字抄会得到 `sqlite_autoindex_*` 那种匿名名，
>    `downgrade()` 与 `ON CONFLICT` 都指不到它。
>
> V3 换 PostgreSQL 时，**以 `storage/schema.py` + `0001_initial_schema.py` 为准**，
> §2 的 SQL 块只当导读。

---

## 3. Repository Protocol

```python
from typing import Protocol, Unpack
from typing_extensions import TypedDict


class VideoUpdatableFields(TypedDict, total=False):
    """允许 update_fields() 更新的字段集。TypedDict + mypy 限定。"""

    title: str
    description: str | None
    published_at: datetime | None
    duration_seconds: float | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    share_count: int | None
    media_path: str | None
    media_source: str | None
    media_aux_paths_json: str | None
    cover_path: str | None
    metadata_json: str | None
    creator_id: int | None
    # 注意：platform / platform_video_id / is_hidden 不可更新（要走专门方法）


class VideoRepository(Protocol):
    async def get(self, id: int) -> Video | None: ...
    async def find_by_platform_id(self, platform: str, platform_video_id: str) -> Video | None: ...
    async def insert(self, video: VideoDraft) -> Video: ...

    # 关键：字段级更新，不再"整行覆盖"（V1 §7.4 解决）
    async def update_fields(self, id: int, **fields: Unpack[VideoUpdatableFields]) -> Video: ...

    async def hide(self, id: int, reason: str) -> Video: ...
    async def unhide(self, id: int) -> Video: ...
    async def list_visible(self, *, filters: VideoFilters, page: Page) -> PagedResult[Video]: ...
    async def attach_transcript(
        self, video_id: int, transcript: TranscriptDraft
    ) -> TranscriptRecord: ...
    async def count(self, *, filters: VideoFilters | None = None) -> int: ...
```

> **实施期修订（2026-09-22, Task 3）**：`attach_transcript()` 原本写的是返回 `Transcript`。
> 那是不成立的：`Transcript` 有必填的 `text` 正文，而正文按本 spec §2.4 的设计**只在磁盘上**，
> DB 行里没有。返回它要么把整篇稿子读回来（白白一次磁盘 IO），要么造一个 `text=""` 的假对象
> （撒谎）。改成 `TranscriptRecord`（存储层行）。
> 采集层拿到的仍然是 `Transcript` —— 两个类型的分工见 `models/transcript.py` 的 docstring。
> **这条改动同时约束 V3**：`Transcript` 不属于存储层，任何"从库里读出口播稿正文"的
> 需求都应走 `FileStorage.transcript_path()` → 读文件。

**`update_fields` 实现纪律**：
- 用 SQLAlchemy Core `update()` 语句，**只 SET 传入的列**
- TypedDict + mypy 限定哪些字段可更新
- 单元测试覆盖"部分字段更新不能抹掉其他字段"（V1 §7.4 回归看护）

**所有 Repository 都遵循同样模式**：`CreatorRepository` / `TranscriptRepository` / `TaskRunRepository` / `EventRepository` / `ManifestRepository` / `PlatformRepository`。

---

## 4. `Storage` 抽象

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
    async def healthcheck(self) -> bool: ...
```

实现：
- `SqliteStorage`（生产，aiosqlite + SQLAlchemy 2.0 async）
- `SqliteStorage.in_memory()`（测试与内存场景，SQLite `:memory:`）

> **实施期修订（2026-09-22, Task 3）**：没有做成第二个类 `InMemoryStorage`。
> 两者差别只有"URL + 建表方式"两处（`:memory:` + `create_all` vs 文件 + Alembic），
> 复制一份实现等于把**漂移的可能性**也复制一份 —— 而 §5 那条 `check_schema_matches_migrations()`
> 看护的存在理由正是"两条建表路径必须一致"，多加一条路径就多加一处要看护的地方。
>
> 因此测试用 `storage` fixture 参数化同一个类的两档（`memory` / `file`），
> 用例数翻倍但实现只有一份。副作用是实打实的：`version_path_separator` 那个坑
> （docs/lessons.md 坑 10）**只有 `[file]` 档红**，只测内存库会完全看不见。
>
> V3 若真要换 PostgreSQL，做法是加一个实现同一个 `Storage` Protocol 的类，
> 而不是给 `SqliteStorage` 加分支。

---

## 5. Alembic 迁移

- `alembic/versions/0001_initial_schema.py` 起
- 每个迁移必须有 `upgrade()` + `downgrade()`
- CI 跑 `alembic upgrade head` + `alembic downgrade base` + `alembic upgrade head`，确保可逆
- V1 → V2 数据迁移走 `tools/migrate_from_v1.py`，**不用 Alembic**

迁移命名：`<rev>_<slug>.py`，如 `0001_initial_schema.py` / `0002_add_video_indexes.py`。

---

## 6. V1 → V2 字段映射

| V1 字段 | V2 字段 | 备注 |
|---|---|---|
| `creators.platform` / `videos.platform` | 同名列，但**取值词汇不同** | **必须翻译**：V1 `local_store.normalize_platform()` 存的是显示名（`抖音` / `B站` / `小红书` / `YouTube`），V2 这一列是 `ForeignKey("platforms.name")` 且存 slug（`douyin` / `bilibili` / …）。原样搬 = 第一条真数据就 FK 失败。表在 `tools/migrate_from_v1.py:V1_PLATFORM_TO_SLUG`，认不出来的平台**记一条错误并跳过该行**，不猜、也不替它建 `platforms` 镜像行 |
| `videos.published_at` | `videos.published_at` | V1 写的是**无时区的本机时间**文本（`now_text()` = `%Y-%m-%d %H:%M:%S`）。按本机时区解读成 aware datetime，并把这个假设随数据记进 `metadata_json.published_at_assumed_tz` —— 不假装它是没有来源的 UTC。不映射它的话 Feed 排序与 `since` 过滤对迁移来的整批行同时失效，而库里看不出异常 |
| `videos.metrics_json` | `view_count` / `like_count` / `comment_count` / `share_count` | 一个文本列拆成四列。解析不出来的**原文**留在 `metadata_json.v1_metrics_json` 并记一条错误，不猜 0 |
| `creators.platform_id` / `creator_platform_id` / `mid` | `creators.platform_id` | 统一命名（V1 §7.11 解决） |
| `videos.creator_platform_id` | `videos.creator_id`（FK） | 通过 `(platform, platform_id)` 查 V2 `creators.id` |
| `videos.transcript_status` / `transcript_char_count` | `transcripts` 表 | 拆出独立表。只有 `已转写` 且稿子非空才搬（实测本机 V1 库：已转写 16 条全部带稿、待转写 5 条全部没有）；`transcripts.engine` 是有 CHECK 枚举的列，来源不明的稿子归 `manual` |
| `videos.is_hidden`（不存在，走 JSON 墓碑） | `videos.is_hidden` | 内化为列（V1 §7.25 解决） |
| `creators.json` | `creators` 表 | 废双源（V1 §7.7 解决） |
| `hidden-videos.json` | `videos.is_hidden` | 废文件。**实际路径是 `downloads/launcher-state/hidden-videos.json`**（V1 `launcher_server.load_state_list()` 一律带 `downloads/` 前缀），少一层前缀 = 读不到 = 静默空集 = 用户删过的作品整批复活。取键口径照 V1 `hidden_video_keys()`：只认 `platform_video_id` 与 `record_id`（外加 `local:<平台>:<vid>` 的尾段），**条目自带的那个 `id` 是随机串，不是作品身份** |
| `feishu-base.sqlite3` | `feishu_sync_state` 表 | 废独立镜像库 |

> **迁移脚本的两条自持前提（2026-09-23）。**
> 1. **`platforms` 镜像行由脚本自己补**（`enabled=False`）。这张表平时由 FastAPI 的
>    lifespan 灌（`main._sync_platform_mirror`），一次性脚本不走 lifespan，不补就是 FK 失败；
>    `enabled=False` 是因为开关的权威源是 `platforms.yaml`，**迁移不许顺手打开任何平台**。
> 2. **单行失败不炸整跑**：每行的写入包在 `except (StorageError, ValidationError)` 里，
>    原文进 `report.errors`。一次跑不完比跑错一半便宜，而状态文件让下一次接着跑。
>
> 另：`--dry-run` 现在真的什么都不写 —— 它不再 `ensure_dirs()`，也不建目标库
> （以前会建出 `intelligence_hub.sqlite3` + 五个目录，然后打印「未写任何东西」）。

---

## 7. V3 重写时的契约

V3 即使换 PostgreSQL：

1. 表名、字段名、约束、索引一致
2. Repository Protocol 一致
3. Alembic 迁移可跨方言（SQLAlchemy Core 设计目标）
4. 媒体路径仍相对存储（V3 换对象存储时只重写 Storage 实现）

→ 业务代码零改动。
