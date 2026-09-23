import type { JSX } from "react";

import { TokenSheet } from "./dev/TokenSheet";

/**
 * Task 10 的临时根组件：整页就是令牌对照。
 *
 * Task 11 会把它换成 `HashRouter + Layout + 7 个业务路由`，`TokenSheet` 挪到
 * `/tokens` 那条开发路由下（不是第 8 个业务页）。留在这里的理由只有一条：
 * 没有这一页，"三件事"（直角 / 3px 黑边 / 无模糊硬阴影）就只能靠读 CSS 判断。
 */
export default function App(): JSX.Element {
  return <TokenSheet />;
}
