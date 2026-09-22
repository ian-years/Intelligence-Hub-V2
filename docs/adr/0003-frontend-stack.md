# ADR-0003: 前端工程化栈

- **状态**：Accepted
- **日期**：2026-09-22
- **决策人**：用户 + Qoder
- **相关**：Q4、ADR-0008（孟菲斯设计令牌）

## 背景

V1 是原生 JS（`launcher/index.html` + `app.js` + `app-core.js` + `app.css`），无构建步骤。纯函数放 `app-core.js`（唯一能被 node 直接测的那块）。

V1 的痛点：
- 没有类型系统，API 响应字段靠手抄
- 没有组件化，状态散在 DOM 与全局变量里
- 没有 HMR，改一行刷一次
- 配置面板要手搓表单（V2 要按 JSON Schema 自动渲染）
- 孟菲斯风格细节多（几何形状、硬阴影、图案背景、动效），原生 CSS 写起来成本翻倍

V2 要：
1. 孟菲斯风格 UI（亮色块、粗黑边、硬阴影、几何形状、不对称布局）
2. 配置面板按 JSON Schema 自动渲染表单
3. 实时事件流（SSE）渲染任务进度
4. 设计令牌作为 V3 的契约（V3 换框架可复用）

## 决定

| 层 | 选型 | 理由 |
|---|---|---|
| 框架 | **React 18 + TypeScript + Vite** | 生态最广，OpenAPI → TS 类型生成的工具链最成熟；Vite HMR 快 |
| 样式 | **Tailwind CSS + CSS 变量** | 孟菲斯的色板/形状/图案都进 `tailwind.config.ts`，**令牌本身是 V3 的契约** |
| 组件底座 | **shadcn/ui（Radix Primitives + 复制粘贴）** | 拿到无障碍的 Dialog/Select/Switch/Combobox，再把外观全部改成孟菲斯；不用 antd/MUI（设计语言太强，改孟菲斯等于打架） |
| 数据层 | **TanStack Query + openapi-typescript** | 异步状态、缓存、重试；从后端 OpenAPI 自动生成 TS 类型 |
| 客户端状态 | **Zustand** | 比 Redux 轻、比 Context 强 |
| 表单 | **React Hook Form + Zod** | Zod schema 可与后端 Pydantic 通过 OpenAPI 对齐 |
| 路由 | **React Router v7（hash router）** | 单机工作台不需要 SSR |
| 动效 | **Framer Motion** | 孟菲斯那种"几何形状跳出来"的感觉需要它 |
| 虚拟滚动 | **@tanstack/react-virtual** | Feed 页瀑布流上千条作品时不卡 |
| 测试 | **Vitest + Testing Library + Playwright** | 单元 + 组件 + E2E |
| Lint/Format | **ESLint + Prettier + Stylelint** | Stylelint 卡硬编码颜色/间距，强制走设计令牌 |
| 构建产物 | **Vite 打包 → FastAPI 静态 serve** | 单端口部署（保持 V1 "一个 8789 端口"的体验） |

**不选**：
- Vue 3 → 同样可行，但 OpenAPI → 类型的工具链 React 更成熟
- Svelte 5 → bundle 更小，但生态对 shadcn 这类组件底座支持弱
- 原生 JS + Web Components → 工程量低但配置面板/表单/动效都得手搓，孟菲斯细节多的风格成本翻倍
- Next.js → 单机工作台不需要 SSR/RSC

## 后果

**好处**：
- **设计令牌（CSS 变量 + JSON）是契约**：V3 即使换 Vue/Svelte/Solid，色板与间距系统直接复用
- **OpenAPI 生成的 TS 类型是契约**：V3 后端就算换 Go/Rust，只要还出 OpenAPI，前端类型不用手写
- **组件 = 实现，可弃**：令牌 + 类型 + 行为契约 = 规范，保留
- shadcn/ui 的复制粘贴模式 = 我们拥有组件源码，可以彻底改成孟菲斯，不受运行时库限制
- TanStack Query 的缓存与重试让 SSE 事件流断线重连无缝

**代价**：
- 引入构建步骤（V1 是 zero-build），`npm install` 与 `vite build` 是开发流程的一部分
- 前端依赖体积（node_modules 约 300 MB）
- React 学习曲线（hooks / context / suspense）对纯后端开发者有门槛
- shadcn/ui 组件复制粘贴意味着我们要自己维护这些组件源码（升级不会自动跟）

**对 V3 的意义**：
- V3 即使重写前端（换框架），`tokens.json` + `openapi-typescript` 生成的类型 + Playwright E2E 用例都可复用
- 孟菲斯设计令牌（色板、形状、排版、图案、动效）是 UI 契约，V3 沿用同一份

**风险**：
- React 18 → 19 迁移（如有）可能影响 shadcn/ui，但 V2 锁 React 18，升级走 ADR
- Vite 6 → 7 升级风险低，配置兼容
- Tailwind 3 → 4 升级有破坏性变更（CSS-first 配置），V2 锁 Tailwind 3，升级走 ADR
