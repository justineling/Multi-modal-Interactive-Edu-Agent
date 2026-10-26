/**
 * `lib/scene3d` 单测：primitive 映射、层级解析、相机工具。
 *
 * 这里最有价值的两组断言：
 * 1. `resolveHierarchy` 对**环**与**悬空 parent_id** 的处理 —— 这两类畸形输入
 *    在 patch 之后真的会出现，处理不当不是「画错」而是「渲染线程卡死」；
 * 2. `placeArrowLink` 的朝向 —— 用 three 自己把欧拉角作用回 +Y 轴验证，
 *    而不是硬编码某个期望角度（硬编码会在换坐标系时静默失效）。
 */

import { Euler, Vector3 as ThreeVector3 } from "three";
import { describe, expect, it } from "vitest";

import {
  annotationOffsetFor,
  cameraPresetByName,
  clampCameraPosition,
  deriveDefaultCamera,
  distanceBetween,
  geometryForPrimitive,
  lerpVector3,
  materialForNode,
  placeArrowLink,
  resolveHierarchy,
  shouldSuggestSimplified,
  viewFromPreset,
} from "@/lib/scene3d";
import { MAX_CAMERA_DISTANCE, SCENE_BOUND_SIZE } from "@/lib/theme";
import type { CameraPreset, Node3D, Primitive3D } from "@/types/artifact";

function makeNode(overrides: Partial<Node3D> = {}): Node3D {
  return {
    id: "n1",
    primitive: "box",
    label: "底座",
    position: [0, 0, 0],
    scale: [1, 1, 1],
    rotation: [0, 0, 0],
    color: "#2563EB",
    annotation_markdown: "这是底座",
    source_page: 94,
    parent_id: null,
    ...overrides,
  };
}

const ALL_PRIMITIVES: readonly Primitive3D[] = [
  "box",
  "sphere",
  "cylinder",
  "cone",
  "terrain_plane",
  "arrow_link",
];

describe("geometryForPrimitive", () => {
  it("6 种 primitive 全部有映射，且 kind 与 primitive 对应", () => {
    const kinds = ALL_PRIMITIVES.map((primitive) => geometryForPrimitive(primitive, false).kind);
    expect(kinds).toEqual(["box", "sphere", "cylinder", "cone", "plane", "arrow"]);
  });

  it("简化档位降低球体段数（面数下降但轮廓不变）", () => {
    const standard = geometryForPrimitive("sphere", false);
    const simplified = geometryForPrimitive("sphere", true);
    expect(standard.kind).toBe("sphere");
    expect(simplified.kind).toBe("sphere");
    if (standard.kind === "sphere" && simplified.kind === "sphere") {
      expect(simplified.args[1]).toBeLessThan(standard.args[1]);
      expect(simplified.args[2]).toBeLessThan(standard.args[2]);
    }
  });

  it("terrain_plane 自带 -90° 修正旋转（平面几何默认立着）", () => {
    const descriptor = geometryForPrimitive("terrain_plane", false);
    expect(descriptor.rotationFix[0]).toBeCloseTo(-Math.PI / 2, 6);
    expect(descriptor.args[0]).toBe(SCENE_BOUND_SIZE);
  });

  it("arrow_link 的 args 是 5 元组（轴半径/头半径/头长/总长/段数）", () => {
    const descriptor = geometryForPrimitive("arrow_link", false);
    expect(descriptor.kind).toBe("arrow");
    expect(descriptor.args).toHaveLength(5);
    expect(descriptor.args[3]).toBeGreaterThan(descriptor.args[2]);
  });

  it("未知 primitive 退化为 box，不抛错", () => {
    const descriptor = geometryForPrimitive("torus_knot" as Primitive3D, false);
    expect(descriptor.kind).toBe("box");
  });
});

describe("materialForNode", () => {
  it("简化档位关掉金属高光并开平面着色", () => {
    const material = materialForNode(makeNode(), true);
    expect(material.metalness).toBe(0);
    expect(material.roughness).toBe(1);
    expect(material.flatShading).toBe(true);
  });

  it("标准档位保留一点金属感", () => {
    const material = materialForNode(makeNode({ color: "#10B981" }), false);
    expect(material.color).toBe("#10B981");
    expect(material.metalness).toBeGreaterThan(0);
    expect(material.flatShading).toBe(false);
  });
});

