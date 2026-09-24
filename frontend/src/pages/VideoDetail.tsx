import type { JSX, ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { useCreator } from "@/api/hooks/useCreators";
import { useHideVideo, useTranscript, useUnhideVideo, useVideo } from "@/api/hooks/useVideos";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PageShell } from "@/components/shared/PageShell";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { QueryState } from "@/components/shared/QueryState";
import { formatCount, formatDuration, formatTime } from "@/lib/formatters";

/**
 * 作品详情（`/video/:id`）：元数据 + 口播稿。**没有播放器** —— V2.0 不做，
 * 媒体只到"文件在哪个相对路径"。这句要写在页面上：一个空白的播放区会被读成"坏了"，
 * 而实际是"这一版就没做"。
 *
 * 地址里的 id 不是数字时**不发请求**（`useVideo(undefined)` 那条 `enabled` 挡着），
 * 直接说实话：404 与"这个地址压根不是作品"是两件事，前者该由后端回答。
 */
export function VideoDetail(): JSX.Element {
  const params = useParams();
  const id = Number(params.id);
  const valid = Number.isInteger(id) && id > 0;

  const video = useVideo(valid ? id : undefined);
  const transcript = useTranscript(valid ? id : undefined);
  const creator = useCreator(video.data?.creator_id ?? undefined);
  const hide = useHideVideo();
  const unhide = useUnhideVideo();

  if (!valid) {
    return (
      <PageShell pattern="waves" className="mx-auto flex max-w-[900px] flex-col gap-5">
        <HardShadowCard className="bg-coral-red">
          <h1>这个地址不是一个作品 id</h1>
          <p className="text-body-md">
            地址里那一段是 <code>{params.id ?? "（空）"}</code>。
            这里没去问后端，因为问了也只会是一个 404，而真正的问题是链接本身。
          </p>
          <Link to="/feed" className="text-mono-sm text-electric-blue">
            回作品流 →
          </Link>
        </HardShadowCard>
      </PageShell>
    );
  }

  return (
    <PageShell pattern="waves" className="mx-auto flex max-w-[900px] flex-col gap-5">
      <Link to="/feed" className="text-mono-sm text-electric-blue">
        ← 回作品流
      </Link>

      <QueryState gate={video} subject="这条作品">
        {(data) => (
          <>
            <HardShadowCard className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-3">
                <PlatformBadge platform={data.platform} />
                <code className="text-mono-sm">{data.platform_video_id}</code>
                {data.is_hidden && (
                  <span className="bg-grey-mist px-3 py-1 text-body-sm">
                    已隐藏：{data.hidden_reason || "没写原因"}（{formatTime(data.hidden_at)}）
                  </span>
                )}
                <span className="ml-auto">
                  {data.is_hidden ? (
                    <MemphisButton
                      variant="secondary"
                      disabled={unhide.isPending}
                      onClick={() => unhide.mutate(data.id)}
                    >
                      {unhide.isPending ? "处理中…" : "取消隐藏"}
                    </MemphisButton>
                  ) : (
                    <MemphisButton
                      variant="danger"
                      disabled={hide.isPending}
                      onClick={() => hide.mutate({ id: data.id, reason: "在作品详情页隐藏" })}
                    >
                      {hide.isPending ? "处理中…" : "隐藏"}
                    </MemphisButton>
                  )}
                </span>
              </div>

              <h1 className="font-display text-display-lg">{data.title || "（没有标题）"}</h1>

              <dl className="grid gap-x-6 gap-y-2 text-body-md sm:grid-cols-2">
                <Field label="作者">
                  {creator.data ? (
                    <Link to="/creators" className="text-electric-blue">
                      {creator.data.name || "（没有昵称）"}
                    </Link>
                  ) : (
                    // 作者表还没到 / 读不到：说清楚是"没读到"，不是"这条没有作者"
                    `未读到（creator_id=${String(data.creator_id ?? "无")}）`
                  )}
                </Field>
                <Field label="发布">{formatTime(data.published_at)}</Field>
                <Field label="时长">{formatDuration(data.duration_seconds)}</Field>
                <Field label="播放">{formatCount(data.view_count)}</Field>
                <Field label="点赞">{formatCount(data.like_count)}</Field>
                <Field label="评论">{formatCount(data.comment_count)}</Field>
                <Field label="收藏/分享">{formatCount(data.share_count)}</Field>
                <Field label="采集于">{formatTime(data.created_at)}</Field>
                <Field label="媒体文件">{data.media_path || "（没有落地文件）"}</Field>
                <Field label="媒体来源">{data.media_source || "（没记录走了哪条路）"}</Field>
              </dl>

              {/* 计划里 V2.1 才有播放器；V2.0 这一页只有文件路径。写明白，别让人对着空白找播放器。 */}
              <p className="text-body-sm">
                这一版没有内嵌播放器：只给媒体文件的相对路径（相对 `data/`）。 播放与字幕时间轴是
                V2.1 的事。
              </p>

              {data.description && (
                <div>
                  <h2 className="font-heading text-h3 font-bold">简介</h2>
                  {/* 简介是外部输入：React 转义，pre-wrap 保住原排版 */}
                  <p className="whitespace-pre-wrap text-body-md">{data.description}</p>
                </div>
              )}
            </HardShadowCard>

            <section aria-label="口播稿">
              <h2 className="font-heading text-h3 font-bold">口播稿</h2>
              <QueryState gate={transcript} subject="口播稿">
                {(body) => (
                  <div className="mt-3">
                    {body === null ? (
                      <HardShadowCard>
                        {/* 后端对"还没有稿子"回 404，这一页把它当正常态：
                            转写是采集之后的独立一步，没做≠出错。 */}
                        <p>
                          这条还没有口播稿。转写是采集之后的独立一步（字幕优先、缺字幕才走 ASR），
                          没做不是出错 —— 想补就发一次 <code>postprocess</code> 任务。
                        </p>
                      </HardShadowCard>
                    ) : (
                      <HardShadowCard className="flex flex-col gap-3">
                        <p className="flex flex-wrap gap-3 text-mono-sm">
                          <span>引擎 {body.engine}</span>
                          <span>语言 {body.language || "未标注"}</span>
                          <span>{formatCount(body.char_count)} 字</span>
                          <span>{formatCount(body.sentence_count)} 句</span>
                        </p>
                        <TranscriptReference
                          summary={body.content_summary}
                          points={body.key_points}
                          method={body.summary_method}
                        />
                        <p className="whitespace-pre-wrap text-body-md">{body.text}</p>
                      </HardShadowCard>
                    )}
                  </div>
                )}
              </QueryState>
            </section>
          </>
        )}
      </QueryState>
    </PageShell>
  );
}

