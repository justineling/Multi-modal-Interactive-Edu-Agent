/**
 * 导出 PDF 的守卫逻辑测试。
 *
 * 只测「不依赖浏览器」的部分：能力判定 + 无 DOM 环境下的失败路径。
 * 真正的分页效果（页数、页面尺寸、是否横向）靠真实浏览器打印验证，
 * 单测环境（node）没有排版引擎，断言像素没有意义。
 */

import { describe, expect, it } from "vitest";

import {
  ExportPdfError,
  PRINT_ROOT_ID,
  canExportPdf,
  exportPdf,
  isPrintSupported,
} from "@/lib/exportPdf";

describe("canExportPdf", () => {
  it("只有 ppt 支持导出", () => {
    expect(canExportPdf("ppt")).toBe(true);
  });

  it("2D / 3D 是实时渲染画布，打印只会得到空白，因此不支持", () => {
    expect(canExportPdf("anim_2d")).toBe(false);
    expect(canExportPdf("scene_3d")).toBe(false);
  });
});

describe("isPrintSupported", () => {
  it("node 环境（无 window）判定为不支持", () => {
    expect(isPrintSupported()).toBe(false);
  });
});

describe("exportPdf", () => {
  it("打印根 id 与渲染层约定一致", () => {
    // 渲染层（SlideRenderer）与导出层（exportPdf）靠这个 id 对接，改名必须同步
    expect(PRINT_ROOT_ID).toBe("print-root");
  });

  it("无 DOM 环境时抛可读错误而不是 TypeError", async () => {
    await expect(exportPdf()).rejects.toBeInstanceOf(ExportPdfError);
    await expect(exportPdf()).rejects.toThrow(/不支持/);
  });
});
