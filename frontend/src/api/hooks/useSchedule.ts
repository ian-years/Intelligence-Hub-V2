import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas } from "../client";
import { keys } from "../keys";

export type ScheduleStatus = Schemas["ScheduleStatus"];
export type CollectJobInfo = Schemas["CollectJobInfo"];
export type ScheduleUpdate = Schemas["ScheduleUpdate"];
export type ScheduleUpdateResponse = Schemas["ScheduleUpdateResponse"];
export type RunNowResponse = Schemas["RunNowResponse"];

/** 定时采集的现状：配置那份 + 调度器上真排着的那份，**并排**给界面看。
 *
 * 两个来源分开显示是这个端点的设计（不是顺手），理由在 `api/v1/schedule.py` 的
 * `ScheduleStatus` docstring 里：只看配置会说谎（改了没生效），只看 job 也会说谎
 * （`scheduler.enabled: false` 时一条都没有，而配置明明写着 08:00）。
 * 所以界面上一律两栏并排，谁也不替谁圆场。 */
export function useSchedule() {
  return useQuery({
    queryKey: keys.schedule,
    queryFn: () => api.get<ScheduleStatus>("/schedule"),
  });
}

/** 改定时采集。请求体**只带改过的那几个键** —— 后端按"没出现的键保持原值"处理。
 *
 * 这一条不是后端偷懒：把整份表原样 POST 回去是前端的常见写法，而这里那样做会把
 * 没在看的字段一起写一遍（`collect_cron: null` 与"不提交这个键"是两件不同的事）。
 * 所以草稿的构造点（`ScheduleCard`）必须显式挑键，见那边的 `planUpdate`。 */
export function useUpdateSchedule() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ScheduleUpdate) => api.put<ScheduleUpdateResponse>("/schedule", body),
    onSuccess: async () => {
      // 重取而不是拿响应体去填：响应里那份 `schedule` 已经是后端算好的了，
      // 但任务清单可能因为平台开关而变（`/api/tasks` 与这里是同一份 enabled 判据）。
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.schedule }),
        client.invalidateQueries({ queryKey: keys.tasks }),
      ]);
    },
  });
}

/** 「立即跑一次」。返回的是 **202 已排队**，不是采完了。
 *
 * 界面拿到 `task_id` 之后要交给任务页的事件流去看结果，这里不许写成"采集完成"。
 * 平台被关掉时后端回 4xx（不是 202），所以按钮不需要在前端预判开关状态 ——
 * 预判就是第二份判据，早晚与后端分叉。 */
export function useRunNow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (platform: string) => api.post<RunNowResponse>("/schedule/run-now", { platform }),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.runs() }),
        client.invalidateQueries({ queryKey: keys.tasks }),
      ]);
    },
  });
}

/** 下次触发时刻的人话写法。`null` 说"没有排上的 job"，**不猜**"明天 08:00"。
 *
 * 三种情况在数据里是分开的：调度器没跑（`scheduler_running: false`）、
 * job 还没 pending（`next_run_time` 为 null）、以及真的算出了一个时刻。
 * 把它们合并成一句"未安排"会把第一种（配置开着但服务没起）藏起来。 */
export function nextRunLabel(job: CollectJobInfo | undefined): string {
  const stamp = job?.next_run_time;
  if (!stamp) return "没有排上的 job";
  const at = new Date(stamp);
  if (Number.isNaN(at.getTime())) return "时间读不出来";
  return new Intl.DateTimeFormat(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(at);
}
