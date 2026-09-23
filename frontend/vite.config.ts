/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const BACKEND = process.env.IH_BACKEND_ORIGIN ?? "http://127.0.0.1:8789";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    // 只绑回环：这个 dev server 会把 /api 与 /api/events(SSE) 代理到后端，
    // 而后端能在你已登录的浏览器里触发 CDP 桥 —— 与 AGENTS.md §1.2 同一个理由。
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: false, ws: false },
      "/openapi.json": { target: BACKEND, changeOrigin: false },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    // 覆盖率门槛与 CI 里那条 `--coverage.thresholds.*=70` 写成同一处：
    // 两边不同值就等于有一个是假的（经验 27 的同一族坑）。
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      thresholds: { lines: 70, functions: 70 },
    },
  },
});
