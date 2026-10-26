/**
 * `useArtifactPatch` 单测 —— 重点验证 **`update_node` 必须字段级 merge**（IMPL-NOTES §五 F1）。
 *
 * 这一条是「写错必崩」级别：后端锚点追问只下发 `{bullets, speaker_notes}`，
 * 其余 9 个字段一个都不带。整体替换会让 `layout` 变 `undefined`，
 * `LAYOUT_REGISTRY[undefined]` 拿不到布局 → 渲染器当场崩溃。
 * 因此下面第一组断言是**证据**，不是覆盖率。
 *
 * 第二组验证「允许路径传父列表层」（§四.4）：insert/delete 会让兄弟下标平移，
 * 若只声明被插入的那个下标，指纹自检会把兄弟平移判成「改了不该改的地方」。
 */

import { describe, expect, it } from "vitest";

import { findUnexpectedChanges } from "@/lib/fingerprint";
import {
  MAX_OPS_PER_REQUEST,
  createAnchor,
} from "@/types/patch";
import type { PatchOp } from "@/types/patch";
import { PatchApplyError, applyPatchOps, runArtifactPatch, selfCheckPatch } from "@/hooks/useArtifactPatch";
import type { ArtifactSpec, Keyframe2DSpec, SlideDeckSpec, SlideNode } from "@/types/artifact";

function makeSlide(index: number, overrides: Partial<SlideNode> = {}): SlideNode {
  return {
    node_id: `slide-${index}`,
    layout: "bullet_points",
    title: `第 ${index} 页标题`,
    subtitle: `Bab 4.${index}`,
    bullets: [`要点 A${index}`, `要点 B${index}`],
    comparison_left: null,
    comparison_right: null,
    timeline_items: null,
    quiz_questions: null,
    speaker_notes: `第 ${index} 页讲解`,
    source_ref: `Form 4 Sejarah, Bab 4.${index}, p.9${index}`,
    ...overrides,
  };
}

function makeDeck(): SlideDeckSpec {
  return {
    type: "ppt",
    topic: "马来亚联邦",
    slides: [makeSlide(1), makeSlide(2), makeSlide(3)],
  };
}

function updateOp(nodePath: string, payload: Record<string, unknown>): PatchOp {
  return { op: "update_node", node_path: nodePath, payload, reason: "锚点追问" };
}

/**
 * 一页幻灯片的「线上 JSON 形状」。
 *
 * 显式逐字段列出而不是直接传 `SlideNode`：`PatchOp.payload` 的类型是
 * `Record<string, unknown>`（局部 dict），接口类型没有索引签名，直接传会过不了类型检查。
 * 这也顺带证明了「payload 是纯 JSON dict」这一契约。
 */
function slidePayload(index: number, nodeId = `slide-${index}`): Record<string, unknown> {
  const slide = makeSlide(index, { node_id: nodeId });
  return {
    node_id: slide.node_id,
    layout: slide.layout,
    title: slide.title,
    subtitle: slide.subtitle,
    bullets: slide.bullets,
    comparison_left: slide.comparison_left,
    comparison_right: slide.comparison_right,
    timeline_items: slide.timeline_items,
    quiz_questions: slide.quiz_questions,
    speaker_notes: slide.speaker_notes,
    source_ref: slide.source_ref,
  };
}

