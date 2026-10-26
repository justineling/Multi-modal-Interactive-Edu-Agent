/// <reference types="vite/client" />

/**
 * `import.meta.env` 的类型声明。
 *
 * 只声明项目实际会读的变量；`VITE_` 前缀是 Vite 的硬规则（非此前缀不会注入客户端）。
 * 全部变量都有安全默认值（见 api/client.ts），因此这里一律 `?`：
 * 环境变量属于**构建期注入**，缺失是正常状态，与后端响应字段的 `| null` 语义无关。
 */
interface ImportMetaEnv {
  /** 后端 API 前缀，默认 `/api`（由 vite 代理到 127.0.0.1:8000） */
  readonly VITE_API_BASE?: string;
  /** 浏览器标题后缀，便于区分本地/演示环境 */
  readonly VITE_APP_TITLE?: string;
  /** 设为 "true" 时在控制台打印每一帧原始 SSE（调试用） */
  readonly VITE_SSE_DEBUG?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
