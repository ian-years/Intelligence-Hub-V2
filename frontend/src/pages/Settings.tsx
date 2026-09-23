import { useMemo, useState } from "react";
import type { JSX, ReactNode } from "react";

import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PageShell } from "@/components/shared/PageShell";
import { ApiError } from "@/api/client";
import {
  usePlatformConfig,
  usePlatforms,
  usePlatformSchema,
  useUpdatePlatformConfig,
} from "@/api/hooks/useConfig";
import { coerceValue, mergeDraft, planForm, type FormField } from "@/lib/schema-form";
import { cn } from "@/lib/utils";

/**
 * 平台配置页。
 *
 * 表单**不是手写的**：字段、顺序、说明、哪些不许渲染，全部来自
 * `/api/platforms/{name}/schema`（`docs/specs/config-schema.md §4`）。
 * 后端 `config.py` 里给字段加的 `Field(description=...)` 是这里唯一的文案来源 ——
 * 那正是 ADR-0012 把"活真相"从 `platforms.yaml` 的注释搬进 schema 的原因。
 *
 * 两条不许含糊的地方：
 * 1. `ui:hidden` 的字段不渲染（后端没有读取路径，渲染出来就是给用户一个能存、
 *    能回显、什么都不做的框），但**必须原样送回** `PUT`：那个端点收整份配置。
 * 2. 保存失败要留着草稿显示原因，不能把表单刷回初值 —— 用户会以为是自己没填。
 */
export function Settings(): JSX.Element {
  const platforms = usePlatforms();
  const [selected, setSelected] = useState("");

  // "选哪个平台"默认值是**推导**出来的，不是 effect 里补写进 state 的：
  // 后者每次渲染都要多走一轮级联（react-hooks 规则直接拒），而且清单为空时会写成 ""。
  const list = platforms.data?.platforms ?? [];
  const active = selected !== "" ? selected : (list[0]?.name ?? "");

  return (
    <PageShell pattern="checker" className="mx-auto flex max-w-[1100px] flex-col gap-6">
      <header>
        <h1 className="font-display text-display-md">设置</h1>
        <p className="text-body-md">
          改完即写盘并热加载（配置层不监听文件变化）。表单是按后端的 JSON Schema 渲染的，
          所以这里看到的字段就是它真的有人读。
        </p>
      </header>

      {platforms.isPaused && (
        <Notice>平台清单的补发被挂起（窗口不在前台），回到前台会自动继续。</Notice>
      )}
      {platforms.isError && (
        <Notice tone="danger">读不到平台清单：{detailOf(platforms.error)}</Notice>
      )}
      {platforms.isPending && !platforms.isPaused && !platforms.isError && <Notice>读取中…</Notice>}

      <div className="flex flex-wrap gap-3">
        {list.map((platform) => (
          <MemphisButton
            key={platform.name}
            variant={active === platform.name ? "primary" : "secondary"}
            onClick={() => setSelected(platform.name)}
          >
            {platform.display_name}
          </MemphisButton>
        ))}
      </div>

      {/* key=平台名：换平台时整棵表单重挂载，草稿与保存结果自然清空 ——
          比在 effect 里 setDraft({}) 少一轮渲染，也不会漏掉某个状态。 */}
      {active !== "" && <PlatformForm key={active} platform={active} />}
    </PageShell>
  );
}

