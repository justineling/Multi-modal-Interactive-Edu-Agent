/* eslint-disable no-restricted-globals */
/**
 * Service Worker —— 只做「应用壳缓存」，让装成桌面应用后即使后端没起也能打开界面。
 *
 * 三条铁律（违反任何一条都会让应用彻底不可用）：
 * 1. **绝不缓存 `/api/`**：对话是 `POST /api/chat/stream` 的 SSE 长连接，
 *    一旦被 SW 缓存/复用，用户会永远看到旧回复，而且 SSE 流会被截断。
 * 2. **network-first，不是 cache-first**：代码更新必须立即生效；
 *    cache-first 会让用户装完应用后永远停在第一天那个版本。
 * 3. **导航请求失败才回退**到 `/offline.html`：不要用缓存里旧的 index.html
 *    假装成功，那会让人以为后端连上了。
 */

const CACHE = "edu-shell-v1";

/** 预缓存的应用壳。逐个 add，单个失败不影响整体安装 */
const SHELL = [
  "/",
  "/index.html",
  "/offline.html",
  "/manifest.webmanifest",
  "/icons/icon.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      await Promise.all(
        SHELL.map(async (url) => {
          try {
            await cache.add(new Request(url, { cache: "reload" }));
          } catch {
            // 某个资源 404 不该让整个 SW 安装失败
          }
        }),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)));
      await self.clients.claim();
    })(),
  );
});

function isApiRequest(url) {
  return url.pathname === "/api" || url.pathname.startsWith("/api/");
}

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // 只处理 GET：POST（含 SSE）一律直接放行，绝不介入
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);

  // 跨域资源不管
  if (url.origin !== self.location.origin) {
    return;
  }

  // 铁律 1：API 请求完全绕过缓存
  if (isApiRequest(url)) {
    return;
  }

  // 导航请求：先网络，失败给离线页
  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          const response = await fetch(request);
          const cache = await caches.open(CACHE);
          cache.put("/index.html", response.clone()).catch(() => undefined);
          return response;
        } catch {
          const cache = await caches.open(CACHE);
          const offline = await cache.match("/offline.html");
          if (offline !== undefined) {
            return offline;
          }
          return new Response(
            "<!doctype html><meta charset=utf-8><body style='background:#0B1220;color:#E8EEF9;font-family:sans-serif;padding:40px'>当前处于离线状态，且离线页未缓存。请联网后重试。</body>",
            { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } },
          );
        }
      })(),
    );
    return;
  }

  // 静态资源：network-first + 缓存兜底
  event.respondWith(
    (async () => {
      try {
        const response = await fetch(request);
        // 只缓存成功的同源响应（opaque / 206 不缓存，避免污染）
        if (response.ok && response.status === 200 && response.type === "basic") {
          const cache = await caches.open(CACHE);
          cache.put(request, response.clone()).catch(() => undefined);
        }
        return response;
      } catch {
        const cached = await caches.match(request);
        if (cached !== undefined) {
          return cached;
        }
        return new Response("", { status: 504, statusText: "Offline and not cached" });
      }
    })(),
  );
});
