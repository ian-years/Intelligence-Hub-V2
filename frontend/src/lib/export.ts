/** `/api/export` 的取值清单：前端这一头**唯一**的一份。
 *
 * 为什么不放在 `ExportLinks.tsx` 里顺手导出：那条 react-refresh 规则会警
 * （组件文件混导出常量会让 Fast Refresh 退化成整页重载），而且这些常量本来就不是视图。
 * `src/api/schema.spec.ts` 逐字比对它们与 OpenAPI 快照里的 enum ——
 * 后端删一个取值而界面还留着、或界面无视契约自己发一个，都会在那里红。
 */
export const EXPORT_ENTITIES = ["videos", "creators"] as const;
export const EXPORT_FORMATS = ["csv", "json"] as const;

export type ExportEntity = (typeof EXPORT_ENTITIES)[number];
export type ExportFormat = (typeof EXPORT_FORMATS)[number];
