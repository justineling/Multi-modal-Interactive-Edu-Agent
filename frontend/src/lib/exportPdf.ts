/**
 * PPT → PDF 导出（浏览器打印管线）。
 *
 * ## 为什么是 `window.print()` 而不是 `html2canvas` + `jsPDF`
 *
 * 1. **公式正确**：课件的公式是 KaTeX 的 SVG 字形。`html2canvas` 会把 DOM 重新画到
 *    canvas 上，SVG 字体经常丢字形或错位；交给浏览器自己的打印管线则一定正确。
 * 2. **矢量输出**：打印产出的是矢量 PDF，放大不糊；截图管线是位图，投屏会糊。
 * 3. **零依赖**：`html2canvas` + `jsPDF` 约 200 kB，且要为字体 / WebGL 单独调参。
 *
 * ## 代价（必须在 UI 上说清楚）
 *
 * 用户点按钮后会经过系统打印对话框，需要自己选「另存为 PDF」。这不是 bug，
 * 是浏览器安全模型决定的：网页无权直接把文件写进用户的下载目录。
 *
 * ## 与渲染层如何配合
 *
 * `SlideRenderer` 会把**全部页**渲染进一个 `id="print-root"` 的节点，并用
 * `createPortal` 挂到 `document.body` 下（屏上 `display: none`）。
 * 这样打印时**不需要**去解开画布那套 flex / 绝对定位 / `overflow: hidden` 的布局，
 * 只要在 `@media print` 里把 `#root` 摘掉、把 `print-root` 显示出来即可
 * （样式见 `src/index.css` 的打印块）。
 *
 * 本模块只负责「等资源就绪 → 调 print → 等打印结束」这段时序，不碰任何 DOM 结构。
 */

import type { ArtifactType } from "@/types/artifact";

/** 打印根节点的 DOM id（由 `SlideRenderer` 渲染，见模块注释）。 */
export const PRINT_ROOT_ID = "print-root";

/**
 * `afterprint` 的兜底等待时长（ms）。
 *
 * Chrome / Edge 的 `window.print()` 是**同步阻塞**的，返回时打印对话框已经关掉，
 * `afterprint` 也在阻塞期间触发过了，因此不会用到这个兜底。
 * 它只服务于「`print()` 立即返回、`afterprint` 稍后才来」的实现（Safari 等），
 * 目的是别让按钮永久卡在「导出中」。
 */
const PRINT_SETTLE_TIMEOUT_MS = 1500;

/**
 * 等待 `document.fonts.ready` 的上限（ms）。
 *
 * 不等字体就打印，PDF 里的中文会退回系统默认字体（字宽全变，版面错位）；
 * 但字体迟迟不就绪时也不能把按钮卡死，所以给一个上限。
 */
const FONT_READY_TIMEOUT_MS = 4000;

/** 单张图片加载的上限（ms）。当前课件没有位图，留给后续扩展。 */
const IMAGE_LOAD_TIMEOUT_MS = 3000;

/** 导出失败时抛出的错误；`message` 直接面向用户展示。 */
export class ExportPdfError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "ExportPdfError";
  }
}

/**
 * 该形态是否支持导出 PDF。
 *
 * 目前只有 `ppt` 走「固定 1280×720 版面 + 打印分页」这条路径；
 * 2D / 3D 是实时渲染的 canvas / WebGL，打印出来只会是一张当前帧的截图，
 * 甚至直接空白 —— 与其给一个骗人的按钮，不如显式禁用并说明原因。
 */
export function canExportPdf(artifactType: ArtifactType): boolean {
  return artifactType === "ppt";
}

/** 环境是否具备打印能力（测试环境 / 极老浏览器没有 `window.print`）。 */
export function isPrintSupported(): boolean {
  return typeof window !== "undefined" && typeof window.print === "function";
}

