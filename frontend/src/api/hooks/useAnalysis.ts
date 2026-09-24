import { useMutation, useQuery } from "@tanstack/react-query";

import { keys } from "../keys";

import { api, type Schemas } from "../client";

/** 分析层那两个端点的钩子（T5.2 / T5.3 的引擎，T4.7 的工坊页是第一个消费方）。
 *
 * 类型全部来自生成的 `schema.d.ts`，一个字段不手抄（同一理由见 `useTopics.ts` 开头）。 */
export type DraftScript = Schemas["DraftScript"];
export type DraftScriptRequest = Schemas["DraftScriptRequest"];
export type BenchmarkAnalysis = Schemas["BenchmarkAnalysis"];

/** 生成初稿是 mutation，不是 query。
 *
 * 它读的是四张写死的表，看起来"像个只读派生"，但语义是"我要一份新的"：
 * 挂进缓存的话，第二次点同一篇拿到的是**缓存里那份**，
 * 而改了选题标题之后缓存键没变（键里只有 body）—— 那不是缓存，那是界面在骗人。 */
export function useGenerateDraftScript() {
  return useMutation({
    mutationFn: (body: DraftScriptRequest) => api.post<DraftScript>("/generate-draft-script", body),
  });
}

/** 读一条对标作品的拆解。`videoId` 给 undefined 时**一个请求都不发**
 * （工坊页刚进来还没有对标作品，那不该是三次 404）。 */
export function useBenchmarkAnalysis(videoId: number | undefined) {
  return useQuery({
    queryKey: keys.benchmark(videoId ?? -1),
    queryFn: () => api.get<BenchmarkAnalysis>("/benchmark-analysis", { video_id: videoId }),
    enabled: videoId !== undefined,
  });
}
