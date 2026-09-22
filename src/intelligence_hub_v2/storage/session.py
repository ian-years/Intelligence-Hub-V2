"""事务上下文：`CURRENT_SESSION`。

单独一个叶子模块，理由不是风格：**这个 ContextVar 同时被两侧需要**——

- `storage/db.py` 的 `SqliteStorage.transaction()` 要 set / reset 它；
- `storage/repositories/base.py` 的 `BaseRepository._scope()` 要读它。

如果它住在 `db.py` 里，`db.py` 就永远不能在顶层 import repositories
（`repositories.base → storage.db` 已经在里面了，会形成真正的循环导入），
于是 `_Repositories` 只能把七个 Repository 的导入塞进函数体 —— 那种
"看不出为什么不能提到顶层"的延迟导入是可发现性黑洞（ruff 为此专门有
`PLC0415`，每次都要人肉判断该不该豁免）。

搬到这里之后两边都是**单向**的：
`session.py`（叶子）← `repositories/base.py` ← `db.py`。

公开路径仍然是 `intelligence_hub_v2.storage.db.CURRENT_SESSION`
（`db.py` 顶层 re-export），V3 换存储实现时契约名不变。
"""

from __future__ import annotations

from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["CURRENT_SESSION"]


CURRENT_SESSION: ContextVar[AsyncSession | None] = ContextVar("ih2_current_session", default=None)
"""当前事务的 session。None = 不在事务里，Repository 各开各的短事务。

用 ContextVar 而不是显式传参：`Storage.transaction()` 里可能调三个 Repository，
显式传 session 就要给每个方法都加一个参数，而这个参数 99% 的时候是 None。
V1 §7.19 的教训是"靠环境变量传上下文"不可靠（子进程拿不到），
但那是**跨进程**；ContextVar 是**同进程内按 asyncio Task 隔离**的，语义正确
（有专门的用例并发跑两个 `transaction()` 确认互不串写）。
"""
