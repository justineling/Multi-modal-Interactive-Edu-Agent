/**
 * 增量补丁的前端应用器（B5 最容易写错的一块）。
 *
 * ## 为什么 `update_node` 必须**字段级 merge**（IMPL-NOTES §五 F1，硬约束）
 *
 * 后端 `update_node` 只携带**被修改的字段**。锚点追问 `/slides/1` 时实测 payload 只有：
 * ```json
 * {"bullets": [...], "speaker_notes": "..."}
 * ```
 * 其余 9 个字段（`node_id` / `layout` / `title` / `subtitle` / `source_ref` …）**一个都不带**。
 * 若用 `setAtPath(spec, "/slides/1", payload)` 整体替换，`layout` 立刻变 `undefined`，
 * `LAYOUT_REGISTRY[undefined]` 拿不到布局 → 渲染器当场崩溃。
 *
 * ## 为什么自检的允许路径要传「父列表层」（§四.4）
 *
 * `insert_node` / `delete_node` 会让**后续兄弟节点的下标整体平移**：在 `/slides/1`
 * 插一页，原来的 `/slides/2..n` 全部换了内容。若只声明 `/slides/1` 为允许变化，
 * 指纹自检会把「兄弟平移」判成「改了不该改的地方」，永远不通过。
 * 因此 insert/delete 一律声明父列表路径（`/slides`），而 update/replace 声明节点自身。
 *
 * ## `focus_camera` 不写进 spec（§四.5）
 *
 * 视角是**渲染态**不是内容态。写进 spec 会让同一份内容因为「看过哪个角度」产生不同指纹，
 * 版本比对（`P0-11`）直接失效。因此它只产出 `focusPreset` 旁路信号。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { getArtifact, patchArtifact, toApiError } from "@/api/client";
import { findUnexpectedChanges } from "@/lib/fingerprint";
import {
  getParentPath,
  insertAtPath,
  isRecord,
  isUnknownArray,
  removeAtPath,
  resolvePointer,
  setAtPath,
} from "@/lib/jsonPointer";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import { MAX_OPS_PER_REQUEST, isValidNodePath } from "@/types/patch";
import type { PatchOp, PatchOpName, PatchResult } from "@/types/patch";
import type { ArtifactSpec } from "@/types/artifact";
import type { ArtifactOut } from "@/types/api";

/** patch 应用失败（路径非法、payload 形状不符、目标节点类型不对） */
export class PatchApplyError extends Error {
  readonly op: PatchOpName | null;

  constructor(message: string, op: PatchOpName | null) {
    super(message);
    this.name = "PatchApplyError";
    this.op = op;
  }
}

/** 本地应用一批 op 的结果 */
export interface PatchApplyResult {
  /** 应用后的 AST（原对象不变；无 op 时是同一引用） */
  readonly spec: ArtifactSpec;
  /** 声明允许变化的路径（update/replace = 节点自身；insert/delete = 父列表） */
  readonly changedPaths: string[];
  /** `focus_camera` 的旁路结果；没有该 op 时为 `null` */
  readonly focusPreset: string | null;
  /** 实际改动内容的 op 数量（不含 `focus_camera`） */
  readonly appliedOps: number;
}

function requirePayload(op: PatchOp): Record<string, unknown> {
  if (op.payload === null || !isRecord(op.payload)) {
    throw new PatchApplyError(`op「${op.op}」缺少对象形式的 payload`, op.op);
  }
  return op.payload;
}

/**
 * 声明一条 op 允许改动的路径。
 *
 * insert / delete 声明到父列表层（见文件头说明）；父路径为根（`""`）时退回节点自身，
 * 因为 `""` 是所有路径的祖先，声明它等于放弃自检。
 */
function declaredPathFor(op: PatchOp): string {
  if (op.op === "insert_node" || op.op === "delete_node") {
    const parent = getParentPath(op.node_path);
    return parent.length > 0 ? parent : op.node_path;
  }
  return op.node_path;
}

/**
 * 纯函数：在本地 AST 上应用一批 op。
 *
 * 不修改入参（每个 `setAtPath` / `insertAtPath` / `removeAtPath` 都返回深拷贝），
 * 因此可以安全地用于「乐观更新 + 失败回滚」。
 */
