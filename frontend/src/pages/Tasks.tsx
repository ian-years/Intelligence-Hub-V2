import type { FormEvent, JSX } from "react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import type { ObjectSchema } from "@/api/json-schema";
import { useCancelRun, useRuns, useRunTask, useTasks, type TaskInfo } from "@/api/hooks/useTasks";
import { useTaskEvents } from "@/events/useTaskEvents";
import { EVENT_TYPES } from "@/events/types";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PageShell } from "@/components/shared/PageShell";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { QueryState } from "@/components/shared/QueryState";
import { TaskTimeline } from "@/components/shared/TaskTimeline";
import { formatTime } from "@/lib/formatters";
import { cn } from "@/lib/utils";

const HISTORY = 12;
const STREAM = 120;

const CONNECTION: Record<string, { label: string; tone: string }> = {
  connecting: { label: "正在连接", tone: "bg-lemon-yellow" },
  open: { label: "事件流已连上", tone: "bg-mint-green" },
  reconnecting: { label: "断线重连中", tone: "bg-lemon-yellow" },
  // 没有 EventSource（或一直连不上）时不许安静：那正是"看起来在跑而其实没在看"
  offline: { label: "事件流不可用（这个环境没有 EventSource）", tone: "bg-coral-red" },
};

/**
 * 任务（`/tasks`）：能发起的、正在跑的、跑过的、以及实时事件流。
 *
 * 发起这一栏**由 schema 决定能不能一键跑**：`required` 为空的任务（`preflight`、
 * 两个 `*_collect`、`postprocess`）可以直接提交 `{}`；
 * 只有一个必填 `url` 的（`single_link`、`add_creator`）给一个链接框；
 * 其余的**明说这里发起不了、该去哪儿**，而不是画一个必然 422 的按钮。
 * 判据取自 `/api/tasks/{name}/schema`，不是前端记的一份任务清单。
 */
export function Tasks(): JSX.Element {
  const tasks = useTasks();
  const runs = useRuns();
  const cancel = useCancelRun();
  const stream = useTaskEvents();

  return (
    <PageShell pattern="checker" className="mx-auto flex max-w-[1100px] flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-display text-display-md">任务</h1>
          <p className="text-body-md">
            能发起的任务、运行历史、实时事件。取消是协作式的：正在跑的那一步做完才停。
          </p>
        </div>
        <span
          className={cn(
            "memphis-border border-ink-black px-3 py-1 text-body-sm",
            (CONNECTION[stream.status] ?? { tone: "bg-grey-mist" }).tone,
          )}
        >
          {(CONNECTION[stream.status] ?? { label: stream.status }).label}
        </span>
      </header>

      <section aria-label="可发起的任务">
        <h2 className="font-heading text-h3 font-bold">可发起</h2>
        <QueryState gate={tasks} subject="任务清单">
          {(list) => (
            <div className="mt-3 grid gap-4 md:grid-cols-2">
              {list.map((task) => (
                <TaskCard key={task.name} task={task} />
              ))}
            </div>
          )}
        </QueryState>
      </section>

      <section aria-label="运行历史">
        <h2 className="font-heading text-h3 font-bold">运行历史（最近 {String(HISTORY)} 次）</h2>
        <QueryState gate={runs} subject="运行历史">
          {(list) => (
            <TaskTimeline
              className="mt-3"
              runs={list.slice(0, HISTORY)}
              onCancel={(run) => cancel.mutate(run.id)}
              empty="（还没有跑过任务）"
            />
          )}
        </QueryState>
        {cancel.isError && (
          <p className="mt-2 break-words text-body-sm text-coral-red">
            取消没送达：
            {cancel.error instanceof ApiError ? cancel.error.detail : cancel.error.message}
          </p>
        )}
      </section>

      <section aria-label="实时事件流">
        <h2 className="font-heading text-h3 font-bold">
          实时事件（最近 {String(STREAM)} 条 · 上限 500 条缓冲）
        </h2>
        {stream.gap && (
          <p className="memphis-border mt-3 bg-lemon-yellow px-3 py-2 text-body-sm">
            连接断开过：断线那段时间的事件**已经缺失** —— 全局事件不落库，重连补不回来。
            想核对发生过什么，看上面的运行历史（那是查库，不经过这条流）。
          </p>
        )}
        <HardShadowCard className="mt-3">
          {stream.events.length === 0 ? (
            <p>
              还没收到事件。
              {stream.error ? `连接那边的话：${stream.error}` : "有任务在跑时这里会滚动。"}
            </p>
          ) : (
            <ul className="flex max-h-[40vh] list-none flex-col gap-1 overflow-auto p-0">
              {stream.events
                .slice(-STREAM)
                .reverse()
                .map((event, index) => (
                  <li
                    key={`${event.timestamp}-${String(event.task_id ?? "global")}-${event.type}-${String(index)}`}
                    className="flex flex-wrap items-baseline gap-3 text-mono-sm"
                  >
                    <span className="bg-grey-mist px-2">{event.type}</span>
                    <span>{formatTime(event.timestamp)}</span>
                    <span>{event.task_id ? `任务 ${event.task_id}` : "全局"}</span>
                  </li>
                ))}
            </ul>
          )}
          <p className="text-body-sm">
            事件类型全集与后端 `EventType` 逐字核过（{String(EVENT_TYPES.length)} 个）。
            没在这份名单里的类型收都收不到 —— 那由用例钉着，不是这里能保证的。
          </p>
        </HardShadowCard>
      </section>
    </PageShell>
  );
}

