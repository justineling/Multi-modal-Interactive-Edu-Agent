/**
 * REST 层契约：统一响应包装、错误码、各资源的出参 / 入参。
 *
 * 三条约定（架构 §9.1 / §9.2）：
 * 1. 所有非流式接口都包成 `{code, data, message, request_id}`；**SSE 不走包装**。
 * 2. `code != "OK"` 时 `data` 为 `null`；前端以 HTTP 判失败、以 `code` 判文案。
 * 3. `ErrorCode` 是前后端唯一共享的字面量集合，本文件是前端侧的镜像；
 *    后端 `errors.py::ErrorCode` 增删码时必须同步改这里（否则文案会退化成兜底值）。
 */

import type {
  ArtifactSpec,
  ArtifactType,
  Citation,
  KnowledgeScope,
} from "@/types/artifact";
import type { Anchor, PatchOp } from "@/types/patch";

// --------------------------------------------------------------------------- //
// 1. 统一响应与错误码
// --------------------------------------------------------------------------- //
/**
 * §9.1 错误码全集（镜像后端 `errors.py::ErrorCode`）。
 *
 * `REQUEST_INVALID` 是后端 A1-A3 新增的 400 码（原表未列），已由交付总监批准，
 * 前端必须收录，否则请求体不合法时会退化成「服务内部错误」文案。
 */
export type ErrorCode =
  | "OK"
  | "WORKSPACE_NOT_FOUND"
  | "DOCUMENT_NOT_FOUND"
  | "ARTIFACT_NOT_FOUND"
  | "UNSUPPORTED_FILE_TYPE"
  | "PDF_ENCRYPTED"
  | "PDF_EMPTY"
  | "PDF_NO_TEXT_LAYER"
  | "PDF_PARSE_FAILED"
  | "PARSE_TASK_CANCELLED"
  | "CONTRACT_VALIDATION_FAILED"
  | "PATCH_PATH_INVALID"
  | "PATCH_BASE_VERSION_CONFLICT"
  | "RETRIEVAL_EMPTY"
  | "LLM_UPSTREAM_ERROR"
  | "LLM_TIMEOUT"
  | "REQUEST_INVALID"
  | "INTERNAL_ERROR";

/** 运行时枚举（顺序与后端一致，便于人工核对）。 */
export const ERROR_CODES: readonly ErrorCode[] = [
  "OK",
  "WORKSPACE_NOT_FOUND",
  "DOCUMENT_NOT_FOUND",
  "ARTIFACT_NOT_FOUND",
  "UNSUPPORTED_FILE_TYPE",
  "PDF_ENCRYPTED",
  "PDF_EMPTY",
  "PDF_NO_TEXT_LAYER",
  "PDF_PARSE_FAILED",
  "PARSE_TASK_CANCELLED",
  "CONTRACT_VALIDATION_FAILED",
  "PATCH_PATH_INVALID",
  "PATCH_BASE_VERSION_CONFLICT",
  "RETRIEVAL_EMPTY",
  "LLM_UPSTREAM_ERROR",
  "LLM_TIMEOUT",
  "REQUEST_INVALID",
  "INTERNAL_ERROR",
];

/** 中文文案（与后端 `_MESSAGES` 逐条一致）。后端给了 message 时优先用后端的。 */
export const ERROR_CODE_MESSAGES: Record<ErrorCode, string> = {
  OK: "",
  WORKSPACE_NOT_FOUND: "工作区不存在或已删除",
  DOCUMENT_NOT_FOUND: "文档不存在",
  ARTIFACT_NOT_FOUND: "该画布产物不存在",
  UNSUPPORTED_FILE_TYPE: "仅支持 PDF 文件",
  PDF_ENCRYPTED: "该 PDF 已加密，请先解除密码保护",
  PDF_EMPTY: "该 PDF 没有可用页面",
  PDF_NO_TEXT_LAYER: "该 PDF 无文本层（疑似扫描件），暂不支持",
  PDF_PARSE_FAILED: "文档解析失败，请重试",
  PARSE_TASK_CANCELLED: "解析已取消",
  CONTRACT_VALIDATION_FAILED: "生成结果未通过契约校验，已自动重试",
  PATCH_PATH_INVALID: "锚点路径非法",
  PATCH_BASE_VERSION_CONFLICT: "画布已被更新，请刷新后重试",
  RETRIEVAL_EMPTY: "课本未覆盖该问题",
  LLM_UPSTREAM_ERROR: "模型服务返回异常",
  LLM_TIMEOUT: "模型响应超时",
  REQUEST_INVALID: "请求参数不合法",
  INTERNAL_ERROR: "服务内部错误",
};

