import { useCallback, useEffect, useRef, useState } from "react";

import { getArtifact, toApiError } from "@/api/client";
import { iterateChatStream } from "@/api/sse";
import { runArtifactPatch } from "@/hooks/useArtifactPatch";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { ChatRequestBody, StreamMode } from "@/types/api";
import type { ArtifactType, KnowledgeScope } from "@/types/artifact";
import type { RetrievalEventData, SseEvent } from "@/types/events";
import type { Anchor } from "@/types/patch";

/** 一条 assistant 消息的「会话内附加信息」：后端不落库，刷新后自然丢失。 */
export interface MessageMeta {
  /** 为什么选这个形态（P0-06③），来自 `modality_decision.reason` */
  modalityReason: string | null;
  /** 检索结果（含命中切片），用于「依据出处」展开 */
  retrieval: RetrievalEventData | null;
}

const EMPTY_META: MessageMeta = { modalityReason: null, retrieval: null };

export interface SendChatInput {
  workspaceId: string;
  query: string;
  mode: StreamMode;
  /** 节点锚点追问；null 表示全局提问 */
  anchor: Anchor | null;
  /** 用户显式指定的形态（P0-06②），null 表示交给路由决策 */
  artifactTypeOverride: ArtifactType | null;
}

export interface ChatStreamState {
  isStreaming: boolean;
  error: string | null;
  meta: Record<string, MessageMeta>;
  send: (input: SendChatInput) => Promise<void>;
  stop: () => void;
  dismissError: () => void;
}

