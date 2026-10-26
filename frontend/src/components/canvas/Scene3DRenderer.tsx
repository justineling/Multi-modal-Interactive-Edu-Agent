import { Html, OrbitControls } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentRef, ReactElement, RefObject } from "react";
import * as THREE from "three";

import { makeAnchorFromNode3D } from "@/components/canvas/anchors";
import type { AnchorFallback } from "@/components/canvas/anchors";
import { cx } from "@/components/common/cx";
import {
  annotationOffsetFor,
  cameraPresetByName,
  clampCameraPosition,
  deriveDefaultCamera,
  detectWebglSupport,
  geometryForPrimitive,
  materialForNode,
  placeArrowLink,
  resolveHierarchy,
  shouldSuggestSimplified,
  viewFromPreset,
} from "@/lib/scene3d";
import type { CameraView, GeometryDescriptor, ResolvedNode3D } from "@/lib/scene3d";
import { DURATION_MS } from "@/lib/theme";
import type { Node3D, SceneGraph3DSpec, Vector3 } from "@/types/artifact";
import type { Anchor } from "@/types/patch";

/** Drei 的 OrbitControls 实例类型（从组件推导，避免直接依赖 three-stdlib 的类型包） */
type OrbitControlsHandle = ComponentRef<typeof OrbitControls>;

interface Scene3DRendererProps {
  spec: SceneGraph3DSpec;
  artifactId: string;
  anchor: Anchor | null;
  onAnchor: (anchor: Anchor) => void;
  readOnly: boolean;
  fallback?: AnchorFallback;
  /** `focus_camera` 补丁带来的机位名：出现即平滑转过去 */
  focusPreset?: string | null;
  /** 转场完成后通知外层清空 `focusPreset`，避免与用户手动轨道旋转打架 */
  onFocusConsumed?: () => void;
}

function addRotation(base: Vector3, fix: Vector3): Vector3 {
  return [base[0] + fix[0], base[1] + fix[1], base[2] + fix[2]];
}

/** 相机平滑转场（`useFrame` 里做指数插值，帧率无关；不要瞬切） */
interface CameraRigProps {
  desired: CameraView | null;
  controlsRef: RefObject<OrbitControlsHandle | null>;
  onArrive: () => void;
}

function CameraRig({ desired, controlsRef, onArrive }: CameraRigProps): null {
  const targetPosition = useRef<THREE.Vector3>(new THREE.Vector3());
  const targetLook = useRef<THREE.Vector3>(new THREE.Vector3());
  const arrivedRef = useRef<boolean>(false);

  useEffect(() => {
    arrivedRef.current = false;
  }, [desired]);

  useFrame(({ camera }, delta) => {
    if (desired === null) {
      return;
    }
    targetPosition.current.set(desired.position[0], desired.position[1], desired.position[2]);
    targetLook.current.set(desired.target[0], desired.target[1], desired.target[2]);

    // 指数插值：t 只与时间有关，掉帧时不会「走得比时间慢」
    const ratio = 1 - Math.exp(-Math.max(delta, 0) * (1000 / DURATION_MS.camera) * 2.2);
    camera.position.lerp(targetPosition.current, ratio);

    const controls = controlsRef.current;
    if (controls !== null) {
      controls.target.lerp(targetLook.current, ratio);
      controls.update();
    } else {
      camera.lookAt(targetLook.current);
    }

    const distance = camera.position.distanceTo(targetPosition.current);
    if (!arrivedRef.current && distance < 0.02) {
      arrivedRef.current = true;
      onArrive();
    }
  });

  return null;
}

