import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  createWorkspace,
  deleteWorkspace,
  listWorkspaces,
  toApiError,
  updateWorkspace,
} from "@/api/client";
import { cx } from "@/components/common/cx";
import { LAYOUT } from "@/lib/theme";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { StreamMode } from "@/types/api";

interface TopBarProps {
  mode: StreamMode;
  onModeChange: (mode: StreamMode) => void;
  bootstrapping: boolean;
}

type EditMode = "none" | "create" | "rename";

const MODE_OPTIONS: ReadonlyArray<{ value: StreamMode; label: string; hint: string }> = [
  { value: "syllabus_strict", label: "课本内", hint: "严格依据教材作答，答案可直接用于考试" },
  { value: "hybrid_extended", label: "含拓展", hint: "课本标准答案 + 深度拓展（明确标注边界）" },
];

/**
 * 顶栏。
 *
 * 一条不可妥协的产品要求（P0-12③）：`demo_mode=true` 时黄标必须**常驻且显眼**。
 * 演示模式意味着画布内容来自确定性 mock 而非真实模型，静默伪装成真模型
 * 会让演示现场的信誉一次性归零，因此这里不做任何「可关闭」的设计。
 */
export function TopBar({ mode, onModeChange, bootstrapping }: TopBarProps): ReactElement {
  const workspaces = useWorkspaceStore((state) => state.workspaces);
  const currentWorkspaceId = useWorkspaceStore((state) => state.currentWorkspaceId);
  const demoMode = useWorkspaceStore((state) => state.demoMode);
  const setCurrentWorkspaceId = useWorkspaceStore((state) => state.setCurrentWorkspaceId);
  const setWorkspaces = useWorkspaceStore((state) => state.setWorkspaces);

  const [editMode, setEditMode] = useState<EditMode>("none");
  const [draftTitle, setDraftTitle] = useState<string>("");
  const [confirmDelete, setConfirmDelete] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const current = workspaces.find((workspace) => workspace.id === currentWorkspaceId) ?? null;

  const reload = useCallback(async (): Promise<void> => {
    const list = await listWorkspaces();
    setWorkspaces(list);
  }, [setWorkspaces]);

  const handleCreate = useCallback(async (): Promise<void> => {
    const title = draftTitle.trim();
    if (title.length === 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createWorkspace({ title });
      await reload();
      setCurrentWorkspaceId(created.id);
      setEditMode("none");
      setDraftTitle("");
    } catch (createError) {
      setError(toApiError(createError).displayMessage);
    } finally {
      setBusy(false);
    }
  }, [draftTitle, reload, setCurrentWorkspaceId]);

  const handleRename = useCallback(async (): Promise<void> => {
    const title = draftTitle.trim();
    if (title.length === 0 || current === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await updateWorkspace(current.id, { title });
      await reload();
      setEditMode("none");
      setDraftTitle("");
    } catch (renameError) {
      setError(toApiError(renameError).displayMessage);
    } finally {
      setBusy(false);
    }
  }, [draftTitle, current, reload]);

  const handleDelete = useCallback(async (): Promise<void> => {
    if (current === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await deleteWorkspace(current.id);
      await reload();
      setConfirmDelete(false);
    } catch (deleteError) {
      setError(toApiError(deleteError).displayMessage);
    } finally {
      setBusy(false);
    }
  }, [current, reload]);

  return (
    <header
      className="flex shrink-0 items-center gap-2 border-b border-line bg-surface-raised px-3"
      style={{ height: LAYOUT.topBarHeight }}
    >
      <span className="shrink-0 text-sm font-semibold tracking-wide text-content-primary">
        多模态沉浸式智能助教
      </span>

      <span className="mx-1 h-4 w-px bg-line" aria-hidden />

      {editMode === "none" ? (
        <>
          <select
            value={currentWorkspaceId ?? ""}
            onChange={(event) => setCurrentWorkspaceId(event.target.value || null)}
            className="max-w-[13rem] rounded-sm border border-line bg-surface-base px-2 py-1 text-xs text-content-secondary focus:border-accent focus:outline-none"
            aria-label="切换工作区"
          >
            {workspaces.length === 0 ? <option value="">（无工作区）</option> : null}
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.title}
              </option>
            ))}
          </select>

          <button
            type="button"
            onClick={() => {
              setEditMode("create");
              setDraftTitle("");
              setConfirmDelete(false);
            }}
            className="rounded-sm border border-line px-2 py-1 text-xxs text-content-secondary transition-colors hover:border-accent/60 hover:text-content-primary"
          >
            新建
          </button>
          <button
            type="button"
            disabled={current === null}
            onClick={() => {
              setEditMode("rename");
              setDraftTitle(current?.title ?? "");
              setConfirmDelete(false);
            }}
            className="rounded-sm border border-line px-2 py-1 text-xxs text-content-secondary transition-colors hover:border-accent/60 hover:text-content-primary disabled:opacity-40"
          >
            重命名
          </button>
          {confirmDelete ? (
            <span className="flex items-center gap-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => void handleDelete()}
                className="rounded-sm border border-palette-red/60 bg-palette-red/10 px-2 py-1 text-xxs text-palette-red disabled:opacity-40"
              >
                确认删除
              </button>
              <button
                type="button"
                onClick={() => setConfirmDelete(false)}
                className="rounded-sm px-1 py-1 text-xxs text-content-muted hover:text-content-primary"
              >
                取消
              </button>
            </span>
          ) : (
            <button
              type="button"
              disabled={current === null}
              onClick={() => setConfirmDelete(true)}
              className="rounded-sm border border-line px-2 py-1 text-xxs text-content-secondary transition-colors hover:border-palette-red/60 hover:text-palette-red disabled:opacity-40"
            >
              删除
            </button>
          )}
        </>
      ) : (
        <span className="flex items-center gap-1">
          <input
            autoFocus
            value={draftTitle}
            onChange={(event) => setDraftTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                void (editMode === "create" ? handleCreate() : handleRename());
              }
              if (event.key === "Escape") {
                setEditMode("none");
              }
            }}
            placeholder={editMode === "create" ? "新工作区名称" : "新的名称"}
            maxLength={120}
            className="w-48 rounded-sm border border-accent/60 bg-surface-base px-2 py-1 text-xs text-content-primary focus:outline-none"
          />
          <button
            type="button"
            disabled={busy || draftTitle.trim().length === 0}
            onClick={() => void (editMode === "create" ? handleCreate() : handleRename())}
            className="rounded-sm bg-accent px-2 py-1 text-xxs text-accent-foreground disabled:opacity-40"
          >
            {busy ? "…" : "确定"}
          </button>
          <button
            type="button"
            onClick={() => setEditMode("none")}
            className="rounded-sm px-1 py-1 text-xxs text-content-muted hover:text-content-primary"
          >
            取消
          </button>
        </span>
      )}

      {error === null ? null : (
        <span className="max-w-[16rem] truncate text-xxs text-palette-red" title={error}>
          {error}
        </span>
      )}

      <div className="ml-auto flex items-center gap-2">
        {bootstrapping ? (
          <span className="animate-pulse-soft font-mono text-xxs text-content-muted">连接后端…</span>
        ) : null}

        {/* 模式切换：课本内 / 含拓展（PRD Q2 默认课本内） */}
        <div className="flex items-center rounded-sm border border-line p-0.5" role="group" aria-label="回答模式">
          {MODE_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              title={option.hint}
              onClick={() => onModeChange(option.value)}
              className={cx(
                "rounded-xs px-2 py-0.5 text-xxs transition-colors",
                mode === option.value
                  ? option.value === "hybrid_extended"
                    ? "bg-scope-extended/20 text-scope-extended"
                    : "bg-accent/20 text-accent"
                  : "text-content-muted hover:text-content-secondary",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>

        {/* P0-12③：演示模式黄标常驻，不可关闭 */}
        {demoMode ? (
          <span
            className="flex items-center gap-1 rounded-full border border-palette-amber/60 bg-palette-amber/15 px-2 py-0.5 text-xxs font-medium text-palette-amber"
            title="当前未配置 LLM_API_KEY，画布内容由确定性 MockGenerator 生成"
          >
            <span aria-hidden>●</span>
            演示模式
          </span>
        ) : (
          <span className="rounded-full border border-palette-green/50 px-2 py-0.5 text-xxs text-palette-green">
            模型在线
          </span>
        )}
      </div>
    </header>
  );
}
