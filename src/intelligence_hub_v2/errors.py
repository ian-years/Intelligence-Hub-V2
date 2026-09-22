"""V2 异常层次。所有自定义异常继承 IntelligenceHubError。

纪律（V1 §1.3 + docs/specs/platform-adapter.md §2.5）：
- 所有平台层异常必须带 platform 与 stage，进清单 failures[]
- 网站侧错误（403/412/352/风控）必须带原文，不允许"看起来在跑"
- 不允许裸 except:，ruff BLE001 强制
"""

from __future__ import annotations


class IntelligenceHubError(Exception):
    """所有 V2 异常的基类。"""


class PlatformError(IntelligenceHubError):
    """平台层异常。必须带 platform + stage + 原文。"""

    def __init__(
        self,
        platform: str,
        stage: str,
        message: str,
        *,
        cause: Exception | None = None,
    ) -> None:
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


class TaskCancelled(IntelligenceHubError):
    """任务被取消（协作式，CancelToken.is_cancelled）。"""


class TaskRejected(IntelligenceHubError):
    """任务被拒绝（平台禁用 / requires 缺失 / 同名任务已在跑）。"""


class ConfigError(IntelligenceHubError):
    """配置错误（YAML 解析失败 / Pydantic 校验失败 / 原子写盘失败）。"""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        super().__init__(message)
