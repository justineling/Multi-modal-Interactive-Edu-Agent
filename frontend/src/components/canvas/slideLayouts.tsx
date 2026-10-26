import type { ComponentType } from "react";

import { BulletPointsLayout } from "@/components/canvas/layouts/BulletPointsLayout";
import { CauseEffectLayout } from "@/components/canvas/layouts/CauseEffectLayout";
import { ComparisonLayout } from "@/components/canvas/layouts/ComparisonLayout";
import { QuizLayout } from "@/components/canvas/layouts/QuizLayout";
import { TimelineLayout } from "@/components/canvas/layouts/TimelineLayout";
import { TitleHeroLayout } from "@/components/canvas/layouts/TitleHeroLayout";
import type { SlideLayoutProps } from "@/components/canvas/layouts/shared";
import type { SlideLayout } from "@/types/artifact";

export type { SlideLayoutProps };

/**
 * 布局注册表（策略模式，SPEC §1.1）。
 *
 * 这是「LLM 只填 JSON、前端确定性渲染」的落点：
 * `SlideRenderer` 拿到一页后只做 `LAYOUT_REGISTRY[slide.layout]`，
 * **不存在任何「根据内容猜布局」的分支**，也不执行模型生成的代码。
 * 新增布局 = 加一个组件 + 在这里注册一行，其它地方不用动。
 */
export const LAYOUT_REGISTRY: Record<SlideLayout, ComponentType<SlideLayoutProps>> = {
  title_hero: TitleHeroLayout,
  bullet_points: BulletPointsLayout,
  comparison: ComparisonLayout,
  timeline: TimelineLayout,
  cause_effect: CauseEffectLayout,
  quiz: QuizLayout,
};

/** 中文布局名：工具栏与降级提示里用，避免把 `bullet_points` 这种内部标识暴露给学生 */
export const LAYOUT_LABELS: Record<SlideLayout, string> = {
  title_hero: "封面",
  bullet_points: "要点",
  comparison: "对比",
  timeline: "时间轴",
  cause_effect: "因果链",
  quiz: "随堂测验",
};

/** 未知 layout 的兜底：用要点布局渲染，绝不白屏 */
export function getLayoutComponent(layout: SlideLayout): ComponentType<SlideLayoutProps> {
  return LAYOUT_REGISTRY[layout] ?? BulletPointsLayout;
}
