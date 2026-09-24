import { useCallback, useEffect, useRef, useState, type JSX } from "react";

import { api, ApiError } from "@/api/client";
import { useQueryClient } from "@tanstack/react-query";
import { keys } from "@/api/keys";
import { useBenchmarkAnalysis, useGenerateDraftScript } from "@/api/hooks/useAnalysis";
import { useDrafts, type Draft } from "@/api/hooks/useTopics";
import { useTopics } from "@/api/hooks/useTopics";
import { useTranscript, useVideos, type Video } from "@/api/hooks/useVideos";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PageShell } from "@/components/shared/PageShell";
import { QueryState } from "@/components/shared/QueryState";
import { formatTime } from "@/lib/formatters";
import {
  AUTOSAVE_DELAY_MS,
  beatsOf,
  isDirty,
  promptLinesOf,
  shouldAutosave,
  BEAT_TEMPLATE_KEYS,
  type DraftTemplateKey,
  VIEW_LABELS,
  WORKSHOP_VIEWS,
  type DraftDraft,
  type WorkshopView,
} from "@/lib/workshop";
import { cn } from "@/lib/utils";

/**
 * 创作工坊（T4.7）：选对标 → 看逐字稿与拆解 → 写稿（自动保存）→ 四视图。
 *
 * 三处刻意的设计：
 *
 * 1. **"生成初稿"只写进编辑器，不落库**。落库是自动保存那一格的事（下一拍）。
 *    点一下就存的话，"我不喜欢这份、再点一次"会留下两条草稿，而工坊页最不缺的就是草稿。
 * 2. **自动保存比的是"上一次落库成功的那一份"**，不是"上一次输入"。
 *    保存失败时输入没再变，如果按输入算，界面会立刻显示"已保存" —— 那句是假的。
 * 3. **空白不发**：后端的 `assert_non_blank_text` 会 422，
 *    一个每 1.6 秒必然失败一次的自动保存，最后会被用户当成"这页坏了"。
 *
 * "截图包"那一格 V2 没有数据源（没有分镜截图这一步，媒体只有成片），
 * 所以它说的是一句实话而不是一个空网格 —— V1 那一格靠的是 V1 才有的截图任务。
 */
export function Workshop(): JSX.Element {
  const [topicName, setTopicName] = useState("");
  const [reference, setReference] = useState<Video | null>(null);
  const [seed, setSeed] = useState<{ id?: number; title: string; body: string; nonce: number }>({
    title: "",
    body: "",
    nonce: 0,
  });

  const topics = useTopics();
  const videos = useVideos({ size: 8 });

  return (
    <PageShell pattern="confetti" className="mx-auto flex max-w-[1200px] flex-col gap-5">
      <header>
        <h1 className="font-display text-display-md">创作工坊</h1>
        <p className="text-body-md">
          左边挑一位对标作品与一个选题，中间写稿（每 {String(AUTOSAVE_DELAY_MS / 1000)}{" "}
          秒自动存一次）， 右边四视图切着看。选题与草稿是两张表，这一页只是把它们缝在一起。
        </p>
      </header>

      <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
        <div className="flex flex-col gap-4">
          <TopicPicker topics={topics} selected={topicName} onPick={(name) => setTopicName(name)} />
          <ReferencePicker
            videos={videos}
            selectedId={reference?.id ?? null}
            onPick={(video) => setReference(video)}
          />
          <DraftPicker
            onPick={(draft) =>
              // nonce 只在这里变：**自动保存新建成功之后不换 key**，
              // 换了会把编辑器重挂载、把刚写好的稿子抹回空 —— 那是真事故，不是实现细节。
              setSeed({
                id: draft.id,
                title: draft.title,
                body: draft.content,
                nonce: seed.nonce + 1,
              })
            }
          />
        </div>

        <div className="flex flex-col gap-4">
          <ReferencePanel video={reference} />
          <DraftEditor
            key={seed.nonce}
            initial={seed}
            topicName={topicName}
            referenceVideoId={reference?.id ?? null}
          />
        </div>
      </div>
    </PageShell>
  );
}

function TopicPicker({
  topics,
  selected,
  onPick,
}: {
  topics: ReturnType<typeof useTopics>;
  selected: string;
  onPick: (name: string) => void;
}): JSX.Element {
  return (
    <HardShadowCard className="flex flex-col gap-2">
      <h2 className="font-heading text-h3 font-bold">选题</h2>
      <QueryState gate={topics} subject="选题">
        {(list) =>
          list.length === 0 ? (
            <p className="text-body-sm">选题库里还没有条目：先去选题页记一条。</p>
          ) : (
            <ul className="flex flex-wrap gap-2">
              {list.map((topic) => (
                <li key={topic.id}>
                  <button
                    type="button"
                    aria-pressed={selected === topic.name}
                    className={cn(
                      "memphis-btn memphis-border border-ink-black !px-3 !py-1 text-body-sm",
                      selected === topic.name
                        ? "bg-electric-blue text-paper-cream"
                        : "bg-paper-cream",
                    )}
                    onClick={() => onPick(topic.name)}
                  >
                    {topic.name}
                  </button>
                </li>
              ))}
            </ul>
          )
        }
      </QueryState>
    </HardShadowCard>
  );
}

