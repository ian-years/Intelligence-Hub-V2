import "@fontsource/archivo-black/400.css";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/700.css";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/jetbrains-mono/400.css";
import "./styles/globals.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter } from "react-router-dom";

import App from "./App";

const container = document.getElementById("root");
if (!container) {
  // index.html 里那个 div 是有的；走到这里只可能是构建产物被改动过。
  // 静默 return 会得到一个"白屏且控制台干净"的现场，那最难查。
  throw new Error("找不到 #root —— index.html 与产物不匹配");
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 重试一次是"后端刚重启"；更多次会把一个明确失败的请求藏成一分钟才出结果的转圈。
      retry: 1,
      staleTime: 5_000,
    },
  },
});

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <App />
      </HashRouter>
    </QueryClientProvider>
  </StrictMode>,
);