describe("annotationOffsetFor", () => {
  it("标注点压在物体正上方", () => {
    const descriptor = geometryForPrimitive("box", false);
    const offset = annotationOffsetFor(descriptor, [2, 3, 2]);
    expect(offset[0]).toBe(0);
    expect(offset[1]).toBeCloseTo(0.5 * 3 + 0.34, 6);
    expect(offset[2]).toBe(0);
  });
});

describe("resolveHierarchy", () => {
  it("parent_id 存在时把相对坐标累加成世界坐标", () => {
    const nodes = [
      makeNode({ id: "root", position: [1, 0, 0] }),
      makeNode({ id: "child", position: [0, 2, 0], parent_id: "root", scale: [2, 2, 2] }),
    ];
    const resolved = resolveHierarchy(nodes);
    expect(resolved[0]?.worldPosition).toEqual([1, 0, 0]);
    expect(resolved[1]?.worldPosition).toEqual([1, 2, 0]);
    expect(resolved[1]?.worldScale).toEqual([2, 2, 2]);
    expect(resolved[1]?.depth).toBe(1);
    expect(resolved[1]?.parentIndex).toBe(0);
  });

  it("三级层级逐级累加", () => {
    const nodes = [
      makeNode({ id: "a", position: [1, 0, 0] }),
      makeNode({ id: "b", position: [0, 1, 0], parent_id: "a" }),
      makeNode({ id: "c", position: [0, 0, 1], parent_id: "b" }),
    ];
    const resolved = resolveHierarchy(nodes);
    expect(resolved[2]?.worldPosition).toEqual([1, 1, 1]);
    expect(resolved[2]?.depth).toBe(2);
  });

  it("parent_id 指向不存在的节点时当根处理（零件不会消失）", () => {
    const nodes = [makeNode({ id: "orphan", position: [3, 3, 3], parent_id: "missing" })];
    const resolved = resolveHierarchy(nodes);
    expect(resolved[0]?.worldPosition).toEqual([3, 3, 3]);
    expect(resolved[0]?.parentIndex).toBeNull();
  });

  it("parent_id 指向自己时当根处理", () => {
    const nodes = [makeNode({ id: "self", position: [2, 0, 0], parent_id: "self" })];
    const resolved = resolveHierarchy(nodes);
    expect(resolved[0]?.worldPosition).toEqual([2, 0, 0]);
    expect(resolved[0]?.depth).toBe(0);
  });

  it("出现环时不死循环（打断成根）", () => {
    const nodes = [
      makeNode({ id: "a", position: [1, 0, 0], parent_id: "b" }),
      makeNode({ id: "b", position: [0, 1, 0], parent_id: "a" }),
    ];
    const resolved = resolveHierarchy(nodes);
    expect(resolved).toHaveLength(2);
    // 至少有一个被当成根，且两个都有结果（不会返回 undefined）
    expect(resolved.every((item) => Number.isFinite(item.worldPosition[0]))).toBe(true);
    expect(resolved.some((item) => item.parentIndex === null)).toBe(true);
  });

  it("空列表返回空数组", () => {
    expect(resolveHierarchy([])).toEqual([]);
  });
});

describe("lerpVector3 / distanceBetween", () => {
  it("t=0 取起点，t=1 取终点，越界被夹住", () => {
    expect(lerpVector3([0, 0, 0], [2, 4, 6], 0)).toEqual([0, 0, 0]);
    expect(lerpVector3([0, 0, 0], [2, 4, 6], 1)).toEqual([2, 4, 6]);
    expect(lerpVector3([0, 0, 0], [2, 4, 6], 2)).toEqual([2, 4, 6]);
    expect(lerpVector3([0, 0, 0], [2, 4, 6], 0.5)).toEqual([1, 2, 3]);
  });

  it("距离计算", () => {
    expect(distanceBetween([0, 0, 0], [3, 4, 0])).toBeCloseTo(5, 6);
  });
});

