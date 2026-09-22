"""请求节流。平台适配器用它把"每分钟最多 N 个外部请求"落到自己这一层。

为什么住在 infra：这条边两端都是"外部世界"（一次桥请求、一次 yt-dlp 子进程），
而它**必须被两个以上适配器共用** —— V1 的教训原文就是
"B站 与抖音各自实现了一份 cookie 阶梯，两处漂移过一次"（见本包 `__init__.py` 纪律第 3 条）。
节流逻辑同样不该有两份：它的默认值来自配置（`RateLimitConfig.per_minute`），
实现有两份就意味着改一处忘一处的概率翻倍。

为什么不是 `asyncio.Semaphore`：信号量管的是"**同时**几个"，
这里要管的是"**每单位时间**几个"。网站侧风控（V1 §7.2 那一整条）吃的是速率，
不是并发数 —— 只有 1 个并发但一秒发 60 个请求照样被封。
"""

from __future__ import annotations

import asyncio

__all__ = ["RatePacer"]


class RatePacer:
    """一个"每分钟最多 N 次"的闸门。

    用单调钟（`loop.time()`）而不是墙上时钟：NTP 把系统时间往回拨一下，
    用 `time.time()` 的实现能让一次等待变成几十年。

    第一次 `wait()` 不等待（`_last=0.0` → 算出来的差是负数）：
    闸门不该在开工前先憋住第一个请求，那只会让每次启动都白等一个间隔。
    """

    def __init__(self, *, per_minute: int) -> None:
        self._interval = 60.0 / max(1, per_minute)
        self._last = 0.0

    @property
    def interval_seconds(self) -> float:
        """两次放行之间的最小间隔。用例与预检读它，不用去翻私有字段。"""
        return self._interval

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        delta = self._interval - (now - self._last)
        if delta > 0:
            await asyncio.sleep(delta)
        self._last = loop.time()
