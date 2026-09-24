// @vitest-environment node
import { readdirSync, readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { RUN_STATUS_META } from "@/lib/run-status";

import { EXPORT_ENTITIES, EXPORT_FORMATS } from "@/lib/export";
import { BEAT_TEMPLATE_KEYS } from "@/lib/workshop";

import { VIDEO_HIDDEN_MODES, VIDEO_SORT_MODES } from "./hooks/useVideos";

/**
 * `src/api/schema.d.ts` 是 `docs/specs/openapi-snapshot.json` 的投影
 * （`npm run gen:api`），而快照本身由 CI 与真实 `create_app().openapi()` 比对。
 * 所以这条用例补的是中间那一环：**改了后端路由 → 快照变 → 忘了重生成类型** 这一步。
 *
 * 双向都比（少一个路径红、多一个路径也红）：单向检查挡不住"生成物比契约新"，
 * 那种情况下页面会去用一个后端还没有的端点，症状是 404 而不是编译错误。
 */

const repoRoot = new URL("../../../", import.meta.url);
const read = (rel: string): string => readFileSync(new URL(rel, repoRoot), "utf8");

const snapshot = JSON.parse(read("docs/specs/openapi-snapshot.json")) as {
  paths: Record<string, Record<string, unknown>>;
  components: { schemas: Record<string, { properties?: Record<string, { enum?: unknown[] }> }> };
};
const generated = read("frontend/src/api/schema.d.ts");

const snapshotPaths = Object.keys(snapshot.paths).sort();
const generatedPaths = [...generated.matchAll(/^ {4}"(\/api[^"]*)": \{/gm)]
  .map((match) => match[1] as string)
  .sort();

describe("src/api/schema.d.ts ↔ openapi 快照", () => {
  it("快照里确实有路径（否则下面两条是空转）", () => {
    expect(snapshotPaths.length).toBeGreaterThan(20);
    expect(generatedPaths.length).toBeGreaterThan(20);
  });

  it("快照里的每个路径都在生成的类型里", () => {
    const missing = snapshotPaths.filter((p) => !generatedPaths.includes(p));
    assertPathsPresent(missing);
  });

  it("生成物里没有快照之外的路径", () => {
    const extra = generatedPaths.filter((p) => !snapshotPaths.includes(p));
    assertPathsAbsent(extra);
  });

  it("每个方法都各有 get/put/post/patch 之一（防止只声明了路径没声明动词）", () => {
    for (const path of snapshotPaths) {
      const methods = Object.keys(snapshot.paths[path] ?? {});
      expect(methods.length, path).toBeGreaterThan(0);
      for (const method of methods) {
        expect(["get", "put", "post", "patch", "delete"]).toContain(method);
      }
    }
  });
});

/** 带 enum 的查询参数 → 前端为此维护的那份名单。
 * 键的形状是 `路径 方法 参数名`。 */
const ENUM_LISTS: Record<string, readonly string[]> = {
  "/api/videos get hidden": VIDEO_HIDDEN_MODES,
  // 爆款回溯（T6.6）发出去的 `"benchmark"` 就来自这份名单，不是又一处字面量。
  "/api/videos get sort": VIDEO_SORT_MODES,

  // 导出那三条：`hidden` 故意复用作品流那份名单，**不是再抄一遍三个字符串** ——
  // 两处各一份的话，加一档可见性时早晚只改一边，症状是"导出比屏幕多一行/少一行"。
  "/api/export get entity": EXPORT_ENTITIES,
  "/api/export get format": EXPORT_FORMATS,
  "/api/export get hidden": VIDEO_HIDDEN_MODES,
};

interface SnapshotParam {
  name: string;
  in: string;
  schema?: { enum?: unknown[] };
}

/** 扫快照，收所有"查询参数且带 enum"的三元组。 */
function enumQueryParams(): Map<string, readonly string[]> {
  const found = new Map<string, readonly string[]>();
  for (const [path, item] of Object.entries(snapshot.paths) as [
    string,
    Record<string, { parameters?: SnapshotParam[] }>,
  ][]) {
    for (const [method, op] of Object.entries(item)) {
      for (const param of op?.parameters ?? []) {
        if (param.in === "query" && Array.isArray(param.schema?.enum)) {
          found.set(`${path} ${method} ${param.name}`, (param.schema?.enum ?? []) as string[]);
        }
      }
    }
  }
  return found;
}

describe("工坊页那张模板名单与请求体 enum", () => {
  it("BEAT_TEMPLATE_KEYS 与 DraftScriptRequest.template_key 的枚举逐字相等", () => {
    // 上面那条扫的是**查询参数**的 enum；`template_key` 在请求体里，那里够不着。
    // 而它正是"前端发一个后端不认的值 → 422"会发生的地方，所以单独钉一条双向相等。
    const declared = snapshot.components?.schemas?.DraftScriptRequest?.properties?.template_key as
      { enum?: unknown[] } | undefined;
    const values = (declared?.enum ?? []).map(String);
    expect(values.length, "快照里没有这个枚举（下面两条都会空转）").toBeGreaterThan(0);
    expect([...BEAT_TEMPLATE_KEYS].sort(), "前端名单与契约不等").toEqual([...values].sort());
  });
});

describe("查询参数里的 enum 与前端名单", () => {
  const fromSnapshot = enumQueryParams();

  it("快照里确实有带 enum 的查询参数（否则下面两条是空转）", () => {
    expect(fromSnapshot.size).toBeGreaterThan(0);
  });

  it("每一个 enum 参数，前端都有一份**逐字相等**的名单", () => {
    for (const [key, values] of fromSnapshot) {
      const mine = ENUM_LISTS[key];
      expect(mine, `前端没有为 ${key} 维护名单，界面会发出契约外的值（422）`).toBeDefined();
      expect([...(mine ?? [])].sort(), key).toEqual([...values].sort());
    }
  });

  it("前端名单里没有快照之外的（后端删掉的 enum 不许留在界面上）", () => {
    const stale = Object.keys(ENUM_LISTS).filter((key) => !fromSnapshot.has(key));
    expect(stale).toEqual([]);
  });
});

/** 任务运行状态是时间线唯一的结论来源。后端加一个 `status` 取值而 `RUN_STATUS_META`
 *  没跟上时，组件那边只保证"原样打出来不崩"（见 `task-timeline.spec.tsx`），
 *  但没颜色、没中文标签的状态摆在那里就是让人猜 —— 所以这一环在契约上钉住。 */
describe("TaskRunRecord.status 的枚举与界面状态表", () => {
  const statuses = (snapshot.components.schemas["TaskRunRecord"]?.properties?.["status"]?.enum ??
    []) as string[];

  it("快照里确实声明了这个枚举（否则下面那条是空转）", () => {
    expect(statuses.length).toBeGreaterThan(0);
  });

  it("RUN_STATUS_META 的键与枚举**双向**相等：少一个=新状态没颜色，多一个=留着没人写的状态", () => {
    expect(Object.keys(RUN_STATUS_META).sort()).toEqual([...statuses].sort());
  });
});

/**
 * 每一个 `api.<verb><T>(path)` 的 `T`，都要与快照给那个端点声明的 2xx schema **同名**。
 *
 * 起因是三个同形状的 bug：`VideoFilter.hidden` 手写成 `boolean`（契约是 enum）、
 * `PATCH /creators/{id}/tracking` 的字段名先写成 `is_tracking`、
 * `useAddCreator` 写的是 `api.post<Creator>` 而那个端点回的是 202 + `TaskAccepted`
 * （连注释都跟着说"立即回一个 Creator"）。前两个是"写用例时人肉去翻快照"抓到的 ——
 * 那种抓法不会覆盖第三个，直到有人真去读那个响应。
 *
 * 自由 `dict[str, Any]` 的路由（`/platforms/{p}/schema`、`/preflight` 那类）没有 schema 可比，
 * 跳过并计入 `skipped`：那几处的形状是手写的，由各自的用例钉
 * （`PreflightSummary` 那一条钉的是生产者字符串形状）。
 */
describe("每个 hook 声明的响应类型与快照同名", () => {
  const hookDir = new URL("./hooks/", import.meta.url);
  const files = readdirSync(hookDir).filter((name) => name.endsWith(".ts"));
  expect(files.length).toBeGreaterThanOrEqual(5);

  const mismatches: string[] = [];
  let checked = 0;
  let skipped = 0;

  /** `/api/videos/{video_id}` 与 `/api/videos/${String(id)}` 要能对上：按段比，`{x}` 当通配。 */
  function findOperation(path: string, verb: string): Record<string, unknown> | undefined {
    for (const [pattern, item] of Object.entries(snapshot.paths)) {
      const patternParts = pattern.split("/");
      const givenParts = path.split("/");
      if (patternParts.length !== givenParts.length) continue;
      const same = patternParts.every((part, index) => {
        const given = givenParts[index] ?? "";
        return part === given || (part.startsWith("{") && part.endsWith("}"));
      });
      if (!same) continue;
      return (item as Record<string, Record<string, unknown>>)[verb];
    }
    return undefined;
  }

  function schemaNameOf(operation: Record<string, unknown> | undefined): string | null {
    const responses = (operation?.["responses"] ?? {}) as Record<string, unknown>;
    const code = Object.keys(responses).find((key) => key.startsWith("2"));
    const content = (responses[code ?? ""] as { content?: Record<string, unknown> } | undefined)
      ?.content;
    const schema = (
      content?.["application/json"] as { schema?: Record<string, unknown> } | undefined
    )?.schema;
    if (typeof schema?.$ref === "string") return basenameOf(schema.$ref);
    if (schema?.type === "array") {
      const items = schema["items"] as { $ref?: string } | undefined;
      if (typeof items?.$ref === "string") return `${basenameOf(items.$ref)}[]`;
    }
    return null;
  }

  function basenameOf(ref: string): string {
    const tail = ref.split("/").pop() ?? ref;
    return tail.replace(/~1/g, "/");
  }

  for (const file of files) {
    const source = read(`frontend/src/api/hooks/${file}`);
    // `export type Creator = Schemas["Creator"]` 这一类：把别名换成它指向的 schema 名
    const aliases = new Map<string, string>();
    for (const match of source.matchAll(/type\s+(\w+)\s*=\s*Schemas\["(\w+)"\]/g)) {
      if (match[1] && match[2]) aliases.set(match[1], match[2]);
    }
    for (const call of source.matchAll(
      /api\.(get|post|put|patch)<\s*([^>]+?)\s*>\(\s*[`'"]([^`'"]+)[`'"]/g,
    )) {
      const [, verb, generic, rawPath] = call;
      if (verb === undefined || generic === undefined || rawPath === undefined) continue;
      const path = `/api${rawPath.replace(/\$\{[^}]*\}/g, "{}")}`;
      const expected = schemaNameOf(findOperation(path, verb));
      if (expected === null) {
        skipped += 1;
        continue;
      }
      const suffix = generic.endsWith("[]") ? "[]" : "";
      const inline = /^Schemas\["(\w+)"\]$/.exec(suffix ? generic.slice(0, -2) : generic);
      const base = inline?.[1] ?? (suffix ? generic.slice(0, -2) : generic);
      const given = `${aliases.get(base) ?? base}${suffix}`;
      checked += 1;
      if (given !== expected) {
        mismatches.push(
          `${file}: ${verb.toUpperCase()} ${path} 声明的是 ${given}，契约给的是 ${expected}`,
        );
      }
    }
  }

  it("扫到了足够多的调用点（否则下面那条是空转）", () => {
    expect(checked).toBeGreaterThanOrEqual(12);
    expect(skipped).toBeGreaterThan(0);
  });

  it("没有一个 hook 的响应类型与契约漂了", () => {
    expect(mismatches, mismatches.join("\n")).toEqual([]);
  });
});

function assertPathsPresent(missing: string[]): void {
  expect(missing, `类型是旧的就忘了 npm run gen:api，缺：${missing.join(", ")}`).toEqual([]);
}

function assertPathsAbsent(extra: string[]): void {
  expect(extra, `生成物里有快照中不存在的路由（后端删了没重生成？）：${extra.join(", ")}`).toEqual(
    [],
  );
}
