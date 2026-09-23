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

/** `summary` 在 OpenAPI 里是自由对象（路由的返回注解是 `dict[str, Any]`），
 * 所以类型只能在前面按 `tasks/preflight.py` 实际写的那 9 个键声明。
 * 保留索引签名是因为它会**按情况少写键**（没有启用平台时那句是空串拼接的结果），
 * 页面因此不许假设某个键一定在。 */
export interface PreflightSummary {
  platforms_ok: number;
  platforms_degraded: number;
  platforms_unreachable: number;
  /** "ok" / "unreachable" */
  storage: string;
  /** "present" / "missing" */
  asr_model: string;
  /** 逗号 + 空格拼的 `k=v`，生产者与解析法都由 `lib/formatters.spec.ts` 钉住 */
  platform_status: string;
  tools_present: string;
  tools_missing: string;
  [key: string]: string | number | undefined;
}

export interface PreflightReport {
  status: PreflightStatus;
  summary: PreflightSummary;
  failures: FailureRecord[];
}

export function usePreflight() {
  return useQuery({
    queryKey: keys.preflight,
    queryFn: () => api.get<PreflightReport>("/preflight"),
    staleTime: 15_000,
  });
}
