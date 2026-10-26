import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import { listChunks } from "@/api/client";
import { cx } from "@/components/common/cx";
import { formatPageRange } from "@/components/chat/pageRefs";
import type { ChunkOut } from "@/types/api";
import type { Citation } from "@/types/artifact";

interface CitationListProps {
  citations: Citation[];
  /** 用于按 section_code 拉原文；null 表示当前 workspace 找不到对应教材 */
  documentId: string | null;
  /** 由页码 chip 点进来时要展开的那一条（索引） */
  activeIndex: number | null;
}

interface ChunkState {
  loading: boolean;
  error: string | null;
  chunks: ChunkOut[];
}

/**
 * 引用列表 + 原文展开（P0-04②）。
 *
 * 「引用可信」是这个产品的核心体感：学生看到 `[p.112]` 时，必须能一键看到
 * 那一页到底写了什么，否则引用就只是一串装饰性文字。
 * 因此每个 chip 都能点开，按 `section_code` 拉真实切片（`GET /documents/{id}/chunks`）。
 */
export function CitationList({ citations, documentId, activeIndex }: CitationListProps): ReactElement | null {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [cache, setCache] = useState<Record<number, ChunkState>>({});

  const load = useCallback(
    async (index: number, sectionCode: string): Promise<void> => {
      if (documentId === null) {
        return;
      }
      setCache((previous) => ({
        ...previous,
        [index]: { loading: true, error: null, chunks: previous[index]?.chunks ?? [] },
      }));
      try {
        const chunks = await listChunks(documentId, { section_code: sectionCode, limit: 3 });
        setCache((previous) => ({
          ...previous,
          [index]: { loading: false, error: null, chunks },
        }));
      } catch (error) {
        setCache((previous) => ({
          ...previous,
          [index]: {
            loading: false,
            error: error instanceof Error ? error.message : "原文加载失败",
            chunks: [],
          },
        }));
      }
    },
    [documentId],
  );

  // 从行内页码 chip 点进来时，自动展开对应引用
  useEffect(() => {
    if (activeIndex === null) {
      return;
    }
    setExpanded(activeIndex);
    const citation = citations[activeIndex];
    if (citation !== undefined && cache[activeIndex] === undefined) {
      void load(activeIndex, citation.section_code);
    }
  }, [activeIndex, citations, cache, load]);

  if (citations.length === 0) {
    return null;
  }

  return (
    <div className="mt-2 border-t border-line/60 pt-2">
      <div className="mb-1 flex items-center gap-1.5">
        <span className="text-xxs font-medium uppercase tracking-wide text-content-muted">
          依据出处
        </span>
        <span className="text-xxs text-content-muted">（点击展开教材原文）</span>
      </div>
      <ul className="flex flex-col gap-1">
        {citations.map((citation, index) => {
          const state = cache[index];
          const isOpen = expanded === index;
          return (
            <li key={`${citation.section_code}-${citation.page_start}-${index}`}>
              <button
                type="button"
                onClick={() => {
                  const next = isOpen ? null : index;
                  setExpanded(next);
                  if (next !== null && state === undefined) {
                    void load(index, citation.section_code);
                  }
                }}
                className={cx(
                  "flex w-full items-center gap-2 rounded-sm border px-2 py-1 text-left transition-colors",
                  isOpen
                    ? "border-accent/50 bg-accent/10"
                    : "border-line bg-surface-sunken/60 hover:border-line-strong hover:bg-surface-raised",
                )}
              >
                <span className="font-mono text-xxs text-content-muted">§{citation.section_code}</span>
                <span className="truncate text-xs text-content-secondary">{citation.label}</span>
                <span className="ml-auto shrink-0 font-mono text-xxs text-palette-sky">
                  {formatPageRange(citation.page_start, citation.page_end)}
                </span>
                <span aria-hidden className="shrink-0 text-xxs text-content-muted">
                  {isOpen ? "▾" : "▸"}
                </span>
              </button>

              {isOpen ? (
                <div className="mt-1 rounded-sm border border-line/70 bg-surface-sunken px-2 py-1.5">
                  {documentId === null ? (
                    <p className="text-xxs text-content-muted">当前工作区没有找到对应教材，无法展开原文。</p>
                  ) : state === undefined || state.loading ? (
                    <p className="text-xxs text-content-muted">正在读取教材原文…</p>
                  ) : state.error !== null ? (
                    <p className="text-xxs text-palette-red">{state.error}</p>
                  ) : state.chunks.length === 0 ? (
                    <p className="text-xxs text-content-muted">该章节没有可显示的切片。</p>
                  ) : (
                    <div className="flex flex-col gap-1.5">
                      {state.chunks.map((chunk) => (
                        <div key={chunk.id}>
                          <div className="mb-0.5 flex items-center gap-2">
                            <span className="font-mono text-xxs text-content-muted">
                              {formatPageRange(chunk.page_start, chunk.page_end)}
                            </span>
                            {chunk.section_title.length > 0 ? (
                              <span className="truncate text-xxs text-content-secondary">
                                {chunk.section_title}
                              </span>
                            ) : null}
                          </div>
                          <p className="whitespace-pre-wrap text-xs leading-relaxed text-content-secondary">
                            {chunk.content.length > 480
                              ? `${chunk.content.slice(0, 480)}…`
                              : chunk.content}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
