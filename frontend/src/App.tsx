import type { JSX } from "react";
import { Route, Routes } from "react-router-dom";

import { Layout } from "@/components/shared/Layout";
import { PageShell } from "@/components/shared/PageShell";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { TokenSheet } from "@/dev/TokenSheet";
import { Preflight } from "@/pages/Preflight";
import { Settings } from "@/pages/Settings";
import type { PatternName } from "@/lib/patterns";

/** Task 12 / 13 未开工的路由渲染这一块，而不是空白页。
 *
 * 空白页会被读成"这个平台没有数据"；写清楚"这一页要等哪个任务"才是真话。
 * Task 12-13 落地时**逐个删掉**这里的条目：留着就是死代码。 */
function PageNotBuilt({
  pattern,
  label,
  task,
}: {
  pattern: PatternName;
  label: string;
  task: string;
}): JSX.Element {
  return (
    <PageShell pattern={pattern}>
      <HardShadowCard>
        <h1>{label}</h1>
        <p className="text-body-md">
          这一页还没开工（<code>{task}</code>）。API 层、SSE 与 stores 已经可用： 见{" "}
          <code>src/api/hooks/</code> 与 <code>src/events/useTaskEvents.ts</code>。
        </p>
      </HardShadowCard>
    </PageShell>
  );
}

export default function App(): JSX.Element {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<PageNotBuilt pattern="dots" label="总览" task="Task 12" />} />
        <Route
          path="feed"
          element={<PageNotBuilt pattern="stripes" label="作品流" task="Task 12" />}
        />
        <Route
          path="video/:id"
          element={<PageNotBuilt pattern="waves" label="作品详情" task="Task 13" />}
        />
        <Route
          path="creators"
          element={<PageNotBuilt pattern="confetti" label="博主" task="Task 13" />}
        />
        <Route
          path="tasks"
          element={<PageNotBuilt pattern="confetti" label="任务" task="Task 13" />}
        />
        <Route path="settings" element={<Settings />} />
        <Route path="preflight" element={<Preflight />} />
        <Route path="tokens" element={<TokenSheet />} />
        <Route
          path="*"
          element={<PageNotBuilt pattern="dots" label="没有这条路由" task="路由表" />}
        />
      </Route>
    </Routes>
  );
}
