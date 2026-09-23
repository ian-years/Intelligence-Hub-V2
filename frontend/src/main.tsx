import "@fontsource/archivo-black/400.css";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/700.css";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/jetbrains-mono/400.css";
import "./styles/globals.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";

const container = document.getElementById("root");
if (!container) {
  // index.html 里那个 div 是有的；走到这里只可能是构建产物被改动过。
  // 静默 return 会得到一个"白屏且控制台干净"的现场，那最难查。
  throw new Error("找不到 #root —— index.html 与产物不匹配");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
