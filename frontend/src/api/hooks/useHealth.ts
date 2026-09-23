import { useQuery } from "@tanstack/react-query";

import { api, type Schemas } from "../client";
import { keys } from "../keys";

export type Health = Schemas["HealthResponse"];

/** `/api/health` 只回答"进程在不在"；依赖好不好问 `usePreflight`（health.py 的注释里写明了这条分工）。 */
export function useHealth() {
  return useQuery({
    queryKey: keys.health,
    queryFn: () => api.get<Health>("/health"),
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
}

export type PreflightStatus = "success" | "partial" | "failed";

export interface FailureRecord {
  platform: string | null;
  stage: string;
  error: string;
  error_kind: string | null;
}

/** 源是 `tasks/preflight.py` 的 `run_preflight`（路由只是把它的三样东西原样交出来）。
 * `summary` 在 OpenAPI 里是自由对象，所以这里按 handler 实际写的那 9 个键声明，
 * 页面**不许**假设某个键一定在（`preflight.py` 会按平台数量少写键）。 */
export interface PreflightReport {
  status: PreflightStatus;
  summary: Record<string, string | number>;
  failures: FailureRecord[];
}

export function usePreflight() {
  return useQuery({
    queryKey: keys.preflight,
    queryFn: () => api.get<PreflightReport>("/preflight"),
    staleTime: 15_000,
  });
}
