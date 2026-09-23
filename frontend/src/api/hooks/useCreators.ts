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

/** 加博主是**提交任务**：`POST /api/creators` 立即回一个 Creator，采集在后台跑，
 * 所以成功后要同时让 creators / videos / runs 失效。
 *
 * `tracking` 请显式传（值取自 `stores/settings.captureTrackingDefault()`）：
 * 这个请求字段自己也有 `default=true`，两边各留一个默认值就是 V1 §7.24 那一坑。 */
export function useAddCreator() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AddCreatorRequest) => api.post<Creator>("/creators", body),
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
