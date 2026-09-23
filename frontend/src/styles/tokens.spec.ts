// @vitest-environment node
/**
 * 这条 spec 只读文件、不碰 DOM，所以跑在 node 环境。
 * （jsdom 下 `import.meta.url` 是 http: URL，`fileURLToPath` 会直接抛
 * "The URL must be of scheme file" —— 第一次跑就撞上了。）
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import yaml from "js-yaml";
import { describe, expect, it } from "vitest";

import { parseTheme, render } from "../../scripts/token-projection";

/**
 * `docs/specs/ui-tokens.md` 是 **Locked 契约**（§12：V3 换框架时令牌直接复用）。
 * 契约文档最容易烂在"注释里说了、代码里没做"这一类（本仓库为此写过三条经验），
 * 所以这里不比对人写的摘要，直接把 spec 里的 YAML 源解出来，逐条核 `tokens.css`。
 *
 * 三个方向各自有断言：
 * 1. spec 里有的令牌 → CSS 必须有同值（`色板 / 阴影 / 间距 / 动效`）
 * 2. CSS 有、`tokens.json` 没有 → 红（产物漂移）
 * 3. `patterns.css` 里 data URI 不能引用色板之外的字面量
 */

const repoRoot = new URL("../../../", import.meta.url);
const read = (rel: string): string => readFileSync(fileURLToPath(new URL(rel, repoRoot)), "utf8");

const SPEC = read("docs/specs/ui-tokens.md");
const CSS = read("frontend/src/styles/tokens.css");
const PATTERNS_CSS = read("frontend/src/styles/patterns.css");
const JSON_BODY = read("frontend/tokens.json");
const TOKENS = parseTheme(CSS);

/** §2/§3 里 `{ink_black}` 这种引用写法 → CSS 变量名。 */
const REFERENCE: Record<string, string> = {
  ink_black: "var(--color-ink-black)",
  paper_cream: "var(--color-paper-cream)",
  mint_green: "var(--color-mint-green)",
  grey_mist: "var(--color-grey-mist)",
  electric_blue: "var(--color-electric-blue)",
  coral_red: "var(--color-coral-red)",
  lemon_yellow: "var(--color-lemon-yellow)",
  hot_pink: "var(--color-hot-pink)",
};

function kebab(name: string): string {
  return name.replaceAll("_", "-");
}

/**
 * 把两边都归一到"最终字面量"：spec 里的 `{ink_black}` 与 CSS 里的 `var(--color-ink-black)`
 * 各自展开一跳。不这么做就只能比字符串相等，而那是 §10 的写法决定的，不是设计。
 */
