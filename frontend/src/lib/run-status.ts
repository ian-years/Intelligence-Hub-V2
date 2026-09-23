import type { TaskRunRecord } from "@/api/hooks/useTasks";

export type RunStatus = TaskRunRecord["status"];

export interface RunMeta {
  label: string;
  /** Tailwind 类：孟菲斯色板里的底色 + 必要时反色文字。 */
  tone: string;
}

/**
 * 任务运行状态 → 界面上那一格。
 *
 * 单独一个 `lib/` 模块而不是留在组件里：这张表是**契约的投影**
 * （`schema.spec.ts` 拿它与 OpenAPI 快照逐字核），而组件要渲染才 import 得动 ——
 * 纯 node 环境的契约用例不该被迫拉起 React。
 *
 * `running` 给蓝色而不是绿色：它既不是"完成"也不是"没问题"。
 * 把在跑的任务涂成绿色是"看起来在跑"的 UI 版（`AGENTS.md §1.3`）。
 */
export const RUN_STATUS_META: Record<RunStatus, RunMeta> = {
  running: { label: "正在跑", tone: "bg-electric-blue text-paper-cream" },
  success: { label: "完成", tone: "bg-mint-green" },
  partial: { label: "部分完成", tone: "bg-lemon-yellow" },
  failed: { label: "失败", tone: "bg-coral-red" },
  timeout: { label: "超时", tone: "bg-coral-red" },
  cancelled: { label: "已取消", tone: "bg-grey-mist" },
};

/** 当成可能缺失来查：契约之外冒出来的取值原样打出来 + 灰底，
 *  而不是折叠成"未知状态"（那会把"前端没跟上"伪装成"任务本来就是这状态"）。 */
const LOOKUP: Partial<Record<RunStatus, RunMeta>> = RUN_STATUS_META;

export function statusMeta(status: RunStatus): RunMeta {
  return LOOKUP[status] ?? { label: status, tone: "bg-grey-mist" };
}
