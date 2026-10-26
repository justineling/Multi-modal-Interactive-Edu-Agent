/**
 * 3D 场景的 primitive 映射、层级解析与相机工具（B5）。
 *
 * 为什么把「primitive → geometry」放在纯函数里而不是直接写进 JSX：
 * 1. **可测**：6 种 primitive 的参数、简化档位的段数、标注点偏移都能脱离 WebGL 断言；
 * 2. **一处定义两处用**：`arrow_link` 既可能是独立箭头，也可能是「父 → 子」的连接线，
 *    方向计算必须在渲染之前就定下来（R3F 里做四元数插值很难测）；
 * 3. **降级可控**：`simplified` 只改段数与材质参数，不改几何语义 —— 关阴影/降段数之后
 *    学生看到的仍是同一个物体，只是不那么光滑。
 *
 * 坐标约定：1 单位 ≈ 1 米（架构 §9.5），场景整体落在原点为中心的 10×10×10 立方体内。
 * `Node3D.position` 在**有 `parent_id` 时是相对父节点的坐标**，`resolveHierarchy` 负责累加。
 */

import { Euler, Quaternion, Vector3 as ThreeVector3 } from "three";

import { MAX_CAMERA_DISTANCE, SCENE_BOUND_SIZE } from "@/lib/theme";
import type { CameraPreset, Node3D, Primitive3D, Vector3 } from "@/types/artifact";

/**
 * 几何描述（判别联合）。
 *
 * `args` 刻意写成**定长元组**而不是 `number[]`：R3F 的 `*Geometry` 组件要求
 * `args: [w?, h?, d?]` 这类元组，传 `number[]` 过不了类型检查（运行时也会静默丢参数）。
 * `rotationFix` 是几何自身需要的额外旋转（如 `terrain_plane` 要躺平）。
 * `extent` 是未乘节点 scale 的半包围盒，用于标注点偏移。
 */
export type GeometryDescriptor =
  | { readonly kind: "box"; readonly args: [number, number, number]; readonly rotationFix: Vector3; readonly extent: Vector3 }
  | { readonly kind: "sphere"; readonly args: [number, number, number]; readonly rotationFix: Vector3; readonly extent: Vector3 }
  | { readonly kind: "cylinder"; readonly args: [number, number, number, number]; readonly rotationFix: Vector3; readonly extent: Vector3 }
  | { readonly kind: "cone"; readonly args: [number, number, number]; readonly rotationFix: Vector3; readonly extent: Vector3 }
  | { readonly kind: "plane"; readonly args: [number, number, number, number]; readonly rotationFix: Vector3; readonly extent: Vector3 }
  | {
      readonly kind: "arrow";
      /** `[轴半径, 箭头半径, 箭头长度, 总长, 圆周段数]` */
      readonly args: [number, number, number, number, number];
      readonly rotationFix: Vector3;
      readonly extent: Vector3;
    };

/** 标准档位段数（与简化档位的比例约 3:1，视觉差异明显但轮廓一致） */
const RADIAL_SEGMENTS = 24;
const SPHERE_WIDTH_SEGMENTS = 32;
const SPHERE_HEIGHT_SEGMENTS = 20;
const PLANE_SEGMENTS = 8;

const SIMPLIFIED_RADIAL_SEGMENTS = 10;
const SIMPLIFIED_SPHERE_WIDTH_SEGMENTS = 12;
const SIMPLIFIED_SPHERE_HEIGHT_SEGMENTS = 8;
const SIMPLIFIED_PLANE_SEGMENTS = 2;

/** `arrow_link` 的默认长度（无父节点时沿 +Y 竖起） */
const ARROW_LENGTH = 2.4;
const ARROW_SHAFT_RADIUS = 0.06;
const ARROW_HEAD_RADIUS = 0.16;
const ARROW_HEAD_LENGTH = 0.42;

