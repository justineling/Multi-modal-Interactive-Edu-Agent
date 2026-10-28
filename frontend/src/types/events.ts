/**
 * SSE 事件契约 —— 与后端 `backend/app/schemas/chat.py` 的 8 个事件模型逐字段对应。
 *
 * 关键约定（架构 §5.4）：
 * 1. **8 个事件类型**，`data` 全部 snake_case，字段名与后端 Pydantic 模型一字不差。
 *    注意「8 个类型」与「7 个有序」不是矛盾：`error` 不在正常流的有序序列里
 *    （见约定 2），所以有序序列长度是 7。
 * 2. 事件顺序固定：`turn_start → retrieval → modality_decision → token* → artifact? →
 *    citations → turn_end`；`error` 可插入任意位置，**之后仍会补发 `turn_end`**。
 * 3. 校验失败重试期间会发 `error{retryable:true}` 但**不中断**流，因此前端不能
 *    一遇 error 就关流。
 * 4. 无 `LLM_API_KEY` 时 `demo_mode: true`，在 `turn_start` 与 `turn_end` 双处携带。
 * 5. 新增**事件类型**等于改协议；给既有事件加**可选字段**是纯增量（如
 *    `retrieval.scope_document_id`），旧前端忽略即可。
 */

import type { ErrorCode } from "@/types/api";
import type { ArtifactSpec, ArtifactType, Citation } from "@/types/artifact";
import type { PatchOp } from "@/types/patch";

/** 8 类事件名（与后端 `SseEventName` Literal 一致）。 */
export type SseEventName =
  | "turn_start"
  | "retrieval"
  | "token"
  | "modality_decision"
  | "artifact"
  | "citations"
  | "error"
  | "turn_end";

export const SSE_EVENT_NAMES: readonly SseEventName[] = [
  "turn_start",
  "retrieval",
  "token",
  "modality_decision",
  "artifact",
  "citations",
  "error",
  "turn_end",
];

/** 正常事件顺序（`error` 不在其中，它可插入任意位置）。 */
export const SSE_EVENT_ORDER: readonly SseEventName[] = [
  "turn_start",
  "retrieval",
  "modality_decision",
  "token",
  "artifact",
  "citations",
  "turn_end",
];

export function isSseEventName(value: string): value is SseEventName {
  return SSE_EVENT_NAMES.some((name) => name === value);
}

/** Retrieval Grader 三态（§7.3）。 */
export type GraderStatus = "SYLLABUS_MATCH" | "PARTIAL_OR_BEYOND" | "NO_MATCH";

/** 失败阶段，用于 UI 区分「检索挂了」还是「生成挂了」。 */
export type ErrorStage = "retrieval" | "route" | "generate" | "validate" | "patch";

/** 形态决策的产物类型，含 `none`（不生成画布产物）。 */
export type ModalityArtifactType = ArtifactType | "none";

// --------------------------------------------------------------------------- //
// 1. turn_start
// --------------------------------------------------------------------------- //
export interface TurnStartEventData {
  turn_id: string;
  workspace_id: string;
  mode: "syllabus_strict" | "hybrid_extended";
  demo_mode: boolean;
  has_anchor: boolean;
  artifact_type_hint: ArtifactType | null;
}

// --------------------------------------------------------------------------- //
// 2. retrieval
// --------------------------------------------------------------------------- //
export interface RetrievedChunk {
  chunk_id: string;
  section_code: string;
  section_title: string;
  page_start: number;
  page_end: number;
  score: number;
  /** 命中片段摘要，用于「依据出处」折叠区 */
  snippet: string;
}

export interface RetrievalEventData {
  grader_status: GraderStatus;
  section_scope: string | null;
  top_score: number;
  chunks: RetrievedChunk[];
  /**
   * 本次检索实际生效的教材作用域（`null` = 跨全部教材）。
   *
   * 多教材同章节号时，`section_scope="4.1"` 不足以说明「按哪本书讲的」——
   * 这个字段用来渲染「当前按《Form 4》4.1 讲解」这类提示条。
   * 后端恒发该字段（可选字段，旧前端忽略即可）。
   */
  scope_document_id: string | null;
}

