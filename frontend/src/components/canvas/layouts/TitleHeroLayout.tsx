import type { ReactElement } from "react";

import { SlideCanvas } from "@/components/canvas/layouts/shared";
import type { SlideLayoutProps } from "@/components/canvas/layouts/shared";

/**
 * `title_hero` —— 封面页。
 *
 * 要点：留白要「敢留」。封面是学生看这份课件的第一个 3 秒，
 * 挤满字的封面会让整份材料显得廉价；这里只放主题、副标题和一条细装饰线。
 * 用深色底是为了和后面几页的白色内容页形成节奏对比（翻页时有「开场感」）。
 */
export function TitleHeroLayout({ slide, pageIndex, totalPages }: SlideLayoutProps): ReactElement {
  return (
    <SlideCanvas tone="dark">
      <div className="flex flex-1 flex-col justify-center" style={{ paddingLeft: 8 }}>
        <span
          style={{
            display: "inline-block",
            alignSelf: "flex-start",
            padding: "6px 14px",
            borderRadius: 999,
            border: "1px solid rgba(37,99,235,0.55)",
            backgroundColor: "rgba(37,99,235,0.16)",
            color: "#93C5FD",
            fontSize: 16,
            letterSpacing: "0.08em",
            marginBottom: 28,
          }}
        >
          {slide.node_id.toUpperCase()}
        </span>

        <h1
          style={{
            margin: 0,
            fontSize: 76,
            lineHeight: 1.12,
            fontWeight: 700,
            letterSpacing: "-0.02em",
            color: "#F8FAFC",
            maxWidth: 980,
          }}
        >
          {slide.title}
        </h1>

        {slide.subtitle === null || slide.subtitle.length === 0 ? null : (
          <p style={{ margin: "22px 0 0", fontSize: 26, color: "#9DB0CC", maxWidth: 860 }}>
            {slide.subtitle}
          </p>
        )}

        <div
          aria-hidden
          style={{
            width: 96,
            height: 4,
            borderRadius: 2,
            marginTop: 40,
            background: "linear-gradient(90deg,#2563EB,#0EA5E9)",
          }}
        />

        {slide.bullets.length === 0 ? null : (
          <ul style={{ margin: "32px 0 0", padding: 0, listStyle: "none", display: "flex", gap: 28 }}>
            {slide.bullets.slice(0, 3).map((bullet) => (
              <li key={bullet} style={{ fontSize: 20, color: "#9DB0CC", display: "flex", gap: 8 }}>
                <span style={{ color: "#0EA5E9" }}>—</span>
                {bullet}
              </li>
            ))}
          </ul>
        )}
      </div>

      <p style={{ position: "absolute", right: 72, top: 56, fontSize: 16, color: "#475569" }}>
        {pageIndex + 1} / {totalPages}
      </p>
    </SlideCanvas>
  );
}
