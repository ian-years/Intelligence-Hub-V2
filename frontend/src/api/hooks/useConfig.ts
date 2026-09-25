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

/** 表单的初值来源。注意返回的是**整份配置**：`PUT` 也要交整份，
 * 所以隐藏字段（`ui:hidden`，不渲染）必须由这一份原样带回去。 */
export interface PlatformConfigResponse {
  platform: string;
  config: Record<string, unknown>;
  health: { status: string; checked_at: string | null } | null;
}

export function usePlatformConfig(platform: string) {
  return useQuery({
    queryKey: keys.platformConfig(platform),
    queryFn: () => api.get<PlatformConfigResponse>(`/platforms/${platform}/config`),
    enabled: platform !== "",
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
      // 三处都要失效：镜像表(`platforms`)、这份配置本身（后端可能改了别的键）、
      // 以及任务表（关掉的平台要让对应任务从 `/api/tasks` 消失）。
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.platforms }),
        client.invalidateQueries({ queryKey: keys.platformConfig(platform) }),
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

export type PlatformControlStatus = Schemas["PlatformControlStatus"];
export type PlatformControlUpdateResponse = Schemas["PlatformControlUpdateResponse"];

/** 四家共用的总闸（ADR-0025）。 */
export function usePlatformControl() {
  return useQuery({
    queryKey: keys.platformControl,
    queryFn: () => api.get<PlatformControlStatus>("/platform-control"),
  });
}

/**
 * 翻总闸之后要作废的清单比平台配置那次 PUT **更长**：它一次改变了四个页面的按钮、
 * 定时名单与每一家的三态。少失效一处 = 那一处继续显示旧状态，
 * 而"旧状态"在这格里恰恰是"看着还能采"。
 *
 * 排期（`keys.schedule`）也在里面：后端 PUT 之后会重排采集 job，
 * 前端不重取的话，设置页会留着"明天 08:00 采四家"而实际一条都没排。
 */
export function useUpdatePlatformControl() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (enabled: boolean) =>
      api.put<PlatformControlUpdateResponse>("/platform-control", { enabled }),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: keys.platformControl }),
        client.invalidateQueries({ queryKey: keys.platforms }),
        client.invalidateQueries({ queryKey: keys.tasks }),
        client.invalidateQueries({ queryKey: keys.schedule }),
      ]);
    },
  });
}
