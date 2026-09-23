export default {
  extends: "stylelint-config-standard",
  rules: {
    // Tailwind v4 的入口与主题块：`@import "tailwindcss"` / `@theme` / `@layer`
    // 都不是标准 CSS at-rule，stylelint-config-standard 会当成未知规则报红。
    "import-notation": null,
    "at-rule-no-unknown": [
      true,
      { ignoreAtRules: ["theme", "layer", "utility", "apply", "source", "variant"] },
    ],
    /**
     * §1/§3 的两条硬纪律，写成规则而不是评审时口头说：
     * - 禁止圆角（只有 `var(--radius-*)` 那两处允许，见下面的 override）
     * - 禁止带模糊的 box-shadow（孟菲斯的阴影是 `6px 6px 0` 三段，出现第四段就是 blur；
     *   也禁 `rgba(` —— 半透明阴影同样是模糊的一种入口）
     */
    "declaration-property-value-disallowed-list": {
      "border-radius": ["/^(?!var\\()/"],
      "box-shadow": ["/\\d+px\\s+\\d+px\\s+\\d/", "/rgba?\\(/"],
    },
    // §3 把直角写成 `corner_radius: "0px"`，而 stylelint 默认要 `0`。
    // 不改测试的比对（那是 spec ↔ CSS 的逐字核对），豁免这条规则：
    // 要么令牌写 `0`、要么 spec 改 `0`，动哪个都是改契约，留到需要时走 ADR。
    "length-zero-no-unit": null,
    "color-named": "never",
    "alpha-value-notation": "number",
    /**
     * 下面四条是对 stylelint-config-standard 的**有意**放宽，每条都有出处：
     * - `custom-property-pattern`：Tailwind v4 的复合主题键就叫
     *   `--text-h1--line-height`（双连字符是它的语法，不是我随手起的名字）。
     * - `selector-class-pattern`：组件类走 BEM（`.memphis-btn--primary`）。
     * - `color-hex-length`：`--fix` 会把 `#0066ff` 缩成 `#06f`，而 §2 的令牌要与
     *   `docs/specs/ui-tokens.md` 逐字对齐（`src/styles/tokens.spec.ts` 比的就是这个），
     *   缩写完那条漂移看护立刻红。
     * - `custom-property-empty-line-before`：令牌按 § 分节，节与节之间空行 + 注释是可读性。
     */
    "custom-property-pattern": [
      "^([a-z][a-z0-9]*)(-{1,2}[a-z0-9]+)*$",
      { message: "CSS 变量用 kebab（可含 Tailwind 的双连字符复合键）" },
    ],
    "selector-class-pattern":
      "^[a-z][a-z0-9]*(-[a-z0-9]+)*(__[a-z0-9]+(-[a-z0-9]+)*)?(--[a-z0-9]+(-[a-z0-9]+)*)?$",
    "color-hex-length": null,
    "custom-property-empty-line-before": [
      "always",
      { ignore: ["after-comment", "after-custom-property", "first-nested"] },
    ],
  },
  overrides: [
    {
      // 令牌源本身当然要用字面量 —— 它就是唯一那处字面量。
      files: ["src/styles/tokens.css"],
      rules: { "declaration-property-value-disallowed-list": null },
    },
  ],
};
