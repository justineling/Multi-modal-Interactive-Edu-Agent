import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";

import { FileDrawer } from "@/components/chat/FileDrawer";
import { Composer } from "@/components/chat/Composer";
import type { DraftRequest } from "@/components/chat/Composer";
import { MessageBubble } from "@/components/chat/MessageBubble";
import { cx } from "@/components/common/cx";
import { useChatStream } from "@/hooks/useChatStream";
import { useDocumentPolling } from "@/hooks/useDocumentPolling";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { DocumentOut, StreamMode, TocNode } from "@/types/api";
import type { ArtifactType } from "@/types/artifact";

/** 空态示例问句：点一下直接填进输入框，降低「不知道能问什么」的门槛 */
const SAMPLE_PROMPTS: readonly string[] = [
  "学习 1.1，帮我梳理这一节的要点",
  "用 2D 动画演示这个过程",
  "用 3D 展示这个空间结构",
];

export interface ChatPanelProps {
  workspaceId: string | null;
  mode: StreamMode;
  draft: DraftRequest;
  onSamplePrompt: (text: string) => void;
}

/** 目录树里找 section_code → documentId 的映射（引用展开原文时要用） */
function buildSectionIndex(documents: readonly DocumentOut[]): Map<string, string> {
  const index = new Map<string, string>();
  const walk = (nodes: readonly TocNode[], documentId: string): void => {
    for (const node of nodes) {
      index.set(node.code, documentId);
      walk(node.children, documentId);
    }
  };
  for (const document of documents) {
    if (document.status === "ready") {
      walk(document.toc_tree, document.id);
    }
  }
  return index;
}

/**
 * 左栏：教材 → 消息流 → 输入区。
 *
 * 三个关键行为：
 * 1. **粘底滚动**：只有当用户本来就在底部时才自动滚动，否则会被「你在往上翻历史」时
 *    每来一个 token 就拽回底部，完全没法读。
 * 2. **锚点消费**：发送后 `turn_end` 会清掉锚点（见 useChatStream），回到全局模式。
 * 3. **引用定位**：`section_code` → `documentId` 的映射一次算好，
 *    避免每条消息每次渲染都遍历目录树。
 */
export function ChatPanel({ workspaceId, mode, draft, onSamplePrompt }: ChatPanelProps): ReactElement {
  const messages = useWorkspaceStore((state) => state.messages);
  const documents = useWorkspaceStore((state) => state.documents);
  const anchor = useWorkspaceStore((state) => state.anchor);
  const clearAnchor = useWorkspaceStore((state) => state.clearAnchor);
  const streamingMessageId = useWorkspaceStore((state) => state.streamingMessageId);

  const { isStreaming, error, meta, send, stop, dismissError } = useChatStream();
  useDocumentPolling();

  const [pickedCode, setPickedCode] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef<boolean>(true);

  const sectionIndex = useMemo(() => buildSectionIndex(documents), [documents]);
  const fallbackDocumentId = useMemo(() => {
    const ready = documents.find((document) => document.status === "ready");
    return ready === undefined ? null : ready.id;
  }, [documents]);

  /** 引用所属教材：先精确匹配，再前缀匹配（`1.1.1` → `1.1`），最后兜底 */
  const resolveDocumentId = useCallback(
    (sectionCode: string | undefined): string | null => {
      if (sectionCode === undefined) {
        return fallbackDocumentId;
      }
      const exact = sectionIndex.get(sectionCode);
      if (exact !== undefined) {
        return exact;
      }
      let best: { code: string; documentId: string } | null = null;
      for (const [code, documentId] of sectionIndex) {
        if (sectionCode.startsWith(code) && (best === null || code.length > best.code.length)) {
          best = { code, documentId };
        }
      }
      return best === null ? fallbackDocumentId : best.documentId;
    },
    [sectionIndex, fallbackDocumentId],
  );

  // 粘底滚动：只在用户位于底部附近时跟随
  useEffect(() => {
    const node = scrollRef.current;
    if (node === null || !stickRef.current) {
      return;
    }
    node.scrollTop = node.scrollHeight;
  }, [messages, streamingMessageId]);

  const handleScroll = useCallback((): void => {
    const node = scrollRef.current;
    if (node === null) {
      return;
    }
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    stickRef.current = distance < 80;
  }, []);

  const handlePickSection = useCallback(
    (node: TocNode): void => {
      setPickedCode(node.code);
      onSamplePrompt(`学习 ${node.code}（${node.title}），帮我梳理要点`);
    },
    [onSamplePrompt],
  );

  const handleSend = useCallback(
    (query: string, override: ArtifactType | null): void => {
      if (workspaceId === null) {
        return;
      }
      stickRef.current = true;
      void send({
        workspaceId,
        query,
        mode,
        anchor,
        artifactTypeOverride: override,
      });
    },
    [workspaceId, mode, anchor, send],
  );

  return (
    <section className="no-print flex h-full min-h-0 flex-col bg-surface-base">
      <FileDrawer workspaceId={workspaceId} onPickSection={handlePickSection} pickedCode={pickedCode} />

      <div ref={scrollRef} onScroll={handleScroll} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <p className="text-sm text-content-secondary">上传教材后，从任意知识点开始提问</p>
            <div className="flex w-full max-w-sm flex-col gap-1.5">
              {SAMPLE_PROMPTS.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  onClick={() => onSamplePrompt(prompt)}
                  className="rounded-sm border border-line bg-surface-raised px-3 py-2 text-left text-xs text-content-secondary transition-colors hover:border-accent/50 hover:text-content-primary"
                >
                  {prompt}
                </button>
              ))}
            </div>
            <p className="max-w-xs text-xxs leading-relaxed text-content-muted">
              提示：点击右侧画布里的某一页或某个部件，可以只针对那一块追问。
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                meta={meta[message.id] ?? null}
                isStreaming={message.id === streamingMessageId}
                documentId={resolveDocumentId(message.citations[0]?.section_code)}
                onClearAnchor={clearAnchor}
              />
            ))}
          </div>
        )}
      </div>

      {error === null ? null : (
        <div className="flex items-start gap-2 border-t border-palette-red/30 bg-palette-red/10 px-3 py-1.5">
          <p className={cx("flex-1 text-xxs leading-relaxed text-palette-red")}>{error}</p>
          <button
            type="button"
            onClick={dismissError}
            className="text-xxs text-palette-red/70 transition-colors hover:text-palette-red"
          >
            知道了
          </button>
        </div>
      )}

      <Composer
        anchor={anchor}
        onClearAnchor={clearAnchor}
        onSend={handleSend}
        isStreaming={isStreaming}
        onStop={stop}
        disabled={workspaceId === null}
        draft={draft}
      />
    </section>
  );
}
