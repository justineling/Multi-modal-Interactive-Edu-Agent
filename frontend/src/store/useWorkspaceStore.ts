/**
 * 工作区状态（Zustand 单 store）。
 *
 * 职责边界（架构 §3.2 + §9）：
 * - **只存状态与纯更新函数**，不发任何 HTTP 请求。请求留在组件 / hooks 层
 *   （B3 的 `useChatStream`、`FileDrawer`），这样 store 可以脱离网络单测。
 * - `anchor` 是「节点锚点追问」的核心状态：画布点击 → `setAnchor` → 输入框出现 chip
 *   → 追问时随请求体发出 → 收到回复后 `clearAnchor`。
 * - `currentWorkspaceId` 落 `localStorage`，刷新后自动恢复最近会话（P0-01②）。
 */

import { create } from "zustand";

import type {
  ArtifactOut,
  DocumentOut,
  MessageOut,
  WorkspaceOut,
} from "@/types/api";
import type { Anchor } from "@/types/patch";

/** 各类加载态（按资源分片，避免「一个转圈全屏」）。 */
export type LoadingKey = "workspaces" | "documents" | "messages" | "artifacts" | "upload" | "patch";

export type LoadingState = Record<LoadingKey, boolean>;

const INITIAL_LOADING: LoadingState = {
  workspaces: false,
  documents: false,
  messages: false,
  artifacts: false,
  upload: false,
  patch: false,
};

const WORKSPACE_STORAGE_KEY = "edu.workspace.currentWorkspaceId";

/** 读回上次的 workspace id。隐私模式 / 禁用存储时静默返回 null。 */
function readPersistedWorkspaceId(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const value = window.localStorage.getItem(WORKSPACE_STORAGE_KEY);
    return value !== null && value.length > 0 ? value : null;
  } catch {
    return null;
  }
}

function persistWorkspaceId(workspaceId: string | null): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    if (workspaceId === null) {
      window.localStorage.removeItem(WORKSPACE_STORAGE_KEY);
    } else {
      window.localStorage.setItem(WORKSPACE_STORAGE_KEY, workspaceId);
    }
  } catch {
    // 存储不可用不影响主流程
  }
}

export interface WorkspaceState {
  // ---- 数据 ----
  workspaces: WorkspaceOut[];
  currentWorkspaceId: string | null;
  documents: DocumentOut[];
  messages: MessageOut[];
  artifacts: ArtifactOut[];

  // ---- 画布 ----
  /** 当前画布产物 id */
  activeArtifactId: string | null;
  /**
   * 正在查看的版本号。
   * `null` 表示「跟随最新版本」；设为具体数字即进入历史版本只读预览。
   */
  activeVersion: number | null;

  // ---- 交互上下文 ----
  /** 画布节点锚点（追问时随请求发出） */
  anchor: Anchor | null;
  /** 是否运行在 mock 模式（顶栏黄标常驻，P0-12） */
  demoMode: boolean;

  // ---- 过程态 ----
  loading: LoadingState;
  /** 正在流式接收的 assistant 消息 id；非 null 即「生成中」 */
  streamingMessageId: string | null;
  /** 最近一次可展示给用户的错误文案 */
  error: string | null;
}

export interface WorkspaceActions {
  // workspaces
  setWorkspaces: (workspaces: WorkspaceOut[]) => void;
  upsertWorkspace: (workspace: WorkspaceOut) => void;
  removeWorkspace: (workspaceId: string) => void;
  setCurrentWorkspaceId: (workspaceId: string | null) => void;

  // documents
  setDocuments: (documents: DocumentOut[]) => void;
  upsertDocument: (document: DocumentOut) => void;
  removeDocument: (documentId: string) => void;

  // messages
  setMessages: (messages: MessageOut[]) => void;
  appendMessage: (message: MessageOut) => void;
  updateMessage: (messageId: string, patch: Partial<MessageOut>) => void;
  /** 流式追加文本增量（高频调用，见实现注释） */
  appendToken: (delta: string) => void;
  beginStreaming: (messageId: string) => void;
  endStreaming: () => void;

  // artifacts
  setArtifacts: (artifacts: ArtifactOut[]) => void;
  upsertArtifact: (artifact: ArtifactOut) => void;
  removeArtifact: (artifactId: string) => void;
  setActiveArtifact: (artifactId: string | null, version?: number | null) => void;
  setActiveVersion: (version: number | null) => void;

  // anchor / demo
  setAnchor: (anchor: Anchor) => void;
  clearAnchor: () => void;
  setDemoMode: (demoMode: boolean) => void;

