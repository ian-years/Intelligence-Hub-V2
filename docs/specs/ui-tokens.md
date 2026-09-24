# Spec: UI Tokens（孟菲斯设计令牌）

> **状态**：Locked（V2.0 起契约稳定，改动需走 ADR）
> **源文件**：`frontend/src/styles/tokens.css`（`@theme` 块是唯一真源）
> + `frontend/tokens.json`（它的**投影**，由 `frontend/scripts/gen-tokens.ts` 生成，不手改）
> **相关 ADR**：[0008](../adr/0008-frontend-ia-and-memphis-tokens.md)、
> [0013](../adr/0013-token-css-is-the-single-source.md)（2026-09-23：原列的
> `frontend/tailwind.config.ts` 不再存在 —— Tailwind v4 的主题入口就是 `@theme`）

孟菲斯风格的设计令牌。**V3 换框架时这份令牌直接复用**，是 UI 契约。

---

## 1. 风格定位

孟菲斯（Memphis Design）是 1980 年代意大利的设计运动，特征：

- **亮色块**：电光蓝、珊瑚红、柠檬黄、薄荷绿、烈粉
- **粗黑描边**：3-4px，无渐变
- **硬阴影**：无模糊，纯色偏移（`6px 6px 0 ink-black`）
- **几何形状**：三角、圆、方、波浪线、锯齿
- **图案背景**：圆点阵、斜条纹、波浪线、几何碎片、棋盘格
- **不对称布局**：故意错位、压叠、超出网格
- **直角，不要圆角**：硬朗、反极简

参考：Ettore Sottsass、Carlton Room Divider、Memphis Milano 档案。

---

## 2. 色板

### 2.1 主色（Primary）

```yaml
electric_blue: "#0066FF"   # 电光蓝
coral_red:     "#FF6B6B"   # 珊瑚红
lemon_yellow:  "#FFD93D"   # 柠檬黄
mint_green:    "#6BCB77"   # 薄荷绿
hot_pink:      "#FF3D9A"   # 烈粉
```

### 2.2 中性色（Neutral）

```yaml
ink_black:   "#0A0A0A"     # 漆黑（描边、阴影、文字）
paper_cream: "#FAF7F2"     # 米白（背景）
grey_mist:   "#D8D8D8"     # 雾灰（禁用态、分隔线）
```

### 2.3 语义色（Semantic）

```yaml
success: "{mint_green}"
warning: "{lemon_yellow}"
danger:  "{coral_red}"
info:    "{electric_blue}"
```

### 2.4 平台色（Platform）

每个平台一个主色 + 一个几何形状：

```yaml
douyin:      { color: "{hot_pink}",      shape: "triangle",  display_name: "抖音" }
bilibili:    { color: "{electric_blue}", shape: "circle",    display_name: "B站" }
xiaohongshu: { color: "{coral_red}",     shape: "square",    display_name: "小红书" }
youtube:     { color: "{lemon_yellow}",  shape: "wave",      display_name: "YouTube" }
```

### 2.5 落在饱和色块上的文字（On accent）

```yaml
on_accent: "#0A0A0A"
```

五个主色是**块**，不是字。块上的字在两版主题里都是近黑这一档，所以它必须是自己的令牌：
暗色版（§11）把 `ink_black` 换成浅色当正文色，如果块上的字跟着它走，
就得到"亮块 + 亮字"。CSS 侧由 `globals.css` 那条
`.bg-coral-red, .bg-lemon-yellow, .bg-mint-green, .bg-hot-pink { color: var(--color-on-accent) }`
统一施加，不用每个调用点自己写。

`electric_blue` **不在那条名单里**：它是五个里唯一深到"近黑字压不住"的一个
（`#0066ff` 上是 4.1:1），它的配字是 `paper_cream` —— 那两个令牌在暗色下互换角色，
所以这一对在两版里都过 4.5:1。漏写配字的位置由 `src/styles/tokens.spec.ts` 扫源码钉住。

被当**正文色**用的色（`text-<色>`）另有一条量法：扫描 `src/**/*.tsx` 里真出现过的
`text-*`，逐个量它压在页面底上的对比度 ≥ 4.5:1（两版都量）。

---

## 3. 形状语言

