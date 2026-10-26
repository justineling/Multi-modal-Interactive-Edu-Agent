import type { ReactElement } from "react";

import { MissingField, SlideCanvas, SlideHeading, truncateList } from "@/components/canvas/layouts/shared";
import type { SlideLayoutProps } from "@/components/canvas/layouts/shared";

/** 单页最多渲染 5 段因果链，超出截断 */
const MAX_STEPS = 5;

/**
 * `cause_effect` —— 因果链。
 *
 * AST 里没有独立的因果字段（SPEC 只给了 `bullets`），因此约定：
 * `bullets` 按顺序就是「因 → 中间环节 → 果」。
 * 若某一条里自带 `→` / `->`，再拆成子步骤，从而支持「多级因果」：
 *
 * ```
 * 关税提高 → 进口减少 → 本地工业兴起
 * ```
 *
 * 渲染成横向链条，箭头用「→」字符而不是 SVG：字符天然参与文字排版，
 * 换行时不会像绝对定位的箭头那样错位。
 */
export function CauseEffectLayout({ slide }: SlideLayoutProps): ReactElement {
  const { visible, hidden } = truncateList(slide.bullets, MAX_STEPS);

  if (visible.length === 0) {
    return (
      <SlideCanvas>
        <SlideHeading title={slide.title} subtitle={slide.subtitle} />
        <MissingField label="因果步骤（bullets）" />
      </SlideCanvas>
    );
  }

  return (
    <SlideCanvas>
      <SlideHeading title={slide.title} subtitle={slide.subtitle} />

      <div style={{ display: "flex", flex: 1, alignItems: "center", flexWrap: "wrap", gap: 14 }}>
        {visible.map((step, index) => {
          const parts = step
            .split(/→|->|⇒/)
            .map((part) => part.trim())
            .filter((part) => part.length > 0);
          const isLast = index === visible.length - 1;

          return (
            <div key={`${index}-${step}`} style={{ display: "flex", alignItems: "center", gap: 14 }}>
              <div
                style={{
                  minWidth: 180,
                  maxWidth: 250,
                  padding: "16px 18px",
                  borderRadius: 12,
                  border: isLast ? "1px solid #10B981" : "1px solid #E2E8F0",
                  backgroundColor: isLast ? "#ECFDF5" : "#FFFFFF",
                  boxShadow: "0 1px 2px rgba(15,23,42,0.05)",
                }}
              >
                {parts.length > 1 ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {parts.map((part, partIndex) => (
                      <span
                        key={`${partIndex}-${part}`}
                        style={{
                          fontSize: 19,
                          lineHeight: 1.45,
                          color: partIndex === parts.length - 1 ? "#0F172A" : "#475569",
                          fontWeight: partIndex === parts.length - 1 ? 600 : 400,
                        }}
                      >
                        {part}
                        {partIndex === parts.length - 1 ? "" : " →"}
                      </span>
                    ))}
                  </div>
                ) : (
                  <p style={{ margin: 0, fontSize: 21, lineHeight: 1.45, color: "#1E293B" }}>
                    {step}
                  </p>
                )}
              </div>

              {isLast ? null : (
                <span aria-hidden style={{ fontSize: 26, color: "#94A3B8", lineHeight: 1 }}>
                  →
                </span>
              )}
            </div>
          );
        })}
      </div>

      {hidden > 0 ? (
        <p style={{ marginTop: 12, fontSize: 16, color: "#94A3B8" }}>…等 {hidden} 段未显示</p>
      ) : null}
    </SlideCanvas>
  );
}
