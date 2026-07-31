/* 链接提取工具 - Service Worker（基础缓存版）
 * 说明：HTTP 环境下 Service Worker 无法注册（浏览器限制），
 * 此文件仅为 HTTPS 部署后启用 PWA 缓存预留。
 * 策略：网络优先，失败回退缓存（适合动态 API 应用）。
 */
const CACHE_NAME = "link-extractor-v1";
const PRECACHE = [
  "/",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  // 仅缓存 GET 静态资源；API 请求走网络（不做缓存，保证数据新鲜）
  if (event.request.method !== "GET" || event.request.url.includes("/api/")) {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
