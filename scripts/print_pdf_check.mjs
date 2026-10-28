/**
 * 「PPT 导出 PDF」回归检查（Chrome DevTools Protocol，零依赖）。
 *
 * 为什么必须用真实浏览器：`@page size` / `break-after: page` / `print-color-adjust`
 * 全部是**排版引擎**的行为，jsdom 里没有任何意义。本脚本直接驱动 headless Chrome
 * 打开生产构建，走 Chrome 自己打印管线的同一套渲染路径（`Page.printToPDF`
 * + `preferCSSPageSize: true`，等价于打印对话框里的「默认缩放」），
 * 再解析产出的 PDF 校验页数与页面尺寸。
 *
 * 前置：
 *   1. `cd frontend && npm run build && npx vite preview --port 4173`
 *   2. `cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000`
 *   3. 库里至少有一个「首个产物是 ppt」的工作区（本脚本自动发现，不写死 UUID）
 *
 * 用法：
 *   node scripts/print_pdf_check.mjs [输出目录]
 *
 * 退出码：0 = 全部通过；1 = 有断言失败（详情见 stdout）。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_URL = process.env.APP_URL ?? "http://localhost:4173/";
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";
const OUT_DIR = resolve(process.argv[2] ?? join(HERE, "..", "frontend", ".e2e-shots"));
const STORAGE_KEY = "edu.workspace.currentWorkspaceId";
const DEBUG_PORT = Number(process.env.CDP_PORT ?? 9335);

/** Chrome 的候选安装路径（Windows / macOS / Linux 各一） */
const CHROME_CANDIDATES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
];

const failures = [];

