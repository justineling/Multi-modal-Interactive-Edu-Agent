/**
 * PWA 注册。
 *
 * 两条硬约束：
 * 1. **只在生产构建注册**（`import.meta.env.PROD`）。dev 下注册 SW 会缓存住 Vite 的
 *    模块请求，导致 HMR 失效、改代码不生效——这是最容易被误判为「代码没保存」的坑。
 * 2. **只注册 `/sw.js`**，路径写死。SW 的作用域由它所在目录决定，`/sw.js` 才能覆盖全站。
 *
 * 注册失败一律静默：装不上桌面应用不该影响正常使用网页版。
 */
export function registerServiceWorker(): void {
  if (!import.meta.env.PROD) {
    return;
  }
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) {
    return;
  }
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // 忽略：SW 不可用（隐私模式 / 非 https 且非 localhost）时网页版仍可正常工作
    });
  });
}
