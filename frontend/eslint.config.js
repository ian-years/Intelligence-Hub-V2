import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/", "coverage/", "src/api/schema.d.ts"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      /**
       * 设计令牌不许被字面量绕过（§2/§3/§5 的"必须走令牌"纪律里，stylelint 只管 CSS，
       * 而 TSX 里的 `className="bg-[#ff6b6b]"` / `style={{color:"#0a0a0a"}}` 它看不见）。
       */
      "no-restricted-syntax": [
        "error",
        {
          selector: "Literal[value=/#[0-9a-fA-F]{6}/]",
          message:
            "别在 TSX 里写十六进制色值：用 var(--color-…)（令牌源是 src/styles/tokens.css）。",
        },
        {
          selector: "TemplateElement[value.cooked=/#[0-9a-fA-F]{6}/]",
          message: "别在模板串里写十六进制色值：用 var(--color-…)。",
        },
      ],
    },
  },
);