/**
 * primitive → geometry。
 *
 * 尺寸基准统一为「1 单位见方」，具体大小由 `Node3D.scale` 决定 ——
 * 这样 patch 只改 scale 就能改尺寸，不必重新生成几何参数。
 */
export function geometryForPrimitive(
  primitive: Primitive3D,
  simplified: boolean,
): GeometryDescriptor {
  const radial = simplified ? SIMPLIFIED_RADIAL_SEGMENTS : RADIAL_SEGMENTS;

  switch (primitive) {
    case "box":
      return {
        kind: "box",
        args: [1, 1, 1],
        rotationFix: [0, 0, 0],
        extent: [0.5, 0.5, 0.5],
      };
    case "sphere":
      return {
        kind: "sphere",
        args: [
          0.5,
          simplified ? SIMPLIFIED_SPHERE_WIDTH_SEGMENTS : SPHERE_WIDTH_SEGMENTS,
          simplified ? SIMPLIFIED_SPHERE_HEIGHT_SEGMENTS : SPHERE_HEIGHT_SEGMENTS,
        ],
        rotationFix: [0, 0, 0],
        extent: [0.5, 0.5, 0.5],
      };
    case "cylinder":
      return {
        kind: "cylinder",
        args: [0.4, 0.4, 1, radial],
        rotationFix: [0, 0, 0],
        extent: [0.4, 0.5, 0.4],
      };
    case "cone":
      return {
        kind: "cone",
        args: [0.5, 1, radial],
        rotationFix: [0, 0, 0],
        extent: [0.5, 0.5, 0.5],
      };
    case "terrain_plane":
      // 平面几何默认立在 XY 平面，必须绕 X 轴 -90° 才躺成「地面」
      return {
        kind: "plane",
        args: [
          SCENE_BOUND_SIZE,
          SCENE_BOUND_SIZE,
          simplified ? SIMPLIFIED_PLANE_SEGMENTS : PLANE_SEGMENTS,
          simplified ? SIMPLIFIED_PLANE_SEGMENTS : PLANE_SEGMENTS,
        ],
        rotationFix: [-Math.PI / 2, 0, 0],
        extent: [SCENE_BOUND_SIZE / 2, 0.02, SCENE_BOUND_SIZE / 2],
      };
    case "arrow_link":
      return {
        kind: "arrow",
        args: [ARROW_SHAFT_RADIUS, ARROW_HEAD_RADIUS, ARROW_HEAD_LENGTH, ARROW_LENGTH, radial],
        rotationFix: [0, 0, 0],
        extent: [ARROW_HEAD_RADIUS, ARROW_LENGTH / 2, ARROW_HEAD_RADIUS],
      };
    default: {
      // 联合类型已穷尽；运行期仍可能收到历史版本里的旧 primitive
      return {
        kind: "box",
        args: [1, 1, 1],
        rotationFix: [0, 0, 0],
        extent: [0.5, 0.5, 0.5],
      };
    }
  }
}

/** 材质描述（与 `meshStandardMaterial` 的 props 同名，渲染层直接展开） */
export interface MaterialDescriptor {
  readonly color: string;
  readonly metalness: number;
  readonly roughness: number;
  readonly opacity: number;
  readonly transparent: boolean;
  readonly flatShading: boolean;
  readonly emissive: string;
  readonly emissiveIntensity: number;
}

/**
 * 节点材质。
 *
 * 标准档位给一点金属感（教学模型看起来更「实体」）；简化档位把金属度归零、
 * 粗糙度拉满并开 `flatShading` —— PBR 的高光计算是移动端 GPU 的主要开销来源，
 * 关掉之后同屏 20 个部件的帧率能翻倍，而轮廓完全不变。
 */
export function materialForNode(node: Node3D, simplified: boolean): MaterialDescriptor {
  if (simplified) {
    return {
      color: node.color,
      metalness: 0,
      roughness: 1,
      opacity: 1,
      transparent: false,
      flatShading: true,
      emissive: node.color,
      emissiveIntensity: 0,
    };
  }
  return {
    color: node.color,
    metalness: 0.18,
    roughness: 0.52,
    opacity: 1,
    transparent: false,
    flatShading: false,
    emissive: node.color,
    emissiveIntensity: 0.08,
  };
}

