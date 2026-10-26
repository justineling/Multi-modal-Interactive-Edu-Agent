import { memo, useCallback, useState } from "react";
import type { ReactElement } from "react";

import { AnchorChip } from "@/components/chat/AnchorChip";
import { CitationList } from "@/components/chat/CitationList";
import { MarkdownView } from "@/components/chat/MarkdownView";
import { cx } from "@/components/common/cx";
import type { MessageMeta } from "@/hooks/useChatStream";
import type { MessageOut } from "@/types/api";

interface MessageBubbleProps {
  message: MessageOut;
  /** 会话内附加信息（后端不落库，刷新后为 null） */
  meta: MessageMeta | null;
  /** 该条是否正在流式接收 */
  isStreaming: boolean;
  /** 用于展开引用原文的教材 id */
  documentId: string | null;
  onClearAnchor?: () => void;
}

/** 知识范围徽章：课本内用中性色、含拓展用琥珀、未覆盖用红（颜色即语义） */
function ScopeBadge({ scope }: { scope: MessageOut["knowledge_scope"] }): ReactElement | null {
  if (scope === null) {
    return null;
  }
  if (scope === "syllabus_strict") {
    return (
      <span className="rounded-xs border border-line-strong px-1.5 py-px text-xxs text-content-secondary">
        课本内
      </span>
    );
  }
  if (scope === "hybrid_extended") {
    return (
      <span className="rounded-xs border border-scope-extended/50 bg-scope-extended/10 px-1.5 py-px text-xxs text-scope-extended">
        含超纲拓展
      </span>
    );
  }
  return (
    <span className="rounded-xs border border-palette-red/50 bg-palette-red/10 px-1.5 py-px text-xxs text-palette-red">
      课本未覆盖
    </span>
  );
}

function MessageBubbleImpl({
  message,
  meta,
  isStreaming,
  documentId,
  onClearAnchor,
}: MessageBubbleProps): ReactElement {
  const [activeCitation, setActiveCitation] = useState<number | null>(null);

  const isUser = message.role === "user";
  const isBlank = message.content_markdown.trim().length === 0;

  /** 页码 chip → 找到页码区间包含它的那条引用并展开 */
  const handlePageRef = useCallback(
    (pageStart: number, pageEnd: number): void => {
      const index = message.citations.findIndex(
        (citation) => pageStart >= citation.page_start && pageEnd <= citation.page_end,
      );
      setActiveCitation(index === -1 ? null : index);
    },
    [message.citations],
  );

  if (isUser) {
    return (
      <div className="flex flex-col items-end gap-1">
        {message.anchor === null ? null : (
          <AnchorChip
            anchor={message.anchor}
            readOnly
            onClear={onClearAnchor ?? (() => undefined)}
            className="max-w-[85%]"
          />
        )}
        <div className="max-w-[85%] rounded-md rounded-br-xs bg-accent/20 px-3 py-2">
          <MarkdownView markdown={message.content_markdown} className="text-content-primary" />
        </div>
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <div
        aria-hidden
        className={cx(
          "mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xxs",
          isStreaming
            ? "animate-pulse-soft border-accent/60 bg-accent/20 text-accent"
            : "border-line-strong bg-surface-raised text-content-secondary",
        )}
      >
        助
      </div>

      <div className="min-w-0 flex-1">
        <div className="mb-1 flex flex-wrap items-center gap-1.5">
          <span className="text-xxs text-content-muted">智能助教</span>
          <ScopeBadge scope={message.knowledge_scope} />
          {message.demo_mode ? (
            <span className="rounded-xs border border-palette-amber/40 px-1 text-xxs text-palette-amber/90">
              演示数据
            </span>
          ) : null}
        </div>

        {/* P0-06③：把「为什么选这个形态」摊开给学生看，而不是黑箱 */}
        {meta?.modalityReason !== null && meta?.modalityReason !== undefined ? (
          <p className="mb-1 text-xxs leading-relaxed text-content-muted">
            形态决策：{meta.modalityReason}
          </p>
        ) : null}

        <div className="rounded-md rounded-tl-xs border border-line bg-surface-raised px-3 py-2">
          {isBlank && isStreaming ? (
            <p className="animate-pulse-soft text-xs text-content-muted">正在检索教材并组织答案…</p>
          ) : (
            <MarkdownView markdown={message.content_markdown} onPageRef={handlePageRef} />
          )}

          <CitationList
            citations={message.citations}
            documentId={documentId}
            activeIndex={activeCitation}
          />

          {message.artifact_id === null ? null : (
            <p className="mt-2 flex items-center gap-1 text-xxs text-content-muted">
              <span aria-hidden>▦</span>
              已更新右侧画布
              {message.artifact_version === null ? null : (
                <span className="font-mono">v{message.artifact_version}</span>
              )}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * 消息气泡。
 *
 * 用 `memo` 包一层很关键：流式追加时 store 只替换「正在流式的那一条」对象，
 * 其余消息引用不变，因此历史气泡不会重渲染（长会话下这是掉帧与否的分界线）。
 */
export const MessageBubble = memo(MessageBubbleImpl);
