/**
 * 设计令牌（Design Tokens）—— 前端唯一权威来源。
 *
 * 三条硬约束：
 * 1. `PALETTE` 必须与后端 `backend/app/schemas/artifacts.py::PALETTE` **逐字一致**
 *    （架构 §9.5）。`Entity2D.color` / `Node3D.color` 在 Pydantic 校验器里会被检查，
 *    前端一旦用别的色值渲染，就会出现「后端拒绝但前端能画」的割裂。
 * 2. 深色是主色调（教育产品单次使用时长长，深色护眼），浅色令牌同步预留，
 *    由 `index.css` 里的 `:root` / `.dark` 两组 CSS 变量承载。
 *    Konva / Three 不认 CSS 变量，所以本文件同时保留具体 hex 值 —— 这是**有意的双份**，
 *    改色时必须与 `index.css` 的 RGB 三元组同改（两份按同一顺序排列，便于 diff 对齐）。
 * 3. 单位约定：2D 逻辑画布 1000×600（实体 x/y 是中心点，非像素）；3D 1 单位 ≈ 1 米。
 */

// --------------------------------------------------------------------------- //
// 调色板（与后端 PALETTE 同序同值，禁止改大小写）
// --------------------------------------------------------------------------- //
export const PALETTE = [
  "#2563EB", // 0 主蓝
  "#0EA5E9", // 1 次蓝
  "#F59E0B", // 2 高亮橙
  "#EF4444", // 3 警示红
  "#10B981", // 4 成功绿
  "#6366F1", // 5 紫
  "#64748B", // 6 中性灰
  "#E2E8F0", // 7 浅底
] as const;

export type PaletteColor = (typeof PALETTE)[number];

/** 语义名 → 色值，供 UI 文案/图例使用。 */
export const PALETTE_SEMANTIC: Record<PaletteColor, string> = {
  "#2563EB": "主蓝",
  "#0EA5E9": "次蓝",
  "#F59E0B": "高亮橙",
  "#EF4444": "警示红",
  "#10B981": "成功绿",
  "#6366F1": "紫",
  "#64748B": "中性灰",
  "#E2E8F0": "浅底",
};

/** 后端把颜色统一转大写存储，前端也按大写比较，避免 "#2563eb" 被判非法。 */
export function normalizePaletteColor(value: string): PaletteColor | null {
  const normalized = value.trim().toUpperCase();
  const hit = PALETTE.find((color) => color === normalized);
  return hit ?? null;
}

export function isPaletteColor(value: string): value is PaletteColor {
  return normalizePaletteColor(value) !== null;
}

// --------------------------------------------------------------------------- //
// 主题色（深色为主，浅色预留）
// --------------------------------------------------------------------------- //
export const DARK_COLORS = {
  surfaceBase: "#0B1220",
  surfaceRaised: "#111A2B",
  surfaceSunken: "#080E19",
  surfaceOverlay: "#16203A",
  lineSubtle: "#1F2B41",
  lineStrong: "#2F3F5C",
  contentPrimary: "#E8EEF9",
  contentSecondary: "#9DB0CC",
  contentMuted: "#6B7F9E",
  accent: PALETTE[0],
  accentSoft: "#1D4ED8",
  info: PALETTE[1],
  warning: PALETTE[2],
  danger: PALETTE[3],
  success: PALETTE[4],
} as const;

export const LIGHT_COLORS = {
  surfaceBase: "#F8FAFC",
  surfaceRaised: "#FFFFFF",
  surfaceSunken: "#EEF2F7",
  surfaceOverlay: "#FFFFFF",
  lineSubtle: "#E2E8F0",
  lineStrong: "#CBD5E1",
  contentPrimary: "#0F172A",
  contentSecondary: "#475569",
  contentMuted: "#64748B",
  accent: PALETTE[0],
  accentSoft: "#3B82F6",
  info: PALETTE[1],
  warning: PALETTE[2],
  danger: PALETTE[3],
  success: PALETTE[4],
} as const;

export type ThemeColors = typeof DARK_COLORS;

export const COLOR_SCHEMES = {
  dark: DARK_COLORS,
  light: LIGHT_COLORS,
} as const;

export type ColorSchemeName = keyof typeof COLOR_SCHEMES;

