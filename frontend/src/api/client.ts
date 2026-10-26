/**
 * REST 客户端：统一响应解包 + 类型化错误 + 全部端点。
 *
 * 三条约定（架构 §9.2）：
 * 1. 所有非流式接口返回 `{code, data, message, request_id}`，由 `unwrap()` 统一拆包；
 *    `code !== "OK"` 一律抛 `ApiError`（带 `code`，UI 用它查中文文案）。
 * 2. **SSE 不走本文件**：`POST /api/chat/stream` 见 `api/sse.ts`（EventSource 不支持 POST）。
 * 3. 端点契约由交付总监裁定，前后端各实现一份；任何一侧要改必须先同步另一方。

 * 注意：`unwrap` 里有**唯一一处** `as T` 断言 —— JSON 的形状由后端 Pydantic
 * `response_model` 保证，前端不做重复的运行时校验（否则等于把 schema 写两遍）。
 */

import { isRecord } from "@/lib/jsonPointer";
import type {
  ArtifactOut,
  ArtifactVersionOut,
  ChatRequestBody,
  ChunkOut,
  CreateWorkspaceRequest,
  DeleteResult,
  DocumentOut,
  DocumentStatusOut,
  ErrorCode,
  GetArtifactQuery,
  HealthOut,
  ListChunksQuery,
  ListMessagesQuery,
  MessageOut,
  UpdateWorkspaceRequest,
  WorkspaceOut,
} from "@/types/api";
import {
  ERROR_CODE_MESSAGES,
  isRetryableErrorCode,
  normalizeErrorCode,
} from "@/types/api";
import type { AgentTurnResponse } from "@/types/artifact";
import type { PatchRequest } from "@/types/patch";

// --------------------------------------------------------------------------- //
// 基础配置
// --------------------------------------------------------------------------- //
const DEFAULT_API_BASE = "/api";

/**
 * 解析 API 前缀。
 *
 * `import.meta.env` 在 node（vitest / 独立脚本）下可能是 undefined，
 * 因此这里用可选链 + 兜底，保证本模块被非浏览器环境导入时不会在加载期抛错。
 */
function resolveApiBase(): string {
  const env: ImportMetaEnv | undefined = import.meta.env;
  const configured = env?.VITE_API_BASE;
  if (typeof configured === "string" && configured.trim().length > 0) {
    return configured.trim().replace(/\/+$/, "");
  }
  return DEFAULT_API_BASE;
}

/** 形如 `/api`，不带尾斜杠。 */
export const API_BASE: string = resolveApiBase();

/** SSE 调试开关（`VITE_SSE_DEBUG=true` 时打印原始帧）。 */
export function isSseDebugEnabled(): boolean {
  const env: ImportMetaEnv | undefined = import.meta.env;
  return env?.VITE_SSE_DEBUG === "true";
}

// --------------------------------------------------------------------------- //
// 错误类型
// --------------------------------------------------------------------------- //
/**
 * 带错误码的 API 异常。
 *
 * `code` 是 §9.1 的字面量集合，UI 直接用 `displayMessage` 渲染，
 * 不要自己拼文案（否则同一错误在不同组件里说法不一致）。
 */
export class ApiError extends Error {
  readonly code: ErrorCode;
  readonly httpStatus: number;
  readonly requestId: string;

  constructor(code: ErrorCode, message: string, httpStatus: number, requestId: string) {
    super(message.length > 0 ? message : ERROR_CODE_MESSAGES[code]);
    this.name = "ApiError";
    this.code = code;
    this.httpStatus = httpStatus;
    this.requestId = requestId;
  }

  /** 是否值得重试（网络抖动 / 上游波动 / 可重试的契约校验失败）。 */
  get retryable(): boolean {
    return isRetryableErrorCode(this.code);
  }

  /** 面向用户的中文文案：优先后端 message，其次本地码表。 */
  get displayMessage(): string {
    return this.message.length > 0 ? this.message : ERROR_CODE_MESSAGES[this.code];
  }
}

/** 把任意异常收敛成 `ApiError`，便于组件统一处理。 */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  if (error instanceof Error) {
    return new ApiError("INTERNAL_ERROR", error.message, 0, "");
  }
  return new ApiError("INTERNAL_ERROR", "未知错误", 0, "");
}

// --------------------------------------------------------------------------- //
// 响应解包
// --------------------------------------------------------------------------- //
/**
 * §9.2 统一解包。
 *
 * - `code !== "OK"` → 抛 `ApiError`（失败态 `data` 恒为 `null`）
 * - `data` 缺失 → 抛 `ApiError`（服务端契约被破坏）
 */
