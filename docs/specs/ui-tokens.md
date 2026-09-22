# Spec: UI Tokens（孟菲斯设计令牌）

> **状态**：Locked（V2.0 起契约稳定，改动需走 ADR）
> **源文件**：`frontend/src/styles/tokens.css` + `frontend/tokens.json` + `frontend/tailwind.config.ts`
> **相关 ADR**：[0008](../adr/0008-frontend-ia-and-memphis-tokens.md)

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

`frontend/tokens.json` 是上述所有令牌的 JSON 版本，**V3 换框架时直接复用**。生成脚本 `frontend/scripts/gen-tokens.ts` 从 YAML 源生成 JSON + CSS 变量 + Tailwind config，保证三处一致。

```json
{
  "color": {
    "primary": {
      "electric_blue": "#0066FF",
      "coral_red": "#FF6B6B",
      "lemon_yellow": "#FFD93D",
      "mint_green": "#6BCB77",
      "hot_pink": "#FF3D9A"
    },
    "neutral": {
      "ink_black": "#0A0A0A",
      "paper_cream": "#FAF7F2",
      "grey_mist": "#D8D8D8"
    },
    "platform": {
      "douyin": "#FF3D9A",
      "bilibili": "#0066FF",
      "xiaohongshu": "#FF6B6B",
      "youtube": "#FFD93D"
    }
  },
  "shape": { ... },
  "typography": { ... },
  "spacing": { ... },
  "motion": { ... },
  "component": { ... }
}
```

---

## 11. 暗色模式（V2.2）

孟菲斯暗色版色板要单独调，**V2.0 不做**。V2.2 实施时：

- 背景：`#1A1A1A` 替代 `paper_cream`
- 文字：`paper_cream` 替代 `ink_black`
- 主色保持亮色（电光蓝/烈粉等在暗背景上更跳）
- 描边与阴影：用 `paper_cream` 替代 `ink_black`
- 令牌走 CSS 变量 `[data-theme="dark"]` 切换

---

## 12. V3 重写时的契约

V3 即使换 Vue/Svelte/Solid：

1. `tokens.json` 直接复用
2. CSS 变量直接复用
3. 平台形状 SVG 直接复用
4. 组件 = 实现，可弃；令牌 = 契约，保留

→ UI 视觉一致性零成本继承。
