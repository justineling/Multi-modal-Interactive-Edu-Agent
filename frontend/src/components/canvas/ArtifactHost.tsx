import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";

import { getArtifact, toApiError } from "@/api/client";
import { deriveAnchorFallback } from "@/components/canvas/anchors";
import { CanvasToolbar } from "@/components/canvas/CanvasToolbar";
import { EmptyCanvas } from "@/components/canvas/EmptyCanvas";
import { SlideRenderer } from "@/components/canvas/SlideRenderer";
import { ErrorBoundary, renderFallback } from "@/components/common/ErrorBoundary";
import { useFocusPreset } from "@/hooks/useArtifactPatch";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { ArtifactOut } from "@/types/api";
import type { ArtifactSpec } from "@/types/artifact";

/**
 * 2D / 3D 渲染器按需加载。
 *
 * 它们各自拖着一条重依赖（Konva ≈ 150 kB、three + drei ≈ 600 kB），而**首屏根本用不到**：
 * 学生进来第一件事是上传教材、看 PPT。用 `lazy` 拆出去后主包不受影响，
 * 也让 3D 的「首帧 ≤ 5s」（P0-09③）少背几百 kB 的解析时间。
 */
const Anim2DRenderer = lazy(() =>
  import("@/components/canvas/Anim2DRenderer").then((module) => ({
    default: module.Anim2DRenderer,
  })),
);

const Scene3DRenderer = lazy(() =>
  import("@/components/canvas/Scene3DRenderer").then((module) => ({
    default: module.Scene3DRenderer,
  })),
);

interface ArtifactHostProps {
  onSamplePrompt: (text: string) => void;
}

/** 懒加载期间的可读占位（不显示转圈：转圈会让人以为「卡了」，一句话更安心） */
function RendererLoading({ label }: { label: string }): ReactElement {
  return (
    <div className="flex h-full items-center justify-center px-6">
      <p className="text-xs text-content-muted">正在加载{label}渲染器…</p>
    </div>
  );
}

/**
 * 画布宿主（策略模式分发器，SPEC §1.1）。
 *
 * 分发只看 `spec.type`，三个分支各自独立：
 * ```
 * ppt      → SlideRenderer     （6 种布局再按 slide.layout 二级分发）
 * anim_2d  → Anim2DRenderer    （Konva + GSAP，懒加载）
 * scene_3d → Scene3DRenderer   （R3F + Drei，懒加载）
 * ```
 * 每个分支都单独套 `ErrorBoundary`：畸形 spec 只会让「这一块」降级成可读提示，
 * 不会把整个工作区搞白屏（P0-07①）。
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
  const messages = useWorkspaceStore((state) => state.messages);
  const storeError = useWorkspaceStore((state) => state.error);
  const setError = useWorkspaceStore((state) => state.setError);

  const [historySpec, setHistorySpec] = useState<ArtifactSpec | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [zoom, setZoom] = useState<number>(1);
  const [isFullscreen, setIsFullscreen] = useState<boolean>(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const [focusPreset, consumeFocusPreset] = useFocusPreset();

  const artifact: ArtifactOut | null =
    artifacts.find((item) => item.id === activeArtifactId) ?? null;

  /**
   * 锚点兜底上下文。
   *
   * 2D/3D 元素上的 `source_page` 可能为 `null`（LLM 没填），此时要退到「最近一次检索的
   * 小节与页码」。`citations` 是落库的，刷新后依然可用，因此从消息历史推导而不是从
   * `useChatStream` 的会话内 state 取（后者刷新即丢）。
   */
  const anchorFallback = useMemo(() => deriveAnchorFallback(messages), [messages]);

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
  const rendererKey = `${artifact?.id ?? "none"}-${activeVersion ?? "latest"}`;

  return (
    <div ref={containerRef} className="flex h-full min-h-0 flex-col bg-surface-sunken">
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

          {/* 补丁失败（含 409 冲突回滚）的提示条：补丁影响的是画布，提示就放在画布上 */}
          {storeError === null ? null : (
            <div className="flex items-start gap-2 border-b border-palette-red/30 bg-palette-red/10 px-3 py-1">
              <p className="flex-1 text-xxs leading-relaxed text-palette-red">{storeError}</p>
              <button
                type="button"
                onClick={() => {
                  setError(null);
                }}
                className="shrink-0 text-xxs text-palette-red/70 transition-colors hover:text-palette-red"
              >
                知道了
              </button>
            </div>
          )}

          <div className="min-h-0 flex-1">
            <ErrorBoundary
              resetKey={rendererKey}
              fallback={(error, reset) => renderFallback(error, reset)}
            >
              {spec.type === "ppt" ? (
                <SlideRenderer
                  spec={spec}
                  artifactId={artifact.id}
                  anchor={readOnly ? null : anchor}
                  onAnchor={setAnchor}
                  readOnly={readOnly}
                  zoom={zoom}
                />
              ) : null}

              {spec.type === "anim_2d" ? (
                <Suspense fallback={<RendererLoading label="2D 动画" />}>
                  <ErrorBoundary resetKey={`${rendererKey}-anim`}>
                    <Anim2DRenderer
                      spec={spec}
                      artifactId={artifact.id}
                      anchor={readOnly ? null : anchor}
                      onAnchor={setAnchor}
                      readOnly={readOnly}
                      fallback={anchorFallback}
                      zoom={zoom}
                    />
                  </ErrorBoundary>
                </Suspense>
              ) : null}

              {spec.type === "scene_3d" ? (
                <Suspense fallback={<RendererLoading label="3D 场景" />}>
                  <ErrorBoundary resetKey={`${rendererKey}-scene`}>
                    <Scene3DRenderer
                      spec={spec}
                      artifactId={artifact.id}
                      anchor={readOnly ? null : anchor}
                      onAnchor={setAnchor}
                      readOnly={readOnly}
                      fallback={anchorFallback}
                      focusPreset={focusPreset}
                      onFocusConsumed={consumeFocusPreset}
                    />
                  </ErrorBoundary>
                </Suspense>
              ) : null}
            </ErrorBoundary>
          </div>
        </>
      )}
    </div>
  );
}
