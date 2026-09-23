import type { JSX, ReactNode } from "react";

import { ApiError } from "@/api/client";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";

/** 一个查询的四态里界面要问的那几个字段。
 *
 * 手写这个窄接口而不是 `UseQueryResult<T>`：一是让"查询 + 别的旗标"的组合都能进来，
 * 二是**强制**这一页只依赖这四态判定，不会顺手又去读 `isFetchedAfterMount`
 * 之类的字段把时序绑死。
 *
 * 注意 `enabled: false` 的查询在这里会一直停在"读取中"（react-query 对禁用查询
 * 报的是 `isPending: true` 且永远没有数据）。那种查询要由页面自己挡在门外，
 * 别让 `QueryState` 替它编一句"读取中"。 */
export interface QueryGate<T = unknown> {
  isPaused: boolean;
  isError: boolean;
  error: Error | null;
  data: T | undefined;
}

/**
 * "读数据的界面"那四种情况的唯一出口。
 *
 * 四条纪律，全部来自 2026-09-23 在真实页面上量到的那一串（`docs/lessons.md` 经验 36）：
 * 1. **`isPaused` 与"还没有数据"必须分开说**：窗口不在前台时 react-query 挂起补发，
 *    `isPending` 会永远为真，只写"读取中…"就等于承诺"马上就出来了"。
 * 2. **读不到要给出原因原文**（`ApiError.detail`），不能空着 —— 空列表长得跟"没有数据"一样。
 * 3. **还没有结论时不许显示任何结论**（连骨架都不给状态色）。
 * 4. 四种情况**互斥**：任何一种都不许让另一种的文案露出来。
 *
 * `Preflight` 与 `Sidebar` 各有一份自己的文案（它们要说的比"读不到"更具体，
 * 而且用例逐条钉着那些句子）；这一份服务的是列表 / 详情那几页。
 */
export function QueryState<T>({
  gate,
  subject,
  children,
}: {
  gate: QueryGate<T>;
  /** 界面要说的那件东西："作品流"、"最近任务"…… 出现在三种非结果态的句子里。 */
  subject: string;
  children: (data: T) => ReactNode;
}): JSX.Element {
  if (gate.isPaused) {
    return (
      <HardShadowCard>
        {/* 文案不嵌 <strong> 之类的子节点：testing-library 的 getByText 只看节点自己的
            直接文本，被拆开的句子问不出来 —— 而"这一栏能不能被用例钉住"就是要紧的事。 */}
        <p>
          {subject}的补发被挂起了：窗口不在前台时 react-query 会暂停重试，回到前台自动继续。
          这一栏不是「{subject}没有问题」。
        </p>
      </HardShadowCard>
    );
  }

  if (gate.isError) {
    return (
      <HardShadowCard className="bg-coral-red">
        <h2>读不到{subject}</h2>
        <p className="break-words text-body-md">{detailOf(gate.error)}</p>
        <p className="text-body-sm">
          这一栏宁可空着也不给结论：把"读不下去"显示成"没有内容"，是界面上最难被发现的一种假绿。
        </p>
      </HardShadowCard>
    );
  }

  if (gate.data === undefined) {
    return (
      <HardShadowCard>
        <span className="flex items-center gap-3">
          <span aria-label="正在读取" className="memphis-loading">
            <i />
            <i />
            <i />
          </span>
          {subject}读取中… —— 还没有结论，这里刻意不显示任何状态
        </span>
      </HardShadowCard>
    );
  }

  return <>{children(gate.data)}</>;
}

/** 错误原文：`ApiError` 的 detail 是后端写清楚的那句话，比 `message` 有用。
 *  不导出：这个文件只该导出组件（react-refresh 的判据），而且现在没有别的读者。 */
function detailOf(error: Error | null | undefined): string {
  if (!error) return "（没有给出原因）";
  return error instanceof ApiError ? error.detail : error.message;
}