export function applyPatchOps(spec: ArtifactSpec, ops: readonly PatchOp[]): PatchApplyResult {
  if (ops.length > MAX_OPS_PER_REQUEST) {
    throw new PatchApplyError(
      `单批最多 ${MAX_OPS_PER_REQUEST} 个补丁，收到 ${ops.length} 个`,
      null,
    );
  }

  let current: ArtifactSpec = spec;
  const changedPaths: string[] = [];
  let focusPreset: string | null = null;
  let appliedOps = 0;

  for (const op of ops) {
    if (!isValidNodePath(op.node_path)) {
      throw new PatchApplyError(`锚点路径非法：${op.node_path}`, op.op);
    }
    // 先取出字面量：switch 收窄后 default 分支里 `op` 已是 never，取不到 `op.op`
    const opName: string = op.op;

    switch (op.op) {
      case "update_node": {
        const payload = requirePayload(op);
        const existing = resolvePointer(current, op.node_path);
        if (!isRecord(existing)) {
          throw new PatchApplyError(
            `update_node 的目标不是对象节点：${op.node_path}`,
            op.op,
          );
        }
        // ★ 字段级 merge：不是整体替换（F1 硬约束）
        const merged: Record<string, unknown> = { ...existing, ...payload };
        current = setAtPath(current, op.node_path, merged);
        changedPaths.push(declaredPathFor(op));
        appliedOps += 1;
        break;
      }

      case "insert_node": {
        const payload = requirePayload(op);
        const existing = resolvePointer(current, op.node_path);
        if (isUnknownArray(existing)) {
          // 路径指向列表容器本身（如 `/slides`）：语义为「追加到末尾」
          current = setAtPath(current, op.node_path, [...existing, payload]);
        } else {
          current = insertAtPath(current, op.node_path, payload);
        }
        changedPaths.push(declaredPathFor(op));
        appliedOps += 1;
        break;
      }

      case "replace_subtree": {
        const payload = requirePayload(op);
        current = setAtPath(current, op.node_path, payload);
        changedPaths.push(declaredPathFor(op));
        appliedOps += 1;
        break;
      }

      case "delete_node": {
        const existing = resolvePointer(current, op.node_path);
        if (isUnknownArray(existing)) {
          // 路径指向列表容器：清空而不是把 `slides` 字段删掉（删字段会让渲染器取到 undefined）
          current = setAtPath(current, op.node_path, []);
        } else {
          current = removeAtPath(current, op.node_path);
        }
        changedPaths.push(declaredPathFor(op));
        appliedOps += 1;
        break;
      }

      case "focus_camera": {
        // 不写入 spec（§四.5）：只把预设名旁路传出
        const payload = op.payload;
        const presetName =
          payload !== null && typeof payload["preset_name"] === "string"
            ? payload["preset_name"]
            : null;
        focusPreset = presetName;
        break;
      }

      default: {
        throw new PatchApplyError(`未知的补丁操作：${opName}`, null);
      }
    }
  }

  return {
    spec: current,
    changedPaths: Array.from(new Set(changedPaths)),
    focusPreset,
    appliedOps,
  };
}

/** 指纹自检结果 */
export interface PatchSelfCheck {
  readonly passed: boolean;
  /** 未被声明的意外改动路径（空数组即通过） */
  readonly unexpected: string[];
}

/**
 * 指纹自检：只有被声明的子树允许变化，其余节点必须**字节级不变**（P0-10②）。
 *
 * 这是前端侧的最后一道闸门：即使后端 ops 语义与前端应用器不一致，
 * 也不会静默地把画布改花 —— 自检不过就以服务端快照为准。
 */
export async function selfCheckPatch(
  before: ArtifactSpec,
  after: ArtifactSpec,
  allowedPaths: readonly string[],
): Promise<PatchSelfCheck> {
  const unexpected = await findUnexpectedChanges(before, after, allowedPaths);
  return { passed: unexpected.length === 0, unexpected };
}

/** patch 的触发来源 */
export type PatchSource =
  /** SSE `artifact` 事件：服务端**已经**应用过，前端只做本地应用 + 校验 */
  | "event"
  /** 前端主动调用 `POST /api/artifacts/{id}/patch`：需要处理 409 冲突并回滚 */
  | "request";

export interface RunPatchInput {
  readonly artifactId: string;
  readonly baseVersion: number;
  readonly ops: readonly PatchOp[];
  /** 服务端给出的新版本号（SSE 事件带；主动请求时为 `null`） */
  readonly version?: number | null;
  readonly source?: PatchSource;
  readonly signal?: AbortSignal;
}

export interface RunPatchOutcome {
  readonly ok: boolean;
  /** 应用后的 AST（失败时为 `null`） */
  readonly spec: ArtifactSpec | null;
  readonly version: number | null;
  readonly changedPaths: string[];
  readonly focusPreset: string | null;
  /** 指纹自检是否通过 */
  readonly selfCheckPassed: boolean;
  /** 面向用户的中文错误文案 */
  readonly error: string | null;
}

function nowIso(): string {
  return new Date().toISOString();
}

