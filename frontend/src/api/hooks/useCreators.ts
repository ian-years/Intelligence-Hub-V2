import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas } from "../client";
import { keys } from "../keys";

export type Creator = Schemas["Creator"];
export type AddCreatorRequest = Schemas["AddCreatorRequest"];
export type TrackingRequest = Schemas["TrackingRequest"];

export function useCreators(platform?: string) {
  return useQuery({
    queryKey: keys.creators(platform),
    queryFn: () => api.get<Creator[]>("/creators", { platform }),
  });
}

export function useCreator(id: number | undefined) {
  return useQuery({
    queryKey: keys.creator(id ?? -1),
    queryFn: () => api.get<Creator>(`/creators/${String(id)}`),
    enabled: id !== undefined,
  });
}

/** 收录博主 = **提交一个 `add_creator` 任务**：这个端点回的是 202 + `{task_id}`
 * （`api/v1/creators.py` 的 `response_model=TaskAccepted`），**不是**一个 Creator。
 * 所以成功后博主不会立刻出现在列表里 —— 界面要说"已排队"，不能说"已添加"，
 * 更不许把响应当 Creator 读它的 `name`（那会显示出 `undefined`）。
 * 原来这里写的是 `api.post<Creator>`，连注释都跟着说"立即回一个 Creator"。
 *
 * `tracking` 请显式传（值取自 `stores/settings.captureTrackingDefault()`）：
 * 这个请求字段自己也有 `default=true`，两边各留一个默认值就是 V1 §7.24 那一坑。 */
export function useAddCreator() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AddCreatorRequest) => api.post<Schemas["TaskAccepted"]>("/creators", body),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["creators"] }),
        client.invalidateQueries({ queryKey: ["videos"] }),
        client.invalidateQueries({ queryKey: keys.runs() }),
      ]);
    },
  });
}

export function useSetTracking(creatorId: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (tracking: boolean) =>
      api.patch<Creator>(`/creators/${creatorId}/tracking`, { tracking } satisfies TrackingRequest),
    onSuccess: () => client.invalidateQueries({ queryKey: ["creators"] }),
  });
}
