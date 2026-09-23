/** Query key 的唯一定义处。
 *
 * 单独一个文件是因为 invalidation 与 query 分处两个组件时，字符串字面量会各写一遍 ——
 * 拼错的那一半表现为"改了但界面没变"，最难查。 */
export const keys = {
  health: ["health"] as const,
  preflight: ["preflight"] as const,
  platforms: ["platforms"] as const,
  platformSchema: (platform: string) => ["platforms", platform, "schema"] as const,
  creators: (platform?: string) => ["creators", platform ?? null] as const,
  creator: (id: number) => ["creators", "detail", id] as const,
  videos: (filter: Record<string, unknown> = {}) => ["videos", filter] as const,
  video: (id: number) => ["videos", "detail", id] as const,
  transcript: (id: number) => ["videos", id, "transcript"] as const,
  tasks: ["tasks"] as const,
  taskSchema: (name: string) => ["tasks", name, "schema"] as const,
  runs: (status?: string) => ["runs", status ?? null] as const,
  run: (id: string) => ["runs", id] as const,
  manifests: ["manifests"] as const,
};
