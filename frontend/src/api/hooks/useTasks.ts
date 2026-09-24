import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas } from "../client";
import type { ObjectSchema } from "../json-schema";
import { keys } from "../keys";

export type TaskInfo = Schemas["TaskInfo"];
export type TaskRunRecord = Schemas["TaskRunRecord"];
export type RunDetail = Schemas["RunDetail"];
export type TaskAccepted = Schemas["TaskAccepted"];
export type ManifestSummary = Schemas["ManifestSummary"];

/** `/api/tasks` 只列**当前可提交**的任务：关掉的平台会让对应任务自动消失
 * （`TaskDefinition.platforms` 那一层，V1 §7.24 的开关语义）。所以这个列表要跟着
 * 平台配置失效，不能缓存到天荒地老。 */
export function useTasks() {
  return useQuery({ queryKey: keys.tasks, queryFn: () => api.get<TaskInfo[]>("/tasks") });
}

export function useTaskSchema(name: string) {
  return useQuery({
    queryKey: keys.taskSchema(name),
    queryFn: () => api.get<ObjectSchema>(`/tasks/${name}/schema`),
    enabled: name !== "",
  });
}

export function useRunTask(name: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (params: Record<string, unknown>) =>
      api.post<TaskAccepted>(`/tasks/${name}/run`, params),
    onSuccess: () => client.invalidateQueries({ queryKey: ["runs"] }),
  });
}

/** 有任务在跑时，运行历史隔这一会儿问一次。 */
const RUNNING_POLL_MS = 1_000;

/** 这一列里还有没有"正在跑"的行。纯函数是为了让那条判据能被单独测到（见 hooks.spec）。 */
export function anyRunning(runs: readonly { status: string }[] | undefined): boolean {
  return (runs ?? []).some((run) => run.status === "running");
}

export function useRuns(status?: string) {
  return useQuery({
    queryKey: keys.runs(status),
    queryFn: () => api.get<TaskRunRecord[]>("/tasks/runs", { status, limit: 50 }),
    /** 有 running 就轮询，没有就不轮。
     *
     * 为什么不是"SSE 事件到了 invalidate 一次"：那一层已经有（`useTaskEvents` 驱动实时事件流
     * 那一栏），但把 query-cache 的依赖塞进事件钩子里，三个页面都会跟着多一层隐式失效 ——
     * 那条路更长。而**没有这一条**的症状是 T5.7 的 e2e 撞出来的：点完"跑一次"，
     * 实时事件在滚，运行历史那一列永远停在"正在跑"，要刷新页面才认账。
     * `useRunTask.onSuccess` 那一次 invalidate 只发生在提交当场，救不了后面。 */
    refetchInterval: (query) => (anyRunning(query.state.data) ? RUNNING_POLL_MS : false),
  });
}

export function useRun(id: string | undefined) {
  return useQuery({
    queryKey: keys.run(id ?? ""),
    queryFn: () => api.get<RunDetail>(`/tasks/runs/${String(id)}`),
    enabled: id !== undefined && id !== "",
  });
}

export function useCancelRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Schemas["CancelResponse"]>(`/tasks/runs/${id}/cancel`),
    // 取消是**请求**不是结果：成功回 202 时任务可能还在 running，界面靠 SSE 事件翻状态，
    // 这里只让列表重取一次，不去假装"已取消"。
    onSuccess: () => client.invalidateQueries({ queryKey: ["runs"] }),
  });
}

export function useManifests(limit = 30) {
  return useQuery({
    queryKey: keys.manifests,
    queryFn: () => api.get<ManifestSummary[]>("/manifests", { limit }),
  });
}
