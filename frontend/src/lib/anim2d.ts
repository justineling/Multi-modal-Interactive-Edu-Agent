/**
 * 2D 关键帧动画的几何换算与补间参数（B5）。
 *
 * 三条不可动摇的前提：
 * 1. **`Entity2D.x` / `y` 是逻辑坐标，不是像素**：画布固定 1000×600，`x`/`y` 是元素
 *    **中心点**（架构 §9.5）。渲染时必须按容器实际尺寸**等比**映射，绝不能直接当 px 用
 *    —— 否则换个窗口宽度整张图就散架。
 * 2. **映射必须是 contain 语义**（等比 + 居中 + 不裁切）：`cover` 会裁掉边缘元素，
 *    而教学动画里「画面外还有一个元素」是不可接受的语义丢失。
 * 3. **补间是状态迁移，不是渲染细节**：`computeStepTransition` 只负责算出「从哪来、到哪去」，
 *    GSAP 负责插值。这样补间逻辑可以脱离 Konva 单测，渲染层只做「把算好的值塞进节点」。
 *
 * devicePixelRatio：Konva 会按 `Konva.pixelRatio` 建层，本模块提供 `clampPixelRatio`
 * 把 DPR 夹在 [1, 2] —— 3x 屏上再翻倍只会让合成开销翻 9 倍，肉眼收益为零。
 */

import { CANVAS_HEIGHT, CANVAS_WIDTH, DURATION_MS, MIN_CANVAS_SCALE } from "@/lib/theme";
import type { Entity2D, EntityShape, KeyframeStep } from "@/types/artifact";

/** 容器可视区尺寸（CSS px，不含 devicePixelRatio） */
export interface Viewport {
  readonly width: number;
  readonly height: number;
}

/** 逻辑坐标 → 像素的等比映射结果 */
export interface StageTransform {
  /** 逻辑单位 → CSS px 的比例（contain，不放大超过容器） */
  readonly scale: number;
  /** 逻辑画布左上角在容器内的像素偏移（居中留白） */
  readonly offsetX: number;
  readonly offsetY: number;
  /** 缩放比低于 `MIN_CANVAS_SCALE`：元素会糊，UI 应提示「请放大容器」 */
  readonly tooSmall: boolean;
}

/** 逻辑画布尺寸（= `CANVAS_WIDTH` × `CANVAS_HEIGHT`） */
export const LOGICAL_SIZE = { width: CANVAS_WIDTH, height: CANVAS_HEIGHT } as const;

/**
 * 各 shape 的基准尺寸（逻辑坐标，未缩放）。
 *
 * `label_box` 的宽度由文字长度决定（见 `entityLogicalSize`），这里只给下限；
 * `zone` 是「区域」语义（虚线大框），刻意做大以便把其它元素圈进去。
 */
export const ENTITY_BASE_SIZE: Record<EntityShape, { readonly width: number; readonly height: number }> = {
  circle: { width: 108, height: 108 },
  rect: { width: 240, height: 128 },
  arrow: { width: 240, height: 72 },
  label_box: { width: 200, height: 64 },
  zone: { width: 380, height: 240 },
};

/** 中文正文字号约 20 逻辑单位时，单字宽度约 15；用于估算 label_box 宽度 */
const LABEL_CHAR_WIDTH = 15;
const LABEL_MIN_WIDTH = 140;
const LABEL_MAX_WIDTH = 460;
const LABEL_HORIZONTAL_PADDING = 48;

/** 容器尺寸 → 逻辑画布的等比映射。容器为 0 时退化为 1:1，避免除零产生 NaN/Infinity。 */
export function computeStageTransform(viewport: Viewport): StageTransform {
  const width = Number.isFinite(viewport.width) ? Math.max(viewport.width, 1) : 1;
  const height = Number.isFinite(viewport.height) ? Math.max(viewport.height, 1) : 1;

  const scale = Math.min(width / CANVAS_WIDTH, height / CANVAS_HEIGHT);
  const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1;

  return {
    scale: safeScale,
    offsetX: (width - CANVAS_WIDTH * safeScale) / 2,
    offsetY: (height - CANVAS_HEIGHT * safeScale) / 2,
    tooSmall: safeScale < MIN_CANVAS_SCALE,
  };
}

/** 逻辑坐标（中心点语义由调用方决定）→ 容器像素坐标 */
export function toPixels(
  x: number,
  y: number,
  transform: StageTransform,
): { readonly x: number; readonly y: number } {
  return {
    x: transform.offsetX + x * transform.scale,
    y: transform.offsetY + y * transform.scale,
  };
}

/** 容器像素坐标 → 逻辑坐标（hover / 点击命中判定用） */
export function toLogical(
  px: number,
  py: number,
  transform: StageTransform,
): { readonly x: number; readonly y: number } {
  const scale = transform.scale > 0 ? transform.scale : 1;
  return {
    x: (px - transform.offsetX) / scale,
    y: (py - transform.offsetY) / scale,
  };
}

/** 元素的逻辑尺寸（宽, 高）。`label_box` 按文字长度估算并夹在 [140, 460]。 */
export function entityLogicalSize(
  entity: Entity2D,
): { readonly width: number; readonly height: number } {
  const base = ENTITY_BASE_SIZE[entity.shape];
  if (entity.shape !== "label_box") {
    return base;
  }
  const estimated = entity.label.length * LABEL_CHAR_WIDTH + LABEL_HORIZONTAL_PADDING;
  const width = Math.min(Math.max(estimated, LABEL_MIN_WIDTH), LABEL_MAX_WIDTH);
  return { width, height: base.height };
}