/** 把产物写回 store（`upsertArtifact` 是 B2 已验收的 action，B5 不新增 action） */
function writeArtifact(artifact: ArtifactOut): void {
  useWorkspaceStore.getState().upsertArtifact(artifact);
}

/**
 * 把补丁失败文案推给画布区的错误条。
 *
 * 复用 B2 已有的 `setError`（不新增 action）；画布区是唯一消费 `error` 的位置，
 * 因为补丁失败影响的是「画布内容」，放在聊天流里会让人以为「提问失败了」。
 */
function reportPatchError(message: string): void {
  useWorkspaceStore.getState().setError(message);
}

/**
 * 执行一次 patch 的完整流水线：本地应用 → 指纹自检 → 乐观写 store → 与服务端对齐。
 *
 * 失败语义（两种来源不同，别混）：
 * - `source === "request"`：409 冲突 → **回滚到原 spec** 并提示「画布已被其他操作更新，请刷新」；
 *   其它错误同样回滚（服务端没存下来，本地不能留着）。
 * - `source === "event"`：服务端已经存下来了，本地应用只是为了让画面先动起来；
 *   自检不过或 GET 失败都**不回滚**，而是以服务端快照为准（GET 成功时覆盖）。
 */
export async function runArtifactPatch(input: RunPatchInput): Promise<RunPatchOutcome> {
  const source: PatchSource = input.source ?? "event";
  const store = useWorkspaceStore.getState();
  const original = store.artifacts.find((item) => item.id === input.artifactId) ?? null;

  if (original === null) {
    const missing = "画布产物不存在，可能已被删除";
    reportPatchError(missing);
    return {
      ok: false,
      spec: null,
      version: null,
      changedPaths: [],
      focusPreset: null,
      selfCheckPassed: false,
      error: missing,
    };
  }

  let applied: PatchApplyResult;
  try {
    applied = applyPatchOps(original.spec, input.ops);
  } catch (applyError) {
    const message =
      applyError instanceof PatchApplyError ? applyError.message : "补丁应用失败";
    reportPatchError(message);
    return {
      ok: false,
      spec: null,
      version: null,
      changedPaths: [],
      focusPreset: null,
      selfCheckPassed: false,
      error: message,
    };
  }

  const selfCheck = await selfCheckPatch(original.spec, applied.spec, applied.changedPaths);
  if (!selfCheck.passed) {
    // 自检不过不是「用户能理解」的错误，只做技术告警；行为上退化为以服务端为准
    console.warn(
      "[useArtifactPatch] 指纹自检发现未声明的改动，将以服务端快照为准：",
      selfCheck.unexpected,
    );
  }

  const targetVersion = input.version ?? original.version + 1;

  // 乐观更新：先让画面动起来（哪怕只有 1 帧的提前量，交互也更跟手）
  if (applied.appliedOps > 0) {
    writeArtifact({
      ...original,
      spec: applied.spec,
      version: targetVersion,
      updated_at: nowIso(),
    });
  }

  const baseOutcome: RunPatchOutcome = {
    ok: true,
    spec: applied.spec,
    version: targetVersion,
    changedPaths: applied.changedPaths,
    focusPreset: applied.focusPreset,
    selfCheckPassed: selfCheck.passed,
    error: null,
  };

  if (applied.focusPreset !== null) {
    // 旁路通知 3D 渲染器转镜头（不写 spec，见 §四.5）
    publishFocusPreset(applied.focusPreset);
  }

  if (source === "event") {
    // 服务端已落库：拉一次权威快照对齐（失败时保留本地结果，不回滚）
    try {
      const fresh = await getArtifact(input.artifactId, {}, input.signal);
      writeArtifact(fresh);
      return { ...baseOutcome, spec: fresh.spec, version: fresh.version };
    } catch {
      return baseOutcome;
    }
  }

  try {
    const server = await patchArtifact(
      {
        artifact_id: input.artifactId,
        base_version: input.baseVersion,
        ops: [...input.ops],
      },
      input.signal,
    );
    writeArtifact(server);
    return { ...baseOutcome, spec: server.spec, version: server.version };
  } catch (requestError) {
    const apiError = toApiError(requestError);
    // 回滚：服务端没接受这批 op，本地不能留着（否则「看到的」和「存下的」不一致）
    writeArtifact(original);
    const message =
      apiError.code === "PATCH_BASE_VERSION_CONFLICT"
        ? "画布已被其他操作更新，请刷新"
        : apiError.displayMessage;
    reportPatchError(message);
    return {
      ok: false,
      spec: null,
      version: original.version,
      changedPaths: applied.changedPaths,
      focusPreset: applied.focusPreset,
      selfCheckPassed: selfCheck.passed,
      error: message,
    };
  }
}

