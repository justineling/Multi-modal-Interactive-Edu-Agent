import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";

import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { cx } from "@/components/common/cx";
import { parseSourceRef } from "@/components/chat/pageRefs";
import { SLIDE_HEIGHT, SLIDE_WIDTH } from "@/components/canvas/layouts/shared";
import { LAYOUT_LABELS, getLayoutComponent } from "@/components/canvas/slideLayouts";
import { createAnchor } from "@/types/patch";
import type { SlideDeckSpec } from "@/types/artifact";
import type { Anchor } from "@/types/patch";

interface SlideRendererProps {
  spec: SlideDeckSpec;
  artifactId: string;
  /** 当前画布锚点；`node_path === /slides/{i}` 的那一页会描边高亮 */
  anchor: Anchor | null;
  onAnchor: (anchor: Anchor) => void;
  /** 历史版本只读态：禁止锚点追问（旧版本的 node_path 可能已不存在） */
  readOnly: boolean;
  /** 用户缩放倍率（1 = 自适应填满）；与容器自适应的 scale 相乘 */
  zoom?: number;
}

/** 设计稿尺寸 → 容器尺寸的缩放比。两个方向取小值，保证整页永远可见 */
function useFitScale(): [(node: HTMLDivElement | null) => void, number] {
  const [scale, setScale] = useState<number>(0.5);
  const observerRef = useRef<ResizeObserver | null>(null);

  const attach = useCallback((node: HTMLDivElement | null): void => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (node === null) {
      return;
    }
    const measure = (): void => {
      const rect = node.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) {
        return;
      }
      // 0.96 留一点边距，避免贴着容器边缘
      const next = Math.min(rect.width / SLIDE_WIDTH, rect.height / SLIDE_HEIGHT) * 0.96;
      setScale(next > 0.05 ? next : 0.05);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    observerRef.current = observer;
  }, []);

  useEffect(() => {
    return () => {
      observerRef.current?.disconnect();
      observerRef.current = null;
    };
  }, []);

  return [attach, scale];
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable;
}

/**
 * PPT 渲染器（B4 的门面）。
 *
 * 版面策略：**固定 1280×720 设计稿 + 等比 transform 缩放**。
 * 这样 6 种布局在任何容器尺寸下的比例关系完全一致（见 `layouts/shared.tsx` 注释）。
 *
 * 交互：
 * - ←/→ 翻页（输入框聚焦时不劫持按键，否则用户没法在提问里用方向键）；
 * - 点击页面 → 回传 `/slides/{i}` 锚点，并把 `source_ref` 里的章节号/页码一起带上
 *   （不带 `section_code` 会导致后端检索不锁定该小节，引用页码跑到同章其它页）；
 * - `speaker_notes` 默认折叠：它是助教的口语化讲解，不该抢版面的注意力，但很有价值。
 */
