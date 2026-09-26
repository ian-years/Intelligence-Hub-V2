import type { FormEvent, JSX } from "react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { useAddCreator, useCreators, useSetTracking, type Creator } from "@/api/hooks/useCreators";
import { usePlatforms } from "@/api/hooks/useConfig";
import { useRunTask, useTasks } from "@/api/hooks/useTasks";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { ExportLinks } from "@/components/shared/ExportLinks";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { CreatorCard } from "@/components/shared/CreatorCard";
import { CreatorBenchmarks } from "@/components/shared/CreatorBenchmarks";
import { PageShell } from "@/components/shared/PageShell";
import { QueryState } from "@/components/shared/QueryState";
import { useSettings } from "@/stores/settings";
import { cn } from "@/lib/utils";
import { Link } from "react-router-dom";

/**
 * 博主库（`/creators`）：列表 + 跟踪开关 + 收录表单。
 *
 * 两处刻意分开：**筛选用的平台**与**收录时指定的平台**是两个状态。
 * 合成一个的症状很实在：按 B站 筛过一遍列表，下一位抖音博主就被打上 `bilibili` 提交了。
 *
 * 收录**不是本地写入**：`POST /api/creators` 起的是 `add_creator` 任务（要跟 302、要拉资料），
 * 回的是 202 + `task_id`。所以成功之后那句话是"已排队"而不是"已添加" ——
 * 博主要等任务跑成才会出现在列表里。把 202 写成"已添加"就是臆造成功（`AGENTS.md §1.3`）。
 *
 * `tracking` 的默认值只有一处真源：`stores/settings` 的 `captureTrackingDefault`
 * （V1 §7.24）。请求体自己那个 `default=true` 不算第二处，因为这里总是显式传。
 */
export function Creators(): JSX.Element {
  const [filterPlatform, setFilterPlatform] = useState("");
  const [addPlatform, setAddPlatform] = useState("");
  const [url, setUrl] = useState("");

  const trackingDefault = useSettings((state) => state.captureTrackingDefault);
  const creators = useCreators(filterPlatform === "" ? undefined : filterPlatform);
  const platforms = usePlatforms();
  const add = useAddCreator();
  /** 「回溯抓取」画不画，问的是后端那份可用清单，不是前端自己记的任务名（经验 42）。
   *  没列出来（未实现 / 该平台被闸挡住而任务本身消失）就没有这个按钮 ——
   *  "按钮在、点下去被拒"这一族在 ADR-0025 里已经记过一次。
   *  注意 `/api/tasks` 顶层**就是数组**，不是 `{tasks: […]}`（经验 42 那个错踩过一次）。 */
  const tasks = useTasks();
  const canBackfill = (tasks.data ?? []).some((task) => task.name === "backfill");

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    add.mutate({
      url,
      tracking: trackingDefault,
      platform: addPlatform === "" ? null : addPlatform,
    });
  }

  return (
    <PageShell pattern="confetti" className="mx-auto flex max-w-[1100px] flex-col gap-5">
      <header>
        <h1 className="font-display text-display-md">博主</h1>
        <p className="text-body-md">
          收录一位博主＝起一个 <code>add_creator</code> 任务（要跟链接、要拉资料）。
          跟踪开关是本地写入，立刻生效。
        </p>
        {/* 这一页的平台筛选是**立刻生效**的（不像作品流要点"查询"），
            所以导出直接跟着 `filterPlatform` 走就是诚实的。 */}
        <ExportLinks
          entity="creators"
          query={{ platform: filterPlatform === "" ? null : filterPlatform }}
        />
      </header>

      <section aria-label="收录博主">
        <h2 className="font-heading text-h3 font-bold">收录</h2>
        <form
          className="mt-3 flex flex-wrap items-end gap-4 memphis-border border-ink-black bg-paper-cream p-4"
          onSubmit={submit}
        >
          <label className="min-w-[260px] flex-1">
            <span className="text-body-sm">博主主页链接（或分享短链）</span>
            <input
              className="memphis-input w-full"
              type="url"
              required
              placeholder="https://www.douyin.com/user/…"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
            />
          </label>
          <label>
            <span className="text-body-sm">平台（留空＝按链接判断）</span>
            <select
              className="memphis-input"
              value={addPlatform}
              onChange={(event) => setAddPlatform(event.target.value)}
            >
              <option value="">自动判断</option>
              {(platforms.data?.platforms ?? []).map((item) => (
                <option key={item.name} value={item.name}>
                  {item.display_name}
                </option>
              ))}
            </select>
          </label>
          <MemphisButton type="submit" disabled={add.isPending}>
            {add.isPending ? "排队中…" : "收录"}
          </MemphisButton>
        </form>

        {add.isError && (
          <HardShadowCard className="mt-3 bg-coral-red">
            <h2>收录没排队上</h2>
            <p className="break-words text-body-md">
              {add.error instanceof ApiError ? add.error.detail : add.error.message}
            </p>
          </HardShadowCard>
        )}
        {add.isSuccess && (
          <HardShadowCard className="mt-3 bg-lemon-yellow">
            <p>
              已排队：任务 <code>{add.data.task_id}</code>。博主要等这个任务跑成才会出现在下面
              那份列表里 —— 进度去 <Link to="/tasks">任务页</Link> 看。
            </p>
          </HardShadowCard>
        )}
      </section>

      <section aria-label="博主列表">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="font-heading text-h3 font-bold">
            列表{filterPlatform === "" ? "（全部平台）" : `（只看 ${filterPlatform}）`}
          </h2>
          <span className="text-body-sm">
            新收录的默认跟踪：
            <span className={cn("ml-2 font-bold", !trackingDefault && "opacity-60")}>
              {trackingDefault ? "跟踪" : "只存档"}
            </span>
          </span>
        </div>

        <div className="mt-3 flex flex-wrap gap-2">
          <FilterChip
            label="全部平台"
            active={filterPlatform === ""}
            onClick={() => setFilterPlatform("")}
          />
          {(platforms.data?.platforms ?? []).map((item) => (
            <FilterChip
              key={item.name}
              label={item.display_name}
              active={filterPlatform === item.name}
              onClick={() => setFilterPlatform(item.name)}
            />
          ))}
        </div>

        <QueryState gate={creators} subject="博主列表">
          {(list) => (
            <div className="mt-3 flex flex-col gap-3">
              {list.length === 0 && (
                <HardShadowCard>
                  <p>
                    {filterPlatform === ""
                      ? "库里还没有博主：用上面那个表单收录一位。"
                      : `${filterPlatform} 这一档还没有博主。换一个平台，或先去设置页看它开没开。`}
                  </p>
                </HardShadowCard>
              )}
              {list.map((creator) => (
                <ToggleRow key={creator.id} creator={creator} canBackfill={canBackfill} />
              ))}
            </div>
          )}
        </QueryState>
      </section>
    </PageShell>
  );
}

