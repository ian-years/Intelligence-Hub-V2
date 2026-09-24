import type { FormEvent, JSX } from "react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import {
  useCreateDraft,
  useCreateTopic,
  useDeleteDraft,
  useDeleteTopic,
  useDrafts,
  useTopics,
  useUpdateDraft,
  type Draft,
  type Topic,
} from "@/api/hooks/useTopics";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PageShell } from "@/components/shared/PageShell";
import { QueryState } from "@/components/shared/QueryState";

/**
 * 选题金矿（`/topics`，V2.2 T5.5）：选题的建/查/删 + 草稿的建/改/删。
 *
 * 两件事放一页是因为它们在同一条工作流上（看到一条选题 → 顺手开一篇草稿），
 * 但**两份数据各问各的**：两个查询、两族缓存键，一个读不到不拖累另一个说"读不到"。
 *
 * 三处刻意的"不假装"：
 * 1. 新建选题是**同步写入**，回 201 + 那条选题，所以成功之后表单清空、列表里立刻有它。
 *    它不像收录博主那样起任务 —— 把一件同步的事演成异步的，界面就要写一句假话。
 * 2. 撞名回的是 409 原文（"topic 已存在（UNIQUE constraint failed…）"），
 *    这里**不**替用户合并两条选题：那需要一个"被合并的那条描述去哪了"的答案，今天没有。
 * 3. 草稿的删除是这一页唯一会销毁文字的动作，所以它没有"批量清理"，也没有
 *    "发布之后自动删掉"。删就是点某一行上那个「删除」。
 */
export function Topics(): JSX.Element {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [draftTitle, setDraftTitle] = useState("");
  const [draftContent, setDraftContent] = useState("");

  const topics = useTopics();
  const drafts = useDrafts();
  const createTopic = useCreateTopic();
  const createDraft = useCreateDraft();

  function submitTopic(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    createTopic.mutate(
      { name, description: description === "" ? null : description },
      {
        // 只在后端确实收下之后清表单：先清再提交的话，失败时用户刚打的那几个字就没了。
        onSuccess: () => {
          setName("");
          setDescription("");
        },
      },
    );
  }

  function submitDraft(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    createDraft.mutate(
      // `status` 显式传：请求类型里它是必填的（`openapi-typescript` 把带 default 的
      // 属性生成成非可选），而"界面这一侧有个默认值、请求体那侧也有一个"正是
      // V1 §7.24 那一坑的形状 —— 与 `useAddCreator` 的 `tracking` 同一处理。
      { title: draftTitle, content: draftContent, status: "draft" },
      {
        onSuccess: () => {
          setDraftTitle("");
          setDraftContent("");
        },
      },
    );
  }

  return (
    <PageShell pattern="waves" className="mx-auto flex max-w-[1100px] flex-col gap-5">
      <header>
        <h1 className="font-display text-display-md">选题金矿</h1>
        <p className="text-body-md">
          一条选题就是一个名字（全库唯一）。草稿在这里写、在这里改状态；
          删除只发生在你点某一行上那个按钮的时候。
        </p>
      </header>

      <section aria-label="选题">
        <h2 className="font-heading text-h3 font-bold">选题</h2>
        <form
          className="mt-3 flex flex-wrap items-end gap-4 memphis-border border-ink-black bg-paper-cream p-4"
          onSubmit={submitTopic}
        >
          <label className="min-w-[220px] flex-1">
            <span className="text-body-sm">选题名</span>
            <input
              className="memphis-input w-full"
              type="text"
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="min-w-[220px] flex-1">
            <span className="text-body-sm">备注（可留空）</span>
            <input
              className="memphis-input w-full"
              type="text"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          <MemphisButton type="submit" disabled={createTopic.isPending}>
            {createTopic.isPending ? "保存中…" : "新建选题"}
          </MemphisButton>
        </form>
        {createTopic.isError && (
          <HardShadowCard className="mt-3 bg-coral-red">
            <h2>选题没存下</h2>
            <p className="break-words text-body-md">
              {createTopic.error instanceof ApiError
                ? createTopic.error.detail
                : createTopic.error.message}
            </p>
          </HardShadowCard>
        )}

        <div className="mt-3 flex flex-col gap-3">
          <QueryState gate={topics} subject="选题列表">
            {(list) => (
              <>
                {list.length === 0 && (
                  <HardShadowCard>
                    <p>还没有选题：用上面那个表单记一条。</p>
                  </HardShadowCard>
                )}
                {list.map((topic) => (
                  <TopicRow key={topic.id} topic={topic} />
                ))}
              </>
            )}
          </QueryState>
        </div>
      </section>

      <section aria-label="草稿">
        <h2 className="font-heading text-h3 font-bold">草稿</h2>
        <form
          className="mt-3 flex flex-wrap items-end gap-4 memphis-border border-ink-black bg-paper-cream p-4"
          onSubmit={submitDraft}
        >
          <label className="min-w-[220px] flex-1">
            <span className="text-body-sm">标题</span>
            <input
              className="memphis-input w-full"
              type="text"
              required
              value={draftTitle}
              onChange={(event) => setDraftTitle(event.target.value)}
            />
          </label>
          <label className="min-w-[260px] flex-[2]">
            <span className="text-body-sm">正文</span>
            <textarea
              className="memphis-input w-full"
              rows={3}
              required
              value={draftContent}
              onChange={(event) => setDraftContent(event.target.value)}
            />
          </label>
          <MemphisButton type="submit" disabled={createDraft.isPending}>
            {createDraft.isPending ? "保存中…" : "新建草稿"}
          </MemphisButton>
        </form>
        {createDraft.isError && (
          <HardShadowCard className="mt-3 bg-coral-red">
            <h2>草稿没存下</h2>
            <p className="break-words text-body-md">
              {createDraft.error instanceof ApiError
                ? createDraft.error.detail
                : createDraft.error.message}
            </p>
          </HardShadowCard>
        )}

        <div className="mt-3 flex flex-col gap-3">
          <QueryState gate={drafts} subject="草稿列表">
            {(list) => (
              <>
                {list.length === 0 && (
                  <HardShadowCard>
                    <p>库里还没有草稿。</p>
                  </HardShadowCard>
                )}
                {list.map((draft) => (
                  <DraftRow key={draft.id} draft={draft} />
                ))}
              </>
            )}
          </QueryState>
        </div>
      </section>
    </PageShell>
  );
}

