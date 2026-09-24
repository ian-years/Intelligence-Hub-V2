import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas } from "../client";
import { keys } from "../keys";

/** 选题与草稿的钩子（T5.5）。两类东西共用一个文件是因为它们共用一个页面，
 *  而键仍分两族（见 `keys.ts` 那条注释）—— 文件合并 ≠ 缓存合并。
 *
 *  类型全部来自 `docs/specs/openapi-snapshot.json` 生成的 `schema.d.ts`，
 *  **这里一个字段都不手写**：手抄的那份会在后端加列的那一天悄悄少一个字段，
 *  而症状是"界面上看不见它"，不是编译错误。 */
export type Topic = Schemas["Topic"];
export type TopicCreate = Schemas["TopicCreate"];
export type Draft = Schemas["DraftRecord"];
export type DraftCreate = Schemas["DraftCreate"];
export type DraftUpdate = Schemas["DraftUpdate"];

export function useTopics(search?: string) {
  return useQuery({
    queryKey: keys.topics(search),
    queryFn: () => api.get<Topic[]>("/topics", { search }),
  });
}

export function useCreateTopic() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: TopicCreate) => api.post<Topic>("/topics", body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["topics"] });
    },
  });
}

/** 删除是 204 无 body：`api.del` 在那里返回 `undefined`，所以这一格**没有**返回值可读。
 *  成功之后靠 invalidate 重取列表来确认"少了一条"，不是靠响应里的字段。 */
export function useDeleteTopic() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.del(`/topics/${String(id)}`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["topics"] });
    },
  });
}

export function useDrafts(status?: string) {
  return useQuery({
    queryKey: keys.drafts(status),
    queryFn: () => api.get<Draft[]>("/drafts", { status }),
  });
}

export function useCreateDraft() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DraftCreate) => api.post<Draft>("/drafts", body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["drafts"] });
    },
  });
}

/** 部分更新：**只把改过的那几栏放进 body**。
 *  后端（`api/v1/drafts.py:_changes`）按"键在不在"决定改不改这一列，
 *  所以这里多带一个 `content: undefined` 与干脆不带它是两回事 ——
 *  `JSON.stringify` 会把 `undefined` 丢掉，看起来一样，但**不要**养成传 null 的习惯：
 *  null 在 `source_video_id` 上今天表示"不改"，那是一列不能清的列。 */
export function useUpdateDraft(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DraftUpdate) => api.patch<Draft>(`/drafts/${String(id)}`, body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["drafts"] });
    },
  });
}

export function useDeleteDraft() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.del(`/drafts/${String(id)}`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["drafts"] });
    },
  });
}