/** 元素在容器内的像素包围盒（左上角 + 尺寸），所有 shape 统一用它做补间基准 */
export interface PixelBox {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

/**
 * 元素包围盒（像素，左上角为原点）。
 *
 * 为什么统一用「左上角包围盒」而不是各自的绘制原点：
 * 补间要在同一个坐标系里做，若 circle 用中心点、rect 用左上角，
 * GSAP 就得为每种 shape 维护一套坐标语义 —— 那是 bug 的温床。
 */
export function entityBox(entity: Entity2D, transform: StageTransform): PixelBox {
  const size = entityLogicalSize(entity);
  const width = size.width * transform.scale;
  const height = size.height * transform.scale;
  const center = toPixels(entity.x, entity.y, transform);
  return {
    x: center.x - width / 2,
    y: center.y - height / 2,
    width,
    height,
  };
}

/** 页码提示文案：`p.94` / `未标注页码`（`source_page` 可空，架构 §5.3） */
export function pageLabel(entity: Entity2D): string {
  const page = entity.source_page;
  if (page === null || !Number.isFinite(page) || page <= 0) {
    return "未标注页码";
  }
  return `p.${Math.trunc(page)}`;
}

/** 把 devicePixelRatio 夹到 [1, 2]：3x 屏的合成开销不值得那点清晰度 */
export function clampPixelRatio(dpr: number): number {
  if (!Number.isFinite(dpr) || dpr <= 0) {
    return 1;
  }
  return Math.min(Math.max(dpr, 1), 2);
}

/** 下标夹取，容忍空列表（`total === 0` 时返回 0） */
export function clampStepIndex(index: number, total: number): number {
  if (total <= 0) {
    return 0;
  }
  if (!Number.isFinite(index)) {
    return 0;
  }
  return Math.min(Math.max(Math.trunc(index), 0), total - 1);
}

/**
 * 关键帧按 `step_index` 升序排列。
 *
 * 后端不保证数组顺序与 `step_index` 一致（LLM 填充，patch 插入后尤其容易乱），
 * 而时间轴必须按语义顺序走，所以渲染前统一排序。
 */
export function orderSteps(steps: readonly KeyframeStep[]): KeyframeStep[] {
  return [...steps].sort((left, right) => left.step_index - right.step_index);
}

/** 时间轴进度（0–1），用于进度条渲染 */
export function stepProgress(index: number, total: number): number {
  if (total <= 1) {
    return 0;
  }
  return clampStepIndex(index, total) / (total - 1);
}

/**
 * 两个关键帧之间的补间时长（ms）。
 *
 * 单步用 `DURATION_MS.slow`（420ms，与全局动效节奏一致）；跨多步时按步数递增，
 * 但最多加到 4 步 —— 从第 1 帧直接跳到第 8 帧还要播 3 秒，用户会以为卡住。
 */
export function tweenDuration(fromStepIndex: number, toStepIndex: number): number {
  const distance = Math.abs(toStepIndex - fromStepIndex);
  if (distance === 0) {
    return 0;
  }
  const extraSteps = Math.min(distance - 1, 4);
  return DURATION_MS.slow + extraSteps * DURATION_MS.fast;
}

/** 单个元素在一次关键帧切换中的迁移描述 */
export interface EntityTransition {
  readonly entity: Entity2D;
  /** 该元素在**上一帧**的包围盒；首次出现时为当前包围盒（配合 `opacity: 0` 淡入） */
  readonly from: PixelBox;
  readonly to: PixelBox;
  /** 起始不透明度：新元素 0，已存在元素 1 */
  readonly fromOpacity: number;
  readonly toOpacity: number;
  /** 是否为本次关键帧新增的元素（用于「淡入」与高亮提示） */
  readonly isNew: boolean;
}

/**
 * 计算一次关键帧切换的迁移表。
 *
 * 语义选择：**只渲染当前帧的元素**。上一帧存在、当前帧消失的元素直接不渲染，
 * 不做淡出 —— 因为「关键帧」的定义就是「此刻画面的完整构图」，
 * 残留上一帧的幽灵元素会让旁白与画面不一致（教学场景下这是错误信息）。
 *
 * @param previous 上一帧的元素列表；首帧传 `null`
 * @param current 当前帧的元素列表
 */
export function computeStepTransition(
  previous: readonly Entity2D[] | null,
  current: readonly Entity2D[],
  transform: StageTransform,
): EntityTransition[] {
  const previousById = new Map<string, Entity2D>();
  if (previous !== null) {
    for (const entity of previous) {
      previousById.set(entity.id, entity);
    }
  }

  return current.map((entity) => {
    const to = entityBox(entity, transform);
    const previousEntity = previousById.get(entity.id);
    if (previousEntity === undefined) {
      return {
        entity,
        from: to,
        to,
        fromOpacity: 0,
        toOpacity: 1,
        isNew: true,
      };
    }
    return {
      entity,
      from: entityBox(previousEntity, transform),
      to,
      fromOpacity: 1,
      toOpacity: 1,
      isNew: false,
    };
  });
}

/** 关键帧的旁白 + 出处（时间轴下方展示；`source_ref` 可空串） */
export interface NarrationLine {
  readonly narration: string;
  readonly sourceRef: string;
}

export function narrationOf(step: KeyframeStep | undefined): NarrationLine {
  if (step === undefined) {
    return { narration: "（本帧没有旁白）", sourceRef: "" };
  }
  const narration = step.narration.trim().length > 0 ? step.narration : "（本帧没有旁白）";
  return { narration, sourceRef: step.source_ref.trim() };
}