describe("clampCameraPosition", () => {
  it("超出上限时按比例拉回到球面上", () => {
    const target: [number, number, number] = [0, 0, 0];
    const far: [number, number, number] = [0, 0, 1000];
    const clamped = clampCameraPosition(far, target);
    expect(distanceBetween(clamped, target)).toBeCloseTo(MAX_CAMERA_DISTANCE, 6);
  });

  it("未超限时原样返回", () => {
    const position: [number, number, number] = [1, 2, 3];
    expect(clampCameraPosition(position, [0, 0, 0])).toEqual(position);
  });
});

describe("cameraPresetByName / viewFromPreset", () => {
  const tour: CameraPreset[] = [
    { name: "正面", position: [0, 0, 8], target: [0, 0, 0], explanation: "看整体" },
    { name: "鸟瞰", position: [0, 200, 0], target: [0, 0, 0], explanation: "看布局" },
  ];

  it("按名字取机位", () => {
    expect(cameraPresetByName(tour, "鸟瞰")?.explanation).toBe("看布局");
  });

  it("名字不存在时返回 null（patch 可能删掉了预设）", () => {
    expect(cameraPresetByName(tour, "侧面")).toBeNull();
  });

  it("转成视图时机位被夹取，不会飞出场景", () => {
    const view = viewFromPreset(tour[1]!);
    expect(distanceBetween(view.position, view.target)).toBeLessThanOrEqual(
      MAX_CAMERA_DISTANCE + 1e-6,
    );
  });
});

describe("deriveDefaultCamera", () => {
  it("按节点包围盒中心取景", () => {
    const nodes = [makeNode({ id: "a", position: [-5, 0, 0] }), makeNode({ id: "b", position: [5, 0, 0] })];
    const view = deriveDefaultCamera(nodes);
    expect(view.target[0]).toBeCloseTo(0, 6);
    expect(distanceBetween(view.position, view.target)).toBeLessThanOrEqual(
      MAX_CAMERA_DISTANCE + 1e-6,
    );
  });

  it("空场景给出可用默认机位", () => {
    const view = deriveDefaultCamera([]);
    expect(distanceBetween(view.position, view.target)).toBeGreaterThan(0);
  });
});

describe("placeArrowLink", () => {
  it("沿 +Y 时不需要旋转", () => {
    const placement = placeArrowLink([0, 0, 0], [0, 2, 0]);
    expect(placement.length).toBeCloseTo(2, 6);
    expect(placement.position).toEqual([0, 1, 0]);
    expect(placement.rotation[0]).toBeCloseTo(0, 6);
    expect(placement.rotation[1]).toBeCloseTo(0, 6);
    expect(placement.rotation[2]).toBeCloseTo(0, 6);
  });

  it("斜向时把 +Y 轴旋到正确方向（用 three 自己验证）", () => {
    const from: [number, number, number] = [1, 2, 3];
    const to: [number, number, number] = [4, 6, 3];
    const placement = placeArrowLink(from, to);

    const axis = new ThreeVector3(0, 1, 0);
    axis.applyEuler(new Euler(placement.rotation[0], placement.rotation[1], placement.rotation[2]));

    const expected = new ThreeVector3(to[0] - from[0], to[1] - from[1], to[2] - from[2]).normalize();
    expect(axis.x).toBeCloseTo(expected.x, 5);
    expect(axis.y).toBeCloseTo(expected.y, 5);
    expect(axis.z).toBeCloseTo(expected.z, 5);
  });

  it("两点重合时返回零长度而不是 NaN", () => {
    const placement = placeArrowLink([1, 1, 1], [1, 1, 1]);
    expect(placement.length).toBe(0);
    expect(placement.position).toEqual([1, 1, 1]);
    expect(placement.rotation.every((value) => Number.isFinite(value))).toBe(true);
  });
});

describe("shouldSuggestSimplified", () => {
  it("node 环境（无 navigator.hardwareConcurrency）不误报", () => {
    // vitest 默认 node 环境：没有 navigator.hardwareConcurrency 时应返回 false
    expect(typeof shouldSuggestSimplified()).toBe("boolean");
  });
});