/** 单个 primitive 的几何（`arrow` 是复合体，单独在 `ArrowLinkMesh` 里画） */
function PrimitiveGeometry({ descriptor }: { descriptor: GeometryDescriptor }): ReactElement {
  switch (descriptor.kind) {
    case "box":
      return <boxGeometry args={descriptor.args} />;
    case "sphere":
      return <sphereGeometry args={descriptor.args} />;
    case "cylinder":
      return <cylinderGeometry args={descriptor.args} />;
    case "cone":
      return <coneGeometry args={descriptor.args} />;
    case "plane":
      return <planeGeometry args={descriptor.args} />;
    default:
      // arrow 由 ArrowLinkMesh 处理；这里给一个安全占位，保证类型收敛且不抛错
      return <boxGeometry args={[0.2, 0.2, 0.2]} />;
  }
}

interface ArrowLinkMeshProps {
  descriptor: Extract<GeometryDescriptor, { kind: "arrow" }>;
  color: string;
  from: Vector3;
  to: Vector3;
  simplified: boolean;
  onClick?: () => void;
}

/** 连接箭头：轴（圆柱）+ 头（圆锥），沿 from→to 摆放 */
function ArrowLinkMesh({
  descriptor,
  color,
  from,
  to,
  simplified,
  onClick,
}: ArrowLinkMeshProps): ReactElement {
  const [shaftRadius, headRadius, headLength, fallbackLength, radial] = descriptor.args;
  const placement = placeArrowLink(from, to);
  const length = placement.length > 0 ? placement.length : fallbackLength;
  const safeHeadLength = Math.min(headLength, length * 0.4);
  const shaftLength = Math.max(length - safeHeadLength, 0.01);
  const shaftCenterY = -safeHeadLength / 2;
  const headCenterY = length / 2 - safeHeadLength / 2;

  return (
    <group position={placement.position} rotation={placement.rotation}>
      <mesh position={[0, shaftCenterY, 0]} onClick={onClick}>
        <cylinderGeometry args={[shaftRadius, shaftRadius, shaftLength, radial]} />
        <meshStandardMaterial
          color={color}
          metalness={simplified ? 0 : 0.18}
          roughness={simplified ? 1 : 0.52}
          flatShading={simplified}
        />
      </mesh>
      <mesh position={[0, headCenterY, 0]} onClick={onClick}>
        <coneGeometry args={[headRadius, safeHeadLength, radial]} />
        <meshStandardMaterial
          color={color}
          metalness={simplified ? 0 : 0.18}
          roughness={simplified ? 1 : 0.52}
          flatShading={simplified}
        />
      </mesh>
    </group>
  );
}

interface SceneNodeProps {
  resolved: ResolvedNode3D;
  parent: ResolvedNode3D | null;
  simplified: boolean;
  active: boolean;
  onSelectNode: (node: Node3D) => void;
  onOpenAnnotation: (node: Node3D) => void;
}

