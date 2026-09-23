import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas } from "../client";
import type { ObjectSchema } from "../json-schema";
import { keys } from "../keys";

export type PlatformSummary = Schemas["PlatformSummary"];
export type PlatformsResponse = Schemas["PlatformsResponse"];
export type ConfigUpdateResponse = Schemas["ConfigUpdateResponse"];

export function usePlatforms() {
  return useQuery({
    queryKey: keys.platforms,
    queryFn: () => api.get<PlatformsResponse>("/platforms"),
  });
}

export function usePlatformSchema(platform: string) {
  return useQuery({
    queryKey: keys.platformSchema(platform),
    queryFn: () => api.get<ObjectSchema>(`/platforms/${platform}/schema`),
    enabled: platform !== "",
  });
}

/** PUT 之后必须 `invalidate` 平台列表：镜像表(`platforms.enabled`)由后端同步，
 * 但前端这一份要重取 —— 关掉的平台会让一批任务从 `/api/tasks` 消失。 */
export function useUpdatePlatformConfig(platform: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (config: Record<string, unknown>) =>
      api.put<ConfigUpdateResponse>(`/platforms/${platform}/config`, config),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.platforms }),
        client.invalidateQueries({ queryKey: keys.tasks }),
      ]);
    },
  });
}

/** `requires_restart` 为真时页面要**说出来**：改了不生效又没人告诉用户，
 * 与"界面渲染出一个后端不实现的开关"是同一类谎。 */
export function restartNeeded(response: ConfigUpdateResponse | undefined): boolean {
  return response?.requires_restart ?? false;
}
