"""存储层：SQLite（SQLAlchemy Core）+ 文件落盘路径。

依赖方向的最底层（`api → core → platforms → infra → storage`），
所以这里**不 import 上层模块**（唯一的例外是 `TYPE_CHECKING` 下的 `AppConfig`，
运行时不产生依赖）。

两个入口：
- `SqliteStorage` —— 库（表、事务、Repository）
- `FileStorage` —— 盘（媒体、口播稿、清单、cookie 的路径）

它们**不互相持有**：一份产物既落盘又落库时（视频、清单），
由调用方（任务 handler）分别问两边要路径与行。V1 的教训是
"库空了就退回磁盘扫描"（§7.25），于是删掉的条目刷新一次就复活 ——
V2 里库是唯一数据源，盘上只有原始产物。
"""

from intelligence_hub_v2.storage.db import (
    SqliteStorage,
    alembic_dir,
    alembic_ini,
    async_url,
    check_schema_matches_migrations,
    current_revision,
    downgrade_migrations,
    resolve_db_path,
    run_migrations,
    sync_url,
)
from intelligence_hub_v2.storage.files import FileStorage, safe_filename
from intelligence_hub_v2.storage.schema import (
    ALL_TABLES,
    TABLE_NAMES,
    creators_table,
    manifests_table,
    metadata,
    platforms_table,
    task_events_table,
    task_runs_table,
    transcripts_table,
    videos_table,
)
from intelligence_hub_v2.storage.session import CURRENT_SESSION

__all__ = [
    "ALL_TABLES",
    "CURRENT_SESSION",
    "TABLE_NAMES",
    "FileStorage",
    "SqliteStorage",
    "alembic_dir",
    "alembic_ini",
    "async_url",
    "check_schema_matches_migrations",
    "creators_table",
    "current_revision",
    "downgrade_migrations",
    "manifests_table",
    "metadata",
    "platforms_table",
    "resolve_db_path",
    "run_migrations",
    "safe_filename",
    "sync_url",
    "task_events_table",
    "task_runs_table",
    "transcripts_table",
    "videos_table",
]
