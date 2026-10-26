import { useState } from "react";
import type { ReactElement } from "react";

import { cx } from "@/components/common/cx";
import type { TocNode } from "@/types/api";

interface TocTreeProps {
  nodes: TocNode[];
  onPick: (node: TocNode) => void;
  /** 当前已选中的章节号（高亮） */
  pickedCode: string | null;
}

interface TocTreeItemProps {
  node: TocNode;
  depth: number;
  onPick: (node: TocNode) => void;
  pickedCode: string | null;
}

/**
 * 目录树单节点。
 *
 * 每层缩进 12px（`depth * 12`），不按 `level` 算——因为降级模式下 `level` 恒为 1，
 * 而实际嵌套仍可能出现；用渲染深度更稳。
 */
function TocTreeItem({ node, depth, onPick, pickedCode }: TocTreeItemProps): ReactElement {
  const [open, setOpen] = useState<boolean>(depth === 0);
  const hasChildren = node.children.length > 0;
  const isPicked = pickedCode === node.code;

  return (
    <li>
      <div
        className={cx(
          "group flex items-center gap-1 rounded-xs pr-1 transition-colors",
          isPicked ? "bg-accent/15" : "hover:bg-surface-overlay",
        )}
        style={{ paddingLeft: depth * 12 }}
      >
        <button
          type="button"
          aria-label={open ? "折叠" : "展开"}
          onClick={() => setOpen((value) => !value)}
          className={cx(
            "w-4 shrink-0 text-xxs text-content-muted transition-transform",
            hasChildren ? "hover:text-content-primary" : "invisible",
            open ? "rotate-90" : "",
          )}
        >
          ▸
        </button>

        <button
          type="button"
          onClick={() => onPick(node)}
          title={`${node.title}（p.${node.start_page}-${node.end_page}）`}
          className="flex min-w-0 flex-1 items-center gap-1.5 py-0.5 text-left"
        >
          <span
            className={cx(
              "shrink-0 font-mono text-xxs",
              isPicked ? "text-accent" : "text-content-muted",
            )}
          >
            {node.code}
          </span>
          <span
            className={cx(
              "truncate text-xs",
              isPicked ? "text-content-primary" : "text-content-secondary group-hover:text-content-primary",
            )}
          >
            {node.title}
          </span>
          <span className="ml-auto shrink-0 font-mono text-xxs text-content-muted">
            {node.start_page === node.end_page
              ? `p.${node.start_page}`
              : `p.${node.start_page}-${node.end_page}`}
          </span>
        </button>
      </div>

      {hasChildren && open ? (
        <ul>
          {node.children.map((child) => (
            <TocTreeItem
              key={child.code}
              node={child}
              depth={depth + 1}
              onPick={onPick}
              pickedCode={pickedCode}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** 教材目录树（P0-03）。点击节点把 `code` 回填到输入框（如「学习 4.1」）。 */
export function TocTree({ nodes, onPick, pickedCode }: TocTreeProps): ReactElement {
  if (nodes.length === 0) {
    return <p className="px-1 py-1 text-xxs text-content-muted">目录为空</p>;
  }
  return (
    <ul className="flex flex-col">
      {nodes.map((node) => (
        <TocTreeItem key={node.code} node={node} depth={0} onPick={onPick} pickedCode={pickedCode} />
      ))}
    </ul>
  );
}
