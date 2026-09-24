import type { JSX, ReactNode } from "react";
import { Link } from "react-router-dom";

import { usePlatforms, type PlatformSummary } from "@/api/hooks/useConfig";
import { useCreators } from "@/api/hooks/useCreators";
import { useHealth, type Health } from "@/api/hooks/useHealth";
import { useRuns } from "@/api/hooks/useTasks";
import { useVideos } from "@/api/hooks/useVideos";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { PageShell } from "@/components/shared/PageShell";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { QueryState, type QueryGate } from "@/components/shared/QueryState";
import { TaskTimeline } from "@/components/shared/TaskTimeline";
import { VideoRow } from "@/components/shared/VideoRow";
import { formatTime } from "@/lib/formatters";
import { cn } from "@/lib/utils";

/** 总览页一次问后端"最新几条"。不是分页大小，是这一屏的密度。 */
const LATEST_VIDEOS = 5;
const RECENT_RUNS = 8;

/**
 * 总览（`/`）：这套东西现在能不能跑、最近在跑什么、最新进了什么。
 *
 * 刻意**不在这一页探测平台健康**：`/api/preflight` 会真去连桥、真发平台请求，
 * 一个"每次进首页都要跑一遍"的网络探测不是总览，是给自己加风控。
 * 平台卡片显示的是配置里的事实（开没开、V2 有没有这个平台的实现），
 * 外加 preflight **上一次**写回镜像的结论与时刻（ADR-0022）—— 那一行写的是"上次探测
 * 于几点"，不是"现在没问题"。没有结论就整行不画，不画灰色"未知"。
 *
 * 四个区块各自过 `QueryState`：一个区块读不到不许把别的区块一起拖成空白。
 */
export function Dashboard(): JSX.Element {
  const health = useHealth();
  const platforms = usePlatforms();
  const runs = useRuns();
  const videos = useVideos({ page: 1, size: LATEST_VIDEOS });
  const creators = useCreators();

  // 作品行只有 creator_id，名字在 `/api/creators` 那一份里。读不到就是读不到 ——
  // `VideoRow` 会退化成 `博主 #<id>`，不会把这一格空成"好像没有作者"。
  const names = new Map(
    (creators.data ?? []).map((creator) => [creator.id, creator.name] as const),
  );

  return (
    <PageShell pattern="dots" className="mx-auto flex max-w-[1100px] flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-display-md">总览</h1>
          <p className="text-body-md">
            平台开关、最近的任务、最新进来的作品。这一页只读，不探测、不改配置。
          </p>
        </div>
        <HealthLine health={health} />
      </header>

      <section aria-label="平台">
        <SectionTitle to="/settings" toLabel="改开关去设置">
          平台
        </SectionTitle>
        <QueryState gate={platforms} subject="平台清单">
          {(list) => (
            <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {list.platforms.map((platform) => (
                <PlatformCard key={platform.name} platform={platform} />
              ))}
            </div>
          )}
        </QueryState>
        <p className="mt-2 text-body-sm">
          这里说的是配置：开没开、V2 实没实现。"现在连不连得上"要去
          <Link to="/preflight" className="text-electric-blue">
            预检页
          </Link>
          探测 —— 那一页会真去连桥与平台，所以这一页不替它跑。
        </p>
      </section>

      <section aria-label="最近任务">
        <SectionTitle to="/tasks" toLabel="全部历史去任务页">
          最近任务
        </SectionTitle>
        <QueryState gate={runs} subject="最近任务">
          {(list) => (
            <TaskTimeline
              className="mt-3"
              runs={list.slice(0, RECENT_RUNS)}
              empty="（还没有跑过任务：先在设置页打开平台，再去任务页提交一次采集）"
            />
          )}
        </QueryState>
      </section>

      <section aria-label="最新作品">
        <SectionTitle to="/feed" toLabel="全部作品去作品流">
          最新作品（按发布时间倒序 {String(LATEST_VIDEOS)} 条）
        </SectionTitle>
        <QueryState gate={videos} subject="最新作品">
          {(page) => (
            <div className="mt-3 flex flex-col gap-3">
              {page.items.length === 0 && (
                <HardShadowCard>
                  <p>
                    库里还没有未隐藏的作品。这是"没有内容"，不是"读不到"：
                    上面两栏要也是空的，说明这套环境还没跑过任何采集。
                  </p>
                </HardShadowCard>
              )}
              {page.items.map((video) => (
                <VideoRow
                  key={video.id}
                  video={video}
                  creatorName={
                    video.creator_id === undefined || video.creator_id === null
                      ? undefined
                      : names.get(video.creator_id)
                  }
                />
              ))}
            </div>
          )}
        </QueryState>
      </section>
    </PageShell>
  );
}

