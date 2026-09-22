"""Netscape 格式 cookie 文件的读写。

**这些文件是有效会话凭证**。V1 README 那句"仓库里没有你的账号"对仓库成立、
对 `data/cookies/` 不成立 —— 按凭证对待：不入库、不外传、清理时当敏感文件。

为什么需要这一层（而不是直接把文件路径传来传去）：
Windows 上 yt-dlp **读不了 Chrome 的 cookie 库**（V1 §7.3）：
Chrome 开着 → `Could not copy Chrome cookie database`；关着 → `Failed to decrypt with DPAPI`。
两句都不等于"视频下不下来"，唯一稳定可用的路径是 `--cookies <文件>`，
而文件只能从 CDP 桥导。所以"有没有 cookie 文件"是一个**可以自动补一步**的状态，
`refresh_from_bridge()` 就是那一步。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from intelligence_hub_v2.errors import BridgeError, CookieError
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.storage.files import FileStorage

if TYPE_CHECKING:
    from intelligence_hub_v2.infra.cdp_bridge import BridgeClient

logger = get_logger(__name__)

__all__ = ["COOKIE_FILE_HEADER", "CookieFreshness", "CookieManager"]

COOKIE_FILE_HEADER = "# Netscape HTTP Cookie File\n"
"""yt-dlp / curl 认这一行开头的文件。**少了它**某些工具会把第一行 cookie 当注释吃掉，
报出来的错是"没有该域的 cookie"，与"真的没登录"完全同形 —— 所以第一行是格式的一部分。"""

EMPTY_COOKIE_FILE_BYTES = len(COOKIE_FILE_HEADER) + 8
"""只有表头（再加一点余量）的文件 = **一条 cookie 都没有**。

`freshness()` 是只 stat 不读内容的快路径，它的用处就是让预检能在不解析文件的情况下
报出"这个 cookie 文件是空壳"。数字从表头长度**算出来**而不是写死：
表头改了一个字，这条判据跟着变，不会漂。