function check(name, condition, detail = "") {
  const ok = Boolean(condition);
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}${detail === "" ? "" : ` :: ${detail}`}`);
  if (!ok) {
    failures.push(name);
  }
  return ok;
}

async function api(path) {
  const response = await fetch(`${API_URL}${path}`);
  if (!response.ok) {
    throw new Error(`GET ${path} -> ${response.status}`);
  }
  return (await response.json()).data;
}

/** 找一个「首个产物就是 ppt」的工作区，这样前端 bootstrap 后会自动选中它。 */
async function discoverPptWorkspace() {
  const workspaces = await api("/api/workspaces");
  for (const workspace of workspaces) {
    const artifacts = await api(`/api/workspaces/${workspace.id}/artifacts`);
    const first = artifacts[0];
    if (first !== undefined && first.artifact_type === "ppt") {
      return { workspaceId: workspace.id, artifact: first };
    }
  }
  throw new Error("库里找不到「首个产物是 ppt」的工作区，请先生成一份 PPT");
}

function findChrome() {
  const found = CHROME_CANDIDATES.find((path) => existsSync(path));
  if (found === undefined) {
    throw new Error("找不到 Chrome / Edge 可执行文件，请设置 CHROME_PATH");
  }
  return process.env.CHROME_PATH ?? found;
}

async function connect(port) {
  const deadline = Date.now() + 20000;
  let target = null;
  while (Date.now() < deadline && target === null) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {
        method: "PUT",
      });
      if (response.ok) {
        target = await response.json();
      }
    } catch {
      await sleep(200);
    }
  }
  if (target === null) {
    throw new Error("Chrome 调试端口没起来");
  }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolveOpen, rejectOpen) => {
    ws.addEventListener("open", resolveOpen, { once: true });
    ws.addEventListener("error", rejectOpen, { once: true });
  });

  let nextId = 0;
  const pending = new Map();
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id === undefined) {
      return;
    }
    const slot = pending.get(message.id);
    if (slot === undefined) {
      return;
    }
    pending.delete(message.id);
    if (message.error) {
      slot.reject(new Error(JSON.stringify(message.error)));
    } else {
      slot.resolve(message.result);
    }
  });

  const send = (method, params = {}) =>
    new Promise((resolveSend, rejectSend) => {
      const id = ++nextId;
      pending.set(id, { resolve: resolveSend, reject: rejectSend });
      ws.send(JSON.stringify({ id, method, params }));
    });

  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails !== undefined) {
      throw new Error(`页面内报错: ${JSON.stringify(result.exceptionDetails)}`);
    }
    return result.result.value;
  };

  return { ws, send, evaluate };
}

/** 从 PDF 字节里读页数与页面尺寸（Chrome 产出的 PDF 未压缩，可直接 grep） */
function parsePdf(raw) {
  const text = raw.toString("latin1");
  const counts = [...text.matchAll(/\/Count\s+(\d+)/g)].map((match) => Number(match[1]));
  const boxes = [...text.matchAll(/\/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]/g)].map(
    (match) => [Number(match[1]), Number(match[2])],
  );
  return { pageCount: Math.max(0, ...counts), boxes };
}

async function main() {
  const { workspaceId, artifact } = await discoverPptWorkspace();
  const slideCount = artifact.spec.slides.length;
  console.log(`\n[发现] workspace=${workspaceId} artifact=${artifact.id} slides=${slideCount}\n`);

  const profileDir = mkdtempSync(join(tmpdir(), "print-pdf-check-"));
  const chrome = spawn(
    findChrome(),
    [
      "--headless=new",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${profileDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--no-proxy-server",
      "--hide-scrollbars",
      "--force-device-scale-factor=1",
      "--window-size=1440,900",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  let session = null;
  try {
    session = await connect(DEBUG_PORT);
    const { send, evaluate } = session;

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });

    console.log("[1] 打开应用并选中含 PPT 的工作区");
    await send("Page.navigate", { url: APP_URL });
    await sleep(1500);
    await evaluate(
      `window.localStorage.setItem(${JSON.stringify(STORAGE_KEY)}, ${JSON.stringify(workspaceId)}); "ok"`,
    );
    await send("Page.navigate", { url: APP_URL });
    await sleep(2500);

    // 等打印根挂载且页数到位
    const deadline = Date.now() + 20000;
    let mounted = 0;
    while (Date.now() < deadline) {
      mounted = await evaluate(`document.getElementById("print-root")?.children.length ?? 0`);
      if (mounted >= slideCount) {
        break;
      }
      await sleep(300);
    }
    check("打印根挂载且页数 = slide 数", mounted === slideCount, `${mounted} / ${slideCount}`);

    console.log("\n[2] 屏幕态：打印根不可见，按钮可用");
    const screen = await evaluate(`(() => {
      const root = document.getElementById("print-root");
      const button = [...document.querySelectorAll("button")]
        .find((item) => item.textContent.trim() === "导出 PDF");
      return {
        deckDisplay: getComputedStyle(root).display,
        buttonFound: button !== undefined,
        buttonDisabled: button?.disabled ?? null,
        buttonTitle: button?.title ?? null,
      };
    })()`);
    check("屏幕上打印根 display:none", screen.deckDisplay === "none", screen.deckDisplay);
    check("「导出 PDF」按钮存在且可用", screen.buttonFound && screen.buttonDisabled === false);
    check(
      "按钮提示写明了「另存为 PDF」与「页眉和页脚」",
      typeof screen.buttonTitle === "string" &&
        screen.buttonTitle.includes("另存为 PDF") &&
        screen.buttonTitle.includes("页眉和页脚"),
      screen.buttonTitle ?? "",
    );

    console.log("\n[3] 点按钮：window.print() 必须被调用，且按钮状态能恢复");
    const click = await evaluate(`(async () => {
      window.__printCalls = 0;
      const original = window.print;
      window.print = () => {
        window.__printCalls += 1;
        window.dispatchEvent(new Event("afterprint"));
      };
      const button = [...document.querySelectorAll("button")]
        .find((item) => item.textContent.trim() === "导出 PDF");
      button.click();
      await new Promise((resolve) => setTimeout(resolve, 1200));
      const result = {
        printCalls: window.__printCalls,
        labelAfter: button.textContent.trim(),
        disabledAfter: button.disabled,
      };
      window.print = original;
      return result;
    })()`);
    check("window.print() 调用 1 次", click.printCalls === 1, String(click.printCalls));
    check(
      "打印结束后按钮恢复可用",
      click.labelAfter === "导出 PDF" && click.disabledAfter === false,
      `${click.labelAfter} / disabled=${String(click.disabledAfter)}`,
    );

    console.log("\n[4] print 媒体：外壳隐藏、每页 1280×720、分页符正确");
    await send("Emulation.setEmulatedMedia", { media: "print" });
    await sleep(400);
    const printState = await evaluate(`(() => {
      const root = document.getElementById("print-root");
      const appRoot = document.getElementById("root");
      return {
        deckDisplay: getComputedStyle(root).display,
        appRootDisplay: appRoot === null ? null : getComputedStyle(appRoot).display,
        pages: [...root.children].map((page) => {
          const rect = page.getBoundingClientRect();
          const style = getComputedStyle(page);
          return {
            w: Math.round(rect.width),
            h: Math.round(rect.height),
            breakAfter: style.breakAfter || style.pageBreakAfter,
          };
        }),
        anyNoPrintVisible: [...document.querySelectorAll(".no-print")].some(
          (node) => getComputedStyle(node).display !== "none",
        ),
      };
    })()`);
    check("print 媒体下打印根可见", printState.deckDisplay !== "none", printState.deckDisplay);
    check("print 媒体下应用外壳隐藏", printState.appRootDisplay === "none", String(printState.appRootDisplay));
    check("print 媒体下没有 .no-print 元素可见", !printState.anyNoPrintVisible);
    check(
      "每页都是 1280×720",
      printState.pages.every((page) => page.w === 1280 && page.h === 720),
      JSON.stringify(printState.pages.slice(0, 2)),
    );
    check(
      "除末页外都 break-after: page",
      printState.pages.slice(0, -1).every((page) => page.breakAfter === "page"),
    );
    check("末页不再分页（避免多出空白页）", printState.pages.at(-1)?.breakAfter === "auto");

    console.log("\n[5] 产出真实 PDF 并校验页数 / 页面尺寸");
    await send("Emulation.setEmulatedMedia", { media: "" });
    await sleep(300);
    const printed = await send("Page.printToPDF", {
      preferCSSPageSize: true,
      printBackground: false, // 模拟打印对话框「背景图形」未勾选（默认态）
      marginTop: 0,
      marginBottom: 0,
      marginLeft: 0,
      marginRight: 0,
    });
    const raw = Buffer.from(printed.data, "base64");
    const pdfPath = join(OUT_DIR, "export-check.pdf");
    writeFileSync(pdfPath, raw);
    const pdf = parsePdf(raw);
    check("PDF 页数 = slide 数", pdf.pageCount === slideCount, `${pdf.pageCount} / ${slideCount}`);
    check("PDF 每页 MediaBox 一致", new Set(pdf.boxes.map(String)).size === 1);
    const box = pdf.boxes[0] ?? [0, 0];
    const widthMm = (box[0] / 72) * 25.4;
    const heightMm = (box[1] / 72) * 25.4;
    check("PDF 页面是横向 16:9", Math.abs(box[0] / box[1] - 16 / 9) < 0.01, `${box[0]}×${box[1]} pt`);
    check(
      "PDF 页面 = 338.7×190.5mm（无白边）",
      Math.abs(widthMm - 338.7) < 1 && Math.abs(heightMm - 190.5) < 1,
      `${widthMm.toFixed(1)}×${heightMm.toFixed(1)} mm`,
    );
    console.log(`\n  PDF 已保存：${pdfPath}`);
  } finally {
    try {
      session?.ws.close();
    } catch {
      /* ignore */
    }
    chrome.kill();
  }

  if (failures.length > 0) {
    console.error(`\n=== 失败 ${failures.length} 项 ===\n${failures.join("\n")}`);
    process.exitCode = 1;
    return;
  }
  console.log("\n=== 全部通过 ===");
}

await main();
