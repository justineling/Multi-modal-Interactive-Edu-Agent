import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactElement } from "react";

import { getArtifact, toApiError } from "@/api/client";
import { CanvasToolbar } from "@/components/canvas/CanvasToolbar";
import { EmptyCanvas } from "@/components/canvas/EmptyCanvas";
import { SlideRenderer } from "@/components/canvas/SlideRenderer";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { ArtifactOut } from "@/types/api";
import type { ArtifactSpec } from "@/types/artifact";

interface ArtifactHostProps {
  onSamplePrompt: (text: string) => void;
}

/** 2D / 3D 渲染器由 B5 实现，这里先给出可读的占位（不假装能画） */
function ComingSoon({ spec }: { spec: ArtifactSpec }): ReactElement {
  const detail =
    spec.type === "anim_2d"
      ? `${spec.steps.length} 个关键帧`
      : spec.type === "scene_3d"
        ? `${spec.nodes.length} 个部件 · ${spec.camera_tour.length} 个机位`
        : "";
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
      <p className="text-sm text-content-secondary">该形态的渲染器正在接入</p>
      <p className="text-xxs text-content-muted">{detail}</p>
      <p className="max-w-sm text-xxs leading-relaxed text-content-muted">
        数据已完整拿到（受控 JSON AST），渲染层接上即可交互。当前可先阅读左侧助教的文字讲解。
      </p>
    </div>
  );
}

/**
 * 画布宿主（策略模式分发器，SPEC §1.1）。
 *
 * 分发只看 `spec.type`，三个分支各自独立：
 * ```
 * ppt      → SlideRenderer     （6 种布局再按 slide.layout 二级分发）
 * anim_2d  → Anim2DRenderer    （B5）
 * scene_3d → Scene3DRenderer   （B5）
 * ```
 * B5 只需把两个占位换成实现，本文件的分发逻辑不用改。
 *
 * 历史版本：选中某个版本时拉 `GET /api/artifacts/{id}?version=` 得到只读快照，
 * 并存成 `historySpec`；`readOnly` 由此推导，避免「在旧版本上继续 patch」造成版本分叉。
 */
export function ArtifactHost({ onSamplePrompt }: ArtifactHostProps): ReactElement {
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const activeArtifactId = useWorkspaceStore((state) => state.activeArtifactId);
  const activeVersion = useWorkspaceStore((state) => state.activeVersion);
  const setActiveVersion = useWorkspaceStore((state) => state.setActiveVersion);
  const anchor = useWorkspaceStore((state) => state.anchor);
  const setAnchor = useWorkspaceStore((state) => state.setAnchor);

  const [historySpec, setHistorySpec] = useState<ArtifactSpec | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number>(1);
  const [isFullscreen, setIsFullscreen] = useState<boolean>(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const artifact: ArtifactOut | null =
    artifacts.find((item) => item.id === activeArtifactId) ?? null;

  // 切换版本：null = 回到最新
  useEffect(() => {
    if (artifact === null || activeVersion === null) {
      setHistorySpec(null);
      setHistoryError(null);
      return;
    }
    if (activeVersion === artifact.version) {
      setHistorySpec(null);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setHistoryError(null);
    getArtifact(artifact.id, { version: activeVersion }, controller.signal)
      .then((snapshot) => {
        if (!cancelled) {
          setHistorySpec(snapshot.spec);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setHistoryError(toApiError(error).displayMessage);
          setHistorySpec(null);
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [artifact, activeVersion]);

  // 切换 artifact 时重置缩放
  useEffect(() => {
    setZoom(1);
  }, [activeArtifactId]);

  useEffect(() => {
    const handler = (): void => {
      setIsFullscreen(document.fullscreenElement !== null);
    };
    document.addEventListener("fullscreenchange", handler);
    return () => {
      document.removeEventListener("fullscreenchange", handler);
    };
  }, []);

  const toggleFullscreen = useCallback((): void => {
    const node = containerRef.current;
    if (node === null) {
      return;
    }
    if (document.fullscreenElement !== null) {
      void document.exitFullscreen();
      return;
    }
    void node.requestFullscreen();
  }, []);

  const spec = historySpec ?? artifact?.spec ?? null;
  const readOnly = historySpec !== null;

  return (
    <div
      ref={containerRef}
      className="flex h-full min-h-0 flex-col bg-surface-sunken"
    >
      {artifact === null || spec === null ? (
        <div className="flex min-h-0 flex-1 items-center justify-center">
          <EmptyCanvas onSamplePrompt={onSamplePrompt} />
        </div>
      ) : (
        <>
          <CanvasToolbar
            artifact={artifact}
            viewingVersion={readOnly ? activeVersion : null}
            onSelectVersion={setActiveVersion}
            zoom={zoom}
            onZoomChange={setZoom}
            onToggleFullscreen={toggleFullscreen}
            isFullscreen={isFullscreen}
            readOnly={readOnly}
          />

          {historyError === null ? null : (
            <p className="border-b border-palette-red/30 bg-palette-red/10 px-3 py-1 text-xxs text-palette-red">
              {historyError}
            </p>
          )}

          <div className="min-h-0 flex-1">
            <ErrorBoundary resetKey={`${artifact.id}-${activeVersion ?? "latest"}`}>
              {spec.type === "ppt" ? (
                <SlideRenderer
                  spec={spec}
                  artifactId={artifact.id}
                  anchor={readOnly ? null : anchor}
                  onAnchor={setAnchor}
                  readOnly={readOnly}
                  zoom={zoom}
                />
              ) : (
                <ComingSoon spec={spec} />
              )}
            </ErrorBoundary>
          </div>
        </>
      )}
    </div>
  );
}
