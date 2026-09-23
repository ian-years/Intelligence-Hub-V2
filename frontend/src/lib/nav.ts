export interface NavItem {
  to: string;
  label: string;
  /** `false` 的路由会渲染"待建（Task N）"而不是一个空白页 —— 见 `PageNotBuilt`。 */
  built: boolean;
}

/** 侧栏的那一份导航表（放在组件文件里会让 react-refresh 整文件失效，所以单独一处）。
 * 第 7 条 `/tokens` 是开发页，不在 ROADMAP 的 7 个业务页里；
 * `/video/:id` 是详情页、不进侧栏。
 *
 * **图案不在这张表里**：每页的图案（§6"一页一种"）由页面自己用 `<PageShell pattern=…>`
 * 声明 —— 中央再存一份"路由 → 图案"就是第二处真相，而且必然漏掉 `/video/:id` 这种
 * 不在侧栏里的路由。 */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "总览", built: false },
  { to: "/feed", label: "作品流", built: false },
  { to: "/creators", label: "博主", built: false },
  { to: "/tasks", label: "任务", built: false },
  { to: "/preflight", label: "预检", built: true },
  { to: "/settings", label: "设置", built: false },
  { to: "/tokens", label: "令牌对照", built: true },
];
