// @vitest-environment node
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { cn } from "./utils";

/**
 * `cn()` = `clsx` + `tailwind-merge`。merge 那一半不是装饰：它按**类组**去重，
 * 于是任何落在 Tailwind 命名空间里的自定义工具类都可能被当成同类冲突吃掉。
 *
 * 这里就是被咬过的地方：`border-memphis`（§3 的 3px 黑边）与 `border-ink-black`
 * 同属 border 组，`cn("border-memphis border-ink-black")` 交出的是
 * `"border-ink-black"` —— 边框宽度安静地回到 0，而 JSX 源码里那个类名还在，
 * 对着代码看是看不出来的。所以改名成 `memphis-border`，并且钉两条。
 */
const CSS = readFileSync(new URL("../styles/globals.css", import.meta.url), "utf8");

/** `@layer utilities { … }` 与 `@layer components { … }` 里定义的类名。
 *  先把注释剥掉：这条纪律管的是**定义**，而注释里正写着旧名字 `border-memphis`
 *  （那是改名原因的一部分，不是待查的类名）。 */
function handWrittenUtilities(): string[] {
  const stripped = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  const names: string[] = [];
  for (const block of stripped.matchAll(/@layer (?:utilities|components)\s*\{([\s\S]*?)\n\}/g)) {
    for (const rule of block[1]?.matchAll(/(?:^|\n)\s*\.([A-Za-z][A-Za-z0-9_-]*)/g) ?? []) {
      if (rule[1] !== undefined) names.push(rule[1]);
    }
  }
  return [...new Set(names)].sort();
}

describe("cn 与自定义工具类", () => {
  it("两条工具类同时给：都要留下，不许谁覆盖谁", () => {
    const merged = cn("memphis-border border-ink-black px-3 py-1", "bg-grey-mist");
    expect(merged.split(" ")).toEqual(
      expect.arrayContaining([
        "memphis-border",
        "border-ink-black",
        "px-3",
        "py-1",
        "bg-grey-mist",
      ]),
    );
  });

  it("落在 Tailwind 命名空间里的名字会被吃掉（这一条是改名原因的证据）", () => {
    // 与上一条对照着读：同样的两个类，只把自定义那条换成 `border-` 前缀就少了一条。
    expect(cn("border-memphis border-ink-black")).toBe("border-ink-black");
  });

  it("自己写的工具类一律带 `memphis-` 前缀，不进 Tailwind 的组", () => {
    const names = handWrittenUtilities();
    // 防空转：扫不到任何类名的话，下面那条断言就只是"没有东西可查"。
    expect(names.length).toBeGreaterThan(5);
    const risky = names.filter((name) => !name.startsWith("memphis-"));
    expect(risky, `这些类名会被 tailwind-merge 归进某个 Tailwind 组：${risky.join(", ")}`).toEqual(
      [],
    );
  });
});
