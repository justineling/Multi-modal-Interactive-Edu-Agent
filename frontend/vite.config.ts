import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * Vite 配置。
 *
 * 两个关键点：
 * 1. `@` alias 必须与 tsconfig.json 的 `paths` 完全一致，否则 `tsc` 通过但运行时 404。
 * 2. `/api` 代理到后端 8000 端口。**SSE 也走这条代理**：vite 的 http-proxy 基于
 *    stream 转发，不会缓冲 `text/event-stream`，因此 `POST /api/chat/stream` 的
 *    逐事件推送能原样到达浏览器（前端仍必须用 fetch + ReadableStream 读取，见 api/sse.ts）。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: false,
        // SSE 场景下不要改写响应，保留原始 text/event-stream 头
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            proxyReq.setHeader("Accept-Encoding", "identity");
          });
        },
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2022",
  },
});
