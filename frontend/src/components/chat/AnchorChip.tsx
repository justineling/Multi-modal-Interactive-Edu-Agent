import type { ReactElement } from "react";

import { cx } from "@/components/common/cx";
import type { Anchor } from "@/types/patch";

interface AnchorChipProps {
  anchor: Anchor;
  onClear: () => void;
  /** 只读展示（历史消息里回看锚点用），不显示关闭按钮 */
  readOnly?: boolean;
  className?: string;
}

/**
 * 锚点 chip（P0-10①）。
 *
 * 三处信息缺一不可：
 * - `node_label`：学生认得出「第 3 页」「左侧矩形」；
 * - 页码：能对上教材，是「引用可信」的心理锚；
 * - `node_path`：等宽小字，让用户（和演示现场的评委）看到系统确实在按节点寻址，
 *   而不是又跑了一遍全量生成。
 */
export function AnchorChip({
  anchor,
  onClear,
  readOnly = false,
  className,
}: AnchorChipProps): ReactElement {
  const pageLabel = anchor.page === null ? null : `p.${anchor.page}`;
  const sectionLabel = anchor.section_code === null ? null : `§${anchor.section_code}`;

  return (
    <div
      className={cx(
        "flex max-w-full items-center gap-2 rounded-sm border border-accent/50 bg-accent/10 px-2 py-1",
        className,
      )}
      title={`锚点路径：${anchor.node_path}`}
    >
      <span aria-hidden className="text-xs text-accent">
        ⌖
      </span>
      <span className="truncate text-xs font-medium text-content-primary">
        {anchor.node_label.length > 0 ? anchor.node_label : "已选中画布节点"}
      </span>
      {pageLabel === null ? null : (
        <span className="shrink-0 font-mono text-xxs text-content-secondary">{pageLabel}</span>
      )}
      {sectionLabel === null ? null : (
        <span className="shrink-0 font-mono text-xxs text-content-muted">{sectionLabel}</span>
      )}
      <span className="shrink-0 truncate font-mono text-xxs text-content-muted opacity-70">
        {anchor.node_path}
      </span>
      {readOnly ? null : (
        <button
          type="button"
          onClick={onClear}
          aria-label="取消锚点"
          className="ml-auto shrink-0 rounded-xs px-1 text-xs text-content-muted transition-colors hover:bg-surface-overlay hover:text-content-primary"
        >
          ×
        </button>
      )}
    </div>
  );
}
