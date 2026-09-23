# ADR-0013: 设计令牌以 `tokens.css` 为唯一真源，`tokens.json` 是投影

- **状态**：Accepted
- **日期**：2026-09-23
- **决策人**：用户 + Qoder
- **相关**：ADR-0008（前端 IA 与孟菲斯令牌）、`docs/specs/ui-tokens.md`、
  `docs/plans/v2.0-implementation.md` Task 10-13、`frontend/scripts/token-projection.ts`

## 背景

`ui-tokens.md §10` 原本设想的形状是：**一份 YAML 源 → 三个产物**
（`tokens.json` + `tokens.css` + `tailwind.config.ts`），由
`frontend/scripts/gen-tokens.ts` 生成，"保证三处一致"。

Task 10 落地时先量了工具链的现状（不是查文档，是把包装上跑起来）：

| 事实 | 怎么量出来的 |
|---|---|
| Tailwind 已是 v4（4.3.3），v4 的主题入口就是 CSS 里的 `@theme` 块；`tailwind.config.ts` + `postcss.config.js` 是 v3 的形状 | `npm view tailwindcss version` → `4.3.3`；装 `@tailwindcss/vite` 后 `@theme` 直接产出工具类 |
| `@theme` **本身就是机器可读的**：`--color-hot-pink: #ff3d9a` 既能被 CSS 用、也能被 Tailwind 生成 `bg-hot-pink`、也能被脚本解析 | `node_modules/.bin/vite build` 出的 `dist/assets/*.css` 里能看到生成的工具类 |
| 装 v3 的 YAML→三份产物等于在 v4 上再造一处源：`@theme` 已经是一处，YAML 是第二处，`tailwind.config.ts` 是第三处 | —— |
| `stylelint-config-standard@39` 的 peer 是 `stylelint ^16.23`，与 `stylelint@17` 冲突，`npm install` 直接 ERESOLVE 失败 | 第一次安装 exit=1，把 stylelint 降到 ^16.23 后才装上 |
| jsdom 30 的传递依赖 `whatwg-url@17` 要求 `node ^22.14 ‖ >=24`，而 CI 里 `NODE_VERSION: '20'` | `npm install` 的 EBADENGINE 警告；本机 node v22.12.0 |
| 计划里 `npx shadcn@latest init` + `add` 的 13 个组件：它要 `components.json`，会把一整套"圆角 + 柔和阴影"的基线样式写进仓库，而 §1 的纪律是**直角、无模糊硬阴影** | 读 `docs/plans/v2.0-implementation.md` Task 10 Step 3 与 `ui-tokens.md §1/§3` |

还有一件与令牌无关但同形状的事：prettier 的 CSS 解析器在第一次 `--write` 时就报
`Unknown word --spacing` —— 根因是我自己写的注释里有一句 `p-*/m-*/gap-*`，
其中 `*/` **把注释提前闭合了**。这不是工具挑剔：那份 `tokens.css` 在浏览器里也是坏的。
"跑一遍格式化工具"在这一次直接抓到了一个真 bug。

## 选项

**A. 照计划做 YAML 源 → 三份产物。**
在 v4 上就是三处真相。而且"三份一致"要靠生成器保证，生成器又要有人跑 ——
`tokens.json` 漂了没人知道，正是本仓库反复在拆的那类东西。

**B. `tokens.css` 当唯一源，`tokens.json` 降级为**投影**（单向生成），漂移由用例核。** ← 选定

**C. 干脆不要 `tokens.json`。**
不行：`§12` 写的是"V3 换框架时 `tokens.json` 直接复用"，那是已经锁进契约的东西；
而且 §7 要求"所有动效走 framer-motion、令牌从 `tokens.json` 读" ——
JS 侧要拿动效值，投影就是它唯一不该复制常量的入口（`src/lib/tokens.ts` 真的在读它）。

## 决定

1. **`frontend/src/styles/tokens.css` 的 `@theme` 是唯一真源**；`tailwind.config.ts`
   与 `postcss.config.js` 不再存在（v4 不需要），`components.json` / shadcn 不引入。
   需要 Radix 的无障碍基元（Dialog / Tooltip / Toast）时**直接装 `@radix-ui/react-*`**
   并自己写孟菲斯皮肤 —— 那样每个文件都能审，不会一次性拿到 22 个圆角组件。
2. **`frontend/tokens.json` 由 `scripts/gen-tokens.ts` 单向生成**（`npm run tokens`），
   结构就是 `{"source": "src/styles/tokens.css", "tokens": {变量名: 原样值}}` ——
   键名与 CSS 变量**一字不差**，所以 §12.2"CSS 变量直接复用"与 §12.1"`tokens.json` 直接复用"
   是同一件事，不需要第二次映射表。
3. **漂移由用例钉**（`src/styles/tokens.spec.ts`），三个方向：
   - spec ↔ CSS：从 `docs/specs/ui-tokens.md` 里**解 YAML 源**再比（§2 色 / §3 形状 /
     §5 十三档间距 / §7 缓动与时长），比之前两边都做一跳 `var()` 与 `{ref}` 展开；
     §3 那一节需要一张手写的"spec 键 → 变量名"表（`hard_shadow` 直译会得到
     `--shadow-hard-shadow`），所以**顺带断言两边键集合相等**，映射表不许漏项也不许多项。
   - CSS ↔ JSON：`tokens.json` 必须逐字节等于重新生成的投影。
   - 图案：`--pattern-*` 每条都要有 `.bg-pattern-*` 工具类；data URI 里的颜色字面量必须在色板里。
4. **解析器只有一份**：`token-projection.ts` 导出纯函数，生成脚本与漂移用例 import 同一个
   `parseTheme` —— 两份解析器就等于一个永远绿但抓不到东西的检查。
5. **工具链版本对齐是门禁的一部分**（经验 27 的前端版）：
   `package.json` 的 `engines.node` 写 `>=22.14`，CI 的 `NODE_VERSION` 从 `'20'` 提到 `'22.14'`，
   stylelint 钉 `^16.23`（与 `stylelint-config-standard@39` 的 peer 一致）。
   vitest 的覆盖率门槛（lines/functions ≥70）写进 `vite.config.ts`，
   与 CI 那条命令行同一个值 —— 两边不同值就等于有一个是假的。

## 后果

- 改了 `tokens.css` 要跑 `npm run tokens` 并重提 `tokens.json`；忘了就会被那条
  "逐字节等于投影"的用例拦住（不是靠人记得）。
- 计划文档 Task 10 的 `tailwind.config.ts` / `postcss.config.js` / `components.json`
  三项**不做**，理由即上文表格；这属于计划与实施有出入，因此记在这里而不是只写在 progress 里。
- §10 那句"生成脚本从 YAML 源生成三份产物"变成不成立的描述，已同步改
  `docs/specs/ui-tokens.md`（§10 与文件头的"源文件"那一行）。
- 令牌的"眼睛看得出的那三件事"目前能验到的程度：用 `evaluate_script` 读
  `getComputedStyle`（卡片 `3px solid` / `border-radius: 0px` / `6px 6px 0`、
  全站非 0 圆角只有 5 个 `.memphis-badge` 的 `9999px`、`document.fonts` 里
  Archivo Black 400 与 Space Grotesk 700 状态 `loaded`）。**像素级截图仍未验**
  （in-app browser 当时没有可见表面，`take_screenshot` 回 `VIEWPORT_UNAVAILABLE`）。
- 以后新增非颜色令牌（如 `--switch-knob` 这类尺寸）同样只有一处：写进 `@theme`，
  投影自动跟上，漂移用例自动覆盖（它比的是全集，不是白名单）。
