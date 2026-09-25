import type { JSX } from "react";

import { ApiError } from "@/api/client";
import {
  usePlatformControl,
  usePlatforms,
  useUpdatePlatformControl,
  type PlatformSummary,
} from "@/api/hooks/useConfig";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { QueryState } from "@/components/shared/QueryState";
import { controlViewOf, platformStateOf, updateViewOf } from "@/lib/platform-state";
import { cn } from "@/lib/utils";

/**
 * 四家共用的那个总闸（ADR-0025 的界面侧）。
 *
 * 这一块存在的理由不是"给一个开关"，而是让**两种关**在界面上分得开：
 * 「你自己关掉了这一家」与「它被总闸盖住」在后端是两个不同的答案
 * （`availability` 的 `own_off` / `master_off`），而在 `platforms.yaml` 里长得一模一样
 * —— 那一位看不出是谁挡的。合成一个开关显示的话，症状就是"我明明打开了它，还是这句"。
 *
 * 三条文案规矩，每条都对应一种会说谎的界面：
 * 1. 关掉时说明**停到什么范围**（新提交的采集、定时、按链接认平台），
 *    以及"已经在跑的那一次不打断" —— 这句不说，人会在关掉之后继续等那一轮的结果。
 * 2. `shadowed_by_env` 非空时逐键点名"下一次启动会被环境变量盖回去"
 *    （与 `ScheduleCard` 同一条纪律：优先级 `yaml < env`，而这个入口只能写 yaml）。
 * 3. 四行状态**只读**：这里不放每家的开关。要改某一家去它自己的表单 ——
 *    在这一格放四个能点的开关，等于把"总闸 + AND"重新解释成"批量写四次"。
 */
export function PlatformControlCard(): JSX.Element {
  const control = usePlatformControl();
  const platforms = usePlatforms();
  const save = useUpdatePlatformControl();

  return (
    <QueryState gate={control} subject="平台总闸">
      {(status) => {
        const view = controlViewOf(status);
        const saved = save.isSuccess && save.data ? updateViewOf(save.data) : null;
        return (
          <HardShadowCard className="p-6">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <h2 className="font-display text-h3">平台总闸</h2>
                <p className="mt-1 text-body-md">
                  四家共用的那一位。写进 config/app.yaml 的 platform_control 段，保存即生效。
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-label="平台总闸"
                aria-checked={view.enabled}
                className="memphis-switch"
                data-on={String(view.enabled)}
                disabled={save.isPending}
                onClick={() => save.mutate(!view.enabled)}
              >
                <span className="sr-only">{view.enabled ? "开" : "关"}</span>
              </button>
            </div>

            <p className="mt-3 text-body-md">
              {view.enabled ? (
                "现在四家都可以提交采集。每一家还能在自己的表单里单独关。"
              ) : (
                <>
                  现在四家一律不可用：采集任务从任务页消失、定时一条都不排、按链接收录也会被拒。
                  <strong>已经在跑的那一次不打断</strong> —— 总闸只管新提交。
                  已经收进库的东西照常能看、能转写、能出截图包。
                </>
              )}
            </p>

            {view.shadowed.length > 0 && (
              <p className="mt-3 memphis-border border-ink-black bg-lemon-yellow p-3 text-body-sm">
                这些键在环境变量里也设着，所以下一次启动会盖掉刚才保存的值：
                {view.shadowed.map((key) => `platform_control.${key}`).join("、")}。 优先级是 yaml
                &lt; env，而这个入口只能写 yaml。
              </p>
            )}

            <PlatformRows platforms={platforms} />

            {save.isError && (
              <p className="mt-3 memphis-border border-ink-black bg-coral-red p-3 text-body-md">
                后端拒了：{detailOf(save.error)}
              </p>
            )}
            {saved && (
              <p className="mt-3 memphis-border border-ink-black bg-mint-green p-3 text-body-md">
                已写盘并生效。
                {saved.changed.length > 0
                  ? `变更字段：${saved.changed.join("、")}。`
                  : " 没有字段实际变化。"}
                {saved.shadowed.length > 0
                  ? " 注意：其中有的键环境变量里也设着，重启后会盖回去。"
                  : ""}
              </p>
            )}
          </HardShadowCard>
        );
      }}
    </QueryState>
  );
}

/** 四行只读状态。清单读不到时**说读不到**，而不是留一个空列表 ——
 * 空列表与"四家都不在名单里"在界面上是同一个样子，而后者是假的。 */
function PlatformRows({ platforms }: { platforms: ReturnType<typeof usePlatforms> }): JSX.Element {
  if (platforms.isError) {
    return (
      <p className="mt-4 memphis-border border-ink-black bg-coral-red p-3 text-body-md">
        读不到平台清单，所以这四行状态给不出：{detailOf(platforms.error)}
      </p>
    );
  }
  const rows: PlatformSummary[] = platforms.data?.platforms ?? [];
  return (
    <ul className="mt-4 flex list-none flex-col gap-2 p-0" aria-label="各家当前状态">
      {rows.length === 0 && <li className="text-body-sm">平台清单读取中…</li>}
      {rows.map((row) => {
        const state = platformStateOf(row);
        return (
          <li
            key={row.name}
            className="memphis-border flex flex-wrap items-center gap-3 border-ink-black p-3"
          >
            <PlatformBadge platform={row.name} label={row.display_name} />
            <code className="text-mono-sm">{row.name}</code>
            <span
              className={cn("memphis-border border-ink-black px-3 py-1 text-body-sm", state.tone)}
            >
              {state.label}
            </span>
            {/* 「它自己那一位是开着的」这句不许省：这一行是只读的，
                人不点总闸就永远不会去开那一家 —— 而关掉总闸时**两件事同时成立**。 */}
            {state.hint && <span className="text-body-sm">{state.hint}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function detailOf(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}
