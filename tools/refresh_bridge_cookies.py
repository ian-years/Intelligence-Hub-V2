"""从 CDP 桥导出 cookie 文件，喂给读不出 Chrome cookie 库的 yt-dlp。

**跑法**：

```bash
uv run python -X utf8 tools/refresh_bridge_cookies.py             # 导 V2 已实现的那两家
uv run python -X utf8 tools/refresh_bridge_cookies.py --domain xiaohongshu.com
make bridge-cookies
```

**跑之前桥得在，而且人得先在桥里登录一次**（`make bridge` 弹的那个 Chrome）：
桥的 profile 里没有该域的 cookie 时，这个工具**如实红**并写"需要先在桥里人工登录一次"，
不会落一个只有表头的空文件 —— 那种文件被 `--cookies` 传出去之后 yt-dlp 不报错，
只是匿名访问，于是 B站 给你 886p 而你以为拿到的是登录档（V1 §7.15）。

三条从 V1 搬过来的规矩：

1. **写盘只有一条路**：`BridgeClient.cookies()` → `CookieManager.refresh_from_bridge()`。
   V1 那个脚本自己 `urlopen` + 自己拼 Netscape 文本；V2 里渲染器只有
   `infra/cookies.py::_render_netscape` 一处（会话 cookie 的 `-1` → `0` 那种细节在里面有用例），
   工具再抄一份就是 §7.11 那一族"同一个东西两个实现"。文件名同理，只有
   `FileStorage.cookies_path(domain)` 说了算（`data/cookies/<域名>.txt`）。
2. **单个域名失败不炸整跑**：每个域名的失败原文进 `result["failed"]`，
   成功的照常落盘，最后 `ok = 有成功 且 零失败`。
3. **默认清单只包含今天有人读的平台**（抖音 + B站）。V1 的默认里还有 `xiaohongshu.com`，
   但小红书适配器在 T2.1 —— 今天导它等于写一份没有读者的凭证，要就显式 `--domain` 给。

产出的文件是**有效会话凭证**：不入库、不外传（`data/` 整个目录已在 `.gitignore` 里）。
`0o600` 会尽力设上，但 Windows 上 POSIX 权限位只是建议性的 —— 真正的保护是那个 gitignore。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

# 脚本入口：把仓库根（tools/ 的上一级）放进 sys.path，好 `import intelligence_hub_v2`。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence_hub_v2.core.config import AppConfig, load_app_config
from intelligence_hub_v2.errors import CookieError
from intelligence_hub_v2.infra.cdp_bridge import BridgeClient
from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.platforms.bilibili.urls import BILI_COOKIE_DOMAIN
from intelligence_hub_v2.platforms.douyin.media import DOUYIN_COOKIE_DOMAIN
from intelligence_hub_v2.storage.files import FileStorage

DEFAULT_DOMAINS: tuple[str, ...] = (DOUYIN_COOKIE_DOMAIN, BILI_COOKIE_DOMAIN)
"""V2 今天**有适配器在读**的那些 cookie 域。加平台时改这里一处，别在调用方各写一份。"""

__all__ = ["DEFAULT_DOMAINS", "main", "refresh_domains"]


def count_cookie_lines(path: Path) -> int:
    """文件里真正的条数（跳过表头与注释）。只用于**报告**，判据不挂在这上面。"""
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


async def refresh_domains(
    domains: list[str],
    bridge: BridgeClient,
    manager: CookieManager,
) -> dict[str, Any]:
    """逐个域名走一遍"取列表 → 渲染 → 原子落盘"；失败的域名原样收进 `failed`。"""
    written: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for domain in domains:
        try:
            path = await manager.refresh_from_bridge(domain, bridge)
        except (CookieError, ValueError) as exc:
            # ValueError 来自 `FileStorage.cookies_path()` 的"这到底像个域名吗"那道闸。
            failed[domain] = f"{type(exc).__name__}: {exc}"
            continue
        written[domain] = {
            "path": str(path),
            "cookies": count_cookie_lines(path),
            "size_bytes": path.stat().st_size,
        }
    return {"bridge": bridge.base_url, "written": written, "failed": failed}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 CDP 桥导出 Netscape cookie 文件（给 yt-dlp 的 --cookies 用）"
    )
    parser.add_argument(
        "--domain",
        action="append",
        default=[],
        help=f"要导出的域名，可重复；默认 {' + '.join(DEFAULT_DOMAINS)}",
    )
    parser.add_argument(
        "--bridge-url",
        default="",
        help="桥地址，默认取 config 的 cdp_bridge.url（与 BridgeClient 同样只认回环）",
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="产物根目录，默认取 config 的 data.dir（cookie 文件落在其下的 cookies/）",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="单次请求超时（秒）")
    return parser.parse_args(argv)


def domains_from(args: argparse.Namespace) -> list[str]:
    """`--domain` 给了就用给的，没给用默认清单。去重保序，空串丢掉。"""
    requested = [str(item).strip() for item in args.domain if str(item).strip()]
    ordered = requested or list(DEFAULT_DOMAINS)
    return list(dict.fromkeys(ordered))


def resolve_data_dir(args: argparse.Namespace, config: AppConfig) -> FileStorage:
    """`--data-dir` 覆盖配置，否则从配置算 —— 两条路都不许在这里手拼 `cookies/`。"""
    raw = str(args.data_dir or "").strip()
    if raw:
        return FileStorage(Path(raw).expanduser().resolve())
    # root=仓库根：`data.dir` 在配置里是相对路径，相对的是仓库而不是当前工作目录。
    return FileStorage.from_config(config, root=_REPO_ROOT)


async def _run(
    domains: list[str],
    bridge_url: str,
    manager: CookieManager,
    timeout: float,
) -> dict[str, Any]:
    seconds = max(1.0, timeout)
    # client 与刷新必须在同一个事件循环里建与关：跨 asyncio.run() 复用一个
    # httpx.AsyncClient 会撞上"Event loop is closed"（连接池记的是建它的那个 loop）。
    client = httpx.AsyncClient(timeout=httpx.Timeout(seconds, connect=min(5.0, seconds)))
    bridge = BridgeClient(bridge_url, client)
    try:
        return await refresh_domains(domains, bridge, manager)
    finally:
        await bridge.aclose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_app_config()
    url = (str(args.bridge_url).strip() or str(config.cdp_bridge.url)).rstrip("/")
    result = asyncio.run(
        _run(domains_from(args), url, CookieManager(resolve_data_dir(args, config)), args.timeout)
    )
    result["ok"] = bool(result["written"]) and not result["failed"]
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