// --------------------------------------------------------------------------- //
// 几何 / 画布基准（架构 §9.5）
// --------------------------------------------------------------------------- //
/** 2D 逻辑画布宽（实体 x ∈ [0, 1000]，是中心点） */
export const CANVAS_WIDTH = 1000;
/** 2D 逻辑画布高（实体 y ∈ [0, 600]，是中心点） */
export const CANVAS_HEIGHT = 600;
/** 3D 场景整体落在原点为中心的立方体内，边长 10（1 单位 ≈ 1 米） */
export const SCENE_BOUND_SIZE = 10;
/** 3D 相机机位距原点上限 */
export const MAX_CAMERA_DISTANCE = 20;
/** 2D 等比缩放时允许的最小缩放比，低于此值元素会糊，改为显示「请放大容器」 */
export const MIN_CANVAS_SCALE = 0.2;

export const RADIUS = {
  xs: 4,
  sm: 6,
  md: 10,
  lg: 14,
  xl: 20,
  full: 9999,
} as const;

export const SPACING = {
  xxs: 2,
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const FONT_SIZE = {
  xxs: 10,
  xs: 11,
  sm: 12,
  base: 14,
  md: 16,
  lg: 20,
  xl: 26,
  hero: 40,
} as const;

export const FONT_FAMILY = {
  sans: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", "Helvetica Neue", Arial, sans-serif',
  mono: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
} as const;

export const SHADOW = {
  /** 卡片级：列表项、消息气泡 */
  card: "0 1px 2px rgba(2, 6, 23, 0.24), 0 4px 12px rgba(2, 6, 23, 0.18)",
  /** 面板级：抽屉、浮动工具条 */
  panel: "0 8px 24px rgba(2, 6, 23, 0.32), 0 2px 6px rgba(2, 6, 23, 0.24)",
  /** 浮层级：菜单、tooltip */
  floating: "0 16px 40px rgba(2, 6, 23, 0.44), 0 4px 10px rgba(2, 6, 23, 0.3)",
} as const;

/** 动效时长（ms）。GSAP 补间与 framer-motion 共用，保证「同一动作同一节奏」。 */
export const DURATION_MS = {
  instant: 80,
  fast: 150,
  normal: 240,
  slow: 420,
  /** 3D 相机导览 */
  camera: 900,
} as const;

export const EASING = {
  standard: "cubic-bezier(0.4, 0, 0.2, 1)",
  enter: "cubic-bezier(0.16, 1, 0.3, 1)",
  exit: "cubic-bezier(0.4, 0, 1, 1)",
  /** GSAP 补间用的等价字符串 */
  gsapStandard: "power2.out",
} as const;

export const Z_INDEX = {
  canvas: 0,
  canvasOverlay: 10,
  toolbar: 20,
  drawer: 30,
  menu: 40,
  toast: 50,
  modal: 60,
} as const;

/** 布局尺寸。分栏默认宽度用于 Allotment 的 defaultSizes 百分比换算。 */
export const LAYOUT = {
  topBarHeight: 52,
  chatMinWidth: 340,
  canvasMinWidth: 380,
  chatDefaultPercent: 42,
  composerMinHeight: 96,
  /** 分栏拖拽条宽度（与 index.css 的 --sash-size 一致） */
  sashSize: 10,
} as const;

/** 教学排版：幻灯片容器固定 16:9，避免不同屏幕下布局抖动。 */
export const SLIDE_ASPECT_RATIO = 16 / 9;

/** 版本策略（架构 §10 待明确事项 3）：每 artifact 保留最近 20 个可 patch 版本。 */
export const MAX_PATCHABLE_VERSIONS = 20;

export const THEME = {
  palette: PALETTE,
  colors: COLOR_SCHEMES,
  radius: RADIUS,
  spacing: SPACING,
  fontSize: FONT_SIZE,
  fontFamily: FONT_FAMILY,
  shadow: SHADOW,
  duration: DURATION_MS,
  easing: EASING,
  zIndex: Z_INDEX,
  layout: LAYOUT,
  canvas: {
    width: CANVAS_WIDTH,
    height: CANVAS_HEIGHT,
    minScale: MIN_CANVAS_SCALE,
  },
  scene: {
    boundSize: SCENE_BOUND_SIZE,
    maxCameraDistance: MAX_CAMERA_DISTANCE,
  },
  slideAspectRatio: SLIDE_ASPECT_RATIO,
} as const;
