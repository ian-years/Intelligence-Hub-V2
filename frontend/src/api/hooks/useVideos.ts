import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, type Schemas } from "../client";
import { keys } from "../keys";
import type { operations } from "../schema";

export type Video = Schemas["Video"];
export type PagedVideos = Schemas["PagedResult_Video_"];
export type SingleLinkParams = Schemas["SingleLinkParams"];

/** 查询表**从快照派生**，不手写。手写过一次：`hidden?: boolean`，注释还写着
 * "`true` 只看已隐藏"，而后端收的是 `visible | hidden | all`（`Literal` 查询参数）。
 * 症状是作品流一改筛选就 422 —— 而 `client.spec.ts` 与 `hooks.spec.tsx` 当时把
 * `hidden=false` 当成期望 URL 钉住了：断言的是前端自己的错，所以全绿。
 * 现在这份名单由 `schema.spec.ts` 与快照逐字核。 */
export type VideoFilter = NonNullable<
  operations["list_videos_api_videos_get"]["parameters"]["query"]
>;

/** 界面上"可见性"这个选择器能给的全部取值。 */
export const VIDEO_HIDDEN_MODES = ["visible", "hidden", "all"] as const;
export type VideoHiddenMode = (typeof VIDEO_HIDDEN_MODES)[number];

// 用 `type` 而不是 `interface`：只有 type alias 才有隐式索引签名（`Query` 要的是那个）。

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
  /** ADR-0015：摘要与要点跟着稿子走，所以跟着同一个端点一起交出。 */
  content_summary: string | null;
  key_points: string | null;
  /** 这份摘要出自谁：`local-extractive`（≤600 字抽取片段）还是 `v1-imported`
   * （V1 搬来的整篇改写）。两者长得一样，能信的程度不一样，所以必须能被区分。 */
  summary_method: string | null;
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