function PlatformForm({ platform }: { platform: string }): JSX.Element {
  const schema = usePlatformSchema(platform);
  const query = usePlatformConfig(platform);
  const save = useUpdatePlatformConfig(platform);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [openAdvanced, setOpenAdvanced] = useState(false);

  const base = query.data?.config ?? null;
  const plan = useMemo(
    () => (schema.data && base ? planForm(schema.data, mergeDraft(base, draft)) : null),
    [schema.data, base, draft],
  );

  if (query.isError || schema.isError) {
    return (
      <Notice tone="danger">
        读不到 {platform} 的配置：{detailOf(query.error ?? schema.error)}
      </Notice>
    );
  }
  if (query.isPaused || schema.isPaused) {
    return <Notice>补发被挂起（窗口不在前台），回到前台会自动继续。</Notice>;
  }
  if (!plan || !base) {
    return <Notice>读取中…（表单要等 schema 与当前配置都到位才画得出）</Notice>;
  }

  const dirty = Object.keys(draft).length > 0;

  return (
    <HardShadowCard>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <PlatformBadge platform={platform} label={platform} />
        <span className="text-body-sm">
          这份表单由 <code>/api/platforms/{platform}/schema</code> 生成
        </span>
      </div>

      {plan.hidden.length > 0 && (
        <p className="mt-3 memphis-border border-ink-black bg-grey-mist p-3 text-body-sm">
          有 {plan.hidden.length} 项后端不实现，已不显示（原因写在每一项的说明里， 账在{" "}
          <code>docs/adr/0012</code>）。它们仍会原样送回保存请求 —— 少带一项
          就是让后端用默认值覆盖你机器上原有的值。
        </p>
      )}

      <div className="mt-4 flex flex-col gap-5">
        {plan.groups.map((group) => (
          <section key={group.key}>
            {group.key !== "main" && (
              <button
                type="button"
                className="flex w-full items-center justify-between border-0 bg-transparent p-0 text-left font-heading text-h3 font-bold"
                onClick={() => setOpenAdvanced((prev) => !prev)}
                aria-expanded={openAdvanced || !group.advanced}
              >
                <span>
                  {group.advanced ? "高级设置" : group.title}
                  {group.advanced ? "（默认折叠）" : ""}
                </span>
                <span aria-hidden>{group.advanced ? (openAdvanced ? "«" : "»") : ""}</span>
              </button>
            )}
            {(!group.advanced || openAdvanced) && (
              <ul className="mt-3 flex list-none flex-col gap-4 p-0">
                {group.fields.map((field) => (
                  <li key={field.path}>
                    <Field
                      field={field}
                      onChange={(raw) =>
                        setDraft((prev) => ({ ...prev, [field.path]: coerceValue(field, raw) }))
                      }
                    />
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>

      <div className="mt-6 flex flex-wrap items-center gap-4">
        <MemphisButton
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate(mergeDraft(base, draft))}
        >
          {save.isPending ? "保存中…" : "保存"}
        </MemphisButton>
        {!dirty && <span className="text-body-sm">没有改动</span>}
        {dirty && (
          <span className="text-body-sm">有 {Object.keys(draft).length} 处未保存的改动</span>
        )}
      </div>

      {save.isError && (
        <p className="mt-3 memphis-border border-ink-black bg-coral-red p-3 text-body-md">
          后端拒了，表单保持你填的样子：{detailOf(save.error)}
        </p>
      )}
      {save.isSuccess && (
        <p className="mt-3 memphis-border border-ink-black bg-mint-green p-3 text-body-md">
          已写盘并热加载。
          {(save.data.changed_fields ?? []).length > 0
            ? `变更字段：${(save.data.changed_fields ?? []).join("、")}。`
            : " 没有字段实际变化。"}
          {save.data.requires_restart ? " 其中有需要重启服务才生效的项 —— 现在重启。" : ""}
        </p>
      )}
    </HardShadowCard>
  );
}

function Field({
  field,
  onChange,
}: {
  field: FormField;
  onChange: (raw: unknown) => void;
}): JSX.Element {
  // `undefined` = 配置里根本没有这个键 → 显示后端默认值。
  // `null` = 用户把它清空了（或本来就是 None）→ 显示空框。
  // 两者混成一句 `value ?? default` 的话，"清空数字框"会立刻弹回 30，
  // 用户接着打字就得到 3045（2026-09-23 用例抓到的正是这个）。
  const shown = field.value === undefined ? field.default : field.value;
  const id = `f-${field.path.replaceAll(".", "-")}`;
  return (
    <label className="flex flex-col gap-1" htmlFor={id}>
      <span className="font-heading text-body-md font-bold">{field.title}</span>
      {field.description && (
        <span className="text-body-sm text-ink-black/70">{field.description}</span>
      )}
      {field.kind === "boolean" ? (
        <button
          type="button"
          role="switch"
          id={id}
          aria-checked={shown === true}
          className={cn("memphis-switch self-start")}
          data-on={String(shown === true)}
          onClick={() => onChange(!(shown === true))}
        >
          <span className="sr-only">{shown === true ? "开" : "关"}</span>
        </button>
      ) : field.kind === "select" ? (
        <select
          id={id}
          className="memphis-input"
          value={String(shown ?? "")}
          onChange={(event) => onChange(event.target.value)}
        >
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      ) : (
        <input
          id={id}
          className="memphis-input"
          type={field.kind === "number" ? "number" : "text"}
          {...(field.minimum === undefined ? {} : { min: field.minimum })}
          {...(field.maximum === undefined ? {} : { max: field.maximum })}
          value={shown === null || shown === undefined ? "" : String(shown)}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      <span className="text-mono-sm text-ink-black/60">
        {field.path}
        {field.default === null ? "" : `（默认 ${String(field.default)}）`}
      </span>
    </label>
  );
}

function detailOf(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

function Notice({ children, tone }: { children: ReactNode; tone?: "danger" }): JSX.Element {
  return (
    <p
      className={cn(
        "memphis-border border-ink-black p-3 text-body-md",
        tone === "danger" ? "bg-coral-red" : "bg-paper-cream",
      )}
    >
      {children}
    </p>
  );
}
