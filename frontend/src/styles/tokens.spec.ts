// @vitest-environment node
/**
 * 这条 spec 只读文件、不碰 DOM，所以跑在 node 环境。
 * （jsdom 下 `import.meta.url` 是 http: URL，`fileURLToPath` 会直接抛
 * "The URL must be of scheme file" —— 第一次跑就撞上了。）
 */
import { readdirSync, readFileSync } from "node:fs";
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

/** `[data-theme="…"] { --x: y; }` 那两块 → 主题名 → 令牌表。 */
function themeOverrides(): Map<string, Map<string, string>> {
  const out = new Map<string, Map<string, string>>();
  for (const match of CSS.matchAll(/\[data-theme="([a-z]+)"\]\s*\{([\s\S]*?)\}/g)) {
    const tokens = new Map<string, string>();
    for (const decl of (match[2] ?? "").matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
      tokens.set(decl[1] ?? "", (decl[2] ?? "").trim());
    }
    out.set(match[1] ?? "", tokens);
  }
  return out;
}

const DARK = themeOverrides().get("dark") ?? new Map<string, string>();

/** 暗色下的实际值 = `@theme` 打底，暗色块覆盖。 */
function valueOf(name: string, dark: boolean): string {
  return (dark ? DARK.get(name) : undefined) ?? TOKENS[name] ?? "";
}

function channel(unit: number): number {
  const c = unit / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const body = hex.replace("#", "");
  const parts = [0, 2, 4].map((at) => channel(Number.parseInt(body.slice(at, at + 2), 16)));
  return 0.2126 * (parts[0] ?? 0) + 0.7152 * (parts[1] ?? 0) + 0.0722 * (parts[2] ?? 0);
}

/** WCAG 2.1 对比度。"全站可读"这四个字唯一的量法就是它。 */
function contrast(first: string, second: string): number {
  const a = luminance(first);
  const b = luminance(second);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** 五个色块色（`bg-*` 用它们）。`grey-mist` 是次级底，不在这组里。 */
const ACCENTS = ["electric-blue", "coral-red", "lemon-yellow", "mint-green", "hot-pink"];

/** 递归收集 `src/**\/*.tsx`（跳过 spec 与 node_modules）。 */
function tsxSources(): Map<string, string> {
  const found = new Map<string, string>();
  const walk = (dir: URL): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const child = new URL(`${entry.name}`, `${dir.href}/`);
      if (entry.isDirectory()) {
        if (entry.name !== "node_modules") walk(child);
        continue;
      }
      if (!entry.name.endsWith(".tsx") || entry.name.endsWith(".spec.tsx")) continue;
      found.set(entry.name, readFileSync(child, "utf8"));
    }
  };
  walk(new URL("../", import.meta.url));
  return found;
}

describe('§11 暗色版（tokens.css 里的 [data-theme="dark"]）', () => {
  it("暗色块存在且覆盖了那几个角色令牌（否则下面每一条都是空转）", () => {
    expect(DARK.size).toBeGreaterThanOrEqual(4);
    for (const name of ["--color-paper-cream", "--color-ink-black", "--color-grey-mist"]) {
      expect(DARK.has(name), `暗色没换 ${name}`).toBe(true);
    }
  });

  it("暗色块里只出现 @theme 已经声明过的令牌名", () => {
    // 多一个没人声明的名字 = 界面上没有任何东西会读它，而它看起来像"调过了"。
    for (const name of DARK.keys()) {
      expect(TOKENS[name], `@theme 里没有 ${name}`).toBeDefined();
    }
  });

  it("每一个暗色覆盖值都是合法的十六进制色", () => {
    for (const [name, value] of DARK) {
      expect(value, `${name} 不是十六进制色`).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it("两个主题、每一对角色色，对比度都量过线", () => {
    const cases: [string, string, string, number][] = [
      // [前景令牌, 背景令牌, 用途, 门槛]
      ["--color-ink-black", "--color-paper-cream", "正文在面上", 7],
      ["--color-ink-black", "--color-grey-mist", "正文在次级底上", 4.5],
      ["--color-electric-blue", "--color-paper-cream", "链接在面上", 4.5],
      ["--color-on-accent", "--color-coral-red", "字在色块上", 4.5],
      ["--color-on-accent", "--color-lemon-yellow", "字在色块上", 4.5],
      ["--color-on-accent", "--color-mint-green", "字在色块上", 4.5],
      ["--color-on-accent", "--color-hot-pink", "字在色块上", 4.5],
      // 蓝块上的字是 paper-cream 而不是 on-accent，理由见 globals.css 那段注释
      ["--color-paper-cream", "--color-electric-blue", "字在蓝块上", 4.5],
    ];
    for (const dark of [false, true]) {
      for (const [fgToken, bgToken, use, floor] of cases) {
        const ratio = contrast(valueOf(fgToken, dark), valueOf(bgToken, dark));
        const label = `${dark ? "暗" : "亮"}色 ${use}：${fgToken} on ${bgToken}`;
        expect(ratio, `${label} = ${ratio.toFixed(2)} < ${String(floor)}`).toBeGreaterThanOrEqual(
          floor,
        );
      }
    }
  });

  it("凡是被当正文色用的色，压在页面底上两个主题都过 4.5:1", () => {
    // 判据不是"哪几个色不许当字"（那是人记的规矩），而是"你在源码里真把它当了字，
    // 我就量它"。侧栏与任务页原来那两处 `text-coral-red`（压在 cream 上 2.6:1）
    // 就是这么被逮到的；`text-electric-blue` 当链接色过线（4.6 / 6.4），所以留着。
    // 两个面/线令牌自己不在名单里：`text-paper-cream` 是"色块上的字"，不是"面上的字"。
    const MEASURABLE = [...ACCENTS, "grey-mist", "on-accent"];
    const used = new Set<string>();
    for (const [, text] of tsxSources()) {
      for (const match of text.matchAll(/\btext-([a-z-]+)\b/g)) {
        const name = match[1] ?? "";
        if (MEASURABLE.includes(name)) used.add(name);
      }
    }
    expect(used.size).toBeGreaterThan(0);
    for (const name of used) {
      for (const dark of [false, true]) {
        const ratio = contrast(
          valueOf(`--color-${name}`, dark),
          valueOf("--color-paper-cream", dark),
        );
        expect(
          ratio,
          `text-${name} 在${dark ? "暗" : "亮"}色面上只有 ${ratio.toFixed(2)}:1（要当字用得 ≥4.5）`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("每一处 bg-electric-blue 都显式带着它的配字", () => {
    // 蓝块是五个里唯一"近黑字压不住"的那个（4.1:1），配字必须是 paper-cream。
    // 漏写不会报错，只会得到一段读不清的字 —— 所以扫源码而不是相信写的人。
    const unpaired: string[] = [];
    let seen = 0;
    for (const [name, text] of tsxSources()) {
      for (const line of text.split("\n")) {
        if (!line.includes("bg-electric-blue")) continue;
        seen += 1;
        if (!line.includes("text-paper-cream")) unpaired.push(`${name}: ${line.trim()}`);
      }
    }
    expect(seen, "一处 `bg-electric-blue` 都没有 → 这条在空转").toBeGreaterThan(0);
    expect(unpaired).toEqual([]);
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