（2026-09-22 这条常量在批量脚本编辑里丢过一次，症状是 `looks_empty` 抛 `NameError`。
**ruff 的 F821 与 mypy 都抓得到**（单独验过），所以问题不是工具瞎，
是那一轮门禁跑在引用还没进去之前。规矩由此定了：
**批量改完立刻重跑门禁**，别攒到提交前 —— 见 docs/lessons.md 经验 16。）
"""


class CookieFreshness:
    """cookie 文件的可读状态。给预检与日志用，不参与决策。"""

    __slots__ = ("age_seconds", "exists", "path", "size_bytes")

    def __init__(
        self,
        path: Path,
        *,
        exists: bool,
        age_seconds: float | None,
        size_bytes: int | None,
    ) -> None:
        self.path = path
        self.exists = exists
        self.age_seconds = age_seconds
        self.size_bytes = size_bytes

    @property
    def looks_empty(self) -> bool:
        """只有表头、一条 cookie 都没有。

        这比"文件不存在"更阴：`--cookies <文件>` 传出去了，yt-dlp 不报错，
        只是匿名访问 —— 于是 B站 给你 886p 而你以为拿到了登录档（V1 §7.15）。

        写成两条早返回而不是 `a and b is not None and b <= c`：
        后者在改代码时很容易把最后半句弄丢而**仍然返回一个 bool**（mypy 不会报，
        语义却从"空文件"变成"文件存在"）—— 2026-09-22 这一行就这么坏过一次，
        是冒烟脚本发现"111 字节、两条 cookie 的文件被报成 looks_empty=True"。
        看护：`test_freshness_does_not_call_a_real_file_empty`。
        """
        if not self.exists or self.size_bytes is None:
            return False
        return self.size_bytes <= EMPTY_COOKIE_FILE_BYTES


class CookieManager:
    """`data/cookies/` 的唯一读写入口。

    收 `FileStorage` 而不是一个目录：域名→文件名的规则（`cookies_path(domain)`
    会校验"这到底像个域名吗"）必须只有一处。V1 §7.11 那类"同一个东西三种叫法"
    就是这么长出来的。
    """

    def __init__(self, files: FileStorage) -> None:
        self._files = files

    @property
    def directory(self) -> Path:
        return self._files.cookies_dir

    def exported_path(self, domain: str) -> Path | None:
        """已导出文件的路径；**不存在时返回 None**（不是抛，也不是给一个不存在的路径）。

        "没有 cookie"是常态（第一次跑、刚过期），调用方要能问一句就走别的档位；
        返回一个不存在的路径更糟 —— 它会被原样传给 `--cookies`，
        然后 yt-dlp 报一句"打不开文件"，看起来像权限问题。
        """
        path = self._files.cookies_path(domain)
        return path if path.is_file() else None

    def freshness(self, domain: str) -> CookieFreshness:
        path = self._files.cookies_path(domain)
        try:
            stat = path.stat()
        except OSError:
            return CookieFreshness(path, exists=False, age_seconds=None, size_bytes=None)
        return CookieFreshness(
            path,
            exists=True,
            age_seconds=max(0.0, time.time() - stat.st_mtime),
            size_bytes=stat.st_size,
        )

    def write_netscape(self, domain: str, cookies: list[dict[str, Any]]) -> Path:
        """把桥返回的 cookie 列表写成 Netscape 文件。原子替换，先 `.tmp` 再 `os.replace`。

        抛 `CookieError` 而不是写一个空文件：一条 cookie 都没有就落盘，
        下一次 `exported_path()` 会 happily 返回这个文件，于是整条链路退化成匿名下载
        而没人知道 —— 档位差别是画质，不是"能不能下"，所以症状要隔好几天才浮出来。
        """
        rendered = _render_netscape(cookies)
        if not rendered:
            msg = (
                f"{domain} 的 cookie 列表里没有一条能写成 Netscape 格式"
                f"（拿到 {len(cookies)} 条，缺 name/value 或域名为空）"
            )
            raise CookieError(domain, "store", msg)

        path = self._files.cookies_path(domain)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp")
        try:
            temp.write_text(COOKIE_FILE_HEADER + "".join(rendered), encoding="utf-8")
            temp.replace(path)  # 原子替换
        finally:
            if temp.exists():
                temp.unlink()
        logger.info("cookies.exported", domain=domain, count=len(rendered), path=str(path))
        return path

    async def refresh_from_bridge(self, domain: str, bridge: BridgeClient) -> Path:
        """从桥导出并落盘。V1 `refresh_bridge_cookies.py` 的对应物。

        桥需要人先登录一次（会话级登录态，V1 §6）。**这一步失败要如实红**：
        "桥里没这个域的 cookie" = 还没登录，不是网络问题，也不该被降级成匿名档继续跑。
        """
        try:
            cookies = await bridge.cookies(domain)
        except BridgeError as exc:
            raise CookieError(
                domain, "store", f"从桥取 {domain} 的 cookie 失败（桥起了吗？登录了吗？）: {exc}"
            ) from exc
        if not cookies:
            msg = f"桥里没有 {domain} 的 cookie —— 需要先在桥里人工登录一次"
            raise CookieError(domain, "store", msg)
        return self.write_netscape(domain, cookies)


def _render_netscape(cookies: list[dict[str, Any]]) -> list[str]:
    """Playwright/CDP 的 cookie dict → Netscape 行。

    七个 tab 分隔字段是死的顺序：
    `domain <TAB> includeSubDomains <TAB> path <TAB> secure <TAB> expires <TAB> name <TAB> value`

    两个不明显的点：
    - `flag` 是"域是 `.example.com` 这种通配形式吗"，与 `secure` 不是一回事。
      照抄域名前导点，别顺手写 TRUE。
    - `expires` 必须是整数。Playwright 给 `session` cookie 的是 `-1`，
      而 Netscape 里的"会话 cookie"是 **0** —— 写 -1 有些解析器当成已过期直接丢。
    """
    lines: list[str] = []
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(domain, str) or not domain:
            continue
        expiry = item.get("expires")
        # isinstance(x, bool) 也要挡一下：True 是 int 的子类，会写出 expires=1
        if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or expiry <= 0:
            expires = 0  # 会话 cookie
        else:
            expires = int(expiry)
        path = item.get("path")
        lines.append(
            "\t".join(
                [
                    domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    path if isinstance(path, str) and path else "/",
                    "TRUE" if item.get("secure") else "FALSE",
                    str(expires),
                    name,
                    "" if value is None else str(value),
                ]
            )
            + "\n"
        )
    return lines
