import Konva from "konva";
import gsap from "gsap";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";
import { Arrow, Circle, Group, Layer, Line, Rect, Stage, Text } from "react-konva";

import { makeAnchorFromEntity } from "@/components/canvas/anchors";
import type { AnchorFallback } from "@/components/canvas/anchors";
import { cx } from "@/components/common/cx";
import {
  clampPixelRatio,
  clampStepIndex,
  computeStageTransform,
  computeStepTransition,
  entityBox,
  narrationOf,
  orderSteps,
  pageLabel,
  tweenDuration,
} from "@/lib/anim2d";
import type { StageTransform } from "@/lib/anim2d";
import { CANVAS_HEIGHT, CANVAS_WIDTH, DURATION_MS, EASING, FONT_FAMILY } from "@/lib/theme";
import type { Entity2D, Keyframe2DSpec, KeyframeStep } from "@/types/artifact";
import type { Anchor } from "@/types/patch";

interface Anim2DRendererProps {
  spec: Keyframe2DSpec;
  artifactId: string;
  /** 当前锚点；`/steps/{i}/entities/{j}` 命中的元素会保持高亮 */
  anchor: Anchor | null;
  onAnchor: (anchor: Anchor) => void;
  /** 历史版本只读：禁止锚点追问 */
  readOnly: boolean;
  /** 锚点兜底上下文（section_code / page），见 `anchors.ts` */
  fallback?: AnchorFallback;
  /** 工具条缩放倍率 */
  zoom?: number;
}

/** 每帧之间的停留时间（旁白需要时间读） */
const HOLD_MS = 1100;