function newLocalId(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  return `${prefix}-${random}`;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function scopeFromGrader(status: RetrievalEventData["grader_status"]): KnowledgeScope {
  if (status === "SYLLABUS_MATCH") {
    return "syllabus_strict";
  }
  if (status === "PARTIAL_OR_BEYOND") {
    return "hybrid_extended";
  }
  return "none";
}

/**
 * 流式对话控制器。
 *
 * 三条设计约束（都是踩过的坑）：
 * 1. **幂等守卫**：`abortRef.current !== null` 时直接 return。StrictMode 双挂载 +
 *    用户连点两次发送，都会导致同一条提问发两遍 SSE，后端就会落两份 message。
 * 2. **不订阅 store**：流内的高频更新全部走 `useWorkspaceStore.getState()`，
 *    避免 hook 自身随每个 token 重渲染（那会让整个 ChatPanel 抖动）。
 * 3. **`error` 事件不中断流**：后端在「校验失败 → 自动重试」期间会发
 *    `error{retryable:true}`，之后仍然会补 `turn_end`。此时若 abort，会丢掉正确答案。
 */
export function useChatStream(): ChatStreamState {
  const [isStreaming, setIsStreaming] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<Record<string, MessageMeta>>({});
  const abortRef = useRef<AbortController | null>(null);

  // 卸载时取消进行中的流（切换 workspace 会卸载 ChatPanel，流必须跟着断）
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const patchMeta = useCallback((messageId: string, patch: Partial<MessageMeta>): void => {
    setMeta((previous) => {
      const current = previous[messageId] ?? EMPTY_META;
      return { ...previous, [messageId]: { ...current, ...patch } };
    });
  }, []);

  const stop = useCallback((): void => {
    abortRef.current?.abort();
    abortRef.current = null;
    useWorkspaceStore.getState().endStreaming();
    setIsStreaming(false);
  }, []);

  const dismissError = useCallback((): void => {
    setError(null);
  }, []);

  const send = useCallback(
    async (input: SendChatInput): Promise<void> => {
      const query = input.query.trim();
      if (query.length === 0) {
        return;
      }
      // 幂等守卫：进行中不允许再发
      if (abortRef.current !== null) {
        return;
      }

      const store = useWorkspaceStore.getState();
      const now = new Date().toISOString();
      const userMessageId = newLocalId("local-user");
      const assistantMessageId = newLocalId("local-assistant");

      store.appendMessage({
        id: userMessageId,
        workspace_id: input.workspaceId,
        role: "user",
        content_markdown: query,
        knowledge_scope: null,
        citations: [],
        anchor: input.anchor,
        artifact_id: null,
        artifact_version: null,
        demo_mode: store.demoMode,
        created_at: now,
      });
      store.appendMessage({
        id: assistantMessageId,
        workspace_id: input.workspaceId,
        role: "assistant",
        content_markdown: "",
        knowledge_scope: null,
        citations: [],
        anchor: null,
        artifact_id: null,
        artifact_version: null,
        demo_mode: store.demoMode,
        created_at: now,
      });
      store.beginStreaming(assistantMessageId);

      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);
      setError(null);

      const body: ChatRequestBody = {
        workspace_id: input.workspaceId,
        query,
        mode: input.mode,
      };
      if (input.anchor !== null) {
        body.anchor = input.anchor;
      }
      if (input.artifactTypeOverride !== null) {
        body.artifact_type_override = input.artifactTypeOverride;
      }
      if (input.anchor === null && store.activeArtifactId !== null) {
        // 无锚点时告知「接着这张画布继续改」，后端才能走 patch 而不是新开一张
        body.artifact_id = store.activeArtifactId;
      }

      const handleArtifactEvent = async (event: Extract<SseEvent, { type: "artifact" }>): Promise<void> => {
        const data = event.data;
        const target = useWorkspaceStore.getState();
        if (data.action === "create") {
          if (data.spec === null) {
            return;
          }
          const timestamp = new Date().toISOString();
          target.upsertArtifact({
            id: data.artifact_id,
            workspace_id: input.workspaceId,
            title: data.title ?? "未命名画布",
            artifact_type: data.artifact_type,
            version: data.version,
            spec: data.spec,
            created_at: timestamp,
            updated_at: timestamp,
          });
          target.setActiveArtifact(data.artifact_id, null);
          target.updateMessage(assistantMessageId, {
            artifact_id: data.artifact_id,
            artifact_version: data.version,
          });
          return;
        }

        // patch：服务端已应用，前端本地应用同一批 op 让画面立刻动起来，
        // 再用指纹自检确认「只有目标子树变了」，最后拉一次权威快照对齐。
        target.updateMessage(assistantMessageId, {
          artifact_id: data.artifact_id,
          artifact_version: data.version,
        });

        if (data.ops === null || data.ops.length === 0) {
          // 后端声称是 patch 却没下发 ops：契约异常，退化为整树刷新（宁可慢也不能画错）
          try {
            const fresh = await getArtifact(data.artifact_id);
            const latest = useWorkspaceStore.getState();
            latest.upsertArtifact(fresh);
            latest.setActiveArtifact(data.artifact_id, null);
          } catch (refreshError) {
            setError(toApiError(refreshError).displayMessage);
          }
          return;
        }

        await runArtifactPatch({
          artifactId: data.artifact_id,
          baseVersion: data.base_version ?? Math.max(data.version - 1, 1),
          ops: data.ops,
          version: data.version,
          source: "event",
        });
      };

      try {
        for await (const event of iterateChatStream(body, { signal: controller.signal })) {
          switch (event.type) {
            case "turn_start": {
              useWorkspaceStore.getState().setDemoMode(event.data.demo_mode);
              break;
            }
            case "retrieval": {
              patchMeta(assistantMessageId, { retrieval: event.data });
              useWorkspaceStore
                .getState()
                .updateMessage(assistantMessageId, {
                  knowledge_scope: scopeFromGrader(event.data.grader_status),
                });
              break;
            }
            case "token": {
              // 高频路径：store 内部只替换目标消息对象，其余消息引用不变
              useWorkspaceStore.getState().appendToken(event.data.delta);
              break;
            }
            case "modality_decision": {
              patchMeta(assistantMessageId, { modalityReason: event.data.reason });
              break;
            }
            case "artifact": {
              await handleArtifactEvent(event);
              break;
            }
            case "citations": {
              useWorkspaceStore
                .getState()
                .updateMessage(assistantMessageId, { citations: event.data.citations });
              break;
            }
            case "error": {
              // 不 abort：后端重试后仍会继续发 token 与 turn_end
              setError(event.data.message);
              break;
            }
            case "turn_end": {
              const target = useWorkspaceStore.getState();
              if (event.data.version !== null) {
                target.updateMessage(assistantMessageId, { artifact_version: event.data.version });
              }
              // 锚点已在本次追问中被消费，清掉回到全局提问模式
              target.clearAnchor();
              break;
            }
            default: {
              break;
            }
          }
        }
      } catch (streamError) {
        if (!isAbortError(streamError)) {
          setError(toApiError(streamError).displayMessage);
        }
      } finally {
        abortRef.current = null;
        setIsStreaming(false);
        const target = useWorkspaceStore.getState();
        target.endStreaming();
        // 空回复兜底：避免留下一个永远空白的空气泡
        const produced = target.messages.find((message) => message.id === assistantMessageId);
        if (produced !== undefined && produced.content_markdown.trim().length === 0) {
          target.updateMessage(assistantMessageId, {
            content_markdown: "_本次没有生成内容。可以换个问法，或先上传教材再试。_",
          });
        }
      }
    },
    [patchMeta],
  );

  return { isStreaming, error, meta, send, stop, dismissError };
}
