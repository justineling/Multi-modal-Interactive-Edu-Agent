/**
 * 增量补丁契约 —— 与后端 `backend/app/schemas/patch.py` + `schemas/chat.py::Anchor` 对应。
 *
 * 设计意图（SPEC §1.3 / 架构 §5.2）：追问时后端**只下发局部字段的增量指令**，
 * 前端深拷贝 + JSON Pointer 定点写入，从而保证「未锚定节点字节级不变」。
 * 因此 `payload` 的类型刻意是 `Record<string, unknown> | null`（局部 dict），
 * 而不是整棵 AST —— 类型上就堵死「整树下发」这条退化路径。
 */

import type { ArtifactSpec } from "@/types/artifact";

/** 5 种增量指令。 */
export type PatchOpName =
  | "update_node"
  | "insert_node"
  | "delete_node"
  | "replace_subtree"
  | "focus_camera";

export const PATCH_OP_NAMES: readonly PatchOpName[] = [
  "update_node",
  "insert_node",
  "delete_node",
  "replace_subtree",
  "focus_camera",
];

export function isPatchOpName(value: string): value is PatchOpName {
  return PATCH_OP_NAMES.some((name) => name === value);
}

/** 单批最多 20 个 op（与后端 `MAX_OPS_PER_REQUEST` 一致）。 */
export const MAX_OPS_PER_REQUEST = 20;

/**
 * 允许被 patch 的路径形状（与后端 `NODE_PATH_RE` 逐字一致）。
 *
 * 限定四种根列表，是为了让执行器不必处理任意 JSON Pointer，
 * 也顺手挡掉 `/slides/2/bullets/0` 这类越界路径。
 */
export const NODE_PATH_RE = /^\/(slides|nodes|steps|camera_tour)(\/\d+)?(\/(entities)(\/\d+)?)?$/;

/** 前端侧的路径合法性预检：非法路径不必发到后端，直接提示「锚点路径非法」。 */
export function isValidNodePath(nodePath: string): boolean {
  return NODE_PATH_RE.test(nodePath);
}

/** 单条增量指令。 */
export interface PatchOp {
  op: PatchOpName;
  /** JSON Pointer 风格，形如 `/slides/2`、`/steps/1/entities/3`、`/camera_tour/0` */
  node_path: string;
  /**
   * `update_node` → 该节点的局部字段 dict
   * `insert_node` / `replace_subtree` → 完整新节点 / 子树 dict
   * `delete_node` → 恒为 `null`
   * `focus_camera` → `{preset_name: string}`
   */
  payload: Record<string, unknown> | null;
  /** 可读理由，UI 用于「本次改了什么」的提示 */
  reason: string;
}

/** 一批 patch。`base_version` 是乐观锁：不匹配即 409 `PATCH_BASE_VERSION_CONFLICT`。 */
export interface PatchRequest {
  artifact_id: string;
  base_version: number;
  ops: PatchOp[];
}

/**
 * 画布节点锚点（P0-10①）。
 *
 * 后端 `Anchor` 模型字段与默认值：`node_label=""`、`page=None`、`section_code=None`。
 * 这里全部显式声明（可空写 `| null`），构造时用 `createAnchor()` 填默认值。
 */
export interface Anchor {
  artifact_id: string;
  /** 与 `NODE_PATH_RE` 一致的 JSON Pointer 风格路径 */
  node_path: string;
  node_label: string;
  page: number | null;
  section_code: string | null;
}

/** `createAnchor` 的入参：只有 id 与路径必填，其余给默认值。 */
export interface AnchorInput {
  artifact_id: string;
  node_path: string;
  node_label?: string;
  page?: number | null;
  section_code?: string | null;
}

/** 构造锚点，把「可选」收敛到入参、把「恒在」留给 `Anchor` 本体。 */
export function createAnchor(input: AnchorInput): Anchor {
  return {
    artifact_id: input.artifact_id,
    node_path: input.node_path,
    node_label: input.node_label ?? "",
    page: input.page ?? null,
    section_code: input.section_code ?? null,
  };
}

/** 两个锚点是否指向同一节点（用于「再次点击同一元素」的幂等切换）。 */
export function isSameAnchor(a: Anchor | null, b: Anchor | null): boolean {
  if (a === null || b === null) {
    return a === b;
  }
  return a.artifact_id === b.artifact_id && a.node_path === b.node_path;
}

/**
 * 前端本地应用 patch 的结果（`useArtifactPatch` 的返回值，B5 使用）。
 *
 * `changed_paths` 由本地 diff 得出，用于：
 * 1. 只让被改动的子树重渲染；
 * 2. 与后端 `patch_applied` 日志的 changed_paths 交叉核对。
 */
export interface PatchResult {
  /** 应用补丁后的新 AST（深拷贝，原对象不变） */
  spec: ArtifactSpec;
  /** 发生变化的节点路径（不含其祖先/后代） */
  changed_paths: string[];
  /** 应用前的版本号 */
  base_version: number;
  /** 应用后的版本号（= base_version + 1） */
  version: number;
}
