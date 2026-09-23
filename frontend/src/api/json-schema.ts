/** 后端两处端点直接吐 Pydantic 的 JSON Schema（平台配置与任务参数），
 * 结构一样，所以类型只声明一次。
 *
 * 只声明到**表单渲染器必须看懂**的那几个键：`ui:hidden` / `ui:advanced` 是契约
 * （`docs/specs/config-schema.md §4`、ADR-0012），其余按 JSON Schema 原样留着。 */
export interface PropertySchema {
  title?: string;
  description?: string;
  type?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  "ui:hidden"?: boolean;
  "ui:advanced"?: boolean;
  allOf?: { $ref?: string }[];
  anyOf?: PropertySchema[];
  oneOf?: PropertySchema[];
  minimum?: number;
  maximum?: number;
  $ref?: string;
  properties?: Record<string, PropertySchema>;
}

export interface ObjectSchema {
  title?: string;
  /** Pydantic 的模型 schema 一直给 `"object"`。任务页用它判"这像不像一份 JSON Schema"：
   *  一个信封形状（`{name, kind, params_schema}`）既没有 `type` 也没有 `properties`，
   *  认不出来就该拒绝发起，而不是把"读不到必填"当成"没有必填"。 */
  type?: string;
  /** 顶层字段顺序（`DouyinConfig` 的 `json_schema_extra` 写的是 `["enabled"]`）。 */
  "ui:order"?: string[];
  properties?: Record<string, PropertySchema>;
  $defs?: Record<string, PropertySchema>;
  required?: string[];
}

/** `$ref` 解一层（Pydantic 的嵌套模型都在 `$defs` 里）。解不到就原样返回 null，
 * 由调用方决定"渲染不出来"要显示什么 —— 静默跳过会让一个字段凭空消失。 */
export function resolveRef(
  schema: ObjectSchema,
  property: PropertySchema,
): { name: string; target: PropertySchema } | null {
  const ref = property.$ref ?? property.allOf?.[0]?.$ref;
  if (!ref || !ref.startsWith("#/$defs/")) return null;
  const name = ref.slice("#/$defs/".length);
  const target = schema.$defs?.[name];
  return target ? { name, target } : null;
}
