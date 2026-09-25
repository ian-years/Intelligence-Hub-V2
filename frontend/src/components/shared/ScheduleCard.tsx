import { useMemo, useState } from "react";
import type { JSX } from "react";

import { ApiError } from "@/api/client";
import { nextRunLabel, useRunNow, useSchedule, useUpdateSchedule } from "@/api/hooks/useSchedule";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { QueryState } from "@/components/shared/QueryState";
import {
  draftOf,
  isDirty,
  planUpdate,
  viewOf,
  type ScheduleDraft,
  type ScheduleView,
} from "@/lib/schedule";

/**
 * 定时采集那块开关（ADR-0017 的 UI 面）。
 *
 * 这一块的意义不是"给 cron 一个输入框"，而是把**三件平时会各说各话的事**并排放：
 * 配置里写的 cron、调度器上真排着的 job、以及它算出来的下次触发时刻。
 * 只给第一个的话，"改了没生效"与"生效了但没有平台可采"这两种红长得一模一样。
 *
 * 三条文案规矩，每条都对应一种会说谎的界面：
 * 1. `scheduler_running: false` 时明说"改动写进了配置但没排成 job，下一次启动才生效"，
 *    不许因为 PUT 回了 200 就只显示"已保存"。
 * 2. `shadowed_by_env` 非空时逐键点名"下一次启动会被环境变量盖回去"。优先级是
 *    `yaml < env`，而这个入口只能写 YAML —— 不说这一句，症状就是"我明明设成 08:00，
 *    重启又变回 06:30"，而没有人会去查一个看不见的环境变量。
 * 3. 名单里的平台全被关掉时说"这次一个都不排"。静默少排的症状是那位博主永远不更新，
 *    而配置看着完全正常。
 */
export function ScheduleCard(): JSX.Element {
  const schedule = useSchedule();
  const save = useUpdateSchedule();
  const run = useRunNow();
  const [draft, setDraft] = useState<ScheduleDraft | null>(null);

  return (
    <HardShadowCard className="p-6">
      <h2 className="font-display text-h3">定时采集</h2>
      <p className="mt-1 text-body-md">
        cron 串写进 config/app.yaml 的 scheduler 段，保存即生效（服务不用重启）。 默认不开 ——
        一台没被同意过的机器不该定时拿着登录态去动平台配额。
      </p>

      <QueryState gate={schedule} subject="定时采集">
        {(raw) => {
          const status = viewOf(raw);
          return (
            <Editor
              status={status}
              draft={draft ?? draftOf(status)}
              onDraft={setDraft}
              save={save}
              run={run}
            />
          );
        }}
      </QueryState>
    </HardShadowCard>
  );
}

interface EditorProps {
  status: ScheduleView;
  draft: ScheduleDraft;
  onDraft: (next: ScheduleDraft) => void;
  save: ReturnType<typeof useUpdateSchedule>;
  run: ReturnType<typeof useRunNow>;
}