/** 一位博主一行，开关那笔写入归这一行自己：共享一个 mutation 会让所有行的
 *  「写入中…」一起亮，而 `variables` 只有一个，分不清是哪家在飞。
 *
 *  爆款面板的展开态同样**按行**存：全部一起展开等于对每个博主各发一次 `/api/videos`。 */
function ToggleRow({
  creator,
  canBackfill,
}: {
  creator: Creator;
  canBackfill: boolean;
}): JSX.Element {
  const toggle = useSetTracking(creator.id);
  const backfill = useRunTask("backfill");
  const [openBenchmarks, setOpenBenchmarks] = useState(false);
  // 「回溯抓取」发的是这位**自己**的 `profile_url`：库里那一行就是身份的来源，
  // 拼一个别的字符串（或让用户再粘一遍）等于给同一位博主造第二个身份（V1 §7.1）。
  const canRun = canBackfill && creator.profile_url !== "";
  return (
    <CreatorCard
      creator={creator}
      busy={toggle.isPending}
      onToggleTracking={(next) => toggle.mutate(next)}
      onToggleBenchmarks={() => setOpenBenchmarks((open) => !open)}
      benchmarksOpen={openBenchmarks}
      // `exactOptionalPropertyTypes` 开着（与 `Settings.tsx` 那两行同一个写法）：
      // "没有这个能力"要把 prop **整个省掉**，不是塞一个 `undefined` 进去 ——
      // 后者在类型上就过不了，而它要挡的正是"看起来传了、其实没有"。
      {...(canRun
        ? { onBackfill: () => backfill.mutate({ creator_url: creator.profile_url }) }
        : {})}
      backfillBusy={backfill.isPending}
      backfillNote={
        backfill.isError ? (
          // 状态靠**话说清楚**传达，不靠颜色（红字对色觉障碍等于没有）。
          <>
            没排队上：
            {backfill.error instanceof ApiError ? backfill.error.detail : backfill.error.message}
          </>
        ) : backfill.isSuccess ? (
          <>
            已排队：<code>{backfill.data.task_id}</code>
          </>
        ) : null
      }
    >
      {openBenchmarks && <CreatorBenchmarks creator={creator} />}
    </CreatorCard>
  );
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "memphis-btn memphis-border border-ink-black !px-3 !py-1 text-body-sm",
        active ? "bg-electric-blue text-paper-cream" : "bg-paper-cream",
      )}
    >
      {label}
    </button>
  );
}