/** 参数形状只有三种可信读法 + 一种"读不出来"，最后那种**不给任何发起按钮**。 */
type Shape = "no-required" | "url-only" | "other" | "unknown";

/**
 * 从任务自带的 `params_schema` 判断"能不能一键发起"。
 *
 * 判据必须是**否证式**的，这里踩过一次：`/api/tasks/{name}/schema` 回的是
 * `{name, display_name, kind, params_schema}` 那个信封，而 `TaskInfo.params_schema`
 * 才是里面那层 JSON Schema。当时读的是 `schema.required ?? []`，
 * 信封上没有 `required` → 空数组 → **六个任务全给了「跑一次」**，
 * 其中 `single_link` / `add_creator` 是必填 `url` 的，点下去就是一个 422。
 *
 * 所以先确认"这像个 JSON Schema"（有 `type` 或有 `properties`）再谈必填；
 * 不像就归到 `unknown`，界面上一个发起按钮都不给。
 * （`required` 缺省本身按 JSON Schema 是合法的"无必填"，那种情况有 `type: "object"` 兜着。）
 */
function shapeOf(task: TaskInfo): Shape {
  const schema = task.params_schema as ObjectSchema | undefined;
  if (
    schema === undefined ||
    typeof schema !== "object" ||
    (schema.type === undefined && schema.properties === undefined)
  ) {
    return "unknown";
  }
  const required = (schema.required ?? []).filter((key): key is string => typeof key === "string");
  if (required.length === 0) return "no-required";
  if (required.length === 1 && required[0] === "url") return "url-only";
  return "other";
}

/** 一个任务一张卡。发起能力来自 `/api/tasks` 里那条自带的 `params_schema`，
 *  不再每张卡发一次 `/tasks/{name}/schema`（六张卡就是六次请求问同一件事）。 */
function TaskCard({ task }: { task: TaskInfo }): JSX.Element {
  const shape = shapeOf(task);
  const required = (task.params_schema as ObjectSchema | undefined)?.required ?? [];

  return (
    <HardShadowCard className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-heading text-h3 font-bold">{task.display_name}</span>
        <code className="text-mono-sm">{task.name}</code>
      </div>
      <p className="flex flex-wrap gap-2 text-body-sm">
        <span className="bg-grey-mist px-2">{task.kind}</span>
        {task.platforms.map((platform) => (
          <PlatformBadge key={platform} platform={platform} size="sm" />
        ))}
        {task.platforms.length === 0 && <span>不分平台</span>}
      </p>
      <p className="text-body-sm">
        {task.cancellable ? "可取消" : "不可取消（跑到哪算哪）"} ·{" "}
        {task.timeout_seconds === null ? "没有超时" : `超时 ${String(task.timeout_seconds)} 秒`}
      </p>

      {shape === "no-required" && <RunButton task={task} build={() => ({})} label="跑一次" />}
      {shape === "url-only" && <UrlRunButton task={task} />}
      {shape === "other" && (
        <p className="text-body-sm">
          这一版界面发不了它：必填参数是 <code>{required.join("、")}</code>。
          {task.name === "add_creator"
            ? " 收录博主请去博主页那个表单（那个端点会带上平台与跟踪默认值）。"
            : " 任务参数表单渲染器目前只接进设置页；这里画一个必然 422 的按钮没有意义。"}
        </p>
      )}
      {shape === "unknown" && (
        <p className="text-body-sm bg-coral-red">
          读不出这个任务的参数形状（`params_schema` 不像一份 JSON Schema：既没有 `type` 也没有
          `properties`）。读不出来就不给发起按钮 —— 猜成"没有必填"的后果是一个按下去 必然 422
          的按钮，而这正是这一栏最容易看起来正常的失败方式。
        </p>
      )}
    </HardShadowCard>
  );
}

function RunButton({
  task,
  build,
  label,
}: {
  task: TaskInfo;
  build: () => Record<string, unknown>;
  label: string;
}): JSX.Element {
  const run = useRunTask(task.name);
  return (
    <>
      <MemphisButton
        type="button"
        variant="primary"
        disabled={run.isPending}
        onClick={() => run.mutate(build())}
      >
        {run.isPending ? "排队中…" : label}
      </MemphisButton>
      <Accepted run={run} />
    </>
  );
}

function UrlRunButton({ task }: { task: TaskInfo }): JSX.Element {
  const [url, setUrl] = useState("");
  const run = useRunTask(task.name);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    run.mutate({ url });
  }

  return (
    <form className="flex flex-col gap-2" onSubmit={submit}>
      <label className="flex flex-col gap-1">
        <span className="text-body-sm">链接</span>
        <input
          className="memphis-input"
          type="url"
          required
          value={url}
          onChange={(event) => setUrl(event.target.value)}
        />
      </label>
      <MemphisButton type="submit" disabled={run.isPending}>
        {run.isPending ? "排队中…" : "提交"}
      </MemphisButton>
      <Accepted run={run} />
    </form>
  );
}

/** 提交成功说的是"排到了哪个任务"。不说"完成了"，也不说"成功了"。 */
function Accepted({ run }: { run: { data: { task_id: string } | undefined } }): JSX.Element | null {
  if (run.data === undefined) return null;
  return (
    <p className="text-body-sm">
      已排队：任务 <code>{run.data.task_id}</code>。下面那份历史要等它开始跑才看得到。
    </p>
  );
}
