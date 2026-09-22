"""平台运行态数据模型。

契约来源：docs/specs/data-model.md §2.1（platforms 表）

**配置的权威源是 `config/platforms.yaml`**，这张表只是运行态镜像 ——
存一份是为了让 API 一次查询就能拿到"开关状态 + 健康状态 + 最后检查时间"，
不用回去读盘再拼。所以这里的写操作**不触发**配置热重载（那是 `ConfigManager` 的事）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

HealthStatus = Literal["ok", "degraded", "unreachable", "unknown"]
"""平台健康状态。与 `PlatformHealthChangedPayload` 的取值必须一致。"""

HEALTH_STATUSES: tuple[str, ...] = ("ok", "degraded", "unreachable", "unknown")


class PlatformRecord(BaseModel):
    """DB 行（platforms 表的 Pydantic 映射）。"""

    name: str
    enabled: bool
    config_json: str = "{}"
    health_status: HealthStatus | None = None
    health_checked_at: datetime | None = None
    health_detail: str | None = None

    @property
    def is_healthy(self) -> bool:
        """只有显式 `ok` 才算健康。

        `None`（从没检查过）与 `unknown`（检查了但判不出来）都不是绿灯 ——
        V1 §7.20 那次事故就是"测不到"被显示成"正常"，
        于是三连"未登录"其实是桥的浏览器早死了。
        """
        return self.enabled and self.health_status == "ok"

    def config(self) -> dict[str, Any]:
        """把运行态镜像里的配置副本解析回 dict。

        **这是副本，不是权威源**：权威源是 `config/platforms.yaml`
        （经 `ConfigManager` 校验成 `PlatformConfig`）。这里存的只是
        "上次同步进库时的那份"，用于 API 一次查询就返回、以及事后审计
        "这轮任务跑的时候平台配置长什么样"。

        坏 JSON 抛 `ValueError` 而不是返回 `{}` —— 静默返回空 dict
        会让前端显示"该平台什么参数都没配"，而真正的原因是数据损坏（V1 §1.3）。
        """
        return json.loads(self.config_json) if self.config_json else {}