export function SlideRenderer({
  spec,
  artifactId,
  anchor,
  onAnchor,
  readOnly,
  zoom = 1,
}: SlideRendererProps): ReactElement {
  const [index, setIndex] = useState<number>(0);
  const [notesOpen, setNotesOpen] = useState<boolean>(false);
  const [attachScale, fitScale] = useFitScale();
  const scale = Math.max(0.05, fitScale * zoom);

  const total = spec.slides.length;
  const safeIndex = Math.min(Math.max(index, 0), Math.max(total - 1, 0));
  const slide = spec.slides[safeIndex];

  // 换一份 spec（切换 artifact / 版本）时回到第一页
  useEffect(() => {
    setIndex(0);
  }, [artifactId, spec]);

  const go = useCallback(
    (delta: number): void => {
      setIndex((current) => Math.min(Math.max(current + delta, 0), Math.max(total - 1, 0)));
    },
    [total],
  );

  useEffect(() => {
    const handler = (event: KeyboardEvent): void => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
        return;
      }
      if (isTypingTarget(event.target) || event.isComposing) {
        return;
      }
      event.preventDefault();
      go(event.key === "ArrowLeft" ? -1 : 1);
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
    };
  }, [go]);

  const anchoredIndex = useMemo(() => {
    if (anchor === null) {
      return -1;
    }
    const match = /^\/slides\/(\d+)$/.exec(anchor.node_path);
    if (match === null) {
      return -1;
    }
    const parsed = Number.parseInt(match[1] ?? "", 10);
    return Number.isFinite(parsed) ? parsed : -1;
  }, [anchor]);

  const handlePageClick = useCallback((): void => {
    if (readOnly || slide === undefined) {
      return;
    }
    const info = parseSourceRef(slide.source_ref);
    onAnchor(
      createAnchor({
        artifact_id: artifactId,
        node_path: `/slides/${safeIndex}`,
        node_label: `第 ${safeIndex + 1} 页 · ${slide.title}`,
        page: info.page,
        section_code: info.section_code,
      }),
    );
  }, [artifactId, onAnchor, readOnly, safeIndex, slide]);

  if (slide === undefined) {
    return <p className="p-4 text-xs text-content-muted">这份课件没有任何页面。</p>;
  }

  const Layout = getLayoutComponent(slide.layout);
  const source = parseSourceRef(slide.source_ref);
  const isAnchored = anchoredIndex === safeIndex;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={attachScale} className="relative flex min-h-0 flex-1 items-center justify-center p-3">
        <div
          style={{
            width: SLIDE_WIDTH * scale,
            height: SLIDE_HEIGHT * scale,
            position: "relative",
            flexShrink: 0,
          }}
        >
          <div
            role={readOnly ? undefined : "button"}
            tabIndex={readOnly ? -1 : 0}
            onClick={handlePageClick}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                handlePageClick();
              }
            }}
            title={readOnly ? undefined : "点击这一页，可以只针对它追问"}
            className={cx(
              "absolute left-0 top-0 origin-top-left overflow-hidden rounded-lg bg-white shadow-panel transition-shadow",
              readOnly ? "cursor-default" : "cursor-pointer hover:shadow-floating",
              isAnchored ? "ring-2 ring-accent ring-offset-2 ring-offset-surface-sunken" : "",
            )}
            style={{
              width: SLIDE_WIDTH,
              height: SLIDE_HEIGHT,
              transform: `scale(${scale})`,
            }}
          >
            <ErrorBoundary resetKey={`${artifactId}-${safeIndex}-${slide.layout}`}>
              <Layout slide={slide} pageIndex={safeIndex} totalPages={total} />
            </ErrorBoundary>

            {/* 来源角标：超纲来源用琥珀色，和「课本内」在视觉上分开 */}
            <span
              className="absolute"
              style={{
                right: 24,
                bottom: 18,
                maxWidth: 640,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                padding: "4px 10px",
                borderRadius: 999,
                fontSize: 14,
                border: source.isExtended ? "1px solid #F59E0B" : "1px solid #E2E8F0",
                backgroundColor: source.isExtended ? "#FFFBEB" : "#F8FAFC",
                color: source.isExtended ? "#B45309" : "#64748B",
              }}
            >
              {source.isExtended ? "超纲拓展 · " : ""}
              {slide.source_ref}
            </span>

            <span
              className="absolute font-mono"
              style={{ left: 24, bottom: 18, fontSize: 14, color: "#94A3B8" }}
            >
              {LAYOUT_LABELS[slide.layout]} · {safeIndex + 1}/{total}
            </span>
          </div>

          {readOnly ? (
            <span className="pointer-events-none absolute right-4 top-4 rounded-xs border border-palette-amber/60 bg-palette-amber/15 px-2 py-0.5 text-xxs text-palette-amber">
              历史版本 · 只读
            </span>
          ) : null}
        </div>
      </div>

      {/* 翻页 + 缩略图条 */}
      <div className="shrink-0 border-t border-line bg-surface-base px-3 py-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => go(-1)}
            disabled={safeIndex === 0}
            className="rounded-sm border border-line px-2 py-0.5 text-xs text-content-secondary transition-colors hover:border-accent/60 disabled:opacity-30"
            aria-label="上一页"
          >
            ←
          </button>
          <span className="font-mono text-xxs text-content-muted">
            {safeIndex + 1} / {total}
          </span>
          <button
            type="button"
            onClick={() => go(1)}
            disabled={safeIndex >= total - 1}
            className="rounded-sm border border-line px-2 py-0.5 text-xs text-content-secondary transition-colors hover:border-accent/60 disabled:opacity-30"
            aria-label="下一页"
          >
            →
          </button>

          <button
            type="button"
            onClick={() => setNotesOpen((value) => !value)}
            className={cx(
              "ml-2 rounded-sm px-2 py-0.5 text-xxs transition-colors",
              notesOpen ? "bg-accent/15 text-accent" : "text-content-muted hover:text-content-secondary",
            )}
          >
            助教讲解 {notesOpen ? "▾" : "▸"}
          </button>

          {readOnly ? null : (
            <span className="ml-auto text-xxs text-content-muted">
              点击页面可只针对这一页追问
            </span>
          )}
        </div>

        <div className="mt-2 flex gap-1 overflow-x-auto pb-1">
          {spec.slides.map((item, itemIndex) => (
            <button
              key={`${item.node_id}-${itemIndex}`}
              type="button"
              onClick={() => setIndex(itemIndex)}
              title={item.title}
              className={cx(
                "flex shrink-0 flex-col items-start gap-0.5 rounded-sm border px-2 py-1 text-left transition-colors",
                itemIndex === safeIndex
                  ? "border-accent/60 bg-accent/10"
                  : "border-line bg-surface-raised hover:border-line-strong",
                anchoredIndex === itemIndex ? "ring-1 ring-accent/60" : "",
              )}
              style={{ maxWidth: 132 }}
            >
              <span className="font-mono text-xxs text-content-muted">
                {itemIndex + 1} · {LAYOUT_LABELS[item.layout]}
              </span>
              <span className="w-full truncate text-xxs text-content-secondary">{item.title}</span>
            </button>
          ))}
        </div>

        {notesOpen ? (
          <p className="mt-2 max-h-24 overflow-y-auto whitespace-pre-wrap rounded-sm border border-line bg-surface-raised px-2 py-1.5 text-xs leading-relaxed text-content-secondary">
            {slide.speaker_notes.length > 0 ? slide.speaker_notes : "（本页没有讲解）"}
          </p>
        ) : null}
      </div>
    </div>
  );
}
