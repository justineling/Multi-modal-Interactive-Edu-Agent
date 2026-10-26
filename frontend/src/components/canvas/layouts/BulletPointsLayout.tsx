import type { ReactElement } from "react";

import { MissingField, SlideCanvas, SlideHeading, truncateList } from "@/components/canvas/layouts/shared";
import type { SlideLayoutProps } from "@/components/canvas/layouts/shared";

/** 单页最多渲染 7 条要点：超过就截断 + 提示，绝不把版面挤爆 */
const MAX_BULLETS = 7;

/**
 * `bullet_points` —— 要点列表。
 *
 * 要点：序号 + 宽松行距 + 左侧色块。逐条阅读的信息最怕「糊成一段」，
 * 序号给了学生「讲到第几条」的锚点，也方便助教在讲解里引用（「第 3 条最关键」）。
 */
export function BulletPointsLayout({ slide }: SlideLayoutProps): ReactElement {
  const { visible, hidden } = truncateList(slide.bullets, MAX_BULLETS);

  return (
    <SlideCanvas>
      <SlideHeading title={slide.title} subtitle={slide.subtitle} />

      {visible.length === 0 ? (
        <MissingField label="要点" />
      ) : (
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
            minHeight: 0,
          }}
        >
          <ul
            style={{
              margin: 0,
              padding: 0,
              listStyle: "none",
              display: "flex",
              flexDirection: "column",
              gap: 18,
            }}
          >
            {visible.map((bullet, index) => (
              <li key={`${index}-${bullet}`} style={{ display: "flex", gap: 18, alignItems: "flex-start" }}>
                <span
                  style={{
                    flexShrink: 0,
                    width: 34,
                    height: 34,
                    borderRadius: 10,
                    backgroundColor: index === 0 ? "#2563EB" : "#E2E8F0",
                    color: index === 0 ? "#FFFFFF" : "#475569",
                    fontSize: 17,
                    fontWeight: 600,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    marginTop: 2,
                  }}
                >
                  {index + 1}
                </span>
                <p style={{ margin: 0, fontSize: 24, lineHeight: 1.5, color: "#1E293B" }}>{bullet}</p>
              </li>
            ))}
          </ul>
          {hidden > 0 ? (
            <p style={{ marginTop: 14, fontSize: 16, color: "#94A3B8" }}>…等 {hidden} 条</p>
          ) : null}
        </div>
      )}
    </SlideCanvas>
  );
}
