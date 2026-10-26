/**
 * 画布节点 → 锚点（`Anchor`）的统一构造入口。
 *
 * 这个模块存在的唯一理由：**锚点构造错了，追问的引用页码就会跑到别的章节**。
 * 三处细节都是踩过的坑（IMPL-NOTES §五 F3）：
 *
 * 1. `SlideNode` 只有 `source_ref` 字符串，`Entity2D` / `Node3D` 才有 `source_page` 数值。
 *    因此「页码」优先取数值字段，退化才去正则解析 `source_ref` 里的 `p.94`。
 * 2. `section_code` 一旦丢失，后端 `strict_section=bool(anchor.section_code)` 会退化成
 *    全局检索，学生点了 p.76 却看到 p.70 的引用（E3）。所以这里**绝不下发 `null`**：
 *    真的什么都不知道时用空串 `""` —— 它仍是字符串（满足「不为 null」），
 *    而 Python 侧 `bool("")` 为 `False`，后端会正确地退化为全局检索，
 *    而不是把检索锁死在一个编造的章节号上。
 * 3. `node_path` 必须与后端 `NODE_PATH_RE` 同形，否则直接 400 `PATCH_PATH_INVALID`：
 *    幻灯片 `/slides/{i}`、2D 元素 `/steps/{i}/entities/{j}`、3D 节点 `/nodes/{i}`。
 */

import { parseSourceRef } from "@/components/chat/pageRefs";
import { createAnchor } from "@/types/patch";
import type { Entity2D, Node3D, SlideNode } from "@/types/artifact";
import type { MessageOut } from "@/types/api";
import type { Anchor } from "@/types/patch";

/**
 * 未知章节的哨兵值。
 *
 * 刻意用空串而不是 `"1"` 之类的假章节号：假章节号会让后端把检索锁进一个**错误的小节**，
 * 返回一堆看似相关、实则错位的引用 —— 比「不锁定」危险得多。
 */
export const UNKNOWN_SECTION_CODE = "";

/** 兜底上下文：最近一次检索给出的范围（页面构造锚点时用它补齐 `section_code`） */
export interface AnchorFallback {
  readonly section_code: string | null;
  readonly page: number | null;
}

export const NO_ANCHOR_FALLBACK: AnchorFallback = { section_code: null, page: null };

function firstNonEmpty(values: ReadonlyArray<string | null | undefined>): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }
  return null;
}

function firstValidPage(values: ReadonlyArray<number | null | undefined>): number | null {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value) && value >= 1) {
      return Math.trunc(value);
    }
  }
  return null;
}

/**
 * 章节号解析链：节点自身 → 兜底上下文 → 未知哨兵。
 * 返回**永不为 null** 的字符串（见文件头第 2 条）。
 */
export function resolveSectionCode(
  candidates: ReadonlyArray<string | null | undefined>,
  fallback: AnchorFallback,
): string {
  const hit = firstNonEmpty([...candidates, fallback.section_code]);
  return hit ?? UNKNOWN_SECTION_CODE;
}

/** 页码解析链：节点自身 → 兜底上下文 → 1（后端 `Anchor.page` 允许 null，但 1 更有用） */
export function resolvePage(
  candidates: ReadonlyArray<number | null | undefined>,
  fallback: AnchorFallback,
): number {
  const hit = firstValidPage([...candidates, fallback.page]);
  return hit ?? 1;
}

export interface SlideAnchorInput {
  readonly artifactId: string;
  readonly slideIndex: number;
  readonly slide: SlideNode;
  readonly fallback?: AnchorFallback;
}

/** 幻灯片锚点：`node_path = /slides/{i}` */
export function makeAnchorFromSlide(input: SlideAnchorInput): Anchor {
  const fallback = input.fallback ?? NO_ANCHOR_FALLBACK;
  const parsed = parseSourceRef(input.slide.source_ref);
  const sectionCode = resolveSectionCode([parsed.section_code], fallback);
  const page = resolvePage([parsed.page], fallback);
  return createAnchor({
    artifact_id: input.artifactId,
    node_path: `/slides/${input.slideIndex}`,
    node_label: `第 ${input.slideIndex + 1} 页 · ${input.slide.title}`,
    page,
    section_code: sectionCode,
  });
}

export interface EntityAnchorInput {
  readonly artifactId: string;
  readonly stepIndex: number;
  readonly entityIndex: number;
  readonly entity: Entity2D;
  /** 该关键帧的 `source_ref`（`Entity2D` 没有该字段，只能从所属 step 借） */
  readonly stepSourceRef?: string;
  readonly fallback?: AnchorFallback;
}

/** 2D 元素锚点：`node_path = /steps/{stepIndex}/entities/{entityIndex}` */
export function makeAnchorFromEntity(input: EntityAnchorInput): Anchor {
  const fallback = input.fallback ?? NO_ANCHOR_FALLBACK;
  const parsed =
    input.stepSourceRef === undefined
      ? { section_code: null, page: null }
      : parseSourceRef(input.stepSourceRef);
  const sectionCode = resolveSectionCode([parsed.section_code], fallback);
  const page = resolvePage([input.entity.source_page, parsed.page], fallback);
  return createAnchor({
    artifact_id: input.artifactId,
    node_path: `/steps/${input.stepIndex}/entities/${input.entityIndex}`,
    node_label: `${input.entity.label}（p.${page}）`,
    page,
    section_code: sectionCode,
  });
}

export interface Node3DAnchorInput {
  readonly artifactId: string;
  readonly nodeIndex: number;
  readonly node: Node3D;
  readonly fallback?: AnchorFallback;
}

/** 3D 节点锚点：`node_path = /nodes/{i}` */
export function makeAnchorFromNode3D(input: Node3DAnchorInput): Anchor {
  const fallback = input.fallback ?? NO_ANCHOR_FALLBACK;
  const sectionCode = resolveSectionCode([], fallback);
  const page = resolvePage([input.node.source_page], fallback);
  return createAnchor({
    artifact_id: input.artifactId,
    node_path: `/nodes/${input.nodeIndex}`,
    node_label: `${input.node.label}（p.${page}）`,
    page,
    section_code: sectionCode,
  });
}

/**
 * 从会话历史里推导兜底上下文。
 *
 * 取**最后一条带引用的 assistant 消息**：它的 `citations` 来自同一轮检索
 * （F2 已保证按页码升序），所以 `citations[0]` 就是该小节里页码最小的一处，
 * 与「该次 retrieval 首个 chunk 的 page_start」在锚定小节内等价。
 *
 * 为什么不用 `retrieval` 事件本身：那份数据在 `useChatStream` 的会话内 state 里，
 * 后端不落库，刷新即丢；而 `citations` 是落库的，刷新恢复后依然可用。
 */
export function deriveAnchorFallback(messages: readonly MessageOut[]): AnchorFallback {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message === undefined || message.role !== "assistant") {
      continue;
    }
    const citation = message.citations[0];
    if (citation === undefined) {
      continue;
    }
    return {
      section_code: citation.section_code.length > 0 ? citation.section_code : null,
      page: citation.page_start >= 1 ? citation.page_start : null,
    };
  }
  return NO_ANCHOR_FALLBACK;
}
