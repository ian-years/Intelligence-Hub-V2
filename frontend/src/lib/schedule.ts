/** 定时采集那块表单的纯逻辑：线格式 → 应用视图，以及草稿 → 该 PUT 出去的键集合。
 *
 * 单独一个文件有两个原因，都不是"整洁"：
 * 1. eslint 的 `react-refresh/only-export-components` 不许组件文件混导出函数
 *    （这条在本仓库被拦过一次，见 `lib/export.ts` 的来历）。
 * 2. 这一段值得被单独测：它的判据不是"字段填得对不对"，而是"哪些字段**变了**"，
 *    而后者是 PUT 请求体的形状，出错时表现为"我没打算改的东西被改了"。
 */

import type { ScheduleStatus, ScheduleUpdate } from "@/api/hooks/useSchedule";

export interface ScheduleDraft {
  cron: string;
  /** 空数组 = "所有启用的平台"，与后端的 `collect_platforms: []` 同一语义。 */
  platforms: string[];
  /** `""` = 不限（交回 `null`）。 */
  limit: string;
}

/** 把 OpenAPI 生成出来的"带默认值的可选字段"收成**齐全**的应用视图。
 *
 * FastAPI 给有 `default` 的字段不标 required，于是 `schema.d.ts` 里这些数组是 `?:`；
 * 直接在组件里 `status.jobs.map(...)` 会让 tsc 逐处报"possibly undefined"，
 * 而逐个 `?? []` 会把同一个假设抄五遍。缺字段只可能是后端漏给 —— 在这里一次归零。
 */
export interface ScheduleView extends ScheduleStatus {
  collect_platforms: string[];
  effective_platforms: string[];
  skipped_platforms: string[];
  jobs: NonNullable<ScheduleStatus["jobs"]>;
  shadowed_by_env: string[];
}

export function viewOf(status: ScheduleStatus): ScheduleView {
  return {
    ...status,
    collect_platforms: status.collect_platforms ?? [],
    effective_platforms: status.effective_platforms ?? [],
    skipped_platforms: status.skipped_platforms ?? [],
    jobs: status.jobs ?? [],
    shadowed_by_env: status.shadowed_by_env ?? [],
  };
}

export function draftOf(status: ScheduleStatus): ScheduleDraft {
  return {
    cron: status.collect_cron ?? "",
    platforms: [...(status.collect_platforms ?? [])],
    limit:
      status.collect_limit === null || status.collect_limit === undefined
        ? ""
        : String(status.collect_limit),
  };
}

/** 只把**变了**的键放进请求体。
 *
 * 为什么不是"整份提交"：后端的契约是"没出现的键保持原值"，而 `collect_cron: null`
 * 与"不带这个键"是两件不同的事（前者是关掉定时，后者是不动它）。总是提交全表的话，
 * 用户改一个数字就顺手把 cron 也写了一遍。
 */
export function planUpdate(current: ScheduleStatus, draft: ScheduleDraft): ScheduleUpdate {
  const body: ScheduleUpdate = {};
  const wantedCron = draft.cron.trim();
  if (wantedCron !== (current.collect_cron ?? "")) body.collect_cron = wantedCron || null;

  const wanted = [...draft.platforms].sort();
  const now = [...(current.collect_platforms ?? [])].sort();
  if (wanted.join(",") !== now.join(",")) body.collect_platforms = wanted;

  const wantedLimit = draft.limit.trim() === "" ? null : Number(draft.limit.trim());
  if (wantedLimit !== (current.collect_limit ?? null)) body.collect_limit = wantedLimit;

  return body;
}

/** 表单有没有改动。判据是 `planUpdate` 空不空，而不是逐字段比初值 ——
 * 两处各比一遍早晚会出现"按钮说改动了、请求体说没有"。 */
export function isDirty(status: ScheduleStatus, draft: ScheduleDraft): boolean {
  return Object.keys(planUpdate(status, draft)).length > 0;
}