function Editor({ status, draft, onDraft, save, run }: EditorProps): JSX.Element {
  const dirty = isDirty(status, draft);
  const jobByPlatform = useMemo(
    () => new Map(status.jobs.map((job) => [job.platform, job])),
    [status.jobs],
  );
  const listed = [...new Set([...status.collect_platforms, ...status.effective_platforms])];

  return (
    <div className="mt-4 flex flex-col gap-4">
      <label className="flex flex-col gap-1">
        <span className="font-heading text-body-sm">cron（分 时 日 月 周）</span>
        <input
          className="memphis-border w-full bg-paper px-3 py-2 font-mono"
          value={draft.cron}
          placeholder="留空 = 不开定时采集"
          onChange={(event) => onDraft({ ...draft, cron: event.target.value })}
        />
        <span className="text-body-sm">
          时区 {status.timezone}；盘上现在这条是 {status.collect_cron || "（没开）"}。
          名单一个都不勾时按所有启用的平台跑。
        </span>
      </label>

      <label className="flex w-40 flex-col gap-1">
        <span className="font-heading text-body-sm">每位博主每次最多收几条</span>
        <input
          className="memphis-border bg-paper px-3 py-2 font-mono"
          inputMode="numeric"
          value={draft.limit}
          placeholder="不限"
          onChange={(event) => onDraft({ ...draft, limit: event.target.value })}
        />
      </label>

      <fieldset className="flex flex-col gap-2">
        <legend className="font-heading text-body-sm">
          跑哪些平台（一个都不勾 = 所有启用的平台）
        </legend>
        {status.effective_platforms.length === 0 && status.collect_platforms.length > 0 && (
          <p className="text-body-sm">
            名单里的平台都被关掉了，所以这次一个都不排：{status.skipped_platforms.join("、")}
          </p>
        )}
        {/* 另一种"一条都不排"：名单是空的（= 所有启用的平台）而**总闸**关着（ADR-0025）。
            这一句必须单独有，因为上面那一支在这里不成立 —— `collect_platforms` 是空的、
            `skipped_platforms` 也是空的，于是界面会显示"cron 08:00，下次明天早上"
            而一家都不会跑。规矩 2 说的就是这一格：静默少排平台。 */}
        {!status.master_enabled && (
          <p className="memphis-border mt-1 border-ink-black bg-lemon-yellow p-2 text-body-sm">
            平台总闸现在是关着的，所以四家一家都不排 —— 与这一栏的 cron 无关。
            去上面的「平台总闸」那一格打开。
          </p>
        )}
        <ul className="flex flex-wrap gap-4">
          {listed.map((platform) => (
            <li key={platform} className="flex items-center gap-2 text-body-md">
              <input
                id={`schedule-platform-${platform}`}
                type="checkbox"
                checked={draft.platforms.includes(platform)}
                onChange={(event) =>
                  onDraft({
                    ...draft,
                    platforms: event.target.checked
                      ? [...draft.platforms, platform]
                      : draft.platforms.filter((name) => name !== platform),
                  })
                }
              />
              <label htmlFor={`schedule-platform-${platform}`}>{platform}</label>
              <span className="text-body-sm">
                {jobByPlatform.has(platform)
                  ? `下次 ${nextRunLabel(jobByPlatform.get(platform))}`
                  : "没排上"}
              </span>
              <MemphisButton
                variant="secondary"
                disabled={run.isPending}
                onClick={() => run.mutate(platform)}
              >
                立即跑一次
              </MemphisButton>
            </li>
          ))}
        </ul>
        {run.isError && <p className="text-body-sm">没能排队：{detailOf(run.error)}</p>}
        {run.isSuccess && (
          <p className="text-body-sm">
            已排队 {run.data.task_name} —— 202 只说提交了，没说采到了；结果看任务页那条 run。
          </p>
        )}
      </fieldset>

      <div className="flex flex-wrap items-center gap-3">
        <MemphisButton
          disabled={!dirty || save.isPending}
          onClick={() => {
            const snapshot = draft;
            save.mutate(planUpdate(status, snapshot), {
              // 保存成功后草稿归零到**服务端算回来的那份**，不是本地初值：
              // 后端可以把 `collect_cron: null` 规范化成别的形状，本地猜一份就会立刻
              // 显示"有 N 处未保存的改动"这种假脏。
              onSuccess: () => onDraft(draftOf(status)),
            });
          }}
        >
          {save.isPending ? "保存中…" : "保存定时"}
        </MemphisButton>
        {!dirty && <span className="text-body-sm">排期没有改动</span>}
        {save.isError && <span className="text-body-sm">保存失败：{detailOf(save.error)}</span>}
        {save.isSuccess && (
          <span className="text-body-sm">
            已保存
            {save.data.schedule?.scheduler_running
              ? `并重排了 ${String(viewOf(save.data.schedule).jobs.length)} 条 job`
              : "，但调度器没在跑，要到下一次启动才排上"}
            {(save.data.changed_fields ?? []).length > 0
              ? `（改了 ${(save.data.changed_fields ?? []).join("、")}）`
              : ""}
          </span>
        )}
      </div>

      {!status.scheduler_running && (
        <p className="memphis-border bg-lemon-yellow px-3 py-2 text-body-sm">
          调度器没在跑（scheduler.enabled 关着，或服务还在启动）。 改动能写进配置，但不会排成 job ——
          下一次启动才生效。
        </p>
      )}
      {status.shadowed_by_env.length > 0 && (
        <p className="memphis-border bg-lemon-yellow px-3 py-2 text-body-sm">
          这些键在环境变量里也设着，下一次启动会盖掉这里写的：{status.shadowed_by_env.join("、")}
        </p>
      )}
    </div>
  );
}

function detailOf(error: Error | null): string {
  if (error instanceof ApiError) return error.detail;
  return error?.message ?? "未知错误";
}