describe("applyPatchOps — update_node 必须字段级 merge（F1 硬约束）", () => {
  it("只带 {bullets, speaker_notes} 时，node_id / layout / title 全部保留", () => {
    const spec = makeDeck();
    const result = applyPatchOps(spec, [
      updateOp("/slides/1", {
        bullets: ["新的要点 1", "新的要点 2", "新的要点 3"],
        speaker_notes: "补充后的讲解",
      }),
    ]);

    const deck = result.spec as SlideDeckSpec;
    const slide = deck.slides[1];

    // ★ 三个「整体替换会丢掉」的关键字段
    expect(slide?.node_id).toBe("slide-2");
    expect(slide?.layout).toBe("bullet_points");
    expect(slide?.title).toBe("第 2 页标题");
    // 其余未下发字段也一个都不能少
    expect(slide?.subtitle).toBe("Bab 4.2");
    expect(slide?.source_ref).toBe("Form 4 Sejarah, Bab 4.2, p.92");
    expect(slide?.comparison_left).toBeNull();

    // 下发的字段被覆盖
    expect(slide?.bullets).toEqual(["新的要点 1", "新的要点 2", "新的要点 3"]);
    expect(slide?.speaker_notes).toBe("补充后的讲解");
  });

  it("不修改入参（乐观更新 + 回滚依赖这一点）", () => {
    const spec = makeDeck();
    const before = structuredClone(spec);
    applyPatchOps(spec, [updateOp("/slides/1", { title: "改过的标题" })]);
    expect(spec).toEqual(before);
    expect(spec.slides[1]?.title).toBe("第 2 页标题");
  });

  it("只改一个字段时其余兄弟页面字节级不变", () => {
    const spec = makeDeck();
    const result = applyPatchOps(spec, [updateOp("/slides/1", { title: "新标题" })]);
    const deck = result.spec as SlideDeckSpec;
    expect(deck.slides[0]).toEqual(spec.slides[0]);
    expect(deck.slides[2]).toEqual(spec.slides[2]);
  });

  it("2D 实体级 update 同样 merge（不会把 id/shape/x/y 冲掉）", () => {
    const spec: Keyframe2DSpec = {
      type: "anim_2d",
      title: "关税的影响",
      steps: [
        {
          step_index: 0,
          narration: "第一帧",
          source_ref: "Bab 4.1, p.94",
          entities: [
            {
              id: "entity-1",
              shape: "rect",
              label: "关税",
              x: 300,
              y: 200,
              color: "#2563EB",
              details: "提高进口成本",
              source_page: 94,
            },
          ],
        },
      ],
    };
    const result = applyPatchOps(spec, [
      updateOp("/steps/0/entities/0", { label: "进口关税" }),
    ]);
    const entity = (result.spec as Keyframe2DSpec).steps[0]?.entities[0];
    expect(entity?.id).toBe("entity-1");
    expect(entity?.shape).toBe("rect");
    expect(entity?.x).toBe(300);
    expect(entity?.label).toBe("进口关税");
  });

  it("目标不是对象节点时明确报错，而不是静默写坏 AST", () => {
    const spec: ArtifactSpec = makeDeck();
    // 下标越界 → 路径解析不到对象节点（真实场景：历史版本结构不一致）
    expect(() =>
      applyPatchOps(spec, [
        { op: "update_node", node_path: "/slides/9", payload: { title: "x" }, reason: "" },
      ]),
    ).toThrow(PatchApplyError);
  });
});

describe("applyPatchOps — insert / delete / replace", () => {
  it("insert_node 按下标插入，并把允许路径声明到父列表", () => {
    const spec = makeDeck();
    const result = applyPatchOps(spec, [
      {
        op: "insert_node",
        node_path: "/slides/1",
        payload: slidePayload(9, "slide-new"),
        reason: "补充一页",
      },
    ]);
    const deck = result.spec as SlideDeckSpec;
    expect(deck.slides).toHaveLength(4);
    expect(deck.slides[1]?.node_id).toBe("slide-new");
    expect(deck.slides[1]?.title).toBe("第 9 页标题");
    // ★ 父列表层，而不是 /slides/1
    expect(result.changedPaths).toEqual(["/slides"]);
  });

  it("delete_node 移除节点并把允许路径声明到父列表", () => {
    const spec = makeDeck();
    const result = applyPatchOps(spec, [
      { op: "delete_node", node_path: "/slides/1", payload: null, reason: "删掉重复页" },
    ]);
    const deck = result.spec as SlideDeckSpec;
    expect(deck.slides).toHaveLength(2);
    expect(deck.slides.map((slide) => slide.node_id)).toEqual(["slide-1", "slide-3"]);
    expect(result.changedPaths).toEqual(["/slides"]);
  });

  it("replace_subtree 是整体替换（与 update 的 merge 语义不同）", () => {
    const spec = makeDeck();
    const replacement = {
      node_id: "slide-2",
      layout: "title_hero",
      title: "整页重做",
      speaker_notes: "重做后的讲解",
      source_ref: "Bab 4.2, p.92",
    };
    const result = applyPatchOps(spec, [
      {
        op: "replace_subtree",
        node_path: "/slides/1",
        payload: replacement,
        reason: "整页重做",
      },
    ]);
    const slide = (result.spec as SlideDeckSpec).slides[1];
    expect(slide?.layout).toBe("title_hero");
    // 整体替换：payload 里没有的字段不会从旧节点带过来
    const hasBullets = slide !== undefined && "bullets" in slide;
    expect(hasBullets).toBe(false);
    expect(result.changedPaths).toEqual(["/slides/1"]);
  });
});

describe("applyPatchOps — focus_camera 不写 spec（§四.5）", () => {
  it("只产出 focusPreset 旁路信号，spec 引用不变", () => {
    const spec = makeDeck();
    const result = applyPatchOps(spec, [
      {
        op: "focus_camera",
        node_path: "/slides/0",
        payload: { preset_name: "鸟瞰" },
        reason: "转到鸟瞰机位",
      },
    ]);
    expect(result.focusPreset).toBe("鸟瞰");
    expect(result.spec).toBe(spec);
    expect(result.changedPaths).toEqual([]);
    expect(result.appliedOps).toBe(0);
  });

  it("payload 里没有 preset_name 时不产生信号，也不报错", () => {
    const spec = makeDeck();
    const result = applyPatchOps(spec, [
      { op: "focus_camera", node_path: "/slides/0", payload: {}, reason: "" },
    ]);
    expect(result.focusPreset).toBeNull();
  });
});

