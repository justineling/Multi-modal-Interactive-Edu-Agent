import type { ReactElement } from "react";

import { MissingField, SlideCanvas, SlideHeading, truncateList } from "@/components/canvas/layouts/shared";
import type { SlideLayoutProps } from "@/components/canvas/layouts/shared";

const MAX_ROWS = 6;

/**
 * `comparison` —— 左右对比。
 *
 * 为什么按「行」对齐而不是两个独立列表：对比布局的价值在于**同一维度上横向看差异**。
 * 两个各自滚动的列表会让学生来回扫视找对应项，反而增加认知负担。
 * 因此这里把 `comparison_left[i]` 与 `comparison_right[i]` 放在同一行，
 * 中间一条竖线，两栏用不同强调色（蓝 / 琥珀）区分。
 */
export function ComparisonLayout({ slide }: SlideLayoutProps): ReactElement {
  const left = truncateList(slide.comparison_left, MAX_ROWS);
  const right = truncateList(slide.comparison_right, MAX_ROWS);
  const rowCount = Math.max(left.visible.length, right.visible.length);
  const hidden = Math.max(left.hidden, right.hidden);
  const rowIndexes = Array.from({ length: rowCount }, (_unused, index) => index);

  if (rowCount === 0) {
    return (
      <SlideCanvas>
        <SlideHeading title={slide.title} subtitle={slide.subtitle} />
        <MissingField label="对比内容（comparison_left / comparison_right）" />
      </SlideCanvas>
    );
  }

  return (
    <SlideCanvas>
      <SlideHeading title={slide.title} subtitle={slide.subtitle} />

      <div style={{ display: "flex", flex: 1, minHeight: 0, alignItems: "stretch" }}>
        <div
          style={{
            flex: 1,
            paddingRight: 28,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
          }}
        >
          <p style={{ margin: "0 0 14px", fontSize: 18, fontWeight: 600, color: "#2563EB" }}>对比 A</p>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {rowIndexes.map((index) => (
              <div
                key={`l-${index}`}
                style={{
                  minHeight: 58,
                  padding: "12px 16px",
                  borderRadius: 10,
                  backgroundColor: index % 2 === 0 ? "#EFF6FF" : "#F8FAFC",
                  borderLeft: "3px solid #2563EB",
                  fontSize: 21,
                  lineHeight: 1.45,
                  color: "#1E293B",
                  display: "flex",
                  alignItems: "center",
                }}
              >
                {left.visible[index] ?? ""}
              </div>
            ))}
          </div>
        </div>

        <div
          aria-hidden
          style={{ width: 1, alignSelf: "stretch", backgroundColor: "#E2E8F0", margin: "0 4px" }}
        />

        <div
          style={{
            flex: 1,
            paddingLeft: 28,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
          }}
        >
          <p style={{ margin: "0 0 14px", fontSize: 18, fontWeight: 600, color: "#B45309" }}>对比 B</p>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {rowIndexes.map((index) => (
              <div
                key={`r-${index}`}
                style={{
                  minHeight: 58,
                  padding: "12px 16px",
                  borderRadius: 10,
                  backgroundColor: index % 2 === 0 ? "#FFFBEB" : "#F8FAFC",
                  borderLeft: "3px solid #F59E0B",
                  fontSize: 21,
                  lineHeight: 1.45,
                  color: "#1E293B",
                  display: "flex",
                  alignItems: "center",
                }}
              >
                {right.visible[index] ?? ""}
              </div>
            ))}
          </div>
        </div>
      </div>

      {hidden > 0 ? (
        <p style={{ marginTop: 12, fontSize: 16, color: "#94A3B8" }}>…等 {hidden} 行未显示</p>
      ) : null}
    </SlideCanvas>
  );
}
