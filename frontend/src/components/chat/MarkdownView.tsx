import { Children, isValidElement, useMemo } from "react";
import type { ReactElement, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

import { cx } from "@/components/common/cx";
import { formatPageRange, isExtendedHeading, splitPageRefs } from "@/components/chat/pageRefs";

/** 页码 chip 点击回调：交给 MessageBubble 去展开对应引用的原文 */
export type PageRefHandler = (pageStart: number, pageEnd: number) => void;

interface MarkdownViewProps {
  markdown: string;
  onPageRef?: PageRefHandler;
  className?: string;
}

/**
 * 从一个 React 子节点里提取纯文本（只用于判断 blockquote 的标题是不是「深度拓展」）。
 *
 * `node.props` 在 React 19 的类型里是 `unknown`，这里必须断言一次；
 * 这是第三方边界（React 元素内部结构）唯一无法用类型表达的地方。
 */
function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map((child) => nodeText(child)).join("");
  }
  if (isValidElement(node)) {
    const props: unknown = node.props;
    if (typeof props === "object" && props !== null && "children" in props) {
      return nodeText((props as { children?: ReactNode }).children);
    }
  }
  return "";
}

/** 行内页码 chip：`[p.112]` → 可点击，点击后展开该引用的教材原文 */
function PageRefChip({
  label,
  pageStart,
  pageEnd,
  onPageRef,
}: {
  label: string;
  pageStart: number;
  pageEnd: number;
  onPageRef?: PageRefHandler;
}): ReactElement {
  const clickable = onPageRef !== undefined;
  const text = formatPageRange(pageStart, pageEnd);
  if (!clickable) {
    return <span className="page-chip">{text}</span>;
  }
  return (
    <button
      type="button"
      title={`查看 ${text} 的教材原文`}
      onClick={() => onPageRef(pageStart, pageEnd)}
      className="page-chip transition-colors hover:border-accent/70 hover:bg-accent/20 hover:text-content-primary"
    >
      {text}
      {label === text ? null : <span className="sr-only">{label}</span>}
    </button>
  );
}

/** 把一段纯文本按页码标记切成 chip + 文本 */
function TextWithPageRefs({
  text,
  onPageRef,
}: {
  text: string;
  onPageRef?: PageRefHandler;
}): ReactElement {
  const tokens = useMemo(() => splitPageRefs(text), [text]);
  return (
    <>
      {tokens.map((token, index) =>
        token.kind === "text" ? (
          <span key={`t-${index}`}>{token.text}</span>
        ) : (
          <PageRefChip
            key={`p-${index}-${token.pageStart}`}
            label={token.raw}
            pageStart={token.pageStart}
            pageEnd={token.pageEnd}
            onPageRef={onPageRef}
          />
        ),
      )}
    </>
  );
}

/** 只在字符串子节点上做页码识别；嵌套元素（strong/em）原样透传，避免与 Markdown 解析器打架 */
function withPageRefs(children: ReactNode, onPageRef?: PageRefHandler): ReactNode {
  return Children.map(children, (child, index) => {
    if (typeof child === "string") {
      return <TextWithPageRefs key={`s-${index}`} text={child} onPageRef={onPageRef} />;
    }
    return child;
  });
}

/**
 * Markdown 渲染器：GFM 表格 + LaTeX + 页码 chip + 超纲区块。
 *
 * 为什么不自己写 Markdown 解析：`react-markdown` 走 unified 生态，
 * 输出的是一棵 React 树而不是 `dangerouslySetInnerHTML` 字符串，
 * 因此天然免疫「模型在 markdown 里夹带 HTML」这类注入（P0-07② 的配套要求）。
 */
export function MarkdownView({ markdown, onPageRef, className }: MarkdownViewProps): ReactElement {
  const components = useMemo<Components>(() => {
    const textRenderer = (children: ReactNode): ReactNode => withPageRefs(children, onPageRef);

    return {
      p({ children }) {
        return <p className="my-1.5 leading-relaxed">{textRenderer(children)}</p>;
      },
      li({ children }) {
        return <li className="my-1 leading-relaxed">{textRenderer(children)}</li>;
      },
      td({ children }) {
        return <td>{textRenderer(children)}</td>;
      },
      a({ children, href }) {
        return (
          <a
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="text-palette-sky underline decoration-dotted underline-offset-2 hover:text-accent"
          >
            {children}
          </a>
        );
      },
      blockquote({ children }) {
        const plain = nodeText(children);
        // 后端 hybrid_extended 用 `> **深度拓展（超出课本范围）**` 包裹拓展内容
        if (isExtendedHeading(plain)) {
          return (
            <blockquote className="scope-extended my-2 border-l-2 border-scope-extended py-1 pl-3">
              <div className="mb-1 flex items-center gap-1.5">
                <span className="rounded-xs bg-scope-extended/15 px-1.5 py-px text-xxs font-medium text-scope-extended">
                  超纲拓展
                </span>
                <span className="text-xxs text-content-muted">非考试基准，仅供深挖</span>
              </div>
              <div className="text-[0.95em] text-content-secondary">{textRenderer(children)}</div>
            </blockquote>
          );
        }
        return (
          <blockquote className="my-2 border-l-2 border-line-strong pl-3 text-content-secondary">
            {textRenderer(children)}
          </blockquote>
        );
      },
    };
  }, [onPageRef]);

  return (
    <div className={cx("md-body text-sm text-content-primary", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