  // 过程态
  setLoading: (key: LoadingKey, value: boolean) => void;
  setError: (message: string | null) => void;

  // 重置
  resetWorkspaceScoped: () => void;
  resetAll: () => void;
}

export type WorkspaceStore = WorkspaceState & WorkspaceActions;

const INITIAL_STATE: WorkspaceState = {
  workspaces: [],
  currentWorkspaceId: null,
  documents: [],
  messages: [],
  artifacts: [],
  activeArtifactId: null,
  activeVersion: null,
  anchor: null,
  demoMode: false,
  loading: INITIAL_LOADING,
  streamingMessageId: null,
  error: null,
};

export const useWorkspaceStore = create<WorkspaceStore>()((set) => ({
  ...INITIAL_STATE,
  // 初始化即尝试恢复上次会话（P0-01②）
  currentWorkspaceId: readPersistedWorkspaceId(),

  // ------------------------------------------------------------------ //
  // workspaces
  // ------------------------------------------------------------------ //
  setWorkspaces: (workspaces) =>
    set((state) => {
      // 若当前 workspace 已被删除，回落到最近活跃的一个
      const stillExists = workspaces.some((item) => item.id === state.currentWorkspaceId);
      if (stillExists || workspaces.length === 0) {
        return { workspaces };
      }
      const fallback = workspaces[0];
      if (fallback === undefined) {
        return { workspaces };
      }
      persistWorkspaceId(fallback.id);
      return { workspaces, currentWorkspaceId: fallback.id };
    }),

  upsertWorkspace: (workspace) =>
    set((state) => {
      const index = state.workspaces.findIndex((item) => item.id === workspace.id);
      if (index === -1) {
        return { workspaces: [workspace, ...state.workspaces] };
      }
      const next = state.workspaces.slice();
      next[index] = workspace;
      return { workspaces: next };
    }),

  removeWorkspace: (workspaceId) =>
    set((state) => {
      const workspaces = state.workspaces.filter((item) => item.id !== workspaceId);
      if (state.currentWorkspaceId !== workspaceId) {
        return { workspaces };
      }
      const fallback = workspaces[0] ?? null;
      persistWorkspaceId(fallback === null ? null : fallback.id);
      // 切换 workspace 必须清空工作区级状态，否则会看到上一个库的目录与画布
      return {
        workspaces,
        currentWorkspaceId: fallback === null ? null : fallback.id,
        documents: [],
        messages: [],
        artifacts: [],
        activeArtifactId: null,
        activeVersion: null,
        anchor: null,
        streamingMessageId: null,
        error: null,
      };
    }),

  setCurrentWorkspaceId: (workspaceId) => {
    persistWorkspaceId(workspaceId);
    set({
      currentWorkspaceId: workspaceId,
      documents: [],
      messages: [],
      artifacts: [],
      activeArtifactId: null,
      activeVersion: null,
      anchor: null,
      streamingMessageId: null,
      error: null,
    });
  },

  // ------------------------------------------------------------------ //
  // documents
  // ------------------------------------------------------------------ //
  setDocuments: (documents) => set({ documents }),

  upsertDocument: (document) =>
    set((state) => {
      const index = state.documents.findIndex((item) => item.id === document.id);
      if (index === -1) {
        return { documents: [document, ...state.documents] };
      }
      const next = state.documents.slice();
      next[index] = document;
      return { documents: next };
    }),

  removeDocument: (documentId) =>
    set((state) => ({
      documents: state.documents.filter((item) => item.id !== documentId),
    })),

  // ------------------------------------------------------------------ //
  // messages
  // ------------------------------------------------------------------ //
  setMessages: (messages) => set({ messages }),

  appendMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),

  updateMessage: (messageId, patch) =>
    set((state) => {
      let touched = false;
      const messages = state.messages.map((message) => {
        if (message.id !== messageId) {
          return message;
        }
        touched = true;
        return { ...message, ...patch };
      });
      // 目标不存在时返回原 state 引用，Zustand 会跳过通知（避免无意义重渲染）
      return touched ? { messages } : state;
    }),

  /**
   * 流式追加：只替换目标消息对象，其余消息**保持同一引用**。
   *
   * 为什么不用「每次 set 全量 map 出新对象」：token 事件可能每秒几十次，
   * 若每条消息都换新引用，所有 `MessageBubble` 都会重渲染，长会话会明显掉帧。
   * 这里其余元素引用不变，配合 `memo` + 按 id 选择器的子组件即可做到「只有流式气泡重渲染」。
   * 空 delta 或没有流式目标时直接返回原 state（Zustand 判等后跳过通知）。
   */
  appendToken: (delta) =>
    set((state) => {
      const streamingId = state.streamingMessageId;
      if (streamingId === null || delta.length === 0) {
        return state;
      }
      let touched = false;
      const messages = state.messages.map((message) => {
        if (message.id !== streamingId) {
          return message;
        }
        touched = true;
        return { ...message, content_markdown: message.content_markdown + delta };
      });
      return touched ? { messages } : state;
    }),

  beginStreaming: (messageId) => set({ streamingMessageId: messageId, error: null }),

  endStreaming: () => set({ streamingMessageId: null }),

  // ------------------------------------------------------------------ //
  // artifacts
  // ------------------------------------------------------------------ //
  setArtifacts: (artifacts) =>
    set((state) => {
      const stillExists = artifacts.some((item) => item.id === state.activeArtifactId);
      if (stillExists) {
        return { artifacts };
      }
      const fallback = artifacts[0];
      return {
        artifacts,
        activeArtifactId: fallback === undefined ? null : fallback.id,
        activeVersion: null,
      };
    }),

  upsertArtifact: (artifact) =>
    set((state) => {
      const index = state.artifacts.findIndex((item) => item.id === artifact.id);
      if (index === -1) {
        return {
          artifacts: [artifact, ...state.artifacts],
          activeArtifactId: state.activeArtifactId ?? artifact.id,
        };
      }
      const next = state.artifacts.slice();
      next[index] = artifact;
      return { artifacts: next };
    }),

  removeArtifact: (artifactId) =>
    set((state) => {
      const artifacts = state.artifacts.filter((item) => item.id !== artifactId);
      if (state.activeArtifactId !== artifactId) {
        return { artifacts };
      }
      const fallback = artifacts[0] ?? null;
      return {
        artifacts,
        activeArtifactId: fallback === null ? null : fallback.id,
        activeVersion: null,
        // 产物被删后锚点必然失效
        anchor: null,
      };
    }),

  setActiveArtifact: (artifactId, version = null) =>
    set({
      activeArtifactId: artifactId,
      activeVersion: version,
      // 换画布时旧锚点无意义，一并清掉，避免「锚在 A 的路径上却改 B」
      anchor: null,
    }),

  setActiveVersion: (version) => set({ activeVersion: version }),

  // ------------------------------------------------------------------ //
  // anchor / demo
  // ------------------------------------------------------------------ //
  setAnchor: (anchor) => set({ anchor }),

  clearAnchor: () => set({ anchor: null }),

  setDemoMode: (demoMode) => set({ demoMode }),

  // ------------------------------------------------------------------ //
  // 过程态
  // ------------------------------------------------------------------ //
  setLoading: (key, value) =>
    set((state) => {
      if (state.loading[key] === value) {
        return state;
      }
      return { loading: { ...state.loading, [key]: value } };
    }),

  setError: (message) => set({ error: message }),

  // ------------------------------------------------------------------ //
  // 重置
  // ------------------------------------------------------------------ //
  resetWorkspaceScoped: () =>
    set({
      documents: [],
      messages: [],
      artifacts: [],
      activeArtifactId: null,
      activeVersion: null,
      anchor: null,
      streamingMessageId: null,
      error: null,
      loading: INITIAL_LOADING,
    }),

  resetAll: () => {
    persistWorkspaceId(null);
    set({ ...INITIAL_STATE, loading: { ...INITIAL_LOADING } });
  },
}));

// --------------------------------------------------------------------------- //
// 选择器（组件里用 `useWorkspaceStore(selectXxx)`，避免订阅整个 store）
// --------------------------------------------------------------------------- //
export function selectCurrentWorkspace(state: WorkspaceStore): WorkspaceOut | null {
  return state.workspaces.find((item) => item.id === state.currentWorkspaceId) ?? null;
}

export function selectActiveArtifact(state: WorkspaceStore): ArtifactOut | null {
  return state.artifacts.find((item) => item.id === state.activeArtifactId) ?? null;
}

export function selectIsStreaming(state: WorkspaceStore): boolean {
  return state.streamingMessageId !== null;
}

export function selectHasReadyDocument(state: WorkspaceStore): boolean {
  return state.documents.some((document) => document.status === "ready");
}

export function selectDocumentById(
  documentId: string,
): (state: WorkspaceStore) => DocumentOut | null {
  return (state) => state.documents.find((item) => item.id === documentId) ?? null;
}

export function selectMessageById(messageId: string): (state: WorkspaceStore) => MessageOut | null {
  return (state) => state.messages.find((item) => item.id === messageId) ?? null;
}