const SUMMARY_METHOD_LABEL: Record<string, string> = {
  "local-extractive": "本地抽取式 · 只搬运原文片段，不是结论",
  "v1-imported": "V1 库搬来 · 生产者没有记录，当参考用",
};

/**
 * 摘要 + 候选要点（ADR-0015）。两个字段都可空：空就整块不渲染，不放"（无）"占位 ——
 * 这一页已经有一句"没做≠出错"说稿子了，再摆一个空盒子只会多一个可读的假信号。
 *
 * 那行来源标签是这块的存在理由：本地抽取式（≤600 字的原文片段）与 V1 搬来的那份
 * （实测最长 2982 字的整篇改写）**长得一模一样**，能信的程度却差一档。
 * 库里那一列没这个信息就分不出来，所以端点把 `summary_method` 一起交出。
 */
function TranscriptReference({
  summary,
  points,
  method,
}: {
  summary: string | null;
  points: string | null;
  method: string | null;
}): JSX.Element | null {
  const lines = (points ?? "")
    .split("\n")
    .map((line) => line.replace(/^[-•]\s*/, "").trim())
    .filter(Boolean);
  if (!summary && lines.length === 0) return null;

  return (
    <section className="flex flex-col gap-2 border-t-2 border-dashed border-ink-black pt-3">
      <p className="text-mono-sm">
        {method ? (SUMMARY_METHOD_LABEL[method] ?? method) : "参考材料"}
      </p>
      {summary && <p className="whitespace-pre-wrap text-body-md">{summary}</p>}
      {lines.length > 0 && (
        <ul className="flex list-disc flex-col gap-1 pl-5 text-body-md">
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <div className="flex gap-2">
      <dt className="text-body-sm opacity-70">{label}</dt>
      <dd className="m-0 min-w-0 break-words">
        <code className="text-mono-sm">{children}</code>
      </dd>
    </div>
  );
}
