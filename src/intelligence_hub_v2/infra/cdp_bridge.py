"""CDP 桥的 HTTP 客户端。

桥 = V1 的 `cdp_bridge_server.py`（`127.0.0.1:3457`），一个挂在**人已登录的浏览器**里
执行 JS 的常驻服务。抖音/小红书/B站 的页面枚举都从这里走。

三条必须在这里就处理干净的语义，全部来自 V1 §7.20（那条本会话改过两次的教训）：

1. **`/health` 回 HTTP 503 的意思是"桥在跑、浏览器没了"，不是"桥没起"**。
   自愈（用同一个 profile 重建 context）只发生在**第一条真请求**上，
   所以调用方不能完全信任 `/health` 一位红灯就退出 —— 那等于被自己的健康检查堵死。
   V1 2026-09-21 真栽过一次：文案变成"请先启动 Chrome/CDP 代理"，而桥就在 3457 上跑着。
2. **`HTTPError ⊂ URLError`**，判"连不上"与"回了 5xx"的 except 顺序不能反。
   （V1 那是 stdlib urlopen；这里 httpx 没这个坑，但顺序意识保留：
   先判具体的 `HTTPStatusError`，再判 `ConnectError`。）
3. **`/evaluate` 失败要分两类**。浏览器死了 → 往外抛（让上层触发重建）；
   网站侧错误（`ERR_CONNECTION_CLOSED`、导航被拒）→ 回结构化错误，不抛。
   V1 的契约是"导航失败消化成 `{ok:false}` 会让自愈分支永远走不到"，
   所以死的必须抛。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx

from intelligence_hub_v2.errors import BridgeError

_JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None

__all__ = ["MAX_JS_LENGTH", "BridgeClient", "BridgeHealth"]

MAX_JS_LENGTH = 200_000
"""注入 JS 的长度上限（字符）。V1 的页面模板最大约 6 KB，这里给 30 倍余量。
超过就拒：一个失控的模板（拼接出几十 MB）打过去，症状是桥那边 OOM 或 500，
排查方向会被带到"网站风控"上 —— 那是最贵的一类误判。"""

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
"""连接 5 秒、读写 30 秒。桥在本地回环上，慢只可能是页面本身在等网络。"""


@dataclass(frozen=True)
class BridgeHealth:
    """`/health` 的结论。**四个字段各自回答一个不同的问题**，别合并。

    - `reachable`：端口上有东西在听吗？ False = 桥进程没起。
    - `browser_ok`：桥能不能驱动一个活的页面？ False 且 `reachable` True = 浏览器被关了。
    - `error`：原文（`page.context ... has been closed` 这类），给预检的 detail 用。
    - `page_url`：桥**当前**停在哪一页。只有 `browser_ok` 为真时非空 ——
      V1 的教训是这里回显一个**缓存**值，让人以为浏览器还活着。
    """

    reachable: bool
    browser_ok: bool
    error: str | None = None
    page_url: str | None = None
    status_code: int | None = None

    @property
    def needs_browser_restart(self) -> bool:
        """ "桥在跑、浏览器没了"。自愈只发生在第一条真请求上，所以这个状态**不该拦任务**。"""
        return self.reachable and not self.browser_ok

    @property
    def usable(self) -> bool:
        return self.reachable and self.browser_ok


class BridgeClient:
    """桥的异步客户端。一个 `httpx.AsyncClient` 复用给全平台（连接池 + 一致超时）。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3457",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        # 先要 scheme，再要 host。只查 startswith 会放过 `"https://"`：
        # 空主机让每个请求变成"连不上"，而原因只是配置文件里少打了个 127.0.0.1。
        parsed = httpx.URL(base_url.strip()) if base_url else None
        if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
            msg = f"桥地址必须是带主机的 http/https URL，收到 {base_url!r}"
            raise ValueError(msg)
        # 只绑回环是 V1 的硬约束（§1.2）：这个服务会在人已登录的浏览器里执行任意 JS。
        # 这里不**强制**（本机换端口/Unix socket 是合法配置），但预检会把非回环报成 degraded。
        self._base = base_url.rstrip("/")
        self._http = http
        self._owns_client = http is None

    @property
    def base_url(self) -> str:
        return self._base

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT)
            self._owns_client = True
        return self._http

    async def aclose(self) -> None:
        """只关自己建的客户端。注入了共享 client 时关掉它会连带弄坏别的调用方。"""
        if self._http is not None and self._owns_client:
            await self._http.aclose()
            self._http = None

    # ---- 探活 ----

    async def health(self) -> BridgeHealth:
        """/health。**如实报告，不猜**。

        503 不是异常：那是"桥在跑、浏览器没了"的正式编码（V1 §7.20）。
        """
        try:
            client = await self._client()
            response = await client.get(f"{self._base}/health")
        except httpx.HTTPError as exc:
            # 连不上（端口上没东西）与"回了错误状态码"是两件事，前者才是"桥没起"。
            return BridgeHealth(reachable=False, browser_ok=False, error=_describe(exc))

        if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            return BridgeHealth(
                reachable=True,
                browser_ok=False,
                error=_detail_of(response),
                status_code=response.status_code,
            )
        if response.is_error:
            return BridgeHealth(
                reachable=True,
                browser_ok=False,
                error=_detail_of(response),
                status_code=response.status_code,
            )

        body = _json_or_text(response)
        # isinstance 先收窄：`body` 的类型里有 str/list/None，直接 .get() 是 mypy 的靶子，
        # 也是"桥换了个返回形状"这种漂移唯一会被发现的地方。
        payload = body if isinstance(body, dict) else {}
        ok = bool(payload.get("ok", True))
        return BridgeHealth(
            reachable=True,
            browser_ok=ok,
            error=None if ok else _detail_of(response),
            page_url=str(payload.get("page_url") or "") if ok else None,
            status_code=response.status_code,
        )

    async def bridge_available(self) -> bool:
        """端口上有没有东西在听。**浏览器死了也算"在"**（见 `BridgeHealth.needs_browser_restart`）。

        这就是 V1 §7.20 那三个消费方的 `bridge_available()` 必须对 5xx 返回 True 的位置：
        自愈只发生在第一条真请求上，如果在预检就因为 503 退出，等于被自己的健康检查堵死。
        """
        return (await self.health()).reachable

    # ---- 真请求 ----

    async def cookies(self, domain: str) -> list[dict[str, Any]]:
        """按域名取 cookie（`/cookies?domain=`）。返回 Netscape 转换所需的字段。"""
        body = await self._get_json("/cookies", params={"domain": domain})
        items = body.get("cookies") if isinstance(body, dict) else None
        if not isinstance(items, list):
            msg = f"桥的 /cookies 返回里缺 cookies 列表（收到 {type(items).__name__}）"
            raise BridgeError("bridge", "store", msg)
        return [item for item in items if isinstance(item, dict)]

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        """`/navigate`。

        **浏览器死了要往外抛**（`BridgeError`），不能消化成 `{"ok": false}` ——
        V1 就栽在这一眼：消化之后自愈分支永远走不到，抖音那条 navigate+evaluate
        的路 0.2 秒就返回"导航失败：browser has been closed"，重建一次都没发生。
        网站侧错误（`ERR_CONNECTION_CLOSED` 之类）**照旧返回结构化失败**，
        那条"失败也要擦页面并投毒"的契约不能因为这次改动松动。
        """
        if not url.startswith(("http://", "https://")):
            msg = f"只接受完整的 http/https URL，收到 {url!r}"
            raise ValueError(msg)
        body = await self._post_json("/navigate", {"url": url, "wait_until": wait_until})
        if isinstance(body, dict) and body.get("ok") is False:
            detail = str(body.get("error") or body)
            if _browser_dead(detail):
                raise BridgeError("bridge", "list", f"桥的浏览器已关闭：{detail}")
        return body if isinstance(body, dict) else {"ok": True, "result": body}

    async def evaluate(self, js: str) -> _JSONValue:
        """`/evaluate`：在桥当前页面里执行 JS 并返回 JSON 化的结果。

        太长直接拒（`MAX_JS_LENGTH`）。模板侧的注入纪律（占位符**不许加引号**，
        因为 Python 注入的是 `json.dumps(value)`）属于适配器，见 V1 §2 契约三。
        """
        if len(js) > MAX_JS_LENGTH:
            msg = f"注入的 JS 有 {len(js)} 字符，超过上限 {MAX_JS_LENGTH}（模板拼坏了？）"
            raise ValueError(msg)
        body = await self._post_json("/evaluate", {"expression": js})
        if isinstance(body, dict) and body.get("ok") is False:
            detail = str(body.get("error") or body)
            if _browser_dead(detail):
                raise BridgeError("bridge", "list", f"桥的浏览器已关闭：{detail}")
            raise BridgeError("bridge", "list", f"页面 JS 执行失败：{detail}")
        return body.get("result") if isinstance(body, dict) and "result" in body else body

    # ---- 内部 ----

    async def _get_json(self, path: str, params: dict[str, str]) -> _JSONValue:
        return await self._request("GET", path, params=params)

    async def _post_json(self, path: str, payload: dict[str, Any]) -> _JSONValue:
        return await self._request("POST", path, json_body=payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> _JSONValue:
        url = f"{self._base}{path}"
        """`json_body` 而不是 `json=`：`json` 在本模块是页面 JS 源码的参数名，
        同名会让 `_request("POST", path, json=js)` 这种调用看起来在传 JSON body。
        """
        try:
            client = await self._client()
            # 两条调用而不是 `**({"json": ...})`：后者把类型摊平成一个 dict，
            # mypy 会把 `json=` 的位置认成 `timeout=`（httpx 那个签名是真长）。
            if json_body is not None:
                response = await client.request(method, url, params=params, json=json_body)
            else:
                response = await client.request(method, url, params=params)
        except httpx.HTTPError as exc:
            raise BridgeError(
                "bridge", "list", f"桥没响应（{method} {path}）：{_describe(exc)}"
            ) from exc

        body = _json_or_text(response)
        if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            # 503 从真请求里也**必须**变成异常：那是"该重建了"的信号，
            # 让调用方看到 ok=False 的话自愈永远不会被触发（V1 §7.20）。
            raise BridgeError(
                "bridge", "list", f"桥在跑但浏览器没了（503）：{_detail_of(response)}"
            )
        if response.is_error:
            raise BridgeError("bridge", "list", f"{method} {path} 回了 {response.status_code}")
        return body


_BROWSER_DEAD_MARKERS = (
    "Target page, context or browser has been closed",
    "browser has been closed",
    "Context has been closed",
    "Execution context was destroyed",
)
"""只认这几句 Playwright 原文。判错方向的代价是"给每个 ValueError 弹一个浏览器窗口"，
所以宁可少认也不能多认（V1 §7.20 的 `browser_dead()` 同一套取舍）。"""


def _browser_dead(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _BROWSER_DEAD_MARKERS)


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"超时 {type(exc).__name__}"
    return f"{type(exc).__name__}: {exc}"


def _detail_of(response: httpx.Response) -> str:
    body = _json_or_text(response)
    if isinstance(body, dict):
        for key in ("error", "detail", "reason", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return response.text[:300] or f"HTTP {response.status_code}"


def _json_or_text(response: httpx.Response) -> _JSONValue:
    """`response.json()` 的类型是 Any（JSON 本身没有静态形状）。

    这里把它收成 `_JSONValue` 是有意义的：调用方全都在用 `isinstance(body, dict)`
    收窄，而 Any 会让那句 isinstance 变成"没人检查的空气"。
    """
    try:
        return cast("_JSONValue", response.json())
    except ValueError:
        return response.text
