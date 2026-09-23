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

export function useRuns(status?: string) {
  return useQuery({
    queryKey: keys.runs(status),
    queryFn: () => api.get<TaskRunRecord[]>("/tasks/runs", { status, limit: 50 }),
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