/** 标注点相对节点的局部偏移（世界单位）：压在物体正上方，避免与几何体穿插 */
export function annotationOffsetFor(descriptor: GeometryDescriptor, scale: Vector3): Vector3 {
  const height = Math.abs(descriptor.extent[1] * scale[1]);
  return [0, height + 0.34, 0];
}

// --------------------------------------------------------------------------- //
// 层级解析
// --------------------------------------------------------------------------- //
/** 解析后的节点：世界坐标 / 世界缩放 / 父节点下标 */
export interface ResolvedNode3D {
  readonly node: Node3D;
  /** 世界坐标（父级 position 逐级累加） */
  readonly worldPosition: Vector3;
  /** 世界缩放（父级 scale 逐级相乘） */
  readonly worldScale: Vector3;
  /** 父节点在 `nodes` 数组中的下标；无父或父 id 不存在时为 `null` */
  readonly parentIndex: number | null;
  /** 层级深度，根为 0 */
  readonly depth: number;
}

function addVector3(left: Vector3, right: Vector3): Vector3 {
  return [left[0] + right[0], left[1] + right[1], left[2] + right[2]];
}

function multiplyVector3(left: Vector3, right: Vector3): Vector3 {
  return [left[0] * right[0], left[1] * right[1], left[2] * right[2]];
}

/**
 * 建立父子层级并把相对坐标累加成世界坐标。
 *
 * 三个必须兜住的畸形输入（LLM 填充 + patch 之后都可能出现）：
 * - `parent_id` 指向不存在的节点 → 当根处理（否则整棵子树消失，画面上「零件没了」）；
 * - `parent_id` 指向自己 → 当根处理；
 * - 环（A 的父是 B，B 的父是 A）→ 在检测到重复访问时打断，当根处理。
 *   死循环会让渲染线程直接卡死，比画错位置严重得多。
 */
export function resolveHierarchy(nodes: readonly Node3D[]): ResolvedNode3D[] {
  const indexById = new Map<string, number>();
  nodes.forEach((node, index) => {
    if (!indexById.has(node.id)) {
      indexById.set(node.id, index);
    }
  });

  const resolved: Array<ResolvedNode3D | null> = nodes.map(() => null);
  const visiting = new Set<number>();

  const resolveAt = (index: number): ResolvedNode3D => {
    const cached = resolved[index];
    if (cached !== null && cached !== undefined) {
      return cached;
    }
    const node = nodes[index];
    if (node === undefined) {
      // 理论上不会发生（下标来自 map 遍历）；给一个安全空节点而不是抛错
      const empty: ResolvedNode3D = {
        node: {
          id: `missing-${index}`,
          primitive: "box",
          label: "",
          position: [0, 0, 0],
          scale: [1, 1, 1],
          rotation: [0, 0, 0],
          color: "#64748B",
          annotation_markdown: "",
          source_page: null,
          parent_id: null,
        },
        worldPosition: [0, 0, 0],
        worldScale: [1, 1, 1],
        parentIndex: null,
        depth: 0,
      };
      resolved[index] = empty;
      return empty;
    }

    if (visiting.has(index)) {
      // 环：就地打断，本节点当根
      const root: ResolvedNode3D = {
        node,
        worldPosition: node.position,
        worldScale: node.scale,
        parentIndex: null,
        depth: 0,
      };
      resolved[index] = root;
      return root;
    }

    visiting.add(index);
    const parentId = node.parent_id;
    const parentIndex = parentId === null ? null : (indexById.get(parentId) ?? null);

    let result: ResolvedNode3D;
    if (parentIndex === null || parentIndex === index) {
      result = {
        node,
        worldPosition: node.position,
        worldScale: node.scale,
        parentIndex: null,
        depth: 0,
      };
    } else {
      const parent = resolveAt(parentIndex);
      // 递归期间可能已经把本节点当成「环的断点」缓存成了根（见上面的 visiting 分支），
      // 此时必须复用那份结果，否则会把断点覆盖掉、两个节点互相指认对方为父。
      const cachedAfterRecursion = resolved[index];
      if (cachedAfterRecursion !== null && cachedAfterRecursion !== undefined) {
        visiting.delete(index);
        return cachedAfterRecursion;
      }
      result = {
        node,
        worldPosition: addVector3(parent.worldPosition, node.position),
        worldScale: multiplyVector3(parent.worldScale, node.scale),
        parentIndex,
        depth: parent.depth + 1,
      };
    }

    visiting.delete(index);
    resolved[index] = result;
    return result;
  };

  return nodes.map((_node, index) => resolveAt(index));
}

