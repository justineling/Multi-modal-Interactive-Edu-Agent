/**
 * `lib/jsonPointer.ts` 自验。
 *
 * 重点是 `isAncestorOrSelf` 的 `/` 边界：裸 `startsWith` 会把 `/slides/1`
 * 当成 `/slides/10` 的祖先（后端 `test_patch_engine.py` 有对应负例）。
 */

import { describe, expect, it } from "vitest";

import {
  JsonPointerError,
  getLastSegment,
  getParentPath,
  hasPointer,
  insertAtPath,
  isAncestorOrSelf,
  parsePointer,
  pruneNestedPaths,
  removeAtPath,
  resolvePointer,
  setAtPath,
  toPointer,
} from "@/lib/jsonPointer";

const SPEC = {
  type: "ppt",
  topic: "马来亚联邦",
  slides: [
    { node_id: "slide-1", title: "封面", bullets: ["a", "b"] },
    { node_id: "slide-2", title: "时间轴", bullets: [] },
  ],
};

describe("parsePointer / toPointer", () => {
  it("空串表示根", () => {
    expect(parsePointer("")).toEqual([]);
  });

  it("按 / 切段", () => {
    expect(parsePointer("/slides/2")).toEqual(["slides", "2"]);
  });

  it("转义往返：~1 → /、~0 → ~", () => {
    expect(parsePointer("/a~1b/c~0d")).toEqual(["a/b", "c~d"]);
    expect(toPointer(["a/b", "c~d"])).toBe("/a~1b/c~0d");
  });

  it("非 / 开头视为非法", () => {
    expect(() => parsePointer("slides/2")).toThrow(JsonPointerError);
  });
});

describe("resolvePointer / hasPointer", () => {
  it("能取到嵌套值", () => {
    expect(resolvePointer(SPEC, "/slides/1/title")).toBe("时间轴");
  });

  it("不存在返回 undefined", () => {
    expect(resolvePointer(SPEC, "/slides/9")).toBeUndefined();
    expect(resolvePointer(SPEC, "/slides/1/nope")).toBeUndefined();
  });

  it("数组下标必须是规范整数（拒绝 01 / -1）", () => {
    expect(resolvePointer(SPEC, "/slides/01")).toBeUndefined();
    expect(resolvePointer(SPEC, "/slides/-1")).toBeUndefined();
  });

  it("hasPointer 区分存在与不存在", () => {
    expect(hasPointer(SPEC, "/slides/1")).toBe(true);
    expect(hasPointer(SPEC, "/slides/5")).toBe(false);
  });
});

describe("setAtPath", () => {
  it("返回新对象，原对象不被修改", () => {
    const next = setAtPath(SPEC, "/slides/1/title", "对比表");
    expect(resolvePointer(next, "/slides/1/title")).toBe("对比表");
    expect(resolvePointer(SPEC, "/slides/1/title")).toBe("时间轴");
    expect(next).not.toBe(SPEC);
  });

  it("未触及的分支保持值相等（深拷贝语义）", () => {
    const next = setAtPath(SPEC, "/slides/1/title", "对比表");
    expect(resolvePointer(next, "/slides/0")).toEqual(resolvePointer(SPEC, "/slides/0"));
  });

  it("可以整节点替换", () => {
    const replacement = { node_id: "slide-2", title: "整页重做", bullets: ["x"] };
    const next = setAtPath(SPEC, "/slides/1", replacement);
    expect(resolvePointer(next, "/slides/1")).toEqual(replacement);
  });

  it("中间节点不是容器时抛错", () => {
    expect(() => setAtPath(SPEC, "/slides/0/title/deep", 1)).toThrow(JsonPointerError);
  });
});

describe("insertAtPath / removeAtPath", () => {
  it("插入到数组指定位置", () => {
    const next = insertAtPath(SPEC, "/slides/1", { node_id: "slide-x", title: "插队" });
    expect((resolvePointer(next, "/slides") as unknown[]).length).toBe(3);
    expect(resolvePointer(next, "/slides/1/title")).toBe("插队");
    expect(resolvePointer(SPEC, "/slides/1/title")).toBe("时间轴");
  });

  it("删除数组元素", () => {
    const next = removeAtPath(SPEC, "/slides/0");
    expect((resolvePointer(next, "/slides") as unknown[]).length).toBe(1);
    expect(resolvePointer(next, "/slides/0/title")).toBe("时间轴");
  });

  it("越界删除抛错", () => {
    expect(() => removeAtPath(SPEC, "/slides/9")).toThrow(JsonPointerError);
  });

  it("不允许删除根", () => {
    expect(() => removeAtPath(SPEC, "")).toThrow(JsonPointerError);
  });
});

describe("getParentPath / getLastSegment", () => {
  it("父路径", () => {
    expect(getParentPath("/slides/2/title")).toBe("/slides/2");
    expect(getParentPath("/slides/2")).toBe("/slides");
    expect(getParentPath("/slides")).toBe("");
  });

  it("末段", () => {
    expect(getLastSegment("/slides/2")).toBe("2");
    expect(getLastSegment("")).toBe("");
  });
});

describe("isAncestorOrSelf（/ 边界，最容易写错的地方）", () => {
  it("自身算祖先", () => {
    expect(isAncestorOrSelf("/slides/2", "/slides/2")).toBe(true);
  });

  it("真祖先", () => {
    expect(isAncestorOrSelf("/slides", "/slides/2/title")).toBe(true);
    expect(isAncestorOrSelf("/slides/2", "/slides/2/title")).toBe(true);
  });

  it("/slides/1 不是 /slides/10 的祖先（裸 startsWith 会判错）", () => {
    expect(isAncestorOrSelf("/slides/1", "/slides/10")).toBe(false);
    expect(isAncestorOrSelf("/slides/1", "/slides/1/entities/10")).toBe(true);
  });

  it("根是所有路径的祖先", () => {
    expect(isAncestorOrSelf("", "/slides/0")).toBe(true);
  });

  it("兄弟节点互不为祖先", () => {
    expect(isAncestorOrSelf("/slides/1", "/slides/2")).toBe(false);
  });
});

describe("pruneNestedPaths", () => {
  it("剔除被祖先覆盖的路径", () => {
    expect(pruneNestedPaths(["/slides/2/title", "/slides/2", "/nodes/0"])).toEqual([
      "/nodes/0",
      "/slides/2",
    ]);
  });

  it("去重", () => {
    expect(pruneNestedPaths(["/slides/2", "/slides/2"])).toEqual(["/slides/2"]);
  });
});