function HealthLine({ health }: { health: QueryGate<Health> }): JSX.Element {
  return (
    <QueryState gate={health} subject="后端健康">
      {(report) => (
        <p className="text-mono-sm">
          <span
            className={cn(
              "memphis-border border-ink-black px-3 py-1",
              report.status === "ok" ? "bg-mint-green" : "bg-coral-red",
            )}
          >
            后端 {report.status}
          </span>
          <span className="ml-3">
            {report.version} · {formatTime(report.time)}
          </span>
        </p>
      )}
    </QueryState>
  );
}

function PlatformCard({ platform }: { platform: PlatformSummary }): JSX.Element {
  const state = stateOf(platform);
  return (
    <HardShadowCard className="flex flex-col gap-2">
      <PlatformBadge platform={platform.name} label={platform.display_name} />
      {/* 平台键名也打出来：只给展示名的话，配置里漂出第五个平台时界面会安静地
          显示成"某某"，而那正是该跳出来的时刻。 */}
      <code className="text-mono-sm">{platform.name}</code>
      <span className={cn("memphis-border border-ink-black px-3 py-1 text-body-sm", state.tone)}>
        {state.label}
      </span>
      {/* ADR-0022：灯读的是 `platforms` 镜像里 preflight 上一次写回的结论，
          这一页**不探测**（探测会真连桥与平台，那是预检页的活）。
          没有结论就一整行不画 —— 画一个灰色"未知"会被读成"检查过且没问题"，
          而那正是 §7.20 的形状。有结论就必须连时刻一起画：一个没有时刻的绿灯
          与一个假绿灯没有区别。 */}
      <ProbeLine platform={platform} />
    </HardShadowCard>
  );
}

/** 上次探测的一句话：颜色 + 结论 + 时刻（+ 有原文就把原文带上）。 */
function ProbeLine({ platform }: { platform: PlatformSummary }): JSX.Element | null {
  const status = platform.health_status;
  if (!status || !platform.health_checked_at) return null;
  const tone = HEALTH_TONES[status] ?? "bg-grey-mist";
  return (
    <span className={cn("memphis-border border-ink-black px-3 py-1 text-body-sm", tone)}>
      上次探测 {formatTime(platform.health_checked_at)}：{HEALTH_LABELS[status] ?? status}
      {platform.health_detail ? ` —— ${platform.health_detail}` : ""}
    </span>
  );
}

const HEALTH_LABELS: Record<string, string> = {
  ok: "连得上",
  degraded: "降级",
  unreachable: "连不上",
  unknown: "探了但判不出来",
};

const HEALTH_TONES: Record<string, string> = {
  ok: "bg-mint-green",
  degraded: "bg-lemon-yellow",
  unreachable: "bg-coral-red",
  unknown: "bg-lemon-yellow",
};

/** 三种状态分开给：`enabled` 与 `implemented` 是两件事 ——
 *  配置里开着但 V2 没有实现，症状是"任务列表里没有这个平台的采集"，
 *  涂成"已启用"就是骗人。 */
function stateOf(platform: PlatformSummary): { label: string; tone: string } {
  if (!platform.enabled) return { label: "已关闭", tone: "bg-grey-mist" };
  if (!platform.implemented)
    return { label: "开着，但 V2 没实现这个平台", tone: "bg-lemon-yellow" };
  return { label: "已启用", tone: "bg-mint-green" };
}

function SectionTitle({
  children,
  to,
  toLabel,
}: {
  children: ReactNode;
  to: string;
  toLabel: string;
}): JSX.Element {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-3">
      <h2 className="font-heading text-h3 font-bold">{children}</h2>
      <Link to={to} className="text-mono-sm text-electric-blue">
        {toLabel} →
      </Link>
    </div>
  );
}
