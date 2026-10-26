import { useState } from "react";
import type { ReactElement } from "react";

import { cx } from "@/components/common/cx";
import { VersionMenu } from "@/components/canvas/VersionMenu";
import type { ArtifactOut } from "@/types/api";

interface CanvasToolbarProps {
  artifact: ArtifactOut;
  /** 正在查看的历史版本；null = 跟随最新 */
  viewingVersion: number | null;
  onSelectVersion: (version: number | null) => void;
  zoom: number;
  onZoomChange: (zoom: number) => void;
  onToggleFullscreen: () => void;
  isFullscreen: boolean;
  readOnly: boolean;
}

const TYPE_META: Record<ArtifactOut["artifact_type"], { label: string; className: string }> = {
  ppt: { label: "PPT", className: "border-palette-blue/50 bg-palette-blue/15 text-palette-blue" },
  anim_2d: { label: "2D", className: "border-palette-sky/50 bg-palette-sky/15 text-palette-sky" },
  scene_3d: {
    label: "3D",
    className: "border-palette-indigo/50 bg-palette-indigo/15 text-palette-indigo",
  },
};

const ZOOM_STEP = 0.15;
const ZOOM_MIN = 0.6;
const ZOOM_MAX = 1.8;

/**
 * 画布工具条。
 *
 * 刻意不做「导出 PDF / 分享」这类按钮：MVP 阶段它们只会带来「点了没反应」的坏印象，
 * 保留的每个按钮都必须是真能用的。
 */
export function CanvasToolbar({
  artifact,
  viewingVersion,
  onSelectVersion,
  zoom,
  onZoomChange,
  onToggleFullscreen,
  isFullscreen,
  readOnly,
}: CanvasToolbarProps): ReactElement {
  const [menuOpen, setMenuOpen] = useState<boolean>(false);
  const type = TYPE_META[artifact.artifact_type];

  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-line bg-surface-raised px-3 py-1.5">
      <span className={cx("shrink-0 rounded-xs border px-1.5 py-px text-xxs font-medium", type.className)}>
        {type.label}
      </span>

      <span className="min-w-0 truncate text-xs text-content-primary" title={artifact.title}>
        {artifact.title}
      </span>

      {readOnly && viewingVersion !== null ? (
        <span className="shrink-0 rounded-xs border border-palette-amber/60 bg-palette-amber/15 px-1.5 py-px text-xxs text-palette-amber">
          历史版本 v{viewingVersion} · 只读
        </span>
      ) : null}

      <div className="ml-auto flex items-center gap-1.5">
        <div className="flex items-center gap-1 rounded-sm border border-line px-1">
          <button
            type="button"
            onClick={() => onZoomChange(Math.max(ZOOM_MIN, Number((zoom - ZOOM_STEP).toFixed(2))))}
            disabled={zoom <= ZOOM_MIN}
            className="px-1 text-xs text-content-secondary transition-colors hover:text-content-primary disabled:opacity-30"
            aria-label="缩小"
          >
            −
          </button>
          <button
            type="button"
            onClick={() => onZoomChange(1)}
            className="min-w-[3rem] font-mono text-xxs text-content-muted transition-colors hover:text-content-secondary"
            title="恢复默认缩放"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            type="button"
            onClick={() => onZoomChange(Math.min(ZOOM_MAX, Number((zoom + ZOOM_STEP).toFixed(2))))}
            disabled={zoom >= ZOOM_MAX}
            className="px-1 text-xs text-content-secondary transition-colors hover:text-content-primary disabled:opacity-30"
            aria-label="放大"
          >
            ＋
          </button>
        </div>

        <button
          type="button"
          onClick={onToggleFullscreen}
          className="rounded-sm border border-line px-2 py-0.5 text-xxs text-content-secondary transition-colors hover:border-accent/60 hover:text-content-primary"
        >
          {isFullscreen ? "退出全屏" : "全屏"}
        </button>

        <div className="relative">
          <button
            type="button"
            onClick={() => setMenuOpen((value) => !value)}
            className={cx(
              "rounded-sm border px-2 py-0.5 text-xxs transition-colors",
              menuOpen
                ? "border-accent/60 bg-accent/10 text-accent"
                : "border-line text-content-secondary hover:border-accent/60 hover:text-content-primary",
            )}
          >
            版本 v{artifact.version} ▾
          </button>
          {menuOpen ? (
            <VersionMenu
              artifactId={artifact.id}
              latestVersion={artifact.version}
              viewingVersion={viewingVersion}
              onSelect={onSelectVersion}
              onClose={() => setMenuOpen(false)}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
