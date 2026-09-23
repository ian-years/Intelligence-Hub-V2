import type { JSX } from "react";

import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PageShell } from "@/components/shared/PageShell";
import { ApiError } from "@/api/client";
import { usePreflight, type PreflightReport, type PreflightStatus } from "@/api/hooks/useHealth";
import { parseNames, parsePairs } from "@/lib/formatters";
import { cn } from "@/lib/utils";

const LIGHT: Record<PreflightStatus, { label: string; tone: string; why: string }> = {
  success: { label: "全绿", tone: "bg-mint-green", why: "每一档探测都过" },
  partial: {
    label: "部分可用",
    tone: "bg-lemon-yellow",
    why: "有平台降级：能跑，但那一路会缺东西",
  },
  failed: { label: "有不可用的地方", tone: "bg-coral-red", why: "跑任务有明确会挂的地方" },
};

const STATUS_TONE: Record<string, string> = {
  ok: "bg-mint-green",
  present: "bg-mint-green",
  degraded: "bg-lemon-yellow",
  missing: "bg-coral-red",
  unreachable: "bg-coral-red",
};

/**
 * 环境预检页（`AGENTS.md §1.3` 在运行时的落点）。
 *
 * 这一页存在的意义就一句话：**缺依赖时红着写出来，而不是"没发现问题所以返回 ok"**。
 * 所以三种情况分开渲染，任何一种都不许让另一种的文案露出来：
 * 请求还在飞（还不知道）、读不到（失败原文）、读到了但环境是黄/红的。
 *
 * `tools_missing` 单独看不判红，页面也必须把这句说出来：某个二进制**要紧由平台决定**
 * （抖音缺 yt-dlp 还有页面直链，B站 缺就是死路），那层判断在适配器 `healthcheck()` 里 ——
 * 这里再判一次就是第二处真相（`tasks/preflight.py` 的 docstring 写的是同一件事）。
 */
export function Preflight(): JSX.Element {
  const probe = usePreflight();

  return (
    <PageShell pattern="stripes" className="mx-auto flex max-w-[1100px] flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-display-md">环境预检</h1>
          <p className="text-body-md">
            这套环境到底能不能跑：平台、主库、外部二进制、ASR 模型。只读，不改任何配置。
          </p>
        </div>
        <MemphisButton
          variant="secondary"
          onClick={() => void probe.refetch()}
          disabled={probe.isFetching}
        >
          {probe.isFetching ? "探测中…" : "重新探测"}
        </MemphisButton>
      </header>

      {probe.isPaused && (
        <HardShadowCard>
          <p>
            探测被暂停了：第一次请求已经发出去，但重试要等页面回到前台 （react-query
            在窗口不可见时挂起补发，此时 `status` 仍是 pending）。 回到前台会自动继续 ——
            这一栏**不是**"环境没问题"。
          </p>
        </HardShadowCard>
      )}

      {!probe.isPaused && probe.isError && <Unreadable error={probe.error} />}

      {!probe.isPaused && !probe.isError && !probe.data && (
        <HardShadowCard>
          <span className="flex items-center gap-3">
            <span aria-label="正在探测" className="memphis-loading">
              <i />
              <i />
              <i />
            </span>
            正在探测，还没有结论 —— 这里刻意不显示任何红绿灯
          </span>
        </HardShadowCard>
      )}

      {probe.data && <Report report={probe.data} at={probe.dataUpdatedAt} />}
    </PageShell>
  );
}

function Unreadable({ error }: { error: unknown }): JSX.Element {
  const detail = error instanceof ApiError ? error.detail : String(error);
  return (
    <HardShadowCard className="bg-coral-red">
      <h2>读不到预检结果</h2>
      <p className="break-words text-body-md">{detail}</p>
      <p className="text-body-sm">
        这一页宁可空着也不显示绿灯：把"探测不下去"显示成"没发现问题"，是这个仓库踩过两次的坑。
      </p>
    </HardShadowCard>
  );
}

