/**
 * 三种 JSON AST 的 TypeScript 契约 —— 与后端 `backend/app/schemas/artifacts.py` 逐字段对应。
 *
 * 本文件遵守四条铁律（架构 §5.3 / §9.6）：
 * 1. **字段名一律 snake_case**，与线上 JSON 同名同形，不做 camelCase 映射、不加 alias。
 * 2. **可空字段写 `T | null`，禁止写 `?`**。Pydantic 的 `Optional[T]` 序列化后恒为
 *    `null`，字段不会缺席；写 `?` 会让运行时到处是 `undefined`，渲染器一取属性就崩。
 * 3. **三元组写 `[number, number, number]`，禁止 `number[]`**。R3F 的 `position`
 *    传 `number[]` 不报类型错，但运行时几何体会直接消失。
 * 4. **禁止 `any`、禁止 `as` 强转**。判别联合靠 `spec.type` 收窄，不靠断言。
 */

/** 产物大类。与 `artifacts.artifact_type` 列、SSE `artifact_type` 字段同值。 */
export type ArtifactType = "ppt" | "anim_2d" | "scene_3d";

/** 知识范围。`none` 表示课本未覆盖（Grader NO_MATCH）。 */
export type KnowledgeScope = "syllabus_strict" | "hybrid_extended" | "none";

/** 形态决策动作。 */
export type ModalityDecisionName = "none" | "create_artifact" | "patch_artifact";

/** 三维向量：位置 / 缩放 / 旋转 / 相机机位统一用它，**不要退化成 `number[]`**。 */
export type Vector3 = [number, number, number];

// --------------------------------------------------------------------------- //
// 1. PPT
// --------------------------------------------------------------------------- //
/** 6 种教学布局，与 `slideLayouts.tsx` 的 `LAYOUT_REGISTRY` 键一一对应。 */
export type SlideLayout =
  | "title_hero"
  | "bullet_points"
  | "timeline"
  | "comparison"
  | "cause_effect"
  | "quiz";

/** 运行时校验用（registry 自检、mock 数据校验），顺序与后端 Literal 一致。 */
export const SLIDE_LAYOUTS: readonly SlideLayout[] = [
  "title_hero",
  "bullet_points",
  "timeline",
  "comparison",
  "cause_effect",
  "quiz",
];

export function isSlideLayout(value: string): value is SlideLayout {
  return SLIDE_LAYOUTS.some((layout) => layout === value);
}

/** 时间轴条目（后端扩展字段）。timeline 布局需要结构化年份，靠 bullets 拼字符串无法渲染。 */
export interface TimelineItem {
  year: string;
  label: string;
  detail: string;
  source_page: number | null;
}

/** 随堂测验题目（后端扩展字段）。quiz 布局需要选项与答案索引。 */
export interface QuizQuestion {
  question: string;
  /** 至少 2 项（后端 `min_length=2`） */
  options: string[];
  /** 正确答案下标，后端保证 `< options.length` */
  answer_index: number;
  explanation: string;
}

/**
 * 单页幻灯片。
 *
 * 注意字段名是 `node_id`（不是 `id`）—— 与 `Entity2D` / `Node3D` 的 `id` 不同，
 * 这是 SPEC 原文如此，改动会破坏锚点 `node_path` 的语义。
 */
export interface SlideNode {
  /** 形如 `slide-1`，确定性生成；锚点用它 */
  node_id: string;
  layout: SlideLayout;
  title: string;
  subtitle: string | null;
  /** 后端默认 `[]`，恒为数组，不会是 undefined */
  bullets: string[];
  comparison_left: string[] | null;
  comparison_right: string[] | null;
  timeline_items: TimelineItem[] | null;
  quiz_questions: QuizQuestion[] | null;
  /** AI 助教对该页知识点的口语化讲解 */
  speaker_notes: string;
  /** 课本出处，如 `'Form 4 Sejarah, Bab 4.1, p.94'`；超纲时为 `'Extended Knowledge'` */
  source_ref: string;
}

export interface SlideDeckSpec {
  type: "ppt";
  topic: string;
  /** 后端保证 `min_length=1` */
  slides: SlideNode[];
}

// --------------------------------------------------------------------------- //
// 2. 2D 关键帧动画
// --------------------------------------------------------------------------- //
export type EntityShape = "circle" | "rect" | "arrow" | "label_box" | "zone";

export const ENTITY_SHAPES: readonly EntityShape[] = [
  "circle",
  "rect",
  "arrow",
  "label_box",
  "zone",
];

export function isEntityShape(value: string): value is EntityShape {
  return ENTITY_SHAPES.some((shape) => shape === value);
}

