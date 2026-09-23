import type { JSX } from "react";
import { Route, Routes } from "react-router-dom";

import { Layout } from "@/components/shared/Layout";
import { PageShell } from "@/components/shared/PageShell";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { TokenSheet } from "@/dev/TokenSheet";
import { Creators } from "@/pages/Creators";
import { Dashboard } from "@/pages/Dashboard";
import { Feed } from "@/pages/Feed";
import { Tasks } from "@/pages/Tasks";
import { VideoDetail } from "@/pages/VideoDetail";
import { Preflight } from "@/pages/Preflight";
import { Settings } from "@/pages/Settings";
import type { PatternName } from "@/lib/patterns";

/** 没有对应后端能力或没排期的路由渲染这一块，而不是空白页。
 *
 * 空白页会被读成"这个平台没有数据"；写清楚"这一页要等哪个任务"才是真话。
 * Task 13 落地时**逐个删掉**这里的条目：留着就是死代码。
 * `nav.ts` 的 `built` 旗标与这张路由表由 `src/app.spec.tsx` 逐条核（两边漂了就红）。 */
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
        <Route index element={<Dashboard />} />
        <Route path="feed" element={<Feed />} />
        <Route path="video/:id" element={<VideoDetail />} />
        <Route path="creators" element={<Creators />} />
        <Route path="tasks" element={<Tasks />} />
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