function ReferencePicker({
  videos,
  selectedId,
  onPick,
}: {
  videos: ReturnType<typeof useVideos>;
  selectedId: number | null;
  onPick: (video: Video) => void;
}): JSX.Element {
  return (
    <HardShadowCard className="flex flex-col gap-2">
      <h2 className="font-heading text-h3 font-bold">对标作品</h2>
      <QueryState gate={videos} subject="作品列表">
        {(page) =>
          page.items.length === 0 ? (
            <p className="text-body-sm">库里还没有作品：先去任务页跑一次采集。</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {page.items.map((video) => (
                <li key={video.id}>
                  <button
                    type="button"
                    aria-pressed={selectedId === video.id}
                    className="w-full truncate bg-transparent text-left text-body-md hover:bg-grey-mist"
                    onClick={() => onPick(video)}
                  >
                    {video.title || "（没有标题）"}
                  </button>
                </li>
              ))}
            </ul>
          )
        }
      </QueryState>
    </HardShadowCard>
  );
}

function DraftPicker({ onPick }: { onPick: (draft: Draft) => void }): JSX.Element {
  return (
    <HardShadowCard className="flex flex-col gap-2">
      <h2 className="font-heading text-h3 font-bold">继续写一条</h2>
      <p className="text-body-sm">
        全部草稿在选题页那份列表里；这里只给"打开一条"的入口，两处各一份列表就会各漂各的。
      </p>
      <DraftList onPick={onPick} />
    </HardShadowCard>
  );
}

function DraftList({ onPick }: { onPick: (draft: Draft) => void }): JSX.Element {
  // 只要"还在写的"那些：`status=draft` 是查询参数，不是前端筛（前端筛会把分页外的漏掉）
  const drafts = useDrafts("draft");
  return (
    <QueryState gate={drafts} subject="草稿">
      {(list) => (
        <ul className="flex flex-col gap-1">
          {list.map((draft) => (
            <li key={draft.id}>
              <button
                type="button"
                className="w-full truncate bg-transparent text-left text-body-sm"
                onClick={() => onPick(draft)}
              >
                {draft.title}
              </button>
            </li>
          ))}
        </ul>
      )}
    </QueryState>
  );
}

function ReferencePanel({ video }: { video: Video | null }): JSX.Element {
  const transcript = useTranscript(video?.id);
  const analysis = useBenchmarkAnalysis(video?.id);
  if (video === null) {
    return (
      <HardShadowCard>
        <p className="text-body-sm">
          还没选对标作品：左栏挑一条，这一栏就会给出它的逐字稿与钩子拆解。 没有对标也能写 ——
          那一栏只是参考，不是必填。
        </p>
      </HardShadowCard>
    );
  }
  return (
    <HardShadowCard className="flex flex-col gap-2">
      <h2 className="font-heading text-h3 font-bold">参考 · {video.title}</h2>
      <p className="text-mono-sm">
        拆解来自 <code>/api/benchmark-analysis</code>，稿子来自{" "}
        <code>/api/videos/{String(video.id)}/transcript</code>，两份都是**只读**
      </p>
      <QueryState gate={analysis} subject="拆解">
        {(body) =>
          body === null ? (
            <p className="text-body-sm">这条还没有可拆的口播稿（拆解要读稿子，不读元数据）。</p>
          ) : (
            <p className="text-body-md">
              钩子：<span className="bg-lemon-yellow px-2">{body.hook?.type ?? "判不出类型"}</span>{" "}
              {body.hook?.sentence ?? ""}（{String(body.hook?.punch_score ?? 0)} 分）
            </p>
          )
        }
      </QueryState>
      <QueryState gate={transcript} subject="逐字稿">
        {(body) =>
          body === null ? (
            <p className="text-body-sm">这条没有口播稿：先跑一次 postprocess。</p>
          ) : (
            <details className="text-body-md">
              <summary>逐字稿（{String(body.sentence_count)} 句）</summary>
              <p className="mt-2 whitespace-pre-wrap">{body.text}</p>
            </details>
          )
        }
      </QueryState>
    </HardShadowCard>
  );
}

