import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "@/App";
import { registerServiceWorker } from "@/lib/pwa";
import "allotment/dist/style.css";
import "@/index.css";

const container = document.getElementById("root");

if (container === null) {
  // 挂载点缺失说明 index.html 被改坏，直接抛错比白屏更容易定位
  throw new Error('未找到挂载点 #root，请检查 index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// 生产构建下注册 Service Worker（dev 下不注册，否则会破坏 HMR）
registerServiceWorker();
