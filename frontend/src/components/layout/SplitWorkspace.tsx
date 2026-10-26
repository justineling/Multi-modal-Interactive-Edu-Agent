import { Allotment } from "allotment";
import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { ArtifactHost } from "@/components/canvas/ArtifactHost";
import { ChatPanel } from "@/components/chat/ChatPanel";
import type { DraftRequest } from "@/components/chat/Composer";
import { TopBar } from "@/components/layout/TopBar";
import { useWorkspaceSession } from "@/hooks/useWorkspaceSession";
import { LAYOUT } from "@/lib/theme";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { StreamMode } from "@/types/api";

/**
 * 双栏工作区（正式版，替换 B1 的占位实现）。
 *
 * 状态归属：
 * - `mode`（课本内 / 含拓展）与 `draft`（外部填入输入框的草稿）由本组件持有，
 *   因为它们同时被**顶栏**（左侧子树）与**画布空态**（右侧子树）读写，
 *   放在这里比塞进 store 更轻。
 * - `currentWorkspaceId` 来自 store，并作为 `ChatPanel` 的 `key`：
 *   切换工作区时强制重挂载，从而自动 abort 进行中的流、清空会话内的消息附加信息
 *   （`modality_reason` / `retrieval` 后端不落库，跨工作区复用一定是脏数据）。
 */
export function SplitWorkspace(): ReactElement {
  const { bootstrapping, bootError, retry } = useWorkspaceSession();
  const currentWorkspaceId = useWorkspaceStore((state) => state.currentWorkspaceId);
  const [mode, setMode] = useState<StreamMode>("syllabus_strict");
  const [draft, setDraft] = useState<DraftRequest>({ text: "", token: 0 });

  const applyDraft = useCallback((text: string): void => {
    setDraft((previous) => ({ text, token: previous.token + 1 }));
  }, []);

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-surface-base text-content-primary">
      <TopBar mode={mode} onModeChange={setMode} bootstrapping={bootstrapping} />

      {bootError === null ? null : (
        <div className="flex items-center gap-2 border-b border-palette-red/40 bg-palette-red/10 px-3 py-1.5">
          <p className="flex-1 text-xxs text-palette-red">
            无法连接后端：{bootError}（请确认 `uvicorn` 已在 :8000 启动）
          </p>
          <button
            type="button"
            onClick={retry}
            className="rounded-sm border border-palette-red/50 px-2 py-0.5 text-xxs text-palette-red transition-colors hover:bg-palette-red/15"
          >
            重试
          </button>
        </div>
      )}

      <main className="min-h-0 flex-1">
        <Allotment
          defaultSizes={[LAYOUT.chatDefaultPercent, 100 - LAYOUT.chatDefaultPercent]}
          vertical={false}
        >
          <Allotment.Pane minSize={LAYOUT.chatMinWidth}>
            <ChatPanel
              key={currentWorkspaceId ?? "no-workspace"}
              workspaceId={currentWorkspaceId}
              mode={mode}
              draft={draft}
              onSamplePrompt={applyDraft}
            />
          </Allotment.Pane>

          <Allotment.Pane minSize={LAYOUT.canvasMinWidth}>
            <ArtifactHost onSamplePrompt={applyDraft} />
          </Allotment.Pane>
        </Allotment>
      </main>
    </div>
  );
}