function delay(ms: number): Promise<void> {
  return new Promise<void>((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

/**
 * 等两帧。
 *
 * 打印前必须让浏览器完成一次 layout + style 计算，否则 `@media print` 的规则
 * 可能还没应用到新挂载的节点上（表现为「PDF 里少了几页」）。
 */
function waitForPaint(): Promise<void> {
  return new Promise<void>((resolve) => {
    if (typeof requestAnimationFrame !== "function") {
      resolve();
      return;
    }
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        resolve();
      });
    });
  });
}

/** 等字体就绪（带超时上限，永不 reject）。 */
async function waitForFonts(): Promise<void> {
  // 老浏览器可能没有 `document.fonts`，此时 `?? null` 兜底成「不需要等」
  const fonts: FontFaceSet | null = document.fonts ?? null;
  if (fonts === null) {
    return;
  }
  try {
    await Promise.race([fonts.ready, delay(FONT_READY_TIMEOUT_MS)]);
  } catch {
    // `FontFaceSet.ready` 按规范不会 reject；这里只是防御式兜底，不让导出整体失败。
  }
}

/** 等打印根里尚未加载完的图片（带超时上限，永不 reject）。 */
async function waitForImages(root: HTMLElement): Promise<void> {
  const images: HTMLImageElement[] = Array.from(root.querySelectorAll("img"));
  const pending = images.filter((image) => !image.complete);
  if (pending.length === 0) {
    return;
  }
  const settled = pending.map(
    (image) =>
      new Promise<void>((resolve) => {
        const done = (): void => {
          resolve();
        };
        image.addEventListener("load", done, { once: true });
        image.addEventListener("error", done, { once: true });
      }),
  );
  await Promise.race([Promise.all(settled), delay(IMAGE_LOAD_TIMEOUT_MS)]);
}

/**
 * 调用 `window.print()` 并等到打印真正结束。
 *
 * 监听 `afterprint` 而不是「`print()` 返回即结束」：
 * 同步阻塞的实现里两者等价，异步实现里只有事件才可靠。
 */
function runPrint(): Promise<void> {
  return new Promise<void>((resolve) => {
    let settled = false;
    const finish = (): void => {
      if (settled) {
        return;
      }
      settled = true;
      window.removeEventListener("afterprint", finish);
      resolve();
    };

    window.addEventListener("afterprint", finish);
    try {
      window.print();
    } catch {
      finish();
      return;
    }

    // 同步阻塞的实现到这里 `afterprint` 已经触发过，不必再挂定时器。
    if (!settled) {
      window.setTimeout(finish, PRINT_SETTLE_TIMEOUT_MS);
    }
  });
}

export interface ExportPdfOptions {
  /** 打印根节点；默认按 `PRINT_ROOT_ID` 在文档里查找。 */
  root?: HTMLElement | null;
}

/**
 * 把当前 PPT 的**全部页面**导出成 PDF（经由系统打印对话框）。
 *
 * 时序：字体就绪 → 图片就绪 → 两帧 → `window.print()` → 打印结束。
 * 任何一步失败都会抛出 `ExportPdfError`，`message` 可直接展示给用户。
 *
 * @throws {ExportPdfError} 打印根缺失 / 没有任何页面 / 浏览器不支持打印
 */
export async function exportPdf(options: ExportPdfOptions = {}): Promise<void> {
  if (typeof window === "undefined" || typeof document === "undefined") {
    throw new ExportPdfError("当前环境不支持打印导出");
  }
  if (!isPrintSupported()) {
    throw new ExportPdfError("当前浏览器不支持打印导出，请改用 Chrome 或 Edge");
  }

  const root = options.root ?? document.getElementById(PRINT_ROOT_ID);
  if (root === null) {
    throw new ExportPdfError("画布还没准备好，请稍后重试");
  }
  if (root.childElementCount === 0) {
    throw new ExportPdfError("这份课件没有任何页面，无法导出");
  }

  await waitForFonts();
  await waitForImages(root);
  await waitForPaint();
  await runPrint();
}