function SceneNode({
  resolved,
  parent,
  simplified,
  active,
  onSelectNode,
  onOpenAnnotation,
}: SceneNodeProps): ReactElement {
  const { node, worldPosition, worldScale } = resolved;
  const descriptor = geometryForPrimitive(node.primitive, simplified);
  const material = materialForNode(node, simplified);
  const rotation = addRotation(node.rotation, descriptor.rotationFix);
  const annotationOffset = annotationOffsetFor(descriptor, worldScale);

  const handleSelect = useCallback((): void => {
    onSelectNode(node);
  }, [node, onSelectNode]);

  const handleAnnotation = useCallback((): void => {
    onOpenAnnotation(node);
  }, [node, onOpenAnnotation]);

  const annotationDot = (
    <Html center distanceFactor={9} position={annotationOffset} zIndexRange={[20, 0]}>
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          handleAnnotation();
        }}
        title={node.label}
        /* 标注点只显示一个字，读屏软件需要完整语义，否则只会念出一个孤立汉字 */
        aria-label={`标注点：${node.label}`}
        className={cx(
          "flex h-5 w-5 items-center justify-center rounded-full border text-xxs font-medium transition-transform hover:scale-110",
          active
            ? "border-palette-amber bg-palette-amber text-surface-base"
            : "border-white/70 bg-surface-overlay/90 text-content-secondary",
        )}
      >
        {node.label.slice(0, 1)}
      </button>
    </Html>
  );

  if (descriptor.kind === "arrow") {
    const from: Vector3 =
      parent === null
        ? [worldPosition[0], worldPosition[1] - (descriptor.args[3] * worldScale[1]) / 2, worldPosition[2]]
        : parent.worldPosition;
    const to: Vector3 =
      parent === null
        ? [worldPosition[0], worldPosition[1] + (descriptor.args[3] * worldScale[1]) / 2, worldPosition[2]]
        : worldPosition;
    return (
      <group>
        <ArrowLinkMesh
          descriptor={descriptor}
          color={node.color}
          from={from}
          to={to}
          simplified={simplified}
          onClick={handleSelect}
        />
        <group position={worldPosition}>{annotationDot}</group>
      </group>
    );
  }

  return (
    <group position={worldPosition} rotation={rotation} scale={worldScale}>
      <mesh
        onClick={(event) => {
          event.stopPropagation();
          handleSelect();
        }}
        castShadow={!simplified}
        receiveShadow={!simplified}
      >
        <PrimitiveGeometry descriptor={descriptor} />
        <meshStandardMaterial
          color={material.color}
          metalness={material.metalness}
          roughness={material.roughness}
          opacity={material.opacity}
          transparent={material.transparent}
          flatShading={material.flatShading}
          emissive={material.emissive}
          emissiveIntensity={material.emissiveIntensity}
        />
      </mesh>
      {/* 选中描边：用同几何的线框叠一层，比后处理 outline 便宜得多 */}
      {active ? (
        <mesh scale={1.04}>
          <PrimitiveGeometry descriptor={descriptor} />
          <meshBasicMaterial color="#F59E0B" wireframe />
        </mesh>
      ) : null}
      <group position={annotationOffset}>{annotationDot}</group>
    </group>
  );
}

/**
 * 3D 空间场景渲染器（React-Three-Fiber + Drei）。
 *
 * ## 四个硬性约束的落点
 * 1. **首帧 ≤ 5s（P0-09③）**：`Canvas` 外面套 `Suspense`，内部只做同步几何构建，
 *    不加载任何外部资源（HDR 环境贴图、GLTF 都要走网络，一律不用）。
 *    加载态由 `onCreated` 关掉，学生在 1s 内就能看到第一帧。
 * 2. **相机平滑聚焦**：`CameraRig` 在 `useFrame` 里做指数插值（帧率无关），
 *    到位后回调清空目标 —— 否则用户手动拖轨道会被一直拽回去。
 * 3. **低性能降级**：`simplified` 同时关阴影、降段数、去金属高光；
 *    首次挂载按 CPU 核数自动建议开启，并显示可关闭的提示条。
 * 4. **畸形 spec 不白屏**：`resolveHierarchy` 兜住环 / 悬空 `parent_id`，
 *    未知 primitive 退化成 box；WebGL 不可用时整块降级为文字提示（不挂 Canvas）。
 *
 * ## 层级
 * `resolveHierarchy` 把「相对父节点的坐标」累加成世界坐标后**扁平渲染**。
 * 不用嵌套 `<group>` 承载层级，是因为嵌套会把父级的旋转/缩放一并继承下来，
 * 而 `Node3D.rotation` 的语义是「这个部件自身的朝向」（LLM 按这个语义填）。
 * 父子关系通过 `arrow_link` 的连线可视化，视觉上不缺信息，数值上不会错位。
 */