// --------------------------------------------------------------------------- //
// 3. token
// --------------------------------------------------------------------------- //
export interface TokenEventData {
  /** 文本增量（可能为空串，追加时需容忍） */
  delta: string;
}

// --------------------------------------------------------------------------- //
// 4. modality_decision
// --------------------------------------------------------------------------- //
export interface ModalityDecisionEventData {
  artifact_type: ModalityArtifactType;
  action: "create_artifact" | "patch_artifact" | "none";
  /** 可读理由，UI 需要展示「为什么选 3D」（P0-06①） */
  reason: string;
  user_override: boolean;
}

// --------------------------------------------------------------------------- //
// 5. artifact
// --------------------------------------------------------------------------- //
/**
 * create 与 patch 共用一个事件名，用 `action` 判别。
 *
 * 后端契约保证：
 * - `action === "create"` → `spec` 非空，`ops`/`base_version` 为 `null`
 * - `action === "patch"`  → `ops` 非空、`base_version` 非空，`spec` 为 `null`
 *   （patch 不下发整树，否则退化成全量重生成）
 */
export interface ArtifactEventData {
  action: "create" | "patch";
  artifact_id: string;
  artifact_type: ArtifactType;
  version: number;
  title: string | null;
  spec: ArtifactSpec | null;
  base_version: number | null;
  ops: PatchOp[] | null;
}

// --------------------------------------------------------------------------- //
// 6. citations
// --------------------------------------------------------------------------- //
export interface CitationsEventData {
  citations: Citation[];
}

// --------------------------------------------------------------------------- //
// 7. error
// --------------------------------------------------------------------------- //
export interface ErrorEventData {
  code: ErrorCode;
  message: string;
  stage: ErrorStage;
  /** true 表示后端会自动重试，前端只提示不中断 */
  retryable: boolean;
  attempt: number;
}

// --------------------------------------------------------------------------- //
// 8. turn_end
// --------------------------------------------------------------------------- //
export interface TurnEndEventData {
  turn_id: string;
  duration_ms: number;
  /** 首 token 延迟，用于验收 TTFT ≤ 3s */
  ttft_ms: number;
  version: number | null;
  demo_mode: boolean;
}

// --------------------------------------------------------------------------- //
// 判别联合
// --------------------------------------------------------------------------- //
/** 事件名 → data 结构。新增事件名时只需在这里补一行。 */
export interface SseEventPayloadMap {
  turn_start: TurnStartEventData;
  retrieval: RetrievalEventData;
  token: TokenEventData;
  modality_decision: ModalityDecisionEventData;
  artifact: ArtifactEventData;
  citations: CitationsEventData;
  error: ErrorEventData;
  turn_end: TurnEndEventData;
}

/**
 * 解析后的 SSE 事件：以 `type` 为判别字段的联合。
 *
 * 用 mapped type 从 `SseEventPayloadMap` 派生，避免手写 8 个分支导致漏项。
 * 收窄方式：`switch (event.type) { case "token": event.data.delta ... }`
 */
export type SseEvent = {
  [K in SseEventName]: { type: K; data: SseEventPayloadMap[K] };
}[SseEventName];

/** 按事件名取出对应 data 类型，供回调式 API 标注。 */
export type SseEventData<K extends SseEventName> = SseEventPayloadMap[K];

/** `turn_end` 是流的终点（后端保证必发），hook 据此结束 streaming 态。 */
export function isTerminalEvent(event: SseEvent): boolean {
  return event.type === "turn_end";
}

/** `error` 事件是否值得提示用户「将自动重试」。 */
export function isRetryableError(event: SseEvent): boolean {
  return event.type === "error" && event.data.retryable;
}