type SaveState = "idle" | "dirty" | "saving" | "saved" | "error";

/** 两份稿子是不是同一份。比较而不是记账：省一处状态就少一处会漂移的真相。 */
function sameAs(a: DraftDraft, b: DraftDraft | null): boolean {
  return b !== null && a.title === b.title && a.body === b.body;
}

function DraftEditor({
  initial,
  topicName,
  referenceVideoId,
}: {
  initial: { id?: number; title: string; body: string };
  topicName: string;
  referenceVideoId: number | null;
}): JSX.Element {
  const [draft, setDraft] = useState<DraftDraft>({
    title: initial.title || topicName,
    body: initial.body,
  });
  // 新建成功后 id 就有了，但**不能**让它进 React 状态：一改 key 就重挂载（见父组件那条注释）。
  const idRef = useRef<number | undefined>(initial.id);
  const [saved, setSaved] = useState<DraftDraft | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [phase, setPhase] = useState<"idle" | "saving" | "error">("idle");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [inFlight, setInFlight] = useState<DraftDraft | null>(null);
  const [view, setView] = useState<WorkshopView>("editor");
  const [templateKey, setTemplateKey] = useState<DraftTemplateKey>("tutorial_save_loop");
  const [seedNote, setSeedNote] = useState<string | null>(null);

  const queryClient = useQueryClient();
  const generate = useGenerateDraftScript();

  const persist = useCallback(
    async (value: DraftDraft): Promise<void> => {
      if (!shouldAutosave(value)) return;
      setPhase("saving");
      setInFlight(value);
      setErrorText(null);
      try {
        const row =
          idRef.current === undefined
            ? await api.post<Draft>("/drafts", {
                title: value.title,
                content: value.body,
                source_video_id: referenceVideoId,
                // 状态是新草稿唯一能是的那个值：`draft`。"发布"与"归档"是草稿页那三个
                // 按钮的活，工坊这一页不越过去猜一个终态。
                status: "draft",
              })
            : await api.patch<Draft>(`/drafts/${String(idRef.current)}`, {
                title: value.title,
                content: value.body,
              });
        idRef.current = row.id;
        await queryClient.invalidateQueries({ queryKey: keys.drafts("draft") });
        setSaved(value);
        setSavedAt(row.updated_at ?? null);
        setPhase("idle");
      } catch (error) {
        setPhase("error");
        // 失败要留着草稿与原因：清掉输入等于把用户三分钟的稿子弄丢在保存失败的副作用里
        setErrorText(error instanceof ApiError ? error.detail : String(error));
      }
    },
    [queryClient, referenceVideoId],
  );

  // 让自动保存那个 effect 只依赖"稿子的内容"，不依赖每次渲染都换身份的 mutation 对象。
  const persistRef = useRef(persist);
  useEffect(() => {
    persistRef.current = persist;
  }, [persist]);

  // 保存状态是**算出来的**，不是在 effect 里 setState 出来的：
  // `react-hooks/set-state-in-effect` 挡的正是这种写法，而它挡得对 ——
  // "dirty" 本来就完全由 (当前稿, 上次落库的那份) 决定，存成状态只会与真值漂移。
  const dirty = isDirty(draft, saved) && !sameAs(draft, inFlight);
  const state: SaveState =
    phase === "saving"
      ? "saving"
      : dirty
        ? "dirty"
        : phase === "error"
          ? "error"
          : saved !== null
            ? "saved"
            : "idle";

  useEffect(() => {
    if (!isDirty(draft, saved)) return undefined;
    const timer = setTimeout(() => void persistRef.current(draft), AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [draft, saved]);

  return (
    <HardShadowCard className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-heading text-h3 font-bold">稿子</h2>
        <span className="ml-auto flex gap-2">
          {WORKSHOP_VIEWS.map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={view === item}
              className={cn(
                "memphis-btn memphis-border border-ink-black !px-3 !py-1 text-body-sm",
                view === item ? "bg-electric-blue text-paper-cream" : "bg-paper-cream",
              )}
              onClick={() => setView(item)}
            >
              {VIEW_LABELS[item]}
            </button>
          ))}
        </span>
      </div>

      <SaveLine state={state} savedAt={savedAt} errorText={errorText} />

      <label className="flex flex-col gap-1">
        <span className="text-body-sm">标题</span>
        <input
          className="memphis-input"
          value={draft.title}
          onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))}
        />
      </label>

      {view === "editor" && (
        <label className="flex flex-col gap-1">
          <span className="text-body-sm">正文（一行一格分镜）</span>
          <textarea
            className="memphis-input min-h-[240px] font-mono text-mono-md"
            value={draft.body}
            onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))}
          />
        </label>
      )}
      {view === "beats" && <BeatsView text={draft.body} />}
      {view === "teleprompter" && <TeleprompterView text={draft.body} />}
      {view === "shots" && <ShotsView />}

      <div className="flex flex-wrap items-center gap-2">
        <MemphisButton
          variant="secondary"
          disabled={generate.isPending}
          onClick={() => {
            const topic = draft.title.trim() !== "" ? draft.title : topicName;
            generate.mutate(
              {
                topic,
                video_id: referenceVideoId,
                // mode 在引擎里只回显不改分镜（V1 如此，V2 如实保留并写进字段描述），
                // 所以这里不摆一个"时长"下拉去骗人；template_key 是真换表的那一个。
                mode: "SHORT",
                template_key: templateKey,
              },
              {
                onSuccess: (script) => {
                  // 只写进编辑器，**不顺手落库**（页头那条判据）
                  setDraft((current) => ({ ...current, body: script.script_markdown ?? "" }));
                  setSeedNote(
                    `已把生成的 ${String(script.total_beats ?? 0)} 格放进编辑器（模板：${
                      script.template_name ?? "未标注"
                    }）。还没保存 —— 改动停手 ${String(AUTOSAVE_DELAY_MS / 1000)} 秒后才会落库。`,
                  );
                },
                onError: (error) => {
                  setSeedNote(
                    `生成没成功：${error instanceof ApiError ? error.detail : String(error)}`,
                  );
                },
              },
            );
          }}
        >
          {generate.isPending ? "生成中…" : "从对标生成初稿"}
        </MemphisButton>
        <label className="flex items-center gap-2">
          <span className="text-body-sm">分镜表</span>
          <select
            className="memphis-input"
            value={templateKey}
            onChange={(event) => setTemplateKey(event.target.value as DraftTemplateKey)}
          >
            {BEAT_TEMPLATE_KEYS.map((key) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        </label>
        {seedNote && <p className="text-body-sm">{seedNote}</p>}
      </div>
    </HardShadowCard>
  );
}

