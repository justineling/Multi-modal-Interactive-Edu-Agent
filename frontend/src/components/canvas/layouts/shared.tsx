import type { ReactElement, ReactNode } from "react";

/**
 * 幻灯片设计基准。
 *
 * 为什么是「固定 1280×720 + transform 缩放」而不是流式字号：
 * 幻灯片是**版面艺术**，布局的正确性依赖字号、行距、留白的比例关系。
 * 用 `clamp()` / `vw` 做流式排版，在不同容器宽度下会出现「标题换行、卡片挤爆」的
 * 组合爆炸，几乎不可能调好；而固定设计稿 + 整体缩放可以保证**任何尺寸下版面完全一致**，
 * 只牺牲「字号不被用户单独放大」这一条（投影场景不需要）。
 * 缩放逻辑在 `SlideRenderer` 里，通过容器宽度 / 1280 计算 scale。
 */
export const SLIDE_WIDTH = 1280;
export const SLIDE_HEIGHT = 720;

/** 页面内边距：底部留 96 给来源角标，避免内容与角标打架 */
export const SLIDE_PADDING = "56px 72px 88px";

export interface SlideLayoutProps {
  /** 该页 AST 节点 */
  slide: import("@/types/artifact").SlideNode;
  /** 0 基序号 */
  pageIndex: number;
  totalPages: number;
}

/** 截断列表：超出 `max` 时只渲染前 `max` 条，并返回被隐藏的条数 */
export function truncateList<T>(
  items: readonly T[] | null,
  max: number,
): { visible: T[]; hidden: number } {
  if (items === null) {
    return { visible: [], hidden: 0 };
  }
  if (items.length <= max) {
    return { visible: [...items], hidden: 0 };
  }
  return { visible: items.slice(0, max), hidden: items.length - max };
}

/** 幻灯片底纸：浅色「纸张」质感，与深色画布形成对比（像真的在看课件） */
export function SlideCanvas({
  children,
  tone = "light",
}: {
  children: ReactNode;
  tone?: "light" | "dark";
}): ReactElement {
  return (
    <div
      className="relative flex h-full w-full flex-col overflow-hidden"
      style={{
        padding: SLIDE_PADDING,
        backgroundColor: tone === "light" ? "#FFFFFF" : "#0B1220",
        color: tone === "light" ? "#0F172A" : "#E8EEF9",
      }}
    >
      {children}
    </div>
  );
}

/** 版心标题：所有布局共用，保证「同一份课件看起来是一套」 */
export function SlideHeading({
  title,
  subtitle,
  accent = "#2563EB",
  tone = "light",
  size = 46,
}: {
  title: string;
  subtitle: string | null;
  accent?: string;
  tone?: "light" | "dark";
  size?: number;
}): ReactElement {
  return (
    <div className="shrink-0" style={{ marginBottom: 28 }}>
      <div className="flex items-start gap-3">
        <span
          aria-hidden
          style={{
            display: "block",
            width: 6,
            height: size * 0.92,
            borderRadius: 3,
            backgroundColor: accent,
            marginTop: 4,
          }}
        />
        <h2
          style={{
            fontSize: size,
            lineHeight: 1.22,
            fontWeight: 650,
            letterSpacing: "-0.01em",
            margin: 0,
            color: tone === "light" ? "#0F172A" : "#F8FAFC",
          }}
        >
          {title}
        </h2>
      </div>
      {subtitle === null || subtitle.length === 0 ? null : (
        <p
          style={{
            margin: "10px 0 0 18px",
            fontSize: 20,
            lineHeight: 1.5,
            color: tone === "light" ? "#64748B" : "#9DB0CC",
          }}
        >
          {subtitle}
        </p>
      )}
    </div>
  );
}

/** 「还有 N 条」提示：宁可截断也不让版面爆掉（P0-07①不白屏的具体落法） */
export function MoreHint({ count }: { count: number }): ReactElement | null {
  if (count <= 0) {
    return null;
  }
  return (
    <p style={{ marginTop: 12, fontSize: 16, color: "#94A3B8" }}>
      …等 {count} 条，完整内容见助教讲解
    </p>
  );
}

/** 缺字段时的占位：不抛错、不留白，明确告诉用户「这一页缺什么」 */
export function MissingField({ label }: { label: string }): ReactElement {
  return (
    <div
      className="flex flex-1 items-center justify-center rounded-lg"
      style={{ border: "2px dashed #CBD5E1", color: "#94A3B8", fontSize: 18 }}
    >
      本页缺少「{label}」，可让助教补充生成
    </div>
  );
}