/** 一行一份自己的删除 mutation：共享一个的话所有行会一起亮「删除中…」，
 *  而 `variables` 只有一个，分不清是哪一行在飞（`Creators.tsx` 的 `ToggleRow` 同形）。 */
function TopicRow({ topic }: { topic: Topic }): JSX.Element {
  const remove = useDeleteTopic();
  return (
    <HardShadowCard className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p className="font-heading font-bold">{topic.name}</p>
        <p className="text-body-sm">{topic.description ?? "（没有备注）"}</p>
      </div>
      <div className="flex items-center gap-3">
        {remove.isError && (
          <span className="text-body-sm">
            {remove.error instanceof ApiError ? remove.error.detail : remove.error.message}
          </span>
        )}
        <MemphisButton
          type="button"
          onClick={() => remove.mutate(topic.id)}
          disabled={remove.isPending}
        >
          {remove.isPending ? "删除中…" : "删除"}
        </MemphisButton>
      </div>
    </HardShadowCard>
  );
}

function DraftRow({ draft }: { draft: Draft }): JSX.Element {
  const remove = useDeleteDraft();
  const update = useUpdateDraft(draft.id);
  const published = draft.status === "published";
  return (
    <HardShadowCard className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-[240px] flex-1">
        <p className="font-heading font-bold">{draft.title}</p>
        <p className="text-body-sm">
          {draft.status}
          {draft.source_video_id === null ? "" : `（来源作品 #${String(draft.source_video_id)}）`}
        </p>
        <p className="mt-2 whitespace-pre-wrap text-body-md">{draft.content}</p>
      </div>
      <div className="flex items-center gap-3">
        {(remove.isError || update.isError) && (
          <span className="text-body-sm">{detailOf(remove.error ?? update.error)}</span>
        )}
        <MemphisButton
          type="button"
          disabled={update.isPending}
          onClick={() => update.mutate({ status: published ? "archived" : "published" })}
        >
          {update.isPending ? "改状态中…" : published ? "归档" : "发布"}
        </MemphisButton>
        <MemphisButton
          type="button"
          disabled={remove.isPending}
          onClick={() => remove.mutate(draft.id)}
        >
          {remove.isPending ? "删除中…" : "删除"}
        </MemphisButton>
      </div>
    </HardShadowCard>
  );
}

function detailOf(error: Error | null): string {
  if (!error) return "";
  return error instanceof ApiError ? String(error.detail) : error.message;
}