// --------------------------------------------------------------------------- //
// Hook
// --------------------------------------------------------------------------- //
export interface UseArtifactPatchState {
  readonly applying: boolean;
  readonly error: string | null;
  readonly lastOutcome: RunPatchOutcome | null;
  /** `focus_camera` 旁路信号：3D 渲染器据此平滑转到该机位 */
  readonly focusPreset: string | null;
  readonly apply: (input: RunPatchInput) => Promise<RunPatchOutcome>;
  readonly dismissError: () => void;
  readonly clearFocusPreset: () => void;
}

/**
 * 组件侧包装：给 `runArtifactPatch` 加上「进行中 / 错误 / 最近结果」三态。
 *
 * `apply` 用 ref 做幂等守卫：同一个 artifact 上并发两次 patch 会让 `base_version`
 * 互相踩踏（后到的必然 409），因此进行中直接复用同一个 Promise。
 */
export function useArtifactPatch(): UseArtifactPatchState {
  const [applying, setApplying] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [lastOutcome, setLastOutcome] = useState<RunPatchOutcome | null>(null);
  const [focusPreset, setFocusPreset] = useState<string | null>(null);
  const inFlightRef = useRef<Promise<RunPatchOutcome> | null>(null);

  const apply = useCallback((input: RunPatchInput): Promise<RunPatchOutcome> => {
    const pending = inFlightRef.current;
    if (pending !== null) {
      return pending;
    }
    setApplying(true);
    setError(null);

    const task = runArtifactPatch(input)
      .then((outcome) => {
        setLastOutcome(outcome);
        if (outcome.error !== null) {
          setError(outcome.error);
        }
        if (outcome.focusPreset !== null) {
          setFocusPreset(outcome.focusPreset);
        }
        return outcome;
      })
      .catch((unexpected: unknown) => {
        const message = toApiError(unexpected).displayMessage;
        setError(message);
        const failure: RunPatchOutcome = {
          ok: false,
          spec: null,
          version: null,
          changedPaths: [],
          focusPreset: null,
          selfCheckPassed: false,
          error: message,
        };
        setLastOutcome(failure);
        return failure;
      })
      .finally(() => {
        inFlightRef.current = null;
        setApplying(false);
      });

    inFlightRef.current = task;
    return task;
  }, []);

  const dismissError = useCallback((): void => {
    setError(null);
  }, []);

  const clearFocusPreset = useCallback((): void => {
    setFocusPreset(null);
  }, []);

  return { applying, error, lastOutcome, focusPreset, apply, dismissError, clearFocusPreset };
}

/** 便于 UI 展示的补丁结果（`PatchResult` 契约的本地填充） */
export function toPatchResult(outcome: RunPatchOutcome, baseVersion: number): PatchResult | null {
  if (outcome.spec === null || outcome.version === null) {
    return null;
  }
  return {
    spec: outcome.spec,
    changed_paths: outcome.changedPaths,
    base_version: baseVersion,
    version: outcome.version,
  };
}

// --------------------------------------------------------------------------- //
// focus_camera 旁路信号（pub/sub）
// --------------------------------------------------------------------------- //
/**
 * `focus_camera` 的传递通道。
 *
 * 为什么用 pub/sub 而不是 props：发起 patch 的是 `useChatStream`（聊天侧），
 * 消费机位的是 `Scene3DRenderer`（画布侧），两者不在同一条 props 链上。
 * 走 store 需要新增 action（B2 契约层，本次不动）；走模块级订阅只多 20 行，
 * 且天然做到「没人订阅就丢弃」，不会污染任何持久状态。
 */
type FocusPresetListener = (preset: string | null) => void;

const focusPresetListeners = new Set<FocusPresetListener>();

export function subscribeFocusPreset(listener: FocusPresetListener): () => void {
  focusPresetListeners.add(listener);
  return () => {
    focusPresetListeners.delete(listener);
  };
}

function publishFocusPreset(preset: string | null): void {
  for (const listener of Array.from(focusPresetListeners)) {
    listener(preset);
  }
}

/**
 * 订阅 `focus_camera` 机位名。
 *
 * 返回值：`[preset, consume]`。`consume` 必须在相机转场结束后调用，
 * 否则下一次用户手动拖动轨道时，`CameraRig` 会把镜头拽回预设位置。
 */
export function useFocusPreset(): [string | null, () => void] {
  const [preset, setPreset] = useState<string | null>(null);

  useEffect(() => {
    return subscribeFocusPreset(setPreset);
  }, []);

  const consume = useCallback((): void => {
    setPreset(null);
  }, []);

  return [preset, consume];
}
