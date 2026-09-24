import type { components } from "./schema";

/** 后端 FastAPI 的错误体只有 `detail` 一种形状，但它的类型有两种：
 * 字符串（我们自己 `raise HTTPException(detail="…")`）与数组（422 校验错误）。
 * 混在一个 `unknown` 里传给页面就会到处 `String(error)`，所以这里一次收敛。 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  /** 422 时逐条的 `loc: msg`，用来把表单错误挂回字段上。 */
  readonly fieldErrors: readonly string[];

  constructor(status: number, detail: unknown) {
    const fieldErrors = Array.isArray(detail)
      ? detail.map((item) => {
          const entry = item as { loc?: unknown[]; msg?: unknown };
          const where = (entry.loc ?? []).filter((p) => p !== "body").join(".");
          return `${where || "?"}: ${String(entry.msg ?? "")}`;
        })
      : [];
    // 数组直接 `join` 会得到 `[object Object]；[object Object]` —— 那是把后端
    // 已经写清楚的字段错误**再抹平一次**，用户看到的是一屏乱码而原因在响应体里。
    const text = Array.isArray(detail)
      ? fieldErrors.join("；") || `HTTP ${status}`
      : String(detail ?? `HTTP ${status}`);
    super(`${status} ${text}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = text;
    this.fieldErrors = fieldErrors;
  }
}

export type Query = Record<string, unknown>;

/** 没给 body 就**不发** body 字段（`PATCH /videos/{id}/unhide` 就没有请求体，
 * 发一个 `{}` 会被 `extra="forbid"` 的模型判成校验失败）。 */
function withBody(body: unknown): { body?: string; headers: Record<string, string> } {
  return body === undefined
    ? { headers: {} }
    : { body: JSON.stringify(body), headers: { "Content-Type": "application/json" } };
}

function url(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    qs.set(key, String(value));
  }
  const search = qs.toString();
  return search ? `/api${path}?${search}` : `/api${path}`;
}

/** 唯一的出口：**非 2xx 一律抛 `ApiError`**，不返回"看起来能用"的空值。
 * `AGENTS.md §1.3` 在前端这一头的形状是：页面宁可显示"读不到 + 原因"，
 * 也不能把失败渲染成空列表 —— 空列表长得跟"还没有数据"一模一样。 */
async function request<T>(init: RequestInit, path: string, query?: Query): Promise<T> {
  const response = await fetch(url(path, query), {
    ...init,
    headers: { Accept: "application/json", ...init.headers },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function readDetail(response: Response): Promise<unknown> {
  // 只读一次：`response.json()` 会消耗 body，之后再调 `.text()` 拿到的是空串
  // —— 第一版就是这么把一句 502 的 HTML 读成"没有原因"的。
  const text = await response.text().catch(() => "");
  if (!text) return response.statusText;
  try {
    const body: unknown = JSON.parse(text);
    const detail = (body as { detail?: unknown })?.detail;
    return detail === undefined ? body : detail;
  } catch {
    // 反向代理/后端没起来时常常是一屏 HTML。留前 160 个字符就够定位了。
    return text.slice(0, 160);
  }
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>({ method: "GET" }, path, query),
  post: <T>(path: string, body?: unknown) =>
    request<T>(
      {
        method: "POST",
        body: JSON.stringify(body ?? {}),
        headers: { "Content-Type": "application/json" },
      },
      path,
    ),
  put: <T>(path: string, body?: unknown) => request<T>({ method: "PUT", ...withBody(body) }, path),
  patch: <T>(path: string, body?: unknown) =>
    request<T>({ method: "PATCH", ...withBody(body) }, path),
  /** DELETE。本仓库第一个用它的地方是 T5.5 的选题/草稿（`/api/topics/{id}`、
   *  `/api/drafts/{id}`）：那两个端点回 204 无 body，`request` 已经把 204 翻成
   *  `undefined`，所以调用方拿到的是 `undefined`，**不许**去读它的字段。
   *  默认类型参数写成 `undefined` 而不是 `void`：`void` 在赋值处会被当成
   *  "可以忽略任何返回值"，于是一个忘了 `await` 的删除不会报错。 */
  del: <T = undefined>(path: string) => request<T>({ method: "DELETE" }, path),
};

/** 给"点了就下载"的链接用的地址：**复用同一个 query 归一化**（空串与 null 不进 URL），
 *  否则会出现"`/api/videos` 里空的平台等于不过滤，而导出那条拼成 `platform=`"
 *  这种两份规则分叉。端点自己带 `Content-Disposition: attachment`，所以这里
 *  只需要一个诚实的 href，不需要 fetch + blob。 */
export function downloadUrl(path: string, query?: Query): string {
  return url(path, query);
}

export type Schemas = components["schemas"];
