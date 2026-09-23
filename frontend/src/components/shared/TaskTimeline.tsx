import type { JSX } from "react";

import type { TaskRunRecord } from "@/api/hooks/useTasks";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { statusMeta } from "@/lib/run-status";
import { formatPercent, formatTime } from "@/lib/formatters";
import { cn } from "@/lib/utils";

export interface TaskTimelineProps {
  runs: readonly TaskRunRecord[];
  /** 一行一个动作按钮的位置（任务页给"取消"，总览页不给）。 */
  onCancel?: (run: TaskRunRecord) => void;
  empty?: string;
  className?: string;
}

/**
 * 任务运行记录的时间线：总览页的"最近任务"与任务页的"运行历史"共用一份。
 *
 * 两处要的是同一件事（这次跑的什么、成没成、多久、错在哪），各写一遍就会有两套
 * 状态色 —— 而状态色是这个界面唯一的结论来源。
 */
export function TaskTimeline({
  runs,
  onCancel,
  empty = "（还没有跑过任务）",
  className,
}: TaskTimelineProps): JSX.Element {
  if (runs.length === 0) {
    return <p className={cn("text-body-md", className)}>{empty}</p>;
  }

  return (
    <ol className={cn("flex list-none flex-col gap-3 p-0", className)} data-slot="task-timeline">
      {runs.map((run) => {
        const meta = statusMeta(run.status);
        return (
          <li
            key={run.id}
            className="flex flex-wrap items-center gap-3 memphis-border border-ink-black bg-paper-cream p-3"
            data-run-id={run.id}
            data-status={run.status}
          >
            <span
              className={cn("memphis-border border-ink-black px-3 py-1 text-body-sm", meta.tone)}
            >
              {meta.label}
            </span>
            <code className="text-mono-md">{run.task_name}</code>
            <span className="text-body-sm">{run.kind}</span>
            {run.status === "running" && (
              <span className="text-body-sm" data-slot="progress">
                {formatPercent(run.progress)}
              </span>
            )}
            <span className="ml-auto text-mono-sm">
              {formatTime(run.started_at)}
              {run.ended_at ? ` → ${formatTime(run.ended_at)}` : ""}
            </span>
            {run.error_text && (
              <p className="w-full break-words text-mono-sm" data-slot="error">
                {run.error_text}
              </p>
            )}
            {onCancel !== undefined && run.status === "running" && (
              <MemphisButton
                variant="danger"
                onClick={() => onCancel(run)}
                aria-label={`取消 ${run.task_name}`}
              >
                取消
              </MemphisButton>
            )}
          </li>
        );
      })}
    </ol>
  );
}