```yaml
shape:
  border_width: "3px"
  border_color: "{ink_black}"
  hard_shadow: "6px 6px 0 {ink_black}"           # 无模糊硬阴影
  hard_shadow_lg: "10px 10px 0 {ink_black}"
  hard_shadow_sm: "3px 3px 0 {ink_black}"
  corner_radius: "0px"                            # 直角，不要圆角
  corner_radius_pill: "9999px"                    # 仅用于状态徽章
```

**纪律**：
- 所有卡片、按钮、输入框、模态框都用 `border: 3px solid ink-black` + `box-shadow: 6px 6px 0 ink-black`
- **禁止 `border-radius`**（除非是 `pill` 用于徽章）
- **禁止 `box-shadow` 带模糊**（如 `0 4px 6px rgba(0,0,0,0.1)`）

---

## 4. 排版

```yaml
typography:
  display:
    family: "Archivo Black, sans-serif"
    weight: 900
    sizes:
      xl: { size: "3rem",   line_height: "1.0", letter_spacing: "-0.02em" }
      lg: { size: "2.25rem", line_height: "1.1", letter_spacing: "-0.01em" }
      md: { size: "1.75rem", line_height: "1.2", letter_spacing: "0" }
  heading:
    family: "Space Grotesk, sans-serif"
    weight: 700
    sizes:
      h1: { size: "2rem",   line_height: "1.2" }
      h2: { size: "1.5rem", line_height: "1.3" }
      h3: { size: "1.25rem", line_height: "1.4" }
  body:
    family: "Inter, sans-serif"
    weight: 400
    sizes:
      lg: { size: "1.125rem", line_height: "1.6" }
      md: { size: "1rem",     line_height: "1.6" }
      sm: { size: "0.875rem", line_height: "1.5" }
  mono:
    family: "JetBrains Mono, monospace"
    weight: 400
    sizes:
      md: { size: "0.9375rem", line_height: "1.5" }
      sm: { size: "0.8125rem", line_height: "1.5" }
```

**字体加载**：用 `@fontsource/*` npm 包自托管，不走 Google Fonts CDN（避免外网依赖）。

---

## 5. 间距

```yaml
spacing:
  unit: "4px"
  scale:
    0:  "0"
    1:  "4px"
    2:  "8px"
    3:  "12px"
    4:  "16px"
    5:  "20px"
    6:  "24px"
    8:  "32px"
    10: "40px"
    12: "48px"
    16: "64px"
    20: "80px"
    24: "96px"
```

**纪律**：所有 `padding` / `margin` / `gap` 必须走 scale，**禁止任意值**（Stylelint 兜底）。

---

## 6. 图案背景

SVG 平铺，CSS `background-image` 内联 data URI。每个页面一种图案。

```yaml
pattern:
  dots:
    description: "圆点阵"
    page: "Dashboard"
    svg: |
      <svg width="40" height="40" xmlns="http://www.w3.org/2000/svg">
        <circle cx="20" cy="20" r="2" fill="#0A0A0A" opacity="0.15"/>
      </svg>
  stripes:
    description: "斜条纹"
    page: "Feed / Preflight"
    svg: |
      <svg width="20" height="20" xmlns="http://www.w3.org/2000/svg">
        <path d="M0,20 L20,0" stroke="#0A0A0A" stroke-width="1" opacity="0.1"/>
      </svg>
  waves:
    description: "波浪线"
    page: "Video Detail"
    svg: |
      <svg width="60" height="20" xmlns="http://www.w3.org/2000/svg">
        <path d="M0,10 Q15,0 30,10 T60,10" stroke="#0A0A0A" stroke-width="1.5" fill="none" opacity="0.12"/>
      </svg>
  confetti:
    description: "几何碎片"
    page: "Creators / Tasks"
    svg: |
      <svg width="80" height="80" xmlns="http://www.w3.org/2000/svg">
        <rect x="10" y="10" width="6" height="6" fill="#FF3D9A" opacity="0.2"/>
        <circle cx="50" cy="30" r="3" fill="#0066FF" opacity="0.2"/>
        <polygon points="70,60 76,72 64,72" fill="#FFD93D" opacity="0.25"/>
      </svg>
  checker:
    description: "棋盘格"
    page: "Settings"
    svg: |
      <svg width="40" height="40" xmlns="http://www.w3.org/2000/svg">
        <rect width="20" height="20" fill="#0A0A0A" opacity="0.05"/>
        <rect x="20" y="20" width="20" height="20" fill="#0A0A0A" opacity="0.05"/>
      </svg>
```

