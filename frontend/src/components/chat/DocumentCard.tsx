import { useState } from "react";
import type { ReactElement } from "react";

import { cx } from "@/components/common/cx";
import { TocTree } from "@/components/chat/TocTree";
import { isPageFallbackToc } from "@/types/api";
import type { DocumentOut, TocNode } from "@/types/api";

interface DocumentCardProps {
  document: DocumentOut;
  pickedCode: string | null;
  onPickSection: (node: TocNode) => void;
  onCancel: (documentId: string) => void;
  onDelete: (documentId: string) => void;
  onRetry: (document: DocumentOut) => void;
}

interface StatusStyle {
  label: string;
  className: string;
}

/** 五态样式：pending → parsing → ready | failed | cancelled（没有 processing） */
function statusStyle(document: DocumentOut): StatusStyle {
  switch (document.status) {
    case "pending":
      return { label: "排队中", className: "border-line-strong text-content-secondary" };
    case "parsing":
      return { label: `解析中 ${document.progress}%`, className: "border-accent/60 text-accent" };
    case "ready":
      return { label: "就绪", className: "border-palette-green/50 text-palette-green" };
    case "failed":
      return { label: "解析失败", className: "border-palette-red/50 text-palette-red" };
    case "cancelled":
      return { label: "已取消", className: "border-line-strong text-content-muted" };
    default:
      return { label: document.status, className: "border-line-strong text-content-muted" };
  }
}

function formatSize(pages: number): string {
  return pages > 0 ? `${pages} 页` : "—";
}

/**
 * 单个文档卡片：状态徽章 + 进度条 + 目录树。
 *
 * 进度百分比来自**解析进度**（`GET /documents/{id}/status` 的 `progress`），
 * 而不是上传字节进度 —— 用 fetch 上传拿不到字节级进度（要 XHR），
 * 但对学生来说「解析到第几页」才是有意义的那根进度条。
 */
export function DocumentCard({
  document,
  pickedCode,
  onPickSection,
  onCancel,
  onDelete,
  onRetry,
}: DocumentCardProps): ReactElement {
  const [tocOpen, setTocOpen] = useState<boolean>(true);
  const style = statusStyle(document);
  const isActive = document.status === "pending" || document.status === "parsing";
  const isFallback = document.status === "ready" && isPageFallbackToc(document.toc_tree);

  return (
    <div className="rounded-sm border border-line bg-surface-raised p-2">
      <div className="flex items-center gap-1.5">
        <span aria-hidden className="text-xs text-palette-red/80">
          PDF
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-content-primary" title={document.filename}>
          {document.filename}
        </span>
        <span
          className={cx(
            "shrink-0 rounded-xs border px-1 py-px font-mono text-xxs",
            style.className,
          )}
        >
          {style.label}
        </span>
      </div>

      <div className="mt-1 flex items-center gap-2">
        <span className="font-mono text-xxs text-content-muted">{formatSize(document.page_count)}</span>

        {isActive ? (
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface-sunken">
            <div
              className={cx(
                "h-full rounded-full bg-accent transition-[width] duration-300",
                document.status === "pending" ? "w-1/12 animate-pulse-soft" : "",
              )}
              style={document.status === "parsing" ? { width: `${Math.max(2, document.progress)}%` } : undefined}
            />
          </div>
        ) : (
          <span className="flex-1" />
        )}

        {isActive ? (
          <button
            type="button"
            onClick={() => onCancel(document.id)}
            className="shrink-0 text-xxs text-content-muted transition-colors hover:text-palette-red"
          >
            取消
          </button>
        ) : null}
        {document.status === "failed" ? (
          <button
            type="button"
            onClick={() => onRetry(document)}
            className="shrink-0 text-xxs text-accent transition-colors hover:underline"
          >
            重试
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => onDelete(document.id)}
          className="shrink-0 text-xxs text-content-muted transition-colors hover:text-palette-red"
        >
          删除
        </button>
      </div>

      {document.status === "failed" ? (
        <p className="mt-1 rounded-xs bg-palette-red/10 px-1.5 py-1 text-xxs leading-relaxed text-palette-red">
          {document.error_code === null ? "" : `[${document.error_code}] `}
          {document.error_message ?? "解析失败，请重试"}
        </p>
      ) : null}

      {document.status === "ready" ? (
        <div className="mt-1.5">
          <button
            type="button"
            onClick={() => setTocOpen((value) => !value)}
            className="flex w-full items-center gap-1 text-xxs text-content-muted transition-colors hover:text-content-secondary"
          >
            <span className={cx("transition-transform", tocOpen ? "rotate-90" : "")}>▸</span>
            目录（{document.toc_tree.length} 个一级章节）
          </button>

          {isFallback ? (
            <p className="mt-1 rounded-xs border border-palette-amber/40 bg-palette-amber/10 px-1.5 py-1 text-xxs text-palette-amber">
              本教材未能识别章节结构，已按页切分（仍可正常提问，只是无法按章节号精确锚定）
            </p>
          ) : null}

          {tocOpen ? (
            <div className="mt-1 max-h-56 overflow-y-auto pr-0.5">
              <TocTree nodes={document.toc_tree} onPick={onPickSection} pickedCode={pickedCode} />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