function SaveLine({
  state,
  savedAt,
  errorText,
}: {
  state: SaveState;
  savedAt: string | null;
  errorText: string | null;
}): JSX.Element {
  const copy: Record<SaveState, string> = {
    idle: "没有改动",
    dirty: "有未保存的改动…",
    saving: "保存中…",
    saved: `已保存 ${savedAt ? formatTime(savedAt) : "（后端没给时刻）"}`,
    error: "保存失败",
  };
  return (
    <p aria-live="polite" className="text-mono-sm">
      <span
        className={cn(
          "memphis-border border-ink-black px-3 py-1",
          state === "error" ? "bg-coral-red" : state === "saved" ? "bg-mint-green" : "bg-grey-mist",
        )}
      >
        {copy[state]}
      </span>
      {state === "error" && errorText && <span className="ml-2 break-words">{errorText}</span>}
    </p>
  );
}

function BeatsView({ text }: { text: string }): JSX.Element {
  // 切分规则住在 `lib/workshop.ts::beatsOf`（那条判据在那边被单测钉）。这里再写一遍的话，
  // 两边对"空行"的口径迟早会不一样 —— 而症状是"分镜格数与稿子对不上"。
  const beats = beatsOf(text);
  if (beats.length === 0) return <EmptyView text="正文还是空的，分镜自然也是。" />;
  return (
    <ol aria-label="分镜" className="flex flex-col gap-1">
      {beats.map((beat, index) => (
        <li key={`${String(index)}:${beat.slice(0, 12)}`} className="flex gap-2 text-body-md">
          <code className="text-mono-sm opacity-70">{String(index + 1).padStart(2, "0")}</code>
          <span className="min-w-0 break-words">{beat}</span>
        </li>
      ))}
    </ol>
  );
}

function TeleprompterView({ text }: { text: string }): JSX.Element {
  const lines = promptLinesOf(text);
  if (lines.length === 0) return <EmptyView text="没有可提词的句子。" />;
  return (
    <div role="list" aria-label="提词器" className="flex flex-col gap-3">
      {lines.map((line, index) => (
        <p
          role="listitem"
          key={`${String(index)}:${line.slice(0, 12)}`}
          className="font-heading text-h2"
        >
          {line}
        </p>
      ))}
    </div>
  );
}

function ShotsView(): JSX.Element {
  return (
    <EmptyView text="V2 没有截图包这一步：媒体只有成片，没有按分镜截出来的图。要这一格得先有一个截图任务，那是 V2.2 的事。" />
  );
}

function EmptyView({ text }: { text: string }): JSX.Element {
  return <p className="text-body-sm">{text}</p>;
}
