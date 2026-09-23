/**
 * `src/styles/tokens.css` → `tokens.json` 的投影（§10）。
 *
 * 方向是**单向**的：CSS 是唯一真源，JSON 是产物，永远不该手改 JSON。
 * 为什么不像原计划那样"YAML 源 → 三份产物"：Tailwind v4 的 `@theme` 已经是
 * 机器可读的令牌声明，再引入 YAML 就是第三处真相（ADR-0013）。
 *
 * 这一份只导出函数、没有副作用，因为 `src/styles/tokens.spec.ts` 要 import 同一份
 * 解析逻辑来核对产物 —— **两份解析器就等于一个永远绿但抓不到东西的检查**。
 * 真正写文件的是 `gen-tokens.ts`（`npm run tokens`）。
 */

import { readFileSync, writeFileSync } from "node:fs";

export const TOKENS_CSS = "../src/styles/tokens.css";
export const TOKENS_JSON = "../tokens.json";

const DECLARATION = /(-{2}[a-z0-9-]+)\s*:\s*([^;]+);/g;

/** 取 `@theme { … }` 块里的声明；注释先剥掉，`var(...)` 原样保留（投影不解析引用）。 */
export function parseTheme(css: string): Record<string, string> {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const blocks = [...withoutComments.matchAll(/@theme\s*\{([\s\S]*?)\}/g)];
  const out: Record<string, string> = {};
  for (const block of blocks) {
    const body = block[1];
    if (!body) continue;
    for (const match of body.matchAll(DECLARATION)) {
      const name = match[1];
      const raw = match[2];
      if (name === undefined || raw === undefined) continue;
      out[name] = raw.trim().replaceAll(/\s+/g, " ");
    }
  }
  return out;
}

export function render(tokens: Record<string, string>): string {
  const sorted = Object.fromEntries(
    Object.entries(tokens).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
  );
  return `${JSON.stringify({ source: "src/styles/tokens.css", tokens: sorted }, null, 2)}\n`;
}

export function readTokensCss(): string {
  return readFileSync(new URL(TOKENS_CSS, import.meta.url), "utf8");
}

export function writeTokensJson(css = readTokensCss()): string {
  const body = render(parseTheme(css));
  writeFileSync(new URL(TOKENS_JSON, import.meta.url), body, "utf8");
  return body;
}
