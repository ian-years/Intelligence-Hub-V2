import { resolveRef, type ObjectSchema, type PropertySchema } from "@/api/json-schema";

/**
 * 把后端的 JSON Schema 变成"这个表单要渲染什么"。
 *
 * 单独一个纯函数模块的原因：ADR-0012 之后，`ui:hidden` 的**唯一执行者**就是这里 ——
 * Python 侧的用例只能证明标记在 schema 里，证明界面不渲染它必须靠这份计划函数。
 * 混在组件里就只能靠渲染快照来测，那时候"忘了过滤"和"本来就没数据"长得一样。
 */
export type FieldKind = "boolean" | "number" | "text" | "select";

export interface FormField {
  /** 点分路径，同时是草稿里读写的位置（`rate_limit.per_minute`）。 */
  path: string;
  title: string;
  kind: FieldKind;
  value: unknown;
  default: unknown;
  description?: string;
  options?: string[];
  minimum?: number;
  maximum?: number;
}

export interface FormGroup {
  key: string;
  title: string;
  description?: string;
  /** §4 的 `ui:advanced`：默认折叠，不是不渲染。 */
  advanced: boolean;
  fields: FormField[];
}

export interface FormPlan {
  groups: FormGroup[];
  /** 被隐藏的点分路径。页面要把它变成一句"有 N 项后端不实现，已不显示"，
   * 而不是让那些字段凭空消失。 */
  hidden: string[];
}

const MAIN = "main";

export function planForm(schema: ObjectSchema, config: Record<string, unknown>): FormPlan {
  const hidden: string[] = [];
  const groups: FormGroup[] = [];
  const main: FormField[] = [];

  for (const [key, property] of orderedProperties(schema)) {
    const target = resolveRef(schema, property)?.target ?? undefined;
    const childProps = target?.properties ?? property.properties;
    const childSchema = target ?? property;

    if (property["ui:hidden"] === true) {
      // 整组隐藏时也要逐个记名：只记 `advanced` 的话，页面就不知道里面是哪几个。
      if (childProps) {
        for (const child of Object.keys(childProps)) hidden.push(`${key}.${child}`);
      } else {
        hidden.push(key);
      }
      continue;
    }

    if (childProps) {
      const fields: FormField[] = [];
      const nested: Record<string, unknown> = asRecord(readPath(config, key));
      for (const [child, childProp] of Object.entries(childProps)) {
        if (childProp["ui:hidden"] === true) {
          hidden.push(`${key}.${child}`);
          continue;
        }
        fields.push(toField(`${key}.${child}`, child, childProp, nested[child]));
      }
      // §4 的实现期约定：组内叶子全被隐藏就不渲染这个组（否则点开是空的）。
      if (fields.length > 0) {
        const groupDescription = property.description ?? childSchema.description;
        groups.push({
          key,
          title: childSchema.title ?? key,
          ...(groupDescription === undefined ? {} : { description: groupDescription }),
          advanced: property["ui:advanced"] === true,
          fields,
        });
      }
      continue;
    }

    main.push(toField(key, key, property, config[key]));
  }

  if (main.length > 0) {
    groups.unshift({ key: MAIN, title: "常规", advanced: false, fields: main });
  }
  return { groups, hidden };
}

/** `ui:order` 只决定**顶层**顺序（现在只有抖音写了 `["enabled"]`）。 */
function orderedProperties(schema: ObjectSchema): [string, PropertySchema][] {
  const properties = schema.properties ?? {};
  const entries = Object.entries(properties);
  const order = schema["ui:order"] ?? [];
  const rank = new Map(order.map((name, index) => [name, index]));
  return entries.sort(([a], [b]) => (rank.get(a) ?? order.length) - (rank.get(b) ?? order.length));
}

function toField(path: string, key: string, schema: PropertySchema, value: unknown): FormField {
  return {
    path,
    title: schema.title ?? key,
    kind: kindOf(schema),
    value,
    default: schema.default ?? null,
    ...(schema.description === undefined ? {} : { description: schema.description }),
    ...(schema.enum === undefined ? {} : { options: schema.enum.map(String) }),
    ...(schema.minimum === undefined ? {} : { minimum: schema.minimum }),
    ...(schema.maximum === undefined ? {} : { maximum: schema.maximum }),
  };
}

function kindOf(schema: PropertySchema): FieldKind {
  if (schema.enum !== undefined || schema.const !== undefined) return "select";
  const types = new Set<string>();
  if (schema.type) types.add(schema.type);
  for (const branch of schema.anyOf ?? schema.oneOf ?? []) {
    if (branch.type) types.add(branch.type);
  }
  if (schema.type === "boolean" || types.has("boolean")) return "boolean";
  if (types.has("integer") || types.has("number")) return "number";
  // 其余（含 `Path | None` 这种只有 `anyOf` 没有 `type` 的）一律文本框：
  // 认不出形状就渲染成文本框，也比**不渲染**好 —— 不渲染等于这个字段没人能改。
  return "text";
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function readPath(source: Record<string, unknown>, path: string): unknown {
  let cursor: unknown = source;
  for (const part of path.split(".")) {
    if (typeof cursor !== "object" || cursor === null) return undefined;
    cursor = (cursor as Record<string, unknown>)[part];
  }
  return cursor;
}

/** 控件交回来的原始值 → 请求体里的值。**字符串不许漏进数字框**：
 * 后端 `videos_per_creator: int` 收到 `"45"` 会被 Pydantic 拒（或更糟：安静地转换）。 */
export function coerceValue(field: FormField, raw: unknown): unknown {
  switch (field.kind) {
    case "boolean":
      return raw === true;
    case "number": {
      if (raw === "" || raw === null || raw === undefined) return null;
      const value = Number(raw);
      return Number.isFinite(value) ? value : null;
    }
    case "text": {
      if (raw === null || raw === undefined) return null;
      const text = String(raw).trim();
      return text === "" ? null : text;
    }
    case "select":
      return raw === null || raw === undefined || raw === "" ? null : String(raw);
  }
}

/**
 * 把草稿合回**整份**配置。
 *
 * `PUT /api/platforms/{name}/config` 收的是整个对象（模型是 `extra="forbid"` 的
 * `model_validate`），所以没渲染出来的字段（那些 `ui:hidden` 的）必须原样带回 ——
 * 少带一个键就是让后端拿默认值覆盖掉用户机器上原有的值。
 */
export function mergeDraft(
  base: Record<string, unknown>,
  patch: Record<string, unknown>,
): Record<string, unknown> {
  const out = structuredClone(base) as Record<string, unknown>;
  for (const [path, value] of Object.entries(patch)) {
    const parts = path.split(".");
    const last = parts.pop() as string;
    let cursor: Record<string, unknown> = out;
    for (const part of parts) {
      const next = asRecord(cursor[part]);
      cursor[part] = next;
      cursor = next;
    }
    cursor[last] = value;
  }
  return out;
}