describe("applyPatchOps — 参数校验", () => {
  it("非法路径直接拒绝（不发到后端）", () => {
    expect(() =>
      applyPatchOps(makeDeck(), [updateOp("/slides/0/title", { x: 1 })]),
    ).toThrow(/锚点路径非法/);
  });

  it("update_node 缺 payload 时拒绝", () => {
    expect(() =>
      applyPatchOps(makeDeck(), [
        { op: "update_node", node_path: "/slides/0", payload: null, reason: "" },
      ]),
    ).toThrow(/缺少对象形式的 payload/);
  });

  it("单批超过上限时拒绝", () => {
    const ops: PatchOp[] = Array.from({ length: MAX_OPS_PER_REQUEST + 1 }, () =>
      updateOp("/slides/0", { title: "x" }),
    );
    expect(() => applyPatchOps(makeDeck(), ops)).toThrow(/单批最多/);
  });

  it("空 op 列表返回同一引用（无变化不做深拷贝）", () => {
    const spec = makeDeck();
    const result = applyPatchOps(spec, []);
    expect(result.spec).toBe(spec);
    expect(result.changedPaths).toEqual([]);
  });
});

describe("selfCheckPatch — 指纹自检", () => {
  it("update_node 后只有目标页变化，自检通过", async () => {
    const before = makeDeck();
    const applied = applyPatchOps(before, [
      updateOp("/slides/1", { bullets: ["只有这一页变了"], speaker_notes: "新讲解" }),
    ]);
    const check = await selfCheckPatch(before, applied.spec, applied.changedPaths);
    expect(check.unexpected).toEqual([]);
    expect(check.passed).toBe(true);
  });

  it("insert_node 用父列表路径声明时通过（兄弟下标平移属预期）", async () => {
    const before = makeDeck();
    const applied = applyPatchOps(before, [
      {
        op: "insert_node",
        node_path: "/slides/1",
        payload: slidePayload(9, "slide-new"),
        reason: "",
      },
    ]);
    const check = await selfCheckPatch(before, applied.spec, applied.changedPaths);
    expect(check.passed).toBe(true);
  });

  it("反例：把 insert 的允许路径写成元素层时自检不通过（证明父列表规则必要）", async () => {
    const before = makeDeck();
    const applied = applyPatchOps(before, [
      {
        op: "insert_node",
        node_path: "/slides/1",
        payload: slidePayload(9, "slide-new"),
        reason: "",
      },
    ]);
    const unexpected = await findUnexpectedChanges(before, applied.spec, ["/slides/1"]);
    expect(unexpected.length).toBeGreaterThan(0);
    expect(unexpected.some((path) => path.startsWith("/slides/2"))).toBe(true);
  });

  it("update_node 时若声明了错误的页，其余页的改动会被抓出来", async () => {
    const before = makeDeck();
    const applied = applyPatchOps(before, [
      updateOp("/slides/1", { title: "改的是第 2 页" }),
    ]);
    // 故意声明成第 3 页
    const unexpected = await findUnexpectedChanges(before, applied.spec, ["/slides/2"]);
    expect(unexpected.length).toBeGreaterThan(0);
  });
});

describe("runArtifactPatch — 前置校验", () => {
  it("产物不存在时给出可读错误，且不发网络请求", async () => {
    const outcome = await runArtifactPatch({
      artifactId: "not-exist",
      baseVersion: 1,
      ops: [updateOp("/slides/0", { title: "x" })],
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.error).toContain("画布产物不存在");
    expect(outcome.spec).toBeNull();
  });

  it("非法 op 在本地就被拦下（不会打到后端）", async () => {
    const outcome = await runArtifactPatch({
      artifactId: "not-exist",
      baseVersion: 1,
      ops: [{ op: "update_node", node_path: "/bad/path", payload: {}, reason: "" }],
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.error).toContain("画布产物不存在");
  });
});

describe("锚点构造与 patch 路径一致", () => {
  it("createAnchor 生成的路径能被 patch 校验器接受", () => {
    const anchor = createAnchor({
      artifact_id: "a1",
      node_path: "/steps/0/entities/1",
      node_label: "关税",
      page: 94,
      section_code: "4.1",
    });
    const result = applyPatchOps(
      {
        type: "anim_2d",
        title: "t",
        steps: [
          {
            step_index: 0,
            narration: "",
            source_ref: "",
            entities: [
              {
                id: "e0",
                shape: "circle",
                label: "a",
                x: 1,
                y: 1,
                color: "#2563EB",
                details: "",
                source_page: null,
              },
              {
                id: "e1",
                shape: "rect",
                label: "b",
                x: 2,
                y: 2,
                color: "#0EA5E9",
                details: "",
                source_page: null,
              },
            ],
          },
        ],
      },
      [{ op: "update_node", node_path: anchor.node_path, payload: { label: "改过" }, reason: "" }],
    );
    const entity = (result.spec as Keyframe2DSpec).steps[0]?.entities[1];
    expect(entity?.id).toBe("e1");
    expect(entity?.label).toBe("改过");
  });
});