export function Scene3DRenderer({
  spec,
  artifactId,
  anchor,
  onAnchor,
  readOnly,
  fallback,
  focusPreset = null,
  onFocusConsumed,
}: Scene3DRendererProps): ReactElement {
  const [webgl, setWebgl] = useState<{ supported: boolean; reason: string | null } | null>(null);
  const [ready, setReady] = useState<boolean>(false);
  const [simplified, setSimplified] = useState<boolean>(false);
  const [tourIndex, setTourIndex] = useState<number>(0);
  const [desired, setDesired] = useState<CameraView | null>(null);
  const [annotationNode, setAnnotationNode] = useState<Node3D | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hintDismissed, setHintDismissed] = useState<boolean>(false);

  const controlsRef = useRef<OrbitControlsHandle | null>(null);

  const resolvedNodes = useMemo(() => resolveHierarchy(spec.nodes), [spec.nodes]);
  const defaultView = useMemo(() => deriveDefaultCamera(spec.nodes), [spec.nodes]);

  // WebGL 能力探测（无 DOM 环境下按不支持处理，由下方降级 UI 兜住）
  useEffect(() => {
    setWebgl(detectWebglSupport());
  }, []);

  // 低性能设备默认建议简化渲染
  useEffect(() => {
    setSimplified(shouldSuggestSimplified());
  }, []);

  // 换一份**产物**：回到默认机位，清掉选中与导览进度。
  //
  // 依赖只写 `artifactId`（不含 `spec`）：patch 之后 artifact 对象会被替换，
  // `spec` 引用每次都变；跟着它重置会把学生刚调好的视角和导览进度全部打回原点。
  // 另外不要把 `ready` 置回 false —— Canvas 不会因 spec 变化重新挂载，
  // `onCreated` 不会再触发，置回去会让「正在准备 3D 场景」的遮罩永远挂着。
  useEffect(() => {
    setTourIndex(0);
    setDesired(null);
    setAnnotationNode(null);
    setSelectedId(null);
  }, [artifactId]);

  // focus_camera 补丁：转到指定机位（找不到预设就忽略，不要跳到奇怪的位置）
  useEffect(() => {
    if (focusPreset === null) {
      return;
    }
    const preset = cameraPresetByName(spec.camera_tour, focusPreset);
    if (preset === null) {
      onFocusConsumed?.();
      return;
    }
    setDesired(viewFromPreset(preset));
  }, [focusPreset, onFocusConsumed, spec.camera_tour]);

  const handleFocusConsumed = useCallback((): void => {
    setDesired(null);
    onFocusConsumed?.();
  }, [onFocusConsumed]);

  const handleSelectNode = useCallback(
    (node: Node3D): void => {
      setSelectedId(node.id);
      setAnnotationNode(node);
      if (readOnly) {
        return;
      }
      const index = spec.nodes.findIndex((item) => item.id === node.id);
      if (index < 0) {
        return;
      }
      onAnchor(
        makeAnchorFromNode3D({
          artifactId,
          nodeIndex: index,
          node,
          ...(fallback === undefined ? {} : { fallback }),
        }),
      );
    },
    [artifactId, fallback, onAnchor, readOnly, spec.nodes],
  );

  const handleOpenAnnotation = useCallback(
    (node: Node3D): void => {
      handleSelectNode(node);
      const resolved = resolvedNodes.find((item) => item.node.id === node.id);
      if (resolved === undefined) {
        return;
      }
      // 聚焦：从机位方向拉近到 3.2 倍包围半径，视线中心对准该部件
      const target = resolved.worldPosition;
      const direction = new THREE.Vector3(
        defaultView.position[0] - target[0],
        defaultView.position[1] - target[1],
        defaultView.position[2] - target[2],
      );
      const length = Math.max(direction.length(), 0.001);
      const distance = Math.max(length * 0.55, 3);
      direction.multiplyScalar(distance / length);
      const position: Vector3 = [
        target[0] + direction.x,
        target[1] + direction.y,
        target[2] + direction.z,
      ];
      setDesired({ position: clampCameraPosition(position, target), target });
    },
    [defaultView.position, handleSelectNode, resolvedNodes],
  );

  const goNextTourStop = useCallback((): void => {
    if (spec.camera_tour.length === 0) {
      return;
    }
    const next = (tourIndex + 1) % spec.camera_tour.length;
    setTourIndex(next);
    const preset = spec.camera_tour[next];
    if (preset !== undefined) {
      setDesired(viewFromPreset(preset));
    }
  }, [spec.camera_tour, tourIndex]);

  const resetCamera = useCallback((): void => {
    setDesired(defaultView);
  }, [defaultView]);

  const anchoredNodeIndex = useMemo(() => {
    if (anchor === null) {
      return -1;
    }
    const match = /^\/nodes\/(\d+)$/.exec(anchor.node_path);
    if (match === null) {
      return -1;
    }
    const parsed = Number.parseInt(match[1] ?? "", 10);
    return Number.isFinite(parsed) ? parsed : -1;
  }, [anchor]);

  const currentPreset = spec.camera_tour[tourIndex];

  if (webgl !== null && !webgl.supported) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
        <p className="text-sm text-content-secondary">这台设备暂时无法显示 3D 场景</p>
        <p className="max-w-sm text-xxs leading-relaxed text-content-muted">
          {webgl.reason ?? "浏览器未启用 WebGL"}。3D 内容仍完整存在，可以先看左侧助教的文字讲解。
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 px-3 pt-2">
        <span className="rounded-xs border border-palette-indigo/50 bg-palette-indigo/15 px-1.5 py-px text-xxs text-palette-indigo">
          3D 场景
        </span>
        <span className="min-w-0 truncate text-xs text-content-secondary" title={spec.title}>
          {spec.title}
        </span>
        <span className="ml-auto font-mono text-xxs text-content-muted">
          {spec.nodes.length} 个部件
        </span>
        {readOnly ? (
          <span className="rounded-xs border border-palette-amber/60 bg-palette-amber/15 px-1.5 py-px text-xxs text-palette-amber">
            只读
          </span>
        ) : null}
      </div>

      <div className="relative min-h-0 flex-1">
        <Canvas
          dpr={[1, 2]}
          shadows={!simplified}
          gl={{ antialias: !simplified, powerPreference: "high-performance" }}
          camera={{ position: defaultView.position, fov: 50, near: 0.1, far: 400 }}
          onCreated={() => {
            setReady(true);
          }}
        >
          <color attach="background" args={["#080E19"]} />
          <Suspense fallback={null}>
            {/* 简化渲染时关掉环境光：环境光需要逐像素参与光照计算，是移动端的大头 */}
            {simplified ? null : <ambientLight intensity={0.55} />}
            <hemisphereLight args={["#E2E8F0", "#0B1220", simplified ? 0.8 : 0.45]} />
            <directionalLight
              position={[6, 10, 6]}
              intensity={simplified ? 1.1 : 1.35}
              castShadow={!simplified}
            />

            {resolvedNodes.map((resolved) => (
              <SceneNode
                key={resolved.node.id}
                resolved={resolved}
                parent={resolved.parentIndex === null ? null : (resolvedNodes[resolved.parentIndex] ?? null)}
                simplified={simplified}
                active={
                  selectedId === resolved.node.id ||
                  spec.nodes[anchoredNodeIndex]?.id === resolved.node.id
                }
                onSelectNode={handleSelectNode}
                onOpenAnnotation={handleOpenAnnotation}
              />
            ))}

            <CameraRig
              desired={desired}
              controlsRef={controlsRef}
              onArrive={handleFocusConsumed}
            />
            <OrbitControls
              ref={controlsRef}
              enableDamping
              dampingFactor={0.08}
              minDistance={1.5}
              maxDistance={40}
              target={defaultView.target}
              makeDefault
            />
          </Suspense>
        </Canvas>

        {ready ? null : (
          <div className="absolute inset-0 flex items-center justify-center bg-surface-sunken/80">
            <p className="text-xs text-content-muted">正在准备 3D 场景…</p>
          </div>
        )}

        {/* 右上角：视角操作 */}
        <div className="absolute right-2 top-2 flex flex-col items-end gap-1">
          <button
            type="button"
            onClick={resetCamera}
            className="rounded-sm border border-line-strong bg-surface-overlay/90 px-2 py-0.5 text-xxs text-content-secondary transition-colors hover:border-accent/60 hover:text-content-primary"
          >
            复位视角
          </button>
          <button
            type="button"
            onClick={() => {
              setSimplified((value) => !value);
            }}
            className={cx(
              "rounded-sm border px-2 py-0.5 text-xxs transition-colors",
              simplified
                ? "border-palette-green/60 bg-palette-green/15 text-palette-green"
                : "border-line-strong bg-surface-overlay/90 text-content-secondary hover:border-accent/60",
            )}
            title="关闭阴影、降低面数、去掉高光，低配设备上更流畅"
          >
            {simplified ? "简化渲染：开" : "简化渲染：关"}
          </button>
        </div>

        {simplified || hintDismissed ? null : (
          <div className="absolute bottom-2 left-2 flex max-w-[16rem] items-start gap-1 rounded-sm border border-line bg-surface-overlay/90 px-2 py-1">
            <p className="flex-1 text-xxs leading-relaxed text-content-muted">
              画面卡顿？可以打开右上角「简化渲染」。
            </p>
            <button
              type="button"
              onClick={() => {
                setHintDismissed(true);
              }}
              className="shrink-0 text-xxs text-content-muted hover:text-content-primary"
              aria-label="关闭提示"
            >
              ×
            </button>
          </div>
        )}

        {/* 标注内容面板 */}
        {annotationNode === null ? null : (
          <div className="absolute left-2 top-2 w-64 rounded-md border border-line bg-surface-overlay/95 p-2 shadow-floating">
            <div className="flex items-start gap-1">
              <p className="min-w-0 flex-1 truncate text-xs font-medium text-content-primary">
                {annotationNode.label}
              </p>
              <button
                type="button"
                onClick={() => {
                  setAnnotationNode(null);
                }}
                className="shrink-0 text-xxs text-content-muted hover:text-content-primary"
                aria-label="关闭标注"
              >
                ×
              </button>
            </div>
            <p className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap text-xxs leading-relaxed text-content-secondary">
              {annotationNode.annotation_markdown.length > 0
                ? annotationNode.annotation_markdown
                : "（这个部件没有补充说明）"}
            </p>
            <p className="mt-1 font-mono text-xxs text-content-muted">
              出处：{annotationNode.source_page === null ? "未标注页码" : `p.${annotationNode.source_page}`}
            </p>
            {readOnly ? null : (
              <p className="mt-1 text-xxs text-palette-amber">已带上这个部件的锚点，追问只针对它</p>
            )}
          </div>
        )}
      </div>

      {/* 相机导览 */}
      <div className="shrink-0 border-t border-line bg-surface-base px-3 py-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={goNextTourStop}
            disabled={spec.camera_tour.length === 0}
            className="rounded-sm border border-line px-2 py-0.5 text-xs text-content-secondary transition-colors hover:border-accent/60 disabled:opacity-30"
          >
            下一站
          </button>
          <span className="min-w-0 flex-1 truncate text-xs text-content-secondary">
            {currentPreset === undefined ? "没有预设机位" : currentPreset.name}
          </span>
          <span className="shrink-0 font-mono text-xxs text-content-muted">
            {spec.camera_tour.length === 0 ? "0 / 0" : `${tourIndex + 1} / ${spec.camera_tour.length}`}
          </span>
        </div>
        {currentPreset === undefined ? null : (
          <p className="mt-1 text-xxs leading-relaxed text-content-muted">{currentPreset.explanation}</p>
        )}
        <p className="mt-1 text-xxs text-content-muted">
          拖拽旋转 · 滚轮缩放 · 点标注点可聚焦并看说明
        </p>
      </div>
    </div>
  );
}