**用法**：

```css
.page-dashboard {
  background-color: var(--color-paper-cream);
  background-image: var(--pattern-dots);
  background-repeat: repeat;
}
```

---

## 7. 动效

```yaml
motion:
  easing:
    bounce: "cubic-bezier(0.34, 1.56, 0.64, 1)"   # 弹回
    snap:   "cubic-bezier(0.4, 0, 0.2, 1)"        # 标准
  duration:
    instant: "100ms"
    fast:    "150ms"
    normal:  "250ms"
    slow:    "400ms"
  button_press:
    transform: "scale(0.95) → scale(1.0)"
    duration: "{duration.fast}"
    easing: "{easing.bounce}"
  modal_enter:
    description: "几何形状从四角飞入"
    duration: "{duration.normal}"
    easing: "{easing.bounce}"
  loading:
    description: "旋转三角 / 跳动圆点 / 摇摆波浪线（不用圆环）"
    duration: "{duration.slow}"
    iteration: "infinite"
  card_hover:
    transform: "translate(-2px, -2px)"
    shadow: "{shape.hard_shadow_lg}"
    duration: "{duration.instant}"
```

**纪律**：
- **禁止圆环加载指示器**（孟菲斯不用）
- 所有动效走 `framer-motion`，令牌从 `tokens.json` 读
- 用户系统设置 `prefers-reduced-motion` 时降级为瞬时切换

---

## 8. 组件令牌

```yaml
component:
  card:
    background: "{paper_cream}"
    border: "3px solid {ink_black}"
    shadow: "{shape.hard_shadow}"
    padding: "{spacing.6}"
  button:
    primary:
      background: "{electric_blue}"
      color: "{paper_cream}"
      border: "3px solid {ink_black}"
      shadow: "{shape.hard_shadow_sm}"
      padding: "{spacing.3} {spacing.5}"
      font: "{typography.heading.sizes.h3}"
    secondary:
      background: "{paper_cream}"
      color: "{ink_black}"
      border: "3px solid {ink_black}"
      shadow: "{shape.hard_shadow_sm}"
    danger:
      background: "{coral_red}"
      color: "{paper_cream}"
  badge:
    border: "2px solid {ink_black}"
    padding: "{spacing.1} {spacing.3}"
    font: "{typography.body.sizes.sm}"
    corner_radius: "{shape.corner_radius_pill}"
  input:
    background: "{paper_cream}"
    border: "3px solid {ink_black}"
    padding: "{spacing.3}"
    font: "{typography.body.sizes.md}"
    focus_shadow: "{shape.hard_shadow_sm}"
  switch:
    width: "64px"
    height: "32px"
    border: "3px solid {ink_black}"
    knob_size: "22px"
    on_background: "{mint_green}"
    off_background: "{grey_mist}"
```

---

## 9. 平台徽章

每个平台一个几何形状 SVG，可染色：

```tsx
// frontend/src/components/memphis/PlatformBadge.tsx
const SHAPES = {
  douyin:      <Triangle />,
  bilibili:    <Circle />,
  xiaohongshu: <Square />,
  youtube:     <Wave />,
} as const;

export function PlatformBadge({ platform, size = 'md' }: Props) {
  const color = `var(--color-platform-${platform})`;
  const Shape = SHAPES[platform];
  return (
    <span className="badge" style={{ background: color }}>
      <Shape size={size} />
      <span>{PLATFORM_DISPLAY_NAMES[platform]}</span>
    </span>
  );
}
```

---

## 10. tokens.json（机器可读）

`frontend/tokens.json` 是本节所有令牌的 JSON 版本，**V3 换框架时直接复用**。

**方向是单向的**（2026-09-23 改，见 [`docs/adr/0013`](../adr/0013-token-css-is-the-single-source.md)）：
源是 `frontend/src/styles/tokens.css` 的 `@theme` 块，`tokens.json` 由
`frontend/scripts/gen-tokens.ts`（`npm run tokens`）生成的**投影**，不手改。
原设想是"一份 YAML 源 → JSON + CSS 变量 + Tailwind config 三份产物"，
而 Tailwind v4 的 `@theme` 本身就已经是机器可读的令牌声明 —— 再加一份 YAML 源
就是第三处真相。键名与 CSS 变量**一字不差**，所以 §12.1 与 §12.2 是同一件事。