function unwrap<T>(payload: unknown, httpStatus: number): T {
  if (!isRecord(payload)) {
    throw new ApiError("INTERNAL_ERROR", "响应不是合法的 JSON 对象", httpStatus, "");
  }

  const rawCode = typeof payload["code"] === "string" ? payload["code"] : "INTERNAL_ERROR";
  const message = typeof payload["message"] === "string" ? payload["message"] : "";
  const requestId = typeof payload["request_id"] === "string" ? payload["request_id"] : "";
  const code = normalizeErrorCode(rawCode);

  if (rawCode !== "OK") {
    throw new ApiError(code, message, httpStatus, requestId);
  }

  const data: unknown = payload["data"];
  if (data === null || data === undefined) {
    throw new ApiError("INTERNAL_ERROR", "响应缺少 data 字段", httpStatus, requestId);
  }

  // 唯一断言点：形状由后端 response_model 保证（见文件头注释）
  return data as T;
}

// --------------------------------------------------------------------------- //
// 请求发送
// --------------------------------------------------------------------------- //
export type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** JSON 请求体 */
  body?: unknown;
  /** multipart 请求体（与 body 互斥） */
  formData?: FormData;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const base = `${API_BASE}${normalizedPath}`;
  if (query === undefined) {
    return base;
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) {
      continue;
    }
    params.append(key, String(value));
  }
  const queryString = params.toString();
  return queryString.length > 0 ? `${base}?${queryString}` : base;
}

async function parseJsonBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.trim().length === 0) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** 底层请求：拼 URL、发请求、解包。所有端点方法都走这里。 */
async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;

  if (options.formData !== undefined) {
    // 不手动设 Content-Type：必须让浏览器带上 multipart 的 boundary
    body = options.formData;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? "GET",
      headers,
      body,
      signal: options.signal ?? null,
    });
  } catch (error) {
    // 网络层失败（后端没起 / 被 CORS 拦 / 主动 abort）—— 统一成 INTERNAL_ERROR
    const detail = error instanceof Error ? error.message : String(error);
    throw new ApiError("INTERNAL_ERROR", `网络请求失败：${detail}`, 0, "");
  }

  const payload = await parseJsonBody(response);
  if (!response.ok && payload === null) {
    throw new ApiError(
      "INTERNAL_ERROR",
      `HTTP ${response.status} ${response.statusText}`,
      response.status,
      "",
    );
  }

  return unwrap<T>(payload, response.status);
}

// --------------------------------------------------------------------------- //
// Health
// --------------------------------------------------------------------------- //
/**
 * 后端健康探测。同样走统一包装（`{code:"OK", data:{...}}`）。
 *
 * 用途：启动时确认后端就绪，并取 `demo_mode` 作为顶栏黄标的初始值
 * （SSE 的 `turn_start` 也会带 `demo_mode`，两处一致即说明状态没漂移）。
 */
export function getHealth(signal?: AbortSignal): Promise<HealthOut> {
  return request<HealthOut>("/health", { signal });
}

// --------------------------------------------------------------------------- //
// Workspaces
// --------------------------------------------------------------------------- //
export function listWorkspaces(signal?: AbortSignal): Promise<WorkspaceOut[]> {
  return request<WorkspaceOut[]>("/workspaces", { signal });
}

export function createWorkspace(
  payload: CreateWorkspaceRequest,
  signal?: AbortSignal,
): Promise<WorkspaceOut> {
  return request<WorkspaceOut>("/workspaces", { method: "POST", body: payload, signal });
}

export function getWorkspace(workspaceId: string, signal?: AbortSignal): Promise<WorkspaceOut> {
  return request<WorkspaceOut>(`/workspaces/${encodeURIComponent(workspaceId)}`, { signal });
}

export function updateWorkspace(
  workspaceId: string,
  payload: UpdateWorkspaceRequest,
  signal?: AbortSignal,
): Promise<WorkspaceOut> {
  return request<WorkspaceOut>(`/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "PATCH",
    body: payload,
    signal,
  });
}

export function deleteWorkspace(workspaceId: string, signal?: AbortSignal): Promise<DeleteResult> {
  return request<DeleteResult>(`/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "DELETE",
    signal,
  });
}

// --------------------------------------------------------------------------- //
// Documents
// --------------------------------------------------------------------------- //
/** 上传 PDF（multipart）。后端立即返回 202 + `status: "pending"`，随后轮询 status。 */
export function uploadDocument(
  workspaceId: string,
  file: File,
  signal?: AbortSignal,
): Promise<DocumentOut> {
  const formData = new FormData();
  formData.append("file", file, file.name);
  return request<DocumentOut>(`/workspaces/${encodeURIComponent(workspaceId)}/documents`, {
    method: "POST",
    formData,
    signal,
  });
}

