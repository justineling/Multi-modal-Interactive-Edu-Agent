import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import type { ProxyOptions } from "vite";
import { defineConfig } from "vite";

/**
 * `/api` 代理到后端 8000。
 *
 * **SSE 也走这条代理**：vite 的 http-proxy 基于 stream 转发，不会缓冲
 * `text/event-stream`，因此 `POST /api/chat/stream` 的逐事件推送能原样到达浏览器
 * （前端仍必须用 fetch + ReadableStream 读取，见 api/sse.ts）。
 *
 * `server` 与 `preview` 必须共用同一份配置：PWA 的安装验证只能在 `vite preview`
 * （生产构建 + localhost 安全上下文）下做，如果 preview 没有代理，
 * 装成桌面应用后所有接口都会 404 —— 这正是「dev 能用、装完就废」的典型陷阱。
 */
const apiProxy: Record<string, ProxyOptions> = {
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
};

/**
 * Vite 配置。
 *
 * 两个关键点：
 * 1. `@` alias 必须与 tsconfig.json 的 `paths` 完全一致，否则 `tsc` 通过但运行时 404。
 * 2. 开发与预览两个服务器都要有 `/api` 代理（见上方 `apiProxy`）。
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
    proxy: apiProxy,
  },
  preview: {
    port: 4173,
    proxy: apiProxy,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2022",
    /**
     * three 的 chunk 有 1.1 MB（gzip 320 kB），但它**只在学生真的打开 3D 画布时才下载**，
     * 不在首屏路径上。默认 500 kB 的告警阈值对「懒加载的大依赖」是噪声，
     * 每构建一次就报一次，反而容易漏掉真正需要关注的体积回归。
     */
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        /**
         * 手动分包：KaTeX + markdown 插件链占了 bundle 的一半以上，且**首屏不需要**。
         * 拆出去后主包只剩 React + 业务代码，首屏解析更快，也顺手消掉
         * 「chunk > 500kB」的告警（不拆的话每次构建都在噪声里，容易漏掉真正的告警）。
         *
         * `konva` / `three` 两包更极端（合计 ≈ 750 kB），且只被**懒加载**的 2D/3D
         * 渲染器引用。单独成块后：PPT 场景完全不下载它们，3D 的「首帧 ≤ 5s」
         * 也不必先解析几百 kB 的无关代码。
         */
        manualChunks: {
          react: ["react", "react-dom"],
          markdown: ["react-markdown", "remark-gfm", "remark-math", "rehype-katex"],
          katex: ["katex"],
          konva: ["konva", "react-konva", "gsap"],
          three: ["three", "@react-three/fiber", "@react-three/drei"],
        },
      },
    },
  },
});
