import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, type Schemas } from "../client";
import { keys } from "../keys";

export type Video = Schemas["Video"];
export type PagedVideos = Schemas["PagedResult_Video_"];
export type SingleLinkParams = Schemas["SingleLinkParams"];

// 用 `type` 而不是 `interface`：只有 type alias 才有隐式索引签名（`Query` 要的是那个）。
export type VideoFilter = {
  platform?: string;
  creator_id?: number;
  since?: string;
  search?: string;
  /** 后端语义：`hidden=true` 只看已隐藏，`false`/省略看未隐藏。 */
  hidden?: boolean;
  page?: number;
  size?: number;
};

/** `filter` 整个进 queryKey：漏一个键就是"改了筛选但界面不重取"，
 * 那种 bug 看起来像缓存坏了。 */
export function useVideos(filter: VideoFilter = {}) {
  return useQuery({
    queryKey: keys.videos(filter),
    queryFn: () => api.get<PagedVideos>("/videos", { ...filter }),
    placeholderData: (previous: PagedVideos | undefined) => previous,
  });
}

export function useVideo(id: number | undefined) {
  return useQuery({
    queryKey: keys.video(id ?? -1),
    queryFn: () => api.get<Video>(`/videos/${String(id)}`),
    enabled: id !== undefined,
  });
}

/** 源是 `api/v1/transcripts.py` 的返回字典。OpenAPI 里这个端点是自由对象
 * （路由的返回注解是 `dict[str, Any]`），所以类型只能在前面手写 ——
 * 那份手写由 `src/api/schema.spec.ts` 与快照同源核着（端点存在性那一条）。 */
export interface TranscriptResponse {
  video_id: number;
  engine: string;
  language: string | null;
  char_count: number;
  sentence_count: number;
  /** 正文在磁盘上，由后端拼好交出（V1 §7.5：别让前端自己猜口播稿在哪）。 */
  text: string;
  segments_json: string | null;
}

/** 转写稿：后端对"还没有稿子"回 404，不当错误处理 —— 404 是这里的正常态。 */
export function useTranscript(id: number | undefined) {
  return useQuery({
    queryKey: keys.transcript(id ?? -1),
    queryFn: async () => {
      try {
        return await api.get<TranscriptResponse>(`/videos/${String(id)}/transcript`);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    enabled: id !== undefined,
    retry: 0,
  });
}

export function useHideVideo() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: { id: number; reason?: string }) =>
      api.patch<Video>(`/videos/${id}/hide`, {
        reason: reason ?? "",
      } satisfies Schemas["HideRequest"]),
    onSuccess: () => client.invalidateQueries({ queryKey: ["videos"] }),
  });
}

export function useUnhideVideo() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.patch<Video>(`/videos/${id}/unhide`),
    onSuccess: () => client.invalidateQueries({ queryKey: ["videos"] }),
  });
}

export function useSubmitSingleLink() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SingleLinkParams) =>
      api.post<Schemas["TaskAccepted"]>("/videos/single-link", body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.runs() }),
  });
}
