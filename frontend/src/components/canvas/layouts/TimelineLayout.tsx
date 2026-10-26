import type { ReactElement } from "react";

import { MissingField, SlideCanvas, SlideHeading, truncateList } from "@/components/canvas/layouts/shared";
import type { SlideLayoutProps } from "@/components/canvas/layouts/shared";

/** 横向时间轴最多 5 个节点；超过就改成纵向列表，避免节点挤成一团 */
const MAX_HORIZONTAL = 5;
const MAX_VERTICAL = 7;
/** 卡片与中轴线之间的间距（px，设计稿坐标系） */
const AXIS_GAP = 30;

/**
 * `timeline` —— 时间轴。
 *
 * 数据来自后端扩展字段 `timeline_items: [{year, label, detail, source_page}]`，
 * 不是从 `bullets` 里硬拆字符串 —— 结构化字段才能可靠地渲染年份与页码。
 *
 * 两种排布：
 * - ≤5 个节点：横向时间轴，卡片在中轴线上下交错（绝对定位保证「圆点必然压在线中央」，
 *   用 flex order + transform 做过一版，圆点会随卡片高度漂移，已废弃）；
 * - >5 个节点：纵向列表，避免横向挤压导致文字叠字。
 */
export function TimelineLayout({ slide }: SlideLayoutProps): ReactElement {
  const items = slide.timeline_items;
  if (items === null || items.length === 0) {
    return (
      <SlideCanvas>
        <SlideHeading title={slide.title} subtitle={slide.subtitle} />
        <MissingField label="时间轴条目（timeline_items）" />
      </SlideCanvas>
    );
  }

  const horizontal = items.length <= MAX_HORIZONTAL;
  const { visible, hidden } = truncateList(items, horizontal ? MAX_HORIZONTAL : MAX_VERTICAL);

  return (
    <SlideCanvas>
      <SlideHeading title={slide.title} subtitle={slide.subtitle} />

      {horizontal ? (
        <div style={{ position: "relative", flex: 1, minHeight: 0 }}>
          {/* 中轴线：始终位于内容区垂直中央 */}
          <div
            aria-hidden
            style={{
              position: "absolute",
              left: 8,
              right: 8,
              top: "50%",
              height: 3,
              borderRadius: 2,
              transform: "translateY(-50%)",
              background: "linear-gradient(90deg,#2563EB,#0EA5E9,#F59E0B)",
              opacity: 0.35,
            }}
          />

          <div style={{ display: "flex", height: "100%" }}>
            {visible.map((item, index) => {
              const above = index % 2 === 0;
              return (
                <div key={`${item.year}-${index}`} style={{ flex: 1, position: "relative" }}>
                  <span
                    aria-hidden
                    style={{
                      position: "absolute",
                      top: "50%",
                      left: "50%",
                      width: 16,
                      height: 16,
                      borderRadius: 999,
                      backgroundColor: "#2563EB",
                      border: "3px solid #DBEAFE",
                      transform: "translate(-50%,-50%)",
                      zIndex: 1,
                    }}
                  />
                  <div
                    style={{
                      position: "absolute",
                      left: 6,
                      right: 6,
                      ...(above
                        ? { bottom: `calc(50% + ${AXIS_GAP}px)` }
                        : { top: `calc(50% + ${AXIS_GAP}px)` }),
                      padding: "10px 14px",
                      borderRadius: 10,
                      border: "1px solid #E2E8F0",
                      backgroundColor: "#FFFFFF",
                      boxShadow: "0 1px 2px rgba(15,23,42,0.06)",
                    }}
                  >
                    <p style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#2563EB" }}>{item.year}</p>
                    <p style={{ margin: "4px 0 0", fontSize: 17, lineHeight: 1.4, color: "#334155" }}>
                      {item.label}
                    </p>
                    {item.detail.length === 0 ? null : (
                      <p style={{ margin: "4px 0 0", fontSize: 14, lineHeight: 1.4, color: "#94A3B8" }}>
                        {item.detail}
                      </p>
                    )}
                    {item.source_page === null ? null : (
                      <p style={{ margin: "6px 0 0", fontSize: 13, color: "#94A3B8" }}>p.{item.source_page}</p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", justifyContent: "center" }}>
          <ul
            style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 12 }}
          >
            {visible.map((item, index) => (
              <li key={`${item.year}-${index}`} style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
                <span
                  style={{
                    flexShrink: 0,
                    minWidth: 78,
                    padding: "4px 10px",
                    borderRadius: 8,
                    backgroundColor: "#EFF6FF",
                    color: "#2563EB",
                    fontSize: 18,
                    fontWeight: 700,
                    textAlign: "center",
                  }}
                >
                  {item.year}
                </span>
                <div>
                  <p style={{ margin: 0, fontSize: 21, lineHeight: 1.4, color: "#1E293B" }}>{item.label}</p>
                  {item.detail.length === 0 ? null : (
                    <p style={{ margin: "2px 0 0", fontSize: 16, lineHeight: 1.45, color: "#64748B" }}>
                      {item.detail}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ul>
          {hidden > 0 ? (
            <p style={{ marginTop: 12, fontSize: 16, color: "#94A3B8" }}>…等 {hidden} 个节点未显示</p>
          ) : null}
        </div>
      )}

      {horizontal && hidden > 0 ? (
        <p style={{ marginTop: 12, fontSize: 16, color: "#94A3B8" }}>…等 {hidden} 个节点未显示</p>
      ) : null}
    </SlideCanvas>
  );
}
