import type { JSX } from "react";

import { downloadUrl, type Query } from "@/api/client";
import { EXPORT_FORMATS, type ExportEntity } from "@/lib/export";

/**
 * 导出链接（CSV / JSON 两个，`/api/export`）。
 *
 * 为什么是 `<a>` 而不是按钮：这件事的语义就是"拿一份文件"，读屏与键盘用户的预期是链接
 * （状态栏能提前看到目标地址），而后端已经带了 `Content-Disposition: attachment` ——
 * 用 fetch + blob 那一套只是多写一段会吞掉浏览器进度与文件名的代码。
 * 样式复用 `memphis-btn` 类而不是另画一套，是为了与旁边的按钮同一份设计令牌。
 *
 * `query` 必须是调用方**当前已经应用的筛选**：这个链接承诺的是"屏幕上这一列"。
 * 少带一个条件不会报错，只会给出一份看起来完整、其实口径不同的文件
 * （`AGENTS.md` §1.3 在前端的形状）。
 */
export function ExportLinks({
  entity,
  query,
}: {
  entity: ExportEntity;
  query?: Query;
}): JSX.Element {
  return (
    <span className="flex gap-2">
      {EXPORT_FORMATS.map((format) => (
        <a
          key={format}
          className="memphis-btn memphis-btn--secondary"
          href={downloadUrl("/export", { entity, format, ...query })}
        >
          导出 {format.toUpperCase()}
        </a>
      ))}
    </span>
  );
}