/** 值得重试的码（网络抖动 / 上游波动 / 可重试的校验失败）。 */
export const RETRYABLE_ERROR_CODES: readonly ErrorCode[] = [
  "CONTRACT_VALIDATION_FAILED",
  "LLM_UPSTREAM_ERROR",
  "LLM_TIMEOUT",
  "INTERNAL_ERROR",
];

export function isErrorCode(value: string): value is ErrorCode {
  return ERROR_CODES.some((code) => code === value);
}

/** 把后端可能新增/未知的 code 收敛到兜底值，避免文案渲染出空字符串。 */
export function normalizeErrorCode(value: string): ErrorCode {
  return isErrorCode(value) ? value : "INTERNAL_ERROR";
}

export function errorCodeMessage(code: ErrorCode): string {
  return ERROR_CODE_MESSAGES[code];
}

export function isRetryableErrorCode(code: ErrorCode): boolean {
  return RETRYABLE_ERROR_CODES.some((item) => item === code);
}

/** §9.2 统一响应包装。`data` 在失败态恒为 `null`。 */
export interface ApiResponse<T> {
  code: string;
  data: T | null;
  message: string;
  request_id: string;
}

/** 删除类接口的统一返回（后端 `DeletedOut`，恒为 `true`）。 */
export interface DeleteResult {
  deleted: boolean;
}

/**
 * `GET /api/health` 的 data（A1 实现，同样是统一包装）。
 *
 * 前端用它做两件事：① 顶栏「演示模式」黄标的初始值；② 启动时探测后端是否就绪。
 */
export interface HealthOut {
  demo_mode: boolean;
  app_version: string;
  /** `sqlite+aiosqlite` / `postgresql+asyncpg` */
  database_backend: string;
  /** `local` / `remote` */
  embedding_mode: string;
  /** `MemoryVectorStore` / `PgVectorStore` */
  vector_store: string;
  artifact_types: ArtifactType[];
}

// --------------------------------------------------------------------------- //
// 2. Workspaces
// --------------------------------------------------------------------------- //
/**
 * 工作区。字段名与后端 `schemas/api.py::WorkspaceOut` 一致：
 * 对外契约用 `updated_at`（映射自库内 `last_active_at`），用于「刷新后恢复最近会话」排序。
 */
export interface WorkspaceOut {
  id: string;
  title: string;
  /** ISO8601 UTC 字符串 */
  created_at: string;
  /** 最近活跃时间（= 库内 `last_active_at`） */
  updated_at: string;
}

export interface CreateWorkspaceRequest {
  title: string;
}

export interface UpdateWorkspaceRequest {
  title: string;
}

// --------------------------------------------------------------------------- //
// 3. Documents
// --------------------------------------------------------------------------- //
/** 文档解析状态机（`documents.status` 列）。 */
export type DocumentStatus = "pending" | "parsing" | "ready" | "failed" | "cancelled";

export const DOCUMENT_STATUSES: readonly DocumentStatus[] = [
  "pending",
  "parsing",
  "ready",
  "failed",
  "cancelled",
];

export function isDocumentStatus(value: string): value is DocumentStatus {
  return DOCUMENT_STATUSES.some((status) => status === value);
}

/** 章节号识别模式（仅用于文档说明；后端 `DocumentOut` 未暴露该字段，见 `isPageFallbackToc`）。 */
export type ParserMode = "regex" | "page_fallback";

/**
 * 目录树节点（对应后端 `schemas/api.py::TocNodeOut`）。
 *
 * - `code` 前缀关系构成父子：`4` → `4.1` → `4.1.2`
 * - `start_page` / `end_page` 为 **1 基、闭区间**
 * - 降级模式（章节号识别失败）下 `code` 形如 `P0001`，`level` 恒为 1
 */
export interface TocNode {
  /** 章节号，如 `4` / `4.1` / `4.1.2`；降级模式下为 `P0001` 这类页组编码 */
  code: string;
  title: string;
  /** 1–4 */
  level: number;
  start_page: number;
  end_page: number;
  children: TocNode[];
}

/**
 * 降级页组编码（`P0001`）。
 *
 * 后端 `DocumentOut` 未暴露 `parser_mode`，前端用编码形状判断是否走了 page_fallback，
 * 从而给出「未能识别章节号，已按页码分组」的提示（§7.1 降级策略）。
 */
export const PAGE_FALLBACK_CODE_RE = /^P\d{4}$/;

export function isPageFallbackToc(tocTree: readonly TocNode[]): boolean {
  return tocTree.some((node) => PAGE_FALLBACK_CODE_RE.test(node.code));
}