export function listDocuments(workspaceId: string, signal?: AbortSignal): Promise<DocumentOut[]> {
  return request<DocumentOut[]>(`/workspaces/${encodeURIComponent(workspaceId)}/documents`, {
    signal,
  });
}

/** 含 `toc_tree` 的完整文档信息。 */
export function getDocument(documentId: string, signal?: AbortSignal): Promise<DocumentOut> {
  return request<DocumentOut>(`/documents/${encodeURIComponent(documentId)}`, { signal });
}

/** 轻量轮询接口（前端每 1.5s 一次）。 */
export function getDocumentStatus(
  documentId: string,
  signal?: AbortSignal,
): Promise<DocumentStatusOut> {
  return request<DocumentStatusOut>(`/documents/${encodeURIComponent(documentId)}/status`, {
    signal,
  });
}

export function cancelDocument(documentId: string, signal?: AbortSignal): Promise<DocumentOut> {
  return request<DocumentOut>(`/documents/${encodeURIComponent(documentId)}/cancel`, {
    method: "POST",
    signal,
  });
}

export function deleteDocument(documentId: string, signal?: AbortSignal): Promise<DeleteResult> {
  return request<DeleteResult>(`/documents/${encodeURIComponent(documentId)}`, {
    method: "DELETE",
    signal,
  });
}

export function listChunks(
  documentId: string,
  query: ListChunksQuery = {},
  signal?: AbortSignal,
): Promise<ChunkOut[]> {
  return request<ChunkOut[]>(`/documents/${encodeURIComponent(documentId)}/chunks`, {
    query: { section_code: query.section_code, limit: query.limit },
    signal,
  });
}

// --------------------------------------------------------------------------- //
// Messages
// --------------------------------------------------------------------------- //
export function listMessages(
  workspaceId: string,
  query: ListMessagesQuery = {},
  signal?: AbortSignal,
): Promise<MessageOut[]> {
  return request<MessageOut[]>(`/workspaces/${encodeURIComponent(workspaceId)}/messages`, {
    query: { limit: query.limit },
    signal,
  });
}

// --------------------------------------------------------------------------- //
// Chat（非流式）
// --------------------------------------------------------------------------- //
/**
 * 非流式对话。SSE 版本见 `api/sse.ts::iterateChatStream`。
 *
 * 只有需要「一次拿完整结果」的场景才用它（例如导出、自动化测试）；
 * 交互主链路必须走 SSE，否则 TTFT 无法满足验收。
 */
export function chatOnce(
  payload: ChatRequestBody,
  signal?: AbortSignal,
): Promise<AgentTurnResponse> {
  return request<AgentTurnResponse>("/chat", { method: "POST", body: payload, signal });
}

// --------------------------------------------------------------------------- //
// Artifacts
// --------------------------------------------------------------------------- //
export function listArtifacts(workspaceId: string, signal?: AbortSignal): Promise<ArtifactOut[]> {
  return request<ArtifactOut[]>(`/workspaces/${encodeURIComponent(workspaceId)}/artifacts`, {
    signal,
  });
}

/** `version` 为空时取当前版本；指定则返回该历史版本的只读快照。 */
export function getArtifact(
  artifactId: string,
  query: GetArtifactQuery = {},
  signal?: AbortSignal,
): Promise<ArtifactOut> {
  return request<ArtifactOut>(`/artifacts/${encodeURIComponent(artifactId)}`, {
    query: { version: query.version },
    signal,
  });
}

export function listArtifactVersions(
  artifactId: string,
  signal?: AbortSignal,
): Promise<ArtifactVersionOut[]> {
  return request<ArtifactVersionOut[]>(`/artifacts/${encodeURIComponent(artifactId)}/versions`, {
    signal,
  });
}

/**
 * 应用一批 patch（乐观锁：`base_version` 不匹配返回 409 `PATCH_BASE_VERSION_CONFLICT`）。
 *
 * 返回的是**服务端算出的新快照**。前端仍会用本地 patch + 指纹自检做乐观更新，
 * 两者结果不一致时以服务端为准并刷新整棵 AST。
 */
export function patchArtifact(payload: PatchRequest, signal?: AbortSignal): Promise<ArtifactOut> {
  return request<ArtifactOut>(`/artifacts/${encodeURIComponent(payload.artifact_id)}/patch`, {
    method: "POST",
    body: payload,
    signal,
  });
}

export function deleteArtifact(artifactId: string, signal?: AbortSignal): Promise<DeleteResult> {
  return request<DeleteResult>(`/artifacts/${encodeURIComponent(artifactId)}`, {
    method: "DELETE",
    signal,
  });
}

/** 便于测试与调试：暴露解包函数（不参与业务调用）。 */
export const __testing = { unwrap, buildUrl };
