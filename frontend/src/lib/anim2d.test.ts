/**
 * `lib/anim2d` 单测：坐标换算与补间参数。
 *
 * 重点不是覆盖率，而是把三件「写错就整张图散架」的事钉死：
 * 1. 逻辑坐标 1000×600 与容器像素的等比映射（含居中偏移）；
 * 2. `entityBox` 统一用左上角包围盒（补间坐标系的唯一基准）；
 * 3. 关键帧切换时「老元素移动、新元素淡入」的迁移语义。
 */

import { describe, expect, it } from "vitest";

import {
  clampPixelRatio,
  clampStepIndex,
  computeStageTransform,
  computeStepTransition,
  entityBox,
  entityLogicalSize,
  narrationOf,
  orderSteps,
  pageLabel,
  stepProgress,
  toLogical,
  toPixels,
  tweenDuration,
} from "@/lib/anim2d";
import { CANVAS_HEIGHT, CANVAS_WIDTH, DURATION_MS, MIN_CANVAS_SCALE } from "@/lib/theme";
import type { Entity2D, KeyframeStep } from "@/types/artifact";

function makeEntity(overrides: Partial<Entity2D> = {}): Entity2D {
  return {
    id: "e1",
    shape: "circle",
    label: "关税",
    x: 500,
    y: 300,
    color: "#2563EB",
    details: "关税提高了进口成本",
    source_page: 94,
    ...overrides,
  };
}

describe("computeStageTransform", () => {
  it("容器正好 1000×600 时是 1:1，无偏移", () => {
    const transform = computeStageTransform({ width: CANVAS_WIDTH, height: CANVAS_HEIGHT });
    expect(transform.scale).toBe(1);
    expect(transform.offsetX).toBe(0);
    expect(transform.offsetY).toBe(0);
    expect(transform.tooSmall).toBe(false);
  });

  it("容器变窄时按宽度等比缩放，并在纵向居中留白", () => {
    const transform = computeStageTransform({ width: 500, height: 600 });
    expect(transform.scale).toBe(0.5);
    expect(transform.offsetX).toBe(0);
    // 600 - 600*0.5 = 300 → 上下各留 150
    expect(transform.offsetY).toBe(150);
  });

  it("容器变矮时按高度等比缩放，并在横向居中留白", () => {
    const transform = computeStageTransform({ width: 2000, height: 300 });
    expect(transform.scale).toBe(0.5);
    expect(transform.offsetX).toBe((2000 - 1000 * 0.5) / 2);
    expect(transform.offsetY).toBe(0);
  });

  it("容器为 0 时不产生 NaN / Infinity", () => {
    const transform = computeStageTransform({ width: 0, height: 0 });
    expect(Number.isFinite(transform.scale)).toBe(true);
    expect(transform.scale).toBeGreaterThan(0);
    expect(transform.tooSmall).toBe(true);
  });

  it("缩放比低于下限时给出 tooSmall 提示", () => {
    const tiny = computeStageTransform({ width: 100, height: 60 });
    expect(tiny.scale).toBeLessThan(MIN_CANVAS_SCALE);
    expect(tiny.tooSmall).toBe(true);
  });
});

describe("toPixels / toLogical", () => {
  const transform = computeStageTransform({ width: 500, height: 600 });

  it("逻辑中心点映射到容器像素中心", () => {
    expect(toPixels(500, 300, transform)).toEqual({ x: 250, y: 300 });
  });

  it("往返换算无损", () => {
    const pixel = toPixels(321, 123, transform);
    const logical = toLogical(pixel.x, pixel.y, transform);
    expect(logical.x).toBeCloseTo(321, 6);
    expect(logical.y).toBeCloseTo(123, 6);
  });
});

describe("entityBox", () => {
  it("circle 用中心点 + 固定逻辑尺寸换算成左上角包围盒", () => {
    const transform = computeStageTransform({ width: 1000, height: 600 });
    const box = entityBox(makeEntity(), transform);
    expect(box.width).toBeCloseTo(108, 6);
    expect(box.height).toBeCloseTo(108, 6);
    expect(box.x).toBeCloseTo(500 - 54, 6);
    expect(box.y).toBeCloseTo(300 - 54, 6);
  });

  it("label_box 宽度随文字长度增长，但夹在 [140, 460]", () => {
    const short = entityLogicalSize(makeEntity({ shape: "label_box", label: "短" }));
    const medium = entityLogicalSize(
      makeEntity({ shape: "label_box", label: "这是一段非常非常长的标签文字用来测试宽度上限" }),
    );
    const veryLong = entityLogicalSize(
      makeEntity({
        shape: "label_box",
        label: "超长标签".repeat(20),
      }),
    );
    expect(short.width).toBe(140);
    expect(medium.width).toBeGreaterThan(short.width);
    expect(medium.width).toBeLessThanOrEqual(460);
    expect(veryLong.width).toBe(460);
  });

  it("5 种 shape 都能算出正的宽高（不会出现 0 尺寸导致不可见）", () => {
    const transform = computeStageTransform({ width: 1000, height: 600 });
    for (const shape of ["circle", "rect", "arrow", "label_box", "zone"] as const) {
      const box = entityBox(makeEntity({ shape }), transform);
      expect(box.width).toBeGreaterThan(0);
      expect(box.height).toBeGreaterThan(0);
    }
  });
});