/**
 * 2D 元素。
 *
 * 注意字段名是 `id`（不是 `node_id`）；锚点路径形如 `/steps/{i}/entities/{j}`。
 * `x` / `y` 是 1000×600 逻辑画布下的**中心点**坐标，不是像素。
 */
export interface Entity2D {
  id: string;
  shape: EntityShape;
  label: string;
  /** 归一化坐标，0–1000 */
  x: number;
  /** 归一化坐标，0–600 */
  y: number;
  /** 必须取自 §9.5 调色板（后端校验器会拒绝其他值） */
  color: string;
  /** 点击该元素时弹出的详细知识点解释 */
  details: string;
  /** 后端扩展：hover 时显示课本引用 */
  source_page: number | null;
}

export interface KeyframeStep {
  step_index: number;
  /** 当前步骤的旁白解说 */
  narration: string;
  /** 后端扩展：每关键帧可标注页码 */
  source_ref: string;
  entities: Entity2D[];
}

export interface Keyframe2DSpec {
  type: "anim_2d";
  title: string;
  /** 后端保证 `min_length=1` */
  steps: KeyframeStep[];
}

// --------------------------------------------------------------------------- //
// 3. 3D 空间场景
// --------------------------------------------------------------------------- //
export type Primitive3D =
  | "box"
  | "sphere"
  | "cylinder"
  | "cone"
  | "terrain_plane"
  | "arrow_link";

export const PRIMITIVES_3D: readonly Primitive3D[] = [
  "box",
  "sphere",
  "cylinder",
  "cone",
  "terrain_plane",
  "arrow_link",
];

export function isPrimitive3D(value: string): value is Primitive3D {
  return PRIMITIVES_3D.some((primitive) => primitive === value);
}

/** 3D 节点。锚点路径形如 `/nodes/{i}`。 */
export interface Node3D {
  id: string;
  primitive: Primitive3D;
  label: string;
  position: Vector3;
  scale: Vector3;
  rotation: Vector3;
  /** 必须取自 §9.5 调色板 */
  color: string;
  /** 3D 悬浮标签内容（markdown），点击可聚焦镜头并追问 */
  annotation_markdown: string;
  /** 后端扩展：标注点显示课本引用 */
  source_page: number | null;
  /** 后端扩展：部件从属关系，patch 界定子树时用 */
  parent_id: string | null;
}

export interface CameraPreset {
  name: string;
  position: Vector3;
  target: Vector3;
  explanation: string;
}

export interface SceneGraph3DSpec {
  type: "scene_3d";
  title: string;
  nodes: Node3D[];
  /** 预设导览机位，后端保证 `min_length=1` */
  camera_tour: CameraPreset[];
}

// --------------------------------------------------------------------------- //
// 4. 判别联合
// --------------------------------------------------------------------------- //
/**
 * 三种 AST 的判别联合。以 `spec.type` 收窄（discriminated union），
 * 这是「策略模式分发」的类型基础：`ArtifactHost` 里 `switch (spec.type)` 后
 * 每个分支自动拿到具体类型，无需断言。
 */
export type ArtifactSpec = SlideDeckSpec | Keyframe2DSpec | SceneGraph3DSpec;

export function isSlideDeckSpec(spec: ArtifactSpec): spec is SlideDeckSpec {
  return spec.type === "ppt";
}

export function isKeyframe2DSpec(spec: ArtifactSpec): spec is Keyframe2DSpec {
  return spec.type === "anim_2d";
}

export function isSceneGraph3DSpec(spec: ArtifactSpec): spec is SceneGraph3DSpec {
  return spec.type === "scene_3d";
}

/**
 * 校验「artifact_type 与 spec.type 是否自洽」。
 *
 * 后端理论上不会给出不一致的组合，但版本接口允许 `?version=` 回看历史，
 * 前端仍做一次廉价自检：不一致时渲染器直接降级，而不是画出错位的画面。
 */
export function isSpecTypeConsistent(artifactType: ArtifactType, spec: ArtifactSpec): boolean {
  return artifactType === spec.type;
}

// --------------------------------------------------------------------------- //
// 5. 引用与统一响应
// --------------------------------------------------------------------------- //
/** 引用条目。`page_start`/`page_end` 支撑「页码可回溯区间」。 */
export interface Citation {
  label: string;
  section_code: string;
  page_start: number;
  page_end: number;
}

/** 非流式 `POST /api/chat` 的返回体（SSE 走事件流，不用这个）。 */
export interface AgentTurnResponse {
  reply_markdown: string;
  knowledge_scope: KnowledgeScope;
  citations: Citation[];
  modality_decision: ModalityDecisionName;
  /** 后端 `ArtifactSpec | None` → 恒为 `null` 或具体 spec */
  artifact_payload: ArtifactSpec | null;
}