function resolve(value: string): string {
  const expanded = value
    .replaceAll(/\{([a-z_]+)\}/g, (_all, ref: string) => REFERENCE[ref] ?? "")
    .replaceAll(/var\((--[\w-]+)\)/g, (_all, name: string) => TOKENS[name] ?? _all);
  // CSS 一律小写十六进制、spec 一律大写（§2 的写法），大小写不是设计差异。
  return expanded.replace(/^#([0-9a-fA-F]{6})$/, (_all, hex: string) => `#${hex.toLowerCase()}`);
}

/** 只有十六进制色与已知颜色引用才是"色"，`display_name: "抖音"` 那种跳过。 */
function isColorish(value: string): boolean {
  return /^#[0-9A-Fa-f]{6}$/.test(value) || /^\{[a-z_]+\}$/.test(value);
}

/** 取某个 `## N.` 小节里的所有 ```yaml 块并解析。 */
function sectionYaml(headingPrefix: string): Record<string, unknown>[] {
  const start = SPEC.indexOf(`## ${headingPrefix}`);
  if (start < 0) return [];
  const rest = SPEC.slice(start);
  const end = rest.indexOf("\n---", 1);
  const body = end < 0 ? rest : rest.slice(0, end);
  return [...body.matchAll(/```yaml\n([\s\S]*?)```/g)]
    .map(([, text]) => yaml.load(String(text)) as Record<string, unknown>)
    .filter((value) => value !== null && typeof value === "object");
}

describe("tokens.css ↔ docs/specs/ui-tokens.md", () => {
  it("解析得到东西（否则下面每一条都是空转）", () => {
    expect(Object.keys(TOKENS).length).toBeGreaterThan(40);
    expect(sectionYaml("2.").length).toBeGreaterThan(0);
  });

  it("§2 色板：spec 里每个颜色都有同名 CSS 变量，展开后同值", () => {
    const expected = new Map<string, string>();
    for (const block of sectionYaml("2.")) {
      for (const [name, value] of Object.entries(block)) {
        if (typeof value === "string" && isColorish(value)) {
          expected.set(`--color-${kebab(name)}`, value);
        } else if (value && typeof value === "object") {
          // §2.4 那四条：`{color, shape, display_name}`，只有 color 是色令牌。
          const color = (value as Record<string, unknown>).color;
          if (typeof color === "string" && isColorish(color)) {
            expected.set(`--color-platform-${kebab(name)}`, color);
          }
        }
      }
    }
    expect(expected.size).toBeGreaterThanOrEqual(8 + 4);
    for (const [tokenName, specValue] of expected) {
      expect(resolve(TOKENS[tokenName] ?? ""), `${tokenName}（§2 的 ${specValue}）`).toBe(
        resolve(specValue),
      );
    }
  });

  it("§3 形状：3px 边、三档硬阴影、直角与 pill", () => {
    const shape = Object.assign(
      {},
      ...sectionYaml("3.").map((block) => (block.shape ?? {}) as Record<string, string>),
    );
    /**
     * §3 的键名与 CSS 变量名不是机械映射（`hard_shadow` 若直译会得到 `--shadow-hard-shadow`，
     * 而 Tailwind v4 的阴影工具类来自 `--shadow-*` 命名空间）。
     *
     * 手工映射表正是漂移最爱藏身的地方，所以这里两头钉：**两边的键集合必须相等**
     * （spec 加一档没映射 → 红；改名 → 也红），然后才比值。
     */
    const SHAPE_TOKEN: Record<string, string> = {
      border_width: "--border-width-memphis",
      border_color: "--color-ink-black",
      hard_shadow: "--shadow-hard",
      hard_shadow_lg: "--shadow-hard-lg",
      hard_shadow_sm: "--shadow-hard-sm",
      corner_radius: "--radius-none",
      corner_radius_pill: "--radius-pill",
    };
    expect(Object.keys(shape).sort()).toEqual(Object.keys(SHAPE_TOKEN).sort());

    for (const [key, tokenName] of Object.entries(SHAPE_TOKEN)) {
      expect(resolve(TOKENS[tokenName] ?? ""), `§3 的 ${key} → ${tokenName}`).toBe(
        resolve(shape[key] ?? ""),
      );
    }
  });

  it("§5 间距：13 档 scale 一档不差", () => {
    const scale = Object.entries(
      (
        sectionYaml("5.")
          .map((block) => (block.spacing ?? {}) as Record<string, unknown>)
          .find((value) => "scale" in value) as { scale: Record<string, string> }
      ).scale,
    );
    expect(scale).toHaveLength(13);
    for (const [step, px] of scale) {
      expect(TOKENS[`--spacing-scale-${step}`], `§5 的 ${step} 档`).toBe(px);
    }
    expect(TOKENS["--spacing"], "单位必须是 4px（§5）").toBe("4px");
  });

  it("§7 动效：两档缓动 + 四档时长", () => {
    const motion = sectionYaml("7.")
      .map((block) => (block.motion ?? {}) as Record<string, Record<string, string>>)
      .find((value) => "easing" in value);
    expect(motion).toBeDefined();
    for (const [name, value] of Object.entries(motion?.easing ?? {})) {
      expect(TOKENS[`--ease-${kebab(name)}`]).toBe(value);
    }
    for (const [name, value] of Object.entries(motion?.duration ?? {})) {
      expect(TOKENS[`--duration-${kebab(name)}`]).toBe(value);
    }
  });
});

describe("tokens.css ↔ tokens.json（产物）", () => {
  it("tokens.json 就是 CSS 的投影，不多不少", () => {
    expect(JSON_BODY).toBe(render(TOKENS));
  });
});

describe("patterns.css", () => {
  const names = [...PATTERNS_CSS.matchAll(/(-{2}pattern-[a-z]+)\s*:/g)]
    .map((match) => match[1])
    .filter((name): name is string => name !== undefined);

  it("五种图案都定义了", () => {
    expect(names.sort()).toEqual(
      [
        "--pattern-checker",
        "--pattern-confetti",
        "--pattern-dots",
        "--pattern-stripes",
        "--pattern-waves",
      ].sort(),
    );
  });

  it("每一条 --pattern-* 都有对应的 .bg-pattern-* 工具类", () => {
    for (const name of names) {
      const utility = `.bg-pattern-${name.replace("--pattern-", "")}`;
      expect(PATTERNS_CSS, `${name} 有令牌但没有工具类，等于图案没人能使上劲`).toContain(utility);
    }
  });

  it("data URI 里的颜色字面量都在色板里（改色板不漏图案）", () => {
    const palette = new Set(Object.values(TOKENS).filter((v) => /^#[0-9a-f]{6}$/.test(v)));
    const used = [...PATTERNS_CSS.matchAll(/%23([0-9A-Fa-f]{6})/g)].map(
      (m) => `#${m[1]?.toLowerCase()}`,
    );
    expect(used.length).toBeGreaterThan(4);
    for (const hex of used) {
      expect(palette.has(hex), `图案用了色板外的 ${hex}`).toBe(true);
    }
  });
});
