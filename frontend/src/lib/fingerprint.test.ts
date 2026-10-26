/**
 * `lib/fingerprint.ts` 自验。
 *
 * 三条断言对应 P0-10② 的验收方式：
 * 1. 同一对象两次指纹相同（确定性）
 * 2. 键序不同但内容相同 → 指纹相同（规范化生效）
 * 3. 定点改一个节点 → 只有该路径进 changed_paths，其余节点指纹不变
 */

import { describe, expect, it } from "vitest";

import {
  canonicalize,
  changedPointerPaths,
  diffFingerprints,
  findUnexpectedChanges,
  fingerprint,
  sha256Hex,
} from "@/lib/fingerprint";
import { setAtPath } from "@/lib/jsonPointer";
import type { SlideDeckSpec, SlideNode } from "@/types/artifact";

function makeSlide(nodeId: string, title: string, bullets: string[]): SlideNode {
  return {
    node_id: nodeId,
    layout: "bullet_points",
    title,
    subtitle: null,
    bullets,
    comparison_left: null,
    comparison_right: null,
    timeline_items: null,
    quiz_questions: null,
    speaker_notes: `${title} 的口语化讲解`,
    source_ref: "Form 4 Sejarah, Bab 4.1, p.94",
  };
}

function makeSpec(): SlideDeckSpec {
  return {
    type: "ppt",
    topic: "马来亚联邦",
    slides: [
      makeSlide("slide-1", "封面", ["1948 年成立"]),
      makeSlide("slide-2", "时间轴", ["1945", "1946", "1948"]),
      makeSlide("slide-3", "对比", ["联邦", "联合邦"]),
    ],
  };
}

describe("canonicalize", () => {
  it("键序不同得到同一字符串", () => {
    expect(canonicalize({ b: 1, a: 2 })).toBe(canonicalize({ a: 2, b: 1 }));
  });

  it("紧凑分隔符（无空格）", () => {
    expect(canonicalize({ a: 1, b: [2, 3] })).toBe('{"a":1,"b":[2,3]}');
  });

  it("中文原样输出，不转 \\uXXXX", () => {
    expect(canonicalize({ t: "马来亚" })).toBe('{"t":"马来亚"}');
  });

  it("undefined 值跳过（与 JSON.stringify 一致）", () => {
    expect(canonicalize({ a: 1, b: undefined })).toBe('{"a":1}');
  });

  it("非有限数字按 null", () => {
    expect(canonicalize(Number.NaN)).toBe("null");
    expect(canonicalize(Number.POSITIVE_INFINITY)).toBe("null");
  });
});

describe("sha256Hex / fingerprint", () => {
  it("sha256 与已知向量一致（空串）", async () => {
    expect(await sha256Hex("")).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
  });

  it("同一对象两次指纹相同", async () => {
    const spec = makeSpec();
    expect(await fingerprint(spec)).toBe(await fingerprint(spec));
  });

  it("键序不同但内容相同 → 指纹相同", async () => {
    const a = { x: 1, y: { p: 1, q: 2 } };
    const b = { y: { q: 2, p: 1 }, x: 1 };
    expect(await fingerprint(a)).toBe(await fingerprint(b));
  });

  it("内容变化 → 指纹变化", async () => {
    const spec = makeSpec();
    const changed = setAtPath(spec, "/slides/1/title", "时间轴（重做）");
    expect(await fingerprint(spec)).not.toBe(await fingerprint(changed));
  });
});

describe("changedPointerPaths", () => {
  it("只报最小改动集（不含祖先）", async () => {
    const before = makeSpec();
    const after = setAtPath(before, "/slides/1/title", "时间轴（重做）");
    expect(await changedPointerPaths(before, after)).toEqual(["/slides/1/title"]);
  });

  it("整节点替换（所有字段都变）报该节点路径，而不是每个叶子字段", async () => {
    const before = makeSpec();
    const replacement: SlideNode = {
      node_id: "slide-2-redo",
      layout: "quiz",
      title: "整页重做",
      subtitle: "副标题",
      bullets: ["x"],
      comparison_left: ["左"],
      comparison_right: ["右"],
      timeline_items: [{ year: "1948", label: "成立", detail: "", source_page: null }],
      quiz_questions: [
        { question: "哪一年成立？", options: ["1946", "1948"], answer_index: 1, explanation: "" },
      ],
      speaker_notes: "重做后的讲解",
      source_ref: "Extended Knowledge",
    };
    const after = setAtPath(before, "/slides/1", replacement);
    expect(await changedPointerPaths(before, after)).toEqual(["/slides/1"]);
  });

  it("无改动时为空数组", async () => {
    const before = makeSpec();
    expect(await changedPointerPaths(before, before)).toEqual([]);
  });
});

describe("findUnexpectedChanges（P0-10② 的核心断言）", () => {
  it("改动落在允许路径内 → 无意外变化", async () => {
    const before = makeSpec();
    const after = setAtPath(before, "/slides/1/title", "时间轴（重做）");
    expect(await findUnexpectedChanges(before, after, ["/slides/1"])).toEqual([]);
  });

  it("改动越界 → 报出意外路径", async () => {
    const before = makeSpec();
    const touchedOther = setAtPath(before, "/slides/2/title", "偷偷改了第三页");
    const after = setAtPath(touchedOther, "/slides/1/title", "时间轴（重做）");
    expect(await findUnexpectedChanges(before, after, ["/slides/1"])).toEqual([
      "/slides/2/title",
    ]);
  });

  it("删除节点也算被允许的改动", async () => {
    const before = makeSpec();
    const after = {
      ...before,
      slides: before.slides.filter((slide) => slide.node_id !== "slide-3"),
    };
    expect(await findUnexpectedChanges(before, after, ["/slides/2"])).toEqual([]);
  });
});

describe("diffFingerprints", () => {
  it("区分 added / removed / changed", async () => {
    const before = { a: 1, b: 2, c: 3 };
    const after = { a: 1, b: 20, d: 4 };
    const diff = await diffFingerprints(before, after);
    expect(diff.changed).toContain("/b");
    expect(diff.added).toContain("/d");
    expect(diff.removed).toContain("/c");
  });
});