describe("pageLabel", () => {
  it("有页码时显示 p.N", () => {
    expect(pageLabel(makeEntity({ source_page: 94 }))).toBe("p.94");
  });

  it("页码为空或非法时给出可读文案而不是 undefined", () => {
    expect(pageLabel(makeEntity({ source_page: null }))).toBe("未标注页码");
    expect(pageLabel(makeEntity({ source_page: 0 }))).toBe("未标注页码");
  });
});

describe("clampPixelRatio", () => {
  it("夹在 [1, 2]", () => {
    expect(clampPixelRatio(3)).toBe(2);
    expect(clampPixelRatio(0.5)).toBe(1);
    expect(clampPixelRatio(1.5)).toBe(1.5);
  });

  it("非法值退化为 1", () => {
    expect(clampPixelRatio(Number.NaN)).toBe(1);
    expect(clampPixelRatio(0)).toBe(1);
  });
});

describe("clampStepIndex / stepProgress", () => {
  it("夹取到 [0, total-1]", () => {
    expect(clampStepIndex(-3, 5)).toBe(0);
    expect(clampStepIndex(9, 5)).toBe(4);
    expect(clampStepIndex(2, 5)).toBe(2);
  });

  it("空列表不抛错", () => {
    expect(clampStepIndex(3, 0)).toBe(0);
    expect(stepProgress(0, 0)).toBe(0);
  });

  it("进度从 0 到 1", () => {
    expect(stepProgress(0, 5)).toBe(0);
    expect(stepProgress(4, 5)).toBe(1);
    expect(stepProgress(2, 5)).toBeCloseTo(0.5, 6);
  });
});

describe("orderSteps", () => {
  it("按 step_index 升序（后端不保证数组顺序）", () => {
    const steps: KeyframeStep[] = [
      { step_index: 2, narration: "三", source_ref: "", entities: [] },
      { step_index: 0, narration: "一", source_ref: "", entities: [] },
      { step_index: 1, narration: "二", source_ref: "", entities: [] },
    ];
    expect(orderSteps(steps).map((step) => step.step_index)).toEqual([0, 1, 2]);
  });
});

describe("tweenDuration", () => {
  it("同一帧不需要补间", () => {
    expect(tweenDuration(3, 3)).toBe(0);
  });

  it("单步用统一节奏", () => {
    expect(tweenDuration(0, 1)).toBe(DURATION_MS.slow);
  });

  it("跨多步时长递增但有上限（最多按 4 步累加）", () => {
    expect(tweenDuration(0, 2)).toBe(DURATION_MS.slow + DURATION_MS.fast);
    expect(tweenDuration(0, 99)).toBe(DURATION_MS.slow + 4 * DURATION_MS.fast);
  });
});

describe("computeStepTransition", () => {
  const transform = computeStageTransform({ width: 1000, height: 600 });

  it("首帧所有元素淡入（fromOpacity = 0）", () => {
    const transitions = computeStepTransition(null, [makeEntity()], transform);
    expect(transitions).toHaveLength(1);
    expect(transitions[0]?.isNew).toBe(true);
    expect(transitions[0]?.fromOpacity).toBe(0);
    expect(transitions[0]?.toOpacity).toBe(1);
    // 起点即终点：淡入不位移
    expect(transitions[0]?.from).toEqual(transitions[0]?.to);
  });

  it("已存在的元素从上一帧位置移动到当前帧位置", () => {
    const previous = [makeEntity({ x: 200, y: 200 })];
    const current = [makeEntity({ x: 800, y: 400 })];
    const transitions = computeStepTransition(previous, current, transform);
    const transition = transitions[0];
    expect(transition?.isNew).toBe(false);
    expect(transition?.fromOpacity).toBe(1);
    expect(transition?.from.x).toBeCloseTo(200 - 54, 6);
    expect(transition?.to.x).toBeCloseTo(800 - 54, 6);
  });

  it("上一帧没有的元素按新元素淡入", () => {
    const previous = [makeEntity({ id: "old" })];
    const current = [makeEntity({ id: "new", label: "新元素" })];
    const transitions = computeStepTransition(previous, current, transform);
    expect(transitions).toHaveLength(1);
    expect(transitions[0]?.entity.id).toBe("new");
    expect(transitions[0]?.isNew).toBe(true);
  });

  it("消失的元素不渲染（关键帧语义是「此刻的完整构图」）", () => {
    const previous = [makeEntity({ id: "gone" }), makeEntity({ id: "stay" })];
    const current = [makeEntity({ id: "stay" })];
    const transitions = computeStepTransition(previous, current, transform);
    expect(transitions.map((item) => item.entity.id)).toEqual(["stay"]);
  });
});

describe("narrationOf", () => {
  it("空旁白给出可读占位", () => {
    expect(narrationOf(undefined).narration).toBe("（本帧没有旁白）");
    expect(
      narrationOf({ step_index: 0, narration: "  ", source_ref: " p.5 ", entities: [] }),
    ).toEqual({ narration: "（本帧没有旁白）", sourceRef: "p.5" });
  });
});
