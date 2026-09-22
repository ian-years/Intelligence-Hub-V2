"""`tests/contracts/` 共用的假对象。

为什么手写而不是 `unittest.mock.MagicMock`：这一层的断言几乎都是
"**带的是哪几个 argv**""**导航了几次**""**每一档的原文有没有留全**"，
MagicMock 记的是调用流水，读的人得先在脑子里把顺序跑一遍才知道它在断言什么。
假对象把"看到什么算通过"写在类里，红了直接能读。

Task 14 抽 `PlatformAdapterContractTests` 抽象基类时，这一份就是四个平台共用的那套替身。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from intelligence_hub_v2.infra.cdp_bridge import BridgeHealth


class FakeBridge:
    """CDP 桥。`script` 是一串 `evaluate` 的返回值，按顺序消耗。

    列表里放一个 `Exception` 实例表示"这一次 evaluate 抛它" ——
    V1 §7.20 的看护（浏览器死了必须抛、不能消化成空结果）就是这么写的。
    """

    def __init__(
        self,
        *,
        script: Sequence[Any] = (),
        health: BridgeHealth | None = None,
        base_url: str = "http://127.0.0.1:3457",
    ) -> None:
        self.base_url = base_url
        self.navigated: list[str] = []
        self.evaluated: list[str] = []
        self._script = list(script)
        self._health = health or BridgeHealth(
            reachable=True, browser_ok=True, page_url="about:blank"
        )

    async def health(self) -> BridgeHealth:
        return self._health

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        self.navigated.append(url)
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"只接受完整 URL，收到 {url!r}")
        return {"ok": True, "wait_until": wait_until}

    async def evaluate(self, js: str) -> Any:
        self.evaluated.append(js)
        if not self._script:
            msg = f"FakeBridge 的脚本用完了（第 {len(self.evaluated)} 次 evaluate）"
            raise AssertionError(msg)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def cookies(self, domain: str) -> list[dict[str, Any]]:
        return [
            {"domain": f".{domain}", "name": "sessionid", "value": "fake", "expires": 1893456000}
        ]


class FakeYtDlpRunner:
    """`YtDlpRunner` 的替身。

    记下来的是契约真正在乎的三件事：**给了哪几个档位**（§7.3 的阶梯顺序）、
    **落点名**（`file_template`，与页面直链那条路必须同名）、
    **每一行输出有没有转给进度回调**。
    """

    def __init__(
        self,
        *,
        result: Any = None,
        raises: Exception | None = None,
        emit_lines: Sequence[str] = (),
    ) -> None:
        self.result = result
        self.raises = raises
        self.emit_lines = list(emit_lines)
        self.calls: list[dict[str, Any]] = []

    async def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        variants: Sequence[Any] = (),
        file_template: str = "%(id)s.%(ext)s",
        on_line: Any = None,
        # `timeout=` 是**被替身那份的真实签名**里的参数（`infra.ytdlp.YtDlpRunner.download`）。
        # 替身少一个形参，用例里传 `timeout=` 就 TypeError —— 这条规则在这里是误报，
        # 与 infra 自己的豁免理由同源（见 `pyproject.toml` 的 per-file-ignores）。
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.calls.append(
            {
                "url": url,
                "dest": dest_dir,
                "variants": list(variants),
                "file_template": file_template,
                "timeout": timeout,
            }
        )
        for line in self.emit_lines:
            if on_line is not None:
                on_line(line)
        if self.raises is not None:
            raise self.raises
        return self.result