// --------------------------------------------------------------------------- //
// 相机
// --------------------------------------------------------------------------- //
export function lerpVector3(from: Vector3, to: Vector3, t: number): Vector3 {
  const ratio = Math.min(Math.max(Number.isFinite(t) ? t : 0, 0), 1);
  return [
    from[0] + (to[0] - from[0]) * ratio,
    from[1] + (to[1] - from[1]) * ratio,
    from[2] + (to[2] - from[2]) * ratio,
  ];
}

export function distanceBetween(from: Vector3, to: Vector3): number {
  const dx = from[0] - to[0];
  const dy = from[1] - to[1];
  const dz = from[2] - to[2];
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

/**
 * 把机位夹到「距目标不超过 `max`」。
 *
 * 为什么需要：`camera_tour` 的机位来自 LLM，偶发会给出 1000 单位的坐标，
 * 相机跑到模型里外都看不见东西，学生只会看到一片空白（等同白屏）。
 */
export function clampCameraPosition(
  position: Vector3,
  target: Vector3,
  max: number = MAX_CAMERA_DISTANCE,
): Vector3 {
  const distance = distanceBetween(position, target);
  if (!Number.isFinite(distance) || distance <= max) {
    return position;
  }
  const ratio = max / distance;
  return [
    target[0] + (position[0] - target[0]) * ratio,
    target[1] + (position[1] - target[1]) * ratio,
    target[2] + (position[2] - target[2]) * ratio,
  ];
}

export interface CameraView {
  readonly position: Vector3;
  readonly target: Vector3;
}

/** 按名字取导览机位；找不到返回 `null`（`focus_camera` 的预设名可能已被 patch 删掉） */
export function cameraPresetByName(
  tour: readonly CameraPreset[],
  name: string,
): CameraPreset | null {
  const hit = tour.find((preset) => preset.name === name);
  return hit ?? null;
}

/** 把导览机位转成相机视图（机位已夹取，防止 LLM 给出的坐标飞出场景） */
export function viewFromPreset(preset: CameraPreset): CameraView {
  return {
    position: clampCameraPosition(preset.position, preset.target),
    target: preset.target,
  };
}

/**
 * 由节点包围盒推导默认机位（首帧与「复位」按钮共用）。
 *
 * 不用固定 `[0,0,10]`：3D 场景的尺度由 `scale` 决定，固定机位会让小模型变成一粒沙、
 * 大模型撑满屏幕。
 */
export function deriveDefaultCamera(nodes: readonly Node3D[]): CameraView {
  const fallback: CameraView = { position: [6, 5.2, 8], target: [0, 0, 0] };
  if (nodes.length === 0) {
    return fallback;
  }

  const resolved = resolveHierarchy(nodes);
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let minZ = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  let maxZ = Number.NEGATIVE_INFINITY;

  for (const item of resolved) {
    const [x, y, z] = item.worldPosition;
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    minZ = Math.min(minZ, z);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
    maxZ = Math.max(maxZ, z);
  }

  const target: Vector3 = [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2];
  const radius = Math.max(
    distanceBetween([minX, minY, minZ], [maxX, maxY, maxZ]) / 2,
    1.2,
  );
  const distance = Math.min(Math.max(radius * 2.8, 4), MAX_CAMERA_DISTANCE);
  const position: Vector3 = [
    target[0] + distance * 0.55,
    target[1] + distance * 0.48,
    target[2] + distance * 0.7,
  ];
  return { position: clampCameraPosition(position, target), target };
}

// --------------------------------------------------------------------------- //
// arrow_link 摆位
// --------------------------------------------------------------------------- //
/** 连接箭头的摆放结果（中点 + 朝向 + 长度） */
export interface ArrowLinkPlacement {
  readonly position: Vector3;
  readonly rotation: Vector3;
  readonly length: number;
}

const UP_AXIS = new ThreeVector3(0, 1, 0);

/**
 * 计算「从 from 指向 to」的箭头摆位。
 *
 * 圆柱/圆锥几何的默认轴向是 **+Y**，所以这里用 `setFromUnitVectors(+Y, dir)` 求四元数、
 * 再转欧拉角交给 R3F。手写 `atan2` 两段旋转在斜向时会算错（yaw 与 tilt 的复合顺序
 * 与 three 的默认 'XYZ' 顺序不一致），交给 three 自己的数学库最稳。
 */
export function placeArrowLink(from: Vector3, to: Vector3): ArrowLinkPlacement {
  const start = new ThreeVector3(from[0], from[1], from[2]);
  const end = new ThreeVector3(to[0], to[1], to[2]);
  const direction = end.clone().sub(start);
  const length = direction.length();

  if (!Number.isFinite(length) || length < 1e-6) {
    return { position: [...from], rotation: [0, 0, 0], length: 0 };
  }

  const quaternion = new Quaternion().setFromUnitVectors(UP_AXIS, direction.clone().normalize());
  const euler = new Euler().setFromQuaternion(quaternion);
  const middle = start.clone().add(end).multiplyScalar(0.5);

  return {
    position: [middle.x, middle.y, middle.z],
    rotation: [euler.x, euler.y, euler.z],
    length,
  };
}

// --------------------------------------------------------------------------- //
// 能力探测与降级
// --------------------------------------------------------------------------- //
export interface WebglSupport {
  readonly supported: boolean;
  /** 不支持时的可读原因（直接展示给学生，不要技术黑话） */
  readonly reason: string | null;
}

/** WebGL 能力探测。无 DOM（SSR / 单测）时按「不支持」处理，由渲染层降级为提示。 */
export function detectWebglSupport(): WebglSupport {
  if (typeof document === "undefined") {
    return { supported: false, reason: "当前环境没有 DOM，无法初始化 WebGL" };
  }
  try {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    if (context === null) {
      return { supported: false, reason: "浏览器未启用 WebGL（可能关闭了硬件加速）" };
    }
    return { supported: true, reason: null };
  } catch (error) {
    const detail = error instanceof Error ? error.message : "未知原因";
    return { supported: false, reason: `WebGL 初始化失败：${detail}` };
  }
}

/** 低于该核数视为低性能设备，默认建议开简化渲染 */
export const SIMPLIFIED_SUGGESTED_MAX_CORES = 4;

/**
 * 是否应默认建议「简化渲染」。
 *
 * 只看 CPU 核数：`deviceMemory` / `hardwareConcurrency` 在 Safari 上缺失或失真，
 * 而核数在移动端与低端本上足够有区分度（≤4 核基本等于「PBR + 阴影会掉到 30fps 以下」）。
 */
export function shouldSuggestSimplified(): boolean {
  if (typeof navigator === "undefined") {
    return false;
  }
  const cores = navigator.hardwareConcurrency;
  if (typeof cores !== "number" || !Number.isFinite(cores) || cores <= 0) {
    return false;
  }
  return cores <= SIMPLIFIED_SUGGESTED_MAX_CORES;
}