漂移由 `frontend/src/styles/tokens.spec.ts` 钉：`tokens.json` 必须逐字节等于
"现在重新生成的投影"；本 spec 里下面的 YAML 块也与 `tokens.css` 逐条比
（色板 / 形状 / 间距 scale / 缓动与时长），所以"文档改了没改代码"同样会红。

```json
{
  "source": "src/styles/tokens.css",
  "tokens": {
    "--color-electric-blue": "#0066ff",
    "--color-coral-red": "#ff6b6b",
    "--color-lemon-yellow": "#ffd93d",
    "--color-mint-green": "#6bcb77",
    "--color-hot-pink": "#ff3d9a",
    "--color-ink-black": "#0a0a0a",
    "--color-paper-cream": "#faf7f2",
    "--color-grey-mist": "#d8d8d8",
    "--color-platform-douyin": "var(--color-hot-pink)",
    "--border-width-memphis": "3px",
    "--shadow-hard": "6px 6px 0 var(--color-ink-black)",
    "--radius-none": "0px",
    "--radius-pill": "9999px",
    "--spacing": "4px",
    "--spacing-scale-6": "24px",
    "--ease-bounce": "cubic-bezier(0.34, 1.56, 0.64, 1)",
    "--duration-fast": "150ms"
  }
}
```

上面只是节选（真实文件 75 条，`npm run tokens` 的输出会把条数打出来）。JS 侧读它的是 `frontend/src/lib/tokens.ts`：
`cubic-bezier(...)` 会**解析成四个数**再交给 framer-motion，
时长要求带单位（`150ms` / `0.4s`），看不懂就抛 —— §7 那句
"所有动效走 framer-motion、令牌从 `tokens.json` 读"因此是真的有出处，
而不是每个组件自己写一个 `0.15`。

---

## 11. 暗色模式（V2.1 T5.6 已实施）

孟菲斯暗色版。切换写在 `<html data-theme="…">` 上，令牌仍是 CSS 变量：

- 背景：`#1A1A1A` 替代 `paper_cream`
- 文字与描边、硬阴影：`#FAF7F2`（`paper_cream` 那个字面值）替代 `ink_black`
- 主色保持亮色（烈粉/柠檬黄/薄荷绿在暗背景上更跳）
- 令牌走 `[data-theme="dark"]` 覆盖，组件不引"暗色变体"

三条实施时定下来的补充（原草稿没覆盖到，都带量过的数）：

1. **`grey_mist` 跟着换**（→ `#3D3D47`）。草稿只提了背景与文字两件，但次级底
   （"已隐藏"那一类标签）留在 `#D8D8D8` 上，压浅色正文只有 1.3:1。
2. **`electric_blue` 调亮**（→ `#4D9BFF`）。它同时被当链接文字用
   （`text-electric-blue`），`#0066FF` 压在 `#1A1A1A` 上是 3.6:1，过不了 4.5。
   其余四个主色只当块、不当字，所以只有它需要动。
3. **新增 §2.5 `on_accent`**。"主色保持亮色"与"正文色翻成浅色"两件事叠在一起时，
   色块上的字不能跟着正文色走。

角色与名字在暗色下对不上（`paper-cream` 是深色、`ink-black` 是浅色）是这一节的代价。
为什么不给组件换一组 `--color-surface` / `--color-line` 引用：那要改 400 多处
`bg-paper-cream` / `border-ink-black`，漏一处就是"暗色下某个角落还是白的"，
而**那种漏网没有任何测试能发现**；在这里改三个名字，界面要么全跟、要么当场红。
理由与备选见 `docs/adr/0023-dark-theme-by-role-swap.md`。

`data-theme` 只有 `light` / `dark` 两个值：界面上的第三档"跟随系统"在写进 DOM 之前
就被解析成两者之一（CSS 里没有"跟随"这种状态）。落点唯一：`components/shared/Layout.tsx`。

## 12. V3 重写时的契约

V3 即使换 Vue/Svelte/Solid：

1. `tokens.json` 直接复用
2. CSS 变量直接复用
3. 平台形状 SVG 直接复用
4. 组件 = 实现，可弃；令牌 = 契约，保留

→ UI 视觉一致性零成本继承。
