// @vitest-environment node
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

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

function assertPathsPresent(missing: string[]): void {
  expect(missing, `类型是旧的就忘了 npm run gen:api，缺：${missing.join(", ")}`).toEqual([]);
}

function assertPathsAbsent(extra: string[]): void {
  expect(extra, `生成物里有快照中不存在的路由（后端删了没重生成？）：${extra.join(", ")}`).toEqual(
    [],
  );
}