/** 用 ResizeObserver 跟随容器尺寸；容器未测量到时返回 0（Stage 不渲染，避免画 0×0） */
function useContainerSize(): [
  (node: HTMLDivElement | null) => void,
  { width: number; height: number },
] {
  const [size, setSize] = useState<{ width: number; height: number }>({ width: 0, height: 0 });
  const observerRef = useRef<ResizeObserver | null>(null);

  const attach = useCallback((node: HTMLDivElement | null): void => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (node === null) {
      return;
    }
    const measure = (): void => {
      const rect = node.getBoundingClientRect();
      setSize({ width: rect.width, height: rect.height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    observerRef.current = observer;
  }, []);

  useEffect(() => {
    return () => {
      observerRef.current?.disconnect();
      observerRef.current = null;
    };
  }, []);

  return [attach, size];
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable;
}

/** 单个元素的绘制（局部坐标：包围盒左上角为原点） */
interface EntityShapeProps {
  entity: Entity2D;
  width: number;
  height: number;
  /** 逻辑 → 像素的比例，用于线宽 / 字号这类「不随元素尺寸走」的量 */
  scale: number;
  active: boolean;
}

function EntityShape({ entity, width, height, scale, active }: EntityShapeProps): ReactElement {
  const fontSize = Math.max(9, Math.min(20 * scale, 22));
  const strokeWidth = Math.max(1, (active ? 4 : 2) * scale);
  const label = entity.label.length > 0 ? entity.label : "（未命名）";

  switch (entity.shape) {
    case "circle": {
      const radius = Math.min(width, height) / 2;
      return (
        <>
          <Circle
            x={width / 2}
            y={height / 2}
            radius={radius}
            fill={entity.color}
            opacity={0.9}
            stroke={active ? "#FFFFFF" : entity.color}
            strokeWidth={strokeWidth}
            shadowColor={entity.color}
            shadowBlur={active ? 18 : 0}
          />
          <Text
            x={0}
            y={height / 2 - fontSize}
            width={width}
            height={fontSize * 2}
            text={label}
            fontSize={fontSize}
            fontFamily={FONT_FAMILY.sans}
            fill="#FFFFFF"
            align="center"
            verticalAlign="middle"
            listening={false}
          />
        </>
      );
    }

    case "rect": {
      return (
        <>
          <Rect
            x={0}
            y={0}
            width={width}
            height={height}
            cornerRadius={10 * scale}
            fill={entity.color}
            opacity={0.92}
            stroke={active ? "#FFFFFF" : entity.color}
            strokeWidth={strokeWidth}
          />
          <Text
            x={8 * scale}
            y={0}
            width={width - 16 * scale}
            height={height}
            text={label}
            fontSize={fontSize}
            fontFamily={FONT_FAMILY.sans}
            fill="#FFFFFF"
            align="center"
            verticalAlign="middle"
            wrap="word"
            ellipsis
            listening={false}
          />
        </>
      );
    }

    case "arrow": {
      return (
        <>
          <Arrow
            points={[0, height / 2, width, height / 2]}
            stroke={entity.color}
            fill={entity.color}
            strokeWidth={strokeWidth}
            pointerLength={Math.max(8, 14 * scale)}
            pointerWidth={Math.max(8, 14 * scale)}
            opacity={active ? 1 : 0.9}
          />
          <Text
            x={0}
            y={height / 2 - fontSize * 1.9}
            width={width}
            text={label}
            fontSize={fontSize}
            fontFamily={FONT_FAMILY.sans}
            fill={entity.color}
            align="center"
            listening={false}
          />
        </>
      );
    }

    case "label_box": {
      return (
        <>
          <Rect
            x={0}
            y={0}
            width={width}
            height={height}
            cornerRadius={8 * scale}
            fill={entity.color}
            opacity={0.16}
            stroke={entity.color}
            strokeWidth={strokeWidth}
          />
          <Text
            x={10 * scale}
            y={0}
            width={width - 20 * scale}
            height={height}
            text={label}
            fontSize={fontSize}
            fontFamily={FONT_FAMILY.sans}
            fill={entity.color}
            align="center"
            verticalAlign="middle"
            wrap="word"
            ellipsis
            listening={false}
          />
        </>
      );
    }

    case "zone": {
      return (
        <>
          <Rect
            x={0}
            y={0}
            width={width}
            height={height}
            cornerRadius={18 * scale}
            fill={entity.color}
            opacity={0.1}
            stroke={entity.color}
            strokeWidth={strokeWidth}
            dash={[10 * scale, 8 * scale]}
          />
          <Text
            x={14 * scale}
            y={10 * scale}
            width={width - 28 * scale}
            text={label}
            fontSize={fontSize}
            fontFamily={FONT_FAMILY.sans}
            fill={entity.color}
            listening={false}
          />
        </>
      );
    }

    default: {
      // 运行期可能收到历史版本里的旧 shape：画个占位框，绝不抛错
      return (
        <Rect
          x={0}
          y={0}
          width={width}
          height={height}
          stroke={entity.color}
          strokeWidth={strokeWidth}
          dash={[6, 6]}
        />
      );
    }
  }
}

/**
 * 2D 关键帧动画渲染器（React-Konva + GSAP）。
 *
 * ## 分层（P0-08④ 性能要求的落点）
 * Konva 的 `Layer` 是独立的 `<canvas>`。分成三层不是为了好看，而是为了**只重画变了的层**：
 * - 背景层（网格 + 画布底）：永不重画；
 * - 元素层：GSAP 补间期间每帧重画；
 * - 交互层（高亮框 / 选中描边）：只在选中变化时重画，`listening={false}` 不参与命中测试。
 * 若把三层合成一层，补间期间每帧都要重画网格线，拖动时间轴时掉帧会非常明显。
 *
 * ## 补间
 * GSAP 直接改 Konva 节点的 `x` / `y` / `opacity`，**不触发 React 重渲染**。
 * 这是「连续拖拽 60 次不掉到 30fps」的关键：React 一次渲染都不做，只有 canvas 在画。
 * `computeStepTransition` 只负责算「从哪来、到哪去」，纯函数、可单测。
 *
 * ## 坐标
 * `Entity2D.x/y` 是 1000×600 逻辑画布上的**中心点**。`computeStageTransform` 做等比
 * contain 映射，`entityBox` 把中心点换成统一的左上角包围盒 —— 补间只在包围盒坐标系里做。
 */
export function Anim2DRenderer({
  spec,
  artifactId,
  anchor,
  onAnchor,
  readOnly,
  fallback,
  zoom = 1,
}: Anim2DRendererProps): ReactElement {
  const [attachSize, size] = useContainerSize();
  const steps = useMemo(() => orderSteps(spec.steps), [spec.steps]);
  const total = steps.length;

  const [stepIndex, setStepIndex] = useState<number>(0);
  const [playing, setPlaying] = useState<boolean>(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [hover, setHover] = useState<{ entity: Entity2D; x: number; y: number } | null>(null);

  const entityLayerRef = useRef<Konva.Layer | null>(null);
  const groupRefs = useRef<Map<string, Konva.Group>>(new Map());
  const previousStepRef = useRef<KeyframeStep | null>(null);
  const rafRef = useRef<number | null>(null);
  const pendingIndexRef = useRef<number>(0);

  const safeIndex = clampStepIndex(stepIndex, total);
  const currentStep = steps[safeIndex];

  // 换一份**产物**时回到第一帧。
  //
  // 依赖刻意只写 `artifactId`，不写 `spec`：patch 之后 store 里的 artifact 对象会被替换
  // （`upsertArtifact` / 拉取快照），`spec` 引用每次都变。若跟着 spec 重置，
  // 学生正在看的第 3 帧会在一次无关刷新后跳回第 1 帧、播放也会被打断。
  // 帧数变少的情况由下面的 `clampStepIndex` 兜住。
  useEffect(() => {
    setStepIndex(0);
    setSelected(null);
    setHover(null);
    setPlaying(false);
    previousStepRef.current = null;
  }, [artifactId]);

  // devicePixelRatio 夹到 [1, 2]：3x 屏再翻倍只是把合成开销翻 9 倍
  useEffect(() => {
    Konva.pixelRatio = clampPixelRatio(
      typeof window === "undefined" ? 1 : window.devicePixelRatio,
    );
  }, []);

  const transform: StageTransform = useMemo(() => {
    // zoom > 1 时缩小「逻辑视口」，等价于把画布放大（超出部分裁切，符合「放大看细节」的预期）
    const safeZoom = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
    return computeStageTransform({ width: size.width / safeZoom, height: size.height / safeZoom });
  }, [size.width, size.height, zoom]);

  // ------------------------------------------------------------------ //
  // GSAP 补间
  // ------------------------------------------------------------------ //
  useEffect(() => {
    if (currentStep === undefined) {
      return;
    }
    const previous = previousStepRef.current;
    const transitions = computeStepTransition(
      previous === null ? null : previous.entities,
      currentStep.entities,
      transform,
    );
    const durationSeconds =
      previous === null
        ? DURATION_MS.normal / 1000
        : tweenDuration(previous.step_index, currentStep.step_index) / 1000;

    for (const transition of transitions) {
      const node = groupRefs.current.get(transition.entity.id);
      if (node === undefined) {
        continue;
      }
      gsap.killTweensOf(node);
      // 先落到起点，再补间到终点（React 已经把 x/y 设成终点值，这里覆盖不会被回写）
      node.x(transition.from.x);
      node.y(transition.from.y);
      node.opacity(transition.fromOpacity);
      gsap.to(node, {
        x: transition.to.x,
        y: transition.to.y,
        opacity: transition.toOpacity,
        duration: durationSeconds,
        ease: EASING.gsapStandard,
        onUpdate: () => {
          entityLayerRef.current?.batchDraw();
        },
      });
    }

    previousStepRef.current = currentStep;
    entityLayerRef.current?.batchDraw();
  }, [currentStep, transform]);

  // 卸载时清掉所有补间，避免 GSAP 继续往已销毁的节点写值
  useEffect(() => {
    const refs = groupRefs.current;
    return () => {
      for (const node of refs.values()) {
        gsap.killTweensOf(node);
      }
      refs.clear();
    };
  }, []);

  // ------------------------------------------------------------------ //
  // 播放
  // ------------------------------------------------------------------ //
  useEffect(() => {
    if (!playing || total <= 1) {
      return;
    }
    const current = steps[safeIndex];
    const next = steps[safeIndex + 1];
    if (current === undefined || next === undefined) {
      setPlaying(false);
      return;
    }
    const wait = tweenDuration(current.step_index, next.step_index) + HOLD_MS;
    const timer = window.setTimeout(() => {
      setStepIndex(clampStepIndex(safeIndex + 1, total));
    }, wait);
    return () => {
      window.clearTimeout(timer);
    };
  }, [playing, safeIndex, steps, total]);

  /** rAF 合并：拖动时间轴时每帧最多提交一次 state，避免一次拖拽打出上百次重渲染 */
  const requestStep = useCallback(
    (index: number): void => {
      pendingIndexRef.current = index;
      if (rafRef.current !== null) {
        return;
      }
      rafRef.current = window.requestAnimationFrame(() => {
        rafRef.current = null;
        setStepIndex(clampStepIndex(pendingIndexRef.current, total));
      });
    },
    [total],
  );

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, []);

  // ←/→ 逐帧（输入框聚焦时不劫持）
  useEffect(() => {
    const handler = (event: KeyboardEvent): void => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
        return;
      }
      if (isTypingTarget(event.target) || event.isComposing) {
        return;
      }
      event.preventDefault();
      setPlaying(false);
      setStepIndex((value) => clampStepIndex(value + (event.key === "ArrowLeft" ? -1 : 1), total));
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
    };
  }, [total]);

  const registerNode = useCallback((id: string, node: Konva.Group | null): void => {
    if (node === null) {
      groupRefs.current.delete(id);
      return;
    }
    groupRefs.current.set(id, node);
  }, []);

  const anchoredEntityIndex = useMemo(() => {
    if (anchor === null) {
      return -1;
    }
    const match = /^\/steps\/(\d+)\/entities\/(\d+)$/.exec(anchor.node_path);
    if (match === null) {
      return -1;
    }
    const step = Number.parseInt(match[1] ?? "", 10);
    const entity = Number.parseInt(match[2] ?? "", 10);
    if (step !== safeIndex || !Number.isFinite(entity)) {
      return -1;
    }
    return entity;
  }, [anchor, safeIndex]);

  const handleEntityClick = useCallback(
    (entity: Entity2D, entityIndex: number): void => {
      setSelected(entityIndex);
      if (readOnly || currentStep === undefined) {
        return;
      }
      onAnchor(
        makeAnchorFromEntity({
          artifactId,
          stepIndex: safeIndex,
          entityIndex,
          entity,
          stepSourceRef: currentStep.source_ref,
          ...(fallback === undefined ? {} : { fallback }),
        }),
      );
    },
    [artifactId, currentStep, fallback, onAnchor, readOnly, safeIndex],
  );

  if (currentStep === undefined) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center">
        <p className="text-xs text-content-muted">这份动画没有任何关键帧。</p>
      </div>
    );
  }

  const narration = narrationOf(currentStep);
  const selectedEntity = selected === null ? undefined : currentStep.entities[selected];
  const stageWidth = Math.max(size.width, 0);
  const stageHeight = Math.max(size.height, 0);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center gap-2 px-3 pt-2">
          <span className="rounded-xs border border-palette-sky/50 bg-palette-sky/15 px-1.5 py-px text-xxs text-palette-sky">
            2D 推演
          </span>
          <span className="min-w-0 truncate text-xs text-content-secondary" title={spec.title}>
            {spec.title}
          </span>
          <span className="ml-auto font-mono text-xxs text-content-muted">
            第 {safeIndex + 1} / {total} 帧
          </span>
          {readOnly ? (
            <span className="rounded-xs border border-palette-amber/60 bg-palette-amber/15 px-1.5 py-px text-xxs text-palette-amber">
              只读
            </span>
          ) : null}
        </div>

        <div ref={attachSize} className="relative min-h-0 flex-1">
          {transform.tooSmall ? (
            <p className="absolute left-1/2 top-2 z-10 -translate-x-1/2 rounded-xs bg-surface-overlay px-2 py-1 text-xxs text-content-muted shadow-card">
              容器过小，元素可能看不清，请拉宽画布或使用全屏
            </p>
          ) : null}

          {stageWidth > 0 && stageHeight > 0 ? (
            <Stage
              width={stageWidth}
              height={stageHeight}
              onMouseLeave={() => {
                setHover(null);
              }}
            >
              {/* 1. 背景层：网格 + 逻辑画布边界，永不重画 */}
              <Layer listening={false}>
                <Rect
                  x={transform.offsetX}
                  y={transform.offsetY}
                  width={CANVAS_WIDTH * transform.scale}
                  height={CANVAS_HEIGHT * transform.scale}
                  // 比页面底色亮一档 + 一道描边：否则「画布边界在哪」是看不见的，
                  // 学生会以为元素飘在页面背景上
                  fill="#111A2B"
                  stroke="#2F3F5C"
                  strokeWidth={1}
                  cornerRadius={10}
                />
                {Array.from({ length: 9 }, (_unused, index) => index + 1).map((step) => (
                  <Line
                    key={`grid-x-${step}`}
                    points={[
                      transform.offsetX + (CANVAS_WIDTH * transform.scale * step) / 10,
                      transform.offsetY,
                      transform.offsetX + (CANVAS_WIDTH * transform.scale * step) / 10,
                      transform.offsetY + CANVAS_HEIGHT * transform.scale,
                    ]}
                    stroke="#1F2B41"
                    strokeWidth={1}
                  />
                ))}
                {Array.from({ length: 5 }, (_unused, index) => index + 1).map((step) => (
                  <Line
                    key={`grid-y-${step}`}
                    points={[
                      transform.offsetX,
                      transform.offsetY + (CANVAS_HEIGHT * transform.scale * step) / 6,
                      transform.offsetX + CANVAS_WIDTH * transform.scale,
                      transform.offsetY + (CANVAS_HEIGHT * transform.scale * step) / 6,
                    ]}
                    stroke="#1F2B41"
                    strokeWidth={1}
                  />
                ))}
              </Layer>

              {/* 2. 元素层：补间期间每帧重画 */}
              <Layer ref={entityLayerRef}>
                {currentStep.entities.map((entity, entityIndex) => {
                  const box = entityBox(entity, transform);
                  const isActive =
                    selected === entityIndex || anchoredEntityIndex === entityIndex;
                  return (
                    <Group
                      key={`${entity.id}-${entityIndex}`}
                      ref={(node) => {
                        registerNode(entity.id, node);
                      }}
                      x={box.x}
                      y={box.y}
                      opacity={1}
                      onClick={() => {
                        handleEntityClick(entity, entityIndex);
                      }}
                      onTap={() => {
                        handleEntityClick(entity, entityIndex);
                      }}
                      onMouseEnter={(event) => {
                        const pointer = event.target
                          .getStage()
                          ?.getPointerPosition();
                        setHover({
                          entity,
                          x: pointer?.x ?? box.x,
                          y: pointer?.y ?? box.y,
                        });
                      }}
                      onMouseMove={(event) => {
                        const pointer = event.target
                          .getStage()
                          ?.getPointerPosition();
                        setHover({
                          entity,
                          x: pointer?.x ?? box.x,
                          y: pointer?.y ?? box.y,
                        });
                      }}
                      onMouseLeave={() => {
                        setHover(null);
                      }}
                      hitFunc={(context: Konva.Context, shape: Konva.Shape) => {
                        // 命中区 = 整个包围盒（arrow / zone 的图形是空心的，
                        // 用图形自身做命中会让中间那片区域点不到）
                        context.beginPath();
                        context.rect(0, 0, box.width, box.height);
                        context.closePath();
                        context.fillStrokeShape(shape);
                      }}
                    >
                      <EntityShape
                        entity={entity}
                        width={box.width}
                        height={box.height}
                        scale={transform.scale}
                        active={isActive}
                      />
                    </Group>
                  );
                })}
              </Layer>

              {/* 3. 交互层：选中描边 + 锚点指示，不参与命中测试 */}
              <Layer listening={false}>
                {currentStep.entities.map((entity, entityIndex) => {
                  const isActive = selected === entityIndex || anchoredEntityIndex === entityIndex;
                  if (!isActive) {
                    return null;
                  }
                  const box = entityBox(entity, transform);
                  return (
                    <Rect
                      key={`highlight-${entity.id}-${entityIndex}`}
                      x={box.x - 6}
                      y={box.y - 6}
                      width={box.width + 12}
                      height={box.height + 12}
                      stroke="#F59E0B"
                      strokeWidth={2}
                      cornerRadius={12}
                      dash={[8, 6]}
                    />
                  );
                })}
              </Layer>
            </Stage>
          ) : null}

          {/* hover 页码（HTML 覆盖层：文字渲染比 Konva Text 更清晰，也不占 canvas 绘制预算） */}
          {hover === null ? null : (
            <div
              className="pointer-events-none absolute z-10 rounded-xs border border-line-strong bg-surface-overlay px-1.5 py-0.5 font-mono text-xxs text-content-secondary shadow-card"
              style={{ left: hover.x + 12, top: hover.y + 12 }}
            >
              {hover.entity.label} · {pageLabel(hover.entity)}
            </div>
          )}

          {/* 元素详情（点击后弹出，含 details 知识点解释） */}
          {selectedEntity === undefined ? null : (
            <div className="absolute bottom-2 right-2 z-10 w-64 rounded-md border border-line bg-surface-overlay p-2 shadow-floating">
              <div className="flex items-start gap-1">
                <p className="min-w-0 flex-1 truncate text-xs font-medium text-content-primary">
                  {selectedEntity.label}
                </p>
                <button
                  type="button"
                  onClick={() => {
                    setSelected(null);
                  }}
                  className="shrink-0 text-xxs text-content-muted hover:text-content-primary"
                  aria-label="关闭详情"
                >
                  ×
                </button>
              </div>
              <p className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap text-xxs leading-relaxed text-content-secondary">
                {selectedEntity.details.length > 0 ? selectedEntity.details : "（该元素没有补充解释）"}
              </p>
              <p className="mt-1 font-mono text-xxs text-content-muted">
                出处：{pageLabel(selectedEntity)}
              </p>
              {readOnly ? null : (
                <p className="mt-1 text-xxs text-palette-amber">
                  已带上这个元素的锚点，追问会只针对它
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 时间轴 */}
      <div className="shrink-0 border-t border-line bg-surface-base px-3 py-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => {
              setPlaying((value) => !value);
            }}
            disabled={total <= 1}
            className={cx(
              "rounded-sm border px-2 py-0.5 text-xs transition-colors",
              playing
                ? "border-palette-amber/60 bg-palette-amber/15 text-palette-amber"
                : "border-line text-content-secondary hover:border-accent/60",
              total <= 1 ? "opacity-30" : "",
            )}
            aria-label={playing ? "暂停" : "播放"}
          >
            {playing ? "⏸ 暂停" : "▶ 播放"}
          </button>

          <button
            type="button"
            onClick={() => {
              setPlaying(false);
              setStepIndex((value) => clampStepIndex(value - 1, total));
            }}
            disabled={safeIndex === 0}
            className="rounded-sm border border-line px-2 py-0.5 text-xs text-content-secondary transition-colors hover:border-accent/60 disabled:opacity-30"
            aria-label="上一帧"
          >
            ◀
          </button>
          <button
            type="button"
            onClick={() => {
              setPlaying(false);
              setStepIndex((value) => clampStepIndex(value + 1, total));
            }}
            disabled={safeIndex >= total - 1}
            className="rounded-sm border border-line px-2 py-0.5 text-xs text-content-secondary transition-colors hover:border-accent/60 disabled:opacity-30"
            aria-label="下一帧"
          >
            ▶
          </button>

          <input
            type="range"
            min={0}
            max={Math.max(total - 1, 0)}
            step={1}
            value={safeIndex}
            onChange={(event) => {
              setPlaying(false);
              requestStep(Number.parseInt(event.target.value, 10));
            }}
            className="h-1 flex-1 accent-palette-sky"
            aria-label="关键帧进度"
          />

          <span className="shrink-0 font-mono text-xxs text-content-muted">
            {safeIndex + 1}/{total}
          </span>
        </div>

        <div className="mt-1.5 flex items-start gap-2">
          <p className="min-w-0 flex-1 text-xs leading-relaxed text-content-secondary">
            {narration.narration}
          </p>
          {narration.sourceRef.length > 0 ? (
            <span className="shrink-0 rounded-xs border border-line px-1.5 py-px font-mono text-xxs text-content-muted">
              {narration.sourceRef}
            </span>
          ) : null}
        </div>

        {readOnly ? null : (
          <p className="mt-1 text-xxs text-content-muted">
            点击画面里的元素，可以只针对它追问；悬停可看页码出处。
          </p>
        )}
      </div>
    </div>
  );
}