export interface DocumentOut {
  id: string;
  workspace_id: string;
  filename: string;
  status: DocumentStatus;
  /** 0–100 */
  progress: number;
  page_count: number;
  toc_tree: TocNode[];
  error_code: string | null;
  error_message: string | null;
  created_at: string;
}

/** `GET /api/documents/{id}/status` 的轻量轮询返回（每 1.5s 一次，不带 toc）。 */
export interface DocumentStatusOut {
  status: DocumentStatus;
  progress: number;
  error_code: string | null;
  error_message: string | null;
}

/** 切片预览（`GET /api/documents/{id}/chunks`）。 */
export interface ChunkOut {
  id: string;
  document_id: string;
  /** 永不为空（P0-04①） */
  section_code: string;
  section_title: string;
  level: number;
  /** 同分时保证排序稳定 */
  chunk_index: number;
  page_start: number;
  page_end: number;
  content: string;
  char_len: number;
}

export interface ListChunksQuery {
  section_code?: string;
  limit?: number;
}

// --------------------------------------------------------------------------- //
// 4. Messages
// --------------------------------------------------------------------------- //
export type MessageRole = "user" | "assistant";

export interface MessageOut {
  id: string;
  workspace_id: string;
  role: MessageRole;
  content_markdown: string;
  knowledge_scope: KnowledgeScope | null;
  citations: Citation[];
  /** 该轮追问时的画布锚点，null 表示普通提问 */
  anchor: Anchor | null;
  artifact_id: string | null;
  artifact_version: number | null;
  /** 该轮是否运行在 mock 模式（P0-12 可追溯） */
  demo_mode: boolean;
  created_at: string;
}

export interface ListMessagesQuery {
  /** 后端默认 100，上限 500 */
  limit?: number;
}

// --------------------------------------------------------------------------- //
// 5. Artifacts
// --------------------------------------------------------------------------- //
/**
 * 画布产物（对应后端 `schemas/api.py::ArtifactOut`）。
 *
 * 字段名取 `spec`（与 SSE `artifact` 事件的 `spec` 字段一致），而非数据库列名
 * `spec_json` —— 对外契约与事件流保持同一套字段名，前端才不需要做映射。
 */
export interface ArtifactOut {
  id: string;
  workspace_id: string;
  title: string;
  artifact_type: ArtifactType;
  /** 1 起 */
  version: number;
  spec: ArtifactSpec;
  created_at: string;
  updated_at: string;
}

/**
 * 历史版本（append-only，永不覆盖/删除）。
 *
 * `patchable=false` 表示该版本已超出「最近 N 个可 patch 版本」窗口（§10 第 3 条）：
 * 仍可查看，但不能基于它继续追问，UI 据此禁用「以此版本继续」按钮。
 */
export interface ArtifactVersionOut {
  id: string;
  artifact_id: string;
  version: number;
  spec: ArtifactSpec;
  /** 产生该版本时的补丁指令；全量生成时为 `null` */
  patch_ops: PatchOp[] | null;
  origin_turn_id: string | null;
  patchable: boolean;
  created_at: string;
}

export interface GetArtifactQuery {
  /** 指定版本则返回该历史版本的只读快照 */
  version?: number;
}

// --------------------------------------------------------------------------- //
// 6. Chat
// --------------------------------------------------------------------------- //
/** 回复模式：`syllabus_strict` 只答课本内；`hybrid_extended` 允许双层（课本+拓展）。 */
export type StreamMode = "syllabus_strict" | "hybrid_extended";

export const STREAM_MODES: readonly StreamMode[] = ["syllabus_strict", "hybrid_extended"];

/**
 * `POST /api/chat/stream` 与 `POST /api/chat` 的请求体。
 *
 * 这里用 `?` 而不是 `| null` 是**有意为之**：请求体的可选字段是「可以不带」，
 * 与响应体「字段恒在、可空即 null」是两种不同语义。后端 `ChatRequest` 里
 * `artifact_type_override` / `artifact_id` / `history_limit` 都有默认值，
 * 因此省略合法；而 `history_limit` 传 `null` 反而会被 Pydantic 拒绝。
 */
export interface ChatRequestBody {
  workspace_id: string;
  /** 1–2000 字 */
  query: string;
  /** 默认 `syllabus_strict`（PRD Q2：考试不答偏优先） */
  mode: StreamMode;
  /** 节点锚点追问；不传即普通提问 */
  anchor?: Anchor;
  /** 用户显式指定形态时 100% 遵循（P0-06②） */
  artifact_type_override?: ArtifactType;
  /** 无锚点时用于「接着上一张画布继续改」 */
  artifact_id?: string;
  /** 带多少轮历史，0–50，默认 8 */
  history_limit?: number;
}
