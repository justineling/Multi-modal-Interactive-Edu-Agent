import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { cx } from "@/components/common/cx";
import { VersionMenu } from "@/components/canvas/VersionMenu";
import { ExportPdfError, canExportPdf, exportPdf } from "@/lib/exportPdf";
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
 * 按钮悬停提示：把「会弹系统打印对话框、要自己选另存为 PDF」提前讲清楚。
 *
 * 顺带提醒取消「页眉和页脚」：Chrome 的打印对话框**默认勾选**它，
 * 而 `@page { margin: 0 }` 压不住它 —— 实测（Chrome 156 headless + CDP
 * `displayHeaderFooter: true`）页眉/页脚会**叠在幻灯片内容上**，
 * 在深色封面上尤其明显。CSS 没有任何办法关掉这个选项，只能在提示里说明。
 */
const EXPORT_HINT =
  "打印对话框里把「目标打印机」选为「另存为 PDF」，并取消勾选「页眉和页脚」";

/**
 * 画布工具条。
 *
 * 关于「导出 PDF」：这是**唯一**一个会弹系统对话框的按钮，因此必须在按钮上
 * 把预期讲明白 —— 用户点了之后看到的是打印预览而不是直接下载文件，
 * 不提前说明就会被当成「按钮坏了」。
 *
 * 实现走浏览器打印管线（`window.print()`）而不是截图 + jsPDF：
 * 公式是 KaTeX 的 SVG 字形，截图管线会丢字形；打印产出的是矢量 PDF，放大不糊。
 * 详见 `src/lib/exportPdf.ts` 的模块注释。
 *
 * 只有 `ppt` 可导出：2D / 3D 是实时渲染的 canvas / WebGL，打印只会得到
 * 当前帧的空白或糊图，与其给一个骗人的按钮，不如显式禁用并说明原因。
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
  const [exporting, setExporting] = useState<boolean>(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const type = TYPE_META[artifact.artifact_type];
  const exportable = canExportPdf(artifact.artifact_type);

  const handleExportPdf = useCallback((): void => {
    if (!exportable || exporting) {
      return;
    }
    setExporting(true);
    setExportError(null);
    void exportPdf()
      .catch((error: unknown) => {
        // 失败原因直接展示给用户：静默失败会被误认为「点了没反应」
        setExportError(
          error instanceof ExportPdfError ? error.message : "导出失败，请稍后重试",
        );
      })
      .finally(() => {
        setExporting(false);
      });
  }, [exportable, exporting]);

  return (
    <div className="no-print flex shrink-0 items-center gap-2 border-b border-line bg-surface-raised px-3 py-1.5">
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

      {exportError === null ? null : (
        <span className="max-w-[16rem] shrink-0 truncate text-xxs text-palette-red" title={exportError}>
          {exportError}
        </span>
      )}

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

        <button
          type="button"
          onClick={handleExportPdf}
          disabled={!exportable || exporting}
          title={exportable ? EXPORT_HINT : "当前形态暂不支持导出"}
          aria-label={exportable ? `导出 PDF：${EXPORT_HINT}` : "当前形态暂不支持导出"}
          className={cx(
            "rounded-sm border px-2 py-0.5 text-xxs transition-colors",
            exportable
              ? "border-line text-content-secondary hover:border-accent/60 hover:text-content-primary"
              : "border-line text-content-muted opacity-40",
            exporting ? "cursor-progress border-accent/60 text-accent" : "",
          )}
        >
          {exporting ? "打开打印…" : "导出 PDF"}
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