function Report({ report, at }: { report: PreflightReport; at: number }): JSX.Element {
  const light = LIGHT[report.status];
  const platforms = parsePairs(report.summary.platform_status);
  const present = parseNames(report.summary.tools_present);
  const missing = parseNames(report.summary.tools_missing);

  return (
    <>
      <HardShadowCard className={cn("flex flex-wrap items-center gap-4", light.tone)}>
        <span className="font-heading text-h2 font-bold">{light.label}</span>
        <span className="text-body-md">{light.why}</span>
        <span className="ml-auto text-mono-sm">
          {new Date(at).toLocaleString("zh-CN", { hour12: false })}
        </span>
      </HardShadowCard>

      <HardShadowCard>
        <h2>平台</h2>
        <ul className="mt-3 flex list-none flex-col gap-3 p-0">
          {platforms.length === 0 && <li className="text-body-md">（没有启用的平台）</li>}
          {platforms.map(([platform, status]) => (
            <li className="flex items-center gap-3" key={platform}>
              <PlatformBadge platform={platform} size="sm" />
              {/* 平台名（`platforms.yaml` 的那个 key）也要看得见：只给展示名的话，
                  预检返回了一个没注册的平台名时界面会安静地显示成"某某"。 */}
              <code className="text-mono-sm">{platform}</code>
              <span
                className={cn(
                  "memphis-border border-ink-black px-3 py-1 text-body-sm",
                  STATUS_TONE[status] ?? "bg-grey-mist",
                )}
              >
                {status || "状态未知"}
              </span>
            </li>
          ))}
        </ul>
      </HardShadowCard>

      <div className="grid gap-6 md:grid-cols-2">
        <HardShadowCard>
          <h2>主库与 ASR 模型</h2>
          <dl className="mt-3 flex flex-col gap-2 text-body-md">
            <Line label="主库 SELECT 1" value={report.summary.storage} />
            <Line label="ASR 模型目录" value={report.summary.asr_model} />
          </dl>
        </HardShadowCard>

        <HardShadowCard>
          <h2>PATH 上的外部工具</h2>
          <Chips label="在" names={present} tone="bg-mint-green" empty="（PATH 上一个都没有）" />
          <Chips label="缺" names={missing} tone="bg-coral-red" empty="（无）" />
          <p className="text-body-sm">
            缺工具本身不判红：某个二进制要紧由平台决定（抖音缺 yt-dlp 还有页面直链，B站
            缺就是死路）。那层判断在平台的 healthcheck 里，已经反映在上面那栏红绿灯上了。
          </p>
        </HardShadowCard>
      </div>

      {report.failures.length > 0 && (
        <HardShadowCard>
          <h2>失败明细</h2>
          <ul className="mt-3 flex list-none flex-col gap-4 p-0">
            {report.failures.map((failure, index) => (
              <li
                className="memphis-border border-ink-black p-3"
                key={`${failure.platform ?? "global"}-${String(index)}`}
              >
                <div className="flex flex-wrap gap-3 text-body-sm">
                  <span>{failure.platform ?? "全局"}</span>
                  <span>阶段 {failure.stage}</span>
                  <span className="bg-grey-mist px-2">{failure.error_kind ?? "未分类"}</span>
                </div>
                <p className="mt-2 break-words text-mono-md">{failure.error}</p>
              </li>
            ))}
          </ul>
        </HardShadowCard>
      )}
    </>
  );
}

/** 一个名字一个节点：挤成一句"ffprobe、yt-dlp"的话，测试与屏幕阅读器都点不到单个工具。 */
function Chips({
  label,
  names,
  tone,
  empty,
}: {
  label: string;
  names: string[];
  tone: string;
  empty: string;
}): JSX.Element {
  return (
    <p className="flex flex-wrap items-center gap-2 text-body-md">
      <span>{label}</span>
      {names.length === 0 ? (
        <span className="text-body-sm">{empty}</span>
      ) : (
        names.map((name) => (
          <span
            className={cn("memphis-border border-ink-black px-2 text-body-sm", tone)}
            key={name}
          >
            {name}
          </span>
        ))
      )}
    </p>
  );
}

function Line({
  label,
  value,
}: {
  label: string;
  value: string | number | undefined;
}): JSX.Element {
  const shown = typeof value === "string" ? value : String(value ?? "—");
  return (
    <div className="flex items-center gap-3">
      <dt>{label}</dt>
      <dd
        className={cn(
          "m-0 memphis-border border-ink-black px-3 py-1 text-body-sm",
          STATUS_TONE[shown] ?? "bg-paper-cream",
        )}
      >
        {shown}
      </dd>
    </div>
  );
}
