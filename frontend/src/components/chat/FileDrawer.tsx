import { useCallback, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, ReactElement } from "react";

import { cancelDocument, deleteDocument, toApiError, uploadDocument } from "@/api/client";
import { cx } from "@/components/common/cx";
import { DocumentCard } from "@/components/chat/DocumentCard";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { DocumentOut, TocNode } from "@/types/api";

interface FileDrawerProps {
  workspaceId: string | null;
  onPickSection: (node: TocNode) => void;
  pickedCode: string | null;
}

const PDF_ACCEPT = ".pdf,application/pdf";

/**
 * 文件区（P0-02 / P0-03）：拖拽或点击上传 PDF → 进度 → 目录树。
 *
 * 只接受 PDF：后端 `SUPPORTED_FILE_TYPE` 也会拦，但前端先拦能省一次往返，
 * 而且能立刻给出中文提示。
 */
export function FileDrawer({ workspaceId, onPickSection, pickedCode }: FileDrawerProps): ReactElement {
  const documents = useWorkspaceStore((state) => state.documents);
  const upsertDocument = useWorkspaceStore((state) => state.upsertDocument);
  const removeDocument = useWorkspaceStore((state) => state.removeDocument);
  const [open, setOpen] = useState<boolean>(true);
  const [uploading, setUploading] = useState<boolean>(false);
  const [dragging, setDragging] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  /** 失败重试需要原始 File，按 documentId 记住 */
  const filesRef = useRef<Map<string, File>>(new Map());

  const upload = useCallback(
    async (file: File): Promise<void> => {
      if (workspaceId === null) {
        setError("请先选择一个工作区");
        return;
      }
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        setError(`仅支持 PDF 文件，收到「${file.name}」`);
        return;
      }
      setError(null);
      setUploading(true);
      try {
        const document = await uploadDocument(workspaceId, file);
        filesRef.current.set(document.id, file);
        upsertDocument(document);
        setOpen(true);
      } catch (uploadError) {
        setError(toApiError(uploadError).displayMessage);
      } finally {
        setUploading(false);
      }
    },
    [workspaceId, upsertDocument],
  );

  const handleInputChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>): void => {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (file !== undefined) {
        void upload(file);
      }
    },
    [upload],
  );

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>): void => {
      event.preventDefault();
      setDragging(false);
      const file = event.dataTransfer.files[0];
      if (file !== undefined) {
        void upload(file);
      }
    },
    [upload],
  );

  const handleCancel = useCallback(async (documentId: string): Promise<void> => {
    try {
      const document = await cancelDocument(documentId);
      upsertDocument(document);
    } catch (cancelError) {
      setError(toApiError(cancelError).displayMessage);
    }
  }, [upsertDocument]);

  const handleDelete = useCallback(
    async (documentId: string): Promise<void> => {
      try {
        await deleteDocument(documentId);
        filesRef.current.delete(documentId);
        removeDocument(documentId);
      } catch (deleteError) {
        setError(toApiError(deleteError).displayMessage);
      }
    },
    [removeDocument],
  );

  const handleRetry = useCallback(
    async (document: DocumentOut): Promise<void> => {
      const file = filesRef.current.get(document.id);
      if (file === undefined) {
        setError("原始文件已不在内存中，请重新选择文件上传");
        inputRef.current?.click();
        return;
      }
      await handleDelete(document.id);
      await upload(file);
    },
    [handleDelete, upload],
  );

  return (
    <div className="border-b border-line bg-surface-base">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-surface-raised/60"
      >
        <span className={cx("text-xxs text-content-muted transition-transform", open ? "rotate-90" : "")}>
          ▸
        </span>
        <span className="text-xs font-medium text-content-secondary">教材</span>
        <span className="font-mono text-xxs text-content-muted">{documents.length}</span>
        <span className="ml-auto text-xxs text-content-muted">{open ? "收起" : "展开"}</span>
      </button>

      {open ? (
        <div className="max-h-[38vh] overflow-y-auto px-3 pb-2">
          <div
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
            onClick={() => inputRef.current?.click()}
            className={cx(
              "flex cursor-pointer flex-col items-center gap-1 rounded-sm border border-dashed px-3 py-3 text-center transition-colors",
              dragging
                ? "border-accent bg-accent/10"
                : "border-line-strong hover:border-accent/60 hover:bg-surface-raised/60",
            )}
          >
            <p className="text-xs text-content-secondary">
              {uploading ? "正在上传…" : "拖入 PDF 或点击选择"}
            </p>
            <p className="text-xxs text-content-muted">支持章节号识别，用于按「4.1」精确锚定</p>
            <input
              ref={inputRef}
              type="file"
              accept={PDF_ACCEPT}
              onChange={handleInputChange}
              className="hidden"
            />
          </div>

          {error === null ? null : (
            <div className="mt-1.5 flex items-start gap-1.5 rounded-sm border border-palette-red/40 bg-palette-red/10 px-2 py-1">
              <p className="flex-1 text-xxs leading-relaxed text-palette-red">{error}</p>
              <button
                type="button"
                onClick={() => setError(null)}
                className="text-xxs text-palette-red/70 hover:text-palette-red"
              >
                ×
              </button>
            </div>
          )}

          <div className="mt-1.5 flex flex-col gap-1.5">
            {documents.length === 0 ? (
              <p className="px-1 text-xxs text-content-muted">还没有教材。上传一本 PDF 就能开始。</p>
            ) : (
              documents.map((document) => (
                <DocumentCard
                  key={document.id}
                  document={document}
                  pickedCode={pickedCode}
                  onPickSection={onPickSection}
                  onCancel={(id) => void handleCancel(id)}
                  onDelete={(id) => void handleDelete(id)}
                  onRetry={(item) => void handleRetry(item)}
                />
              ))
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
