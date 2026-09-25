import type { Creator } from "@/api/hooks/useCreators";
import type { TaskRunRecord } from "@/api/hooks/useTasks";
import type { Video } from "@/api/hooks/useVideos";

/** 测试夹具：**工厂**而不是一份大数组。
 *
 * 写死一份"三条作品"的库，测试里就会出现 `toHaveLength(3)` 这种断言 ——
 * 改了夹具就得改一遍断言，而那些断言本来想说的是"每条作品画一行"。
 * 所以这里只给"造一条"的能力，条数由每个用例自己决定并当场数回去。 */

export function makeVideo(over: Partial<Video> = {}): Video {
  return {
    id: 1,
    platform: "douyin",
    platform_video_id: "7123456789012345678",
    title: "示例标题",
    creator_id: 1,
    published_at: "2026-09-20T00:00:00+00:00",
    duration_seconds: 60,
    view_count: 0,
    is_hidden: false,
    // 后端这一位是算出来的（`models/video.Video` 的 validator），但 fixture 必须给：
    // 缺一个必填字段就是 tsc 红，而红的位置在几十个 spec 里，看不出是 fixture 的问题。
    has_video: true,
    media_aux_paths_json: "[]",
    metadata_json: "{}",
    created_at: "2026-09-23T00:00:00+00:00",
    updated_at: "2026-09-23T00:00:00+00:00",
    ...over,
  };
}

export function makeCreator(over: Partial<Creator> = {}): Creator {
  return {
    id: 1,
    platform: "douyin",
    platform_id: "MS4wLjABAAAA",
    name: "示例博主",
    is_tracking: true,
    profile_url: "https://example.com/space/1",
    metadata_json: "{}",
    created_at: "2026-09-23T00:00:00+00:00",
    updated_at: "2026-09-23T00:00:00+00:00",
    ...over,
  };
}

export function makeRun(over: Partial<TaskRunRecord> = {}): TaskRunRecord {
  return {
    id: "run-1",
    task_name: "douyin_collect",
    kind: "collect",
    status: "success",
    progress: 1,
    params_json: "{}",
    config_snapshot_json: "{}",
    started_at: "2026-09-23T00:00:00+00:00",
    ended_at: "2026-09-23T00:05:00+00:00",
    ...over,
  };
}

/** `/api/videos` 的那一页。`total` 与 `items.length` 独立给：分页要能测
 *  "共 137 条，但这一页只有 3 条"这种组合。 */
export function makePage(
  items: readonly Video[],
  over: { total?: number; page?: number; size?: number } = {},
): { items: Video[]; page: number; size: number; total: number } {
  return {
    items: [...items],
    page: over.page ?? 1,
    size: over.size ?? items.length,
    total: over.total ?? items.length,
  };
}
