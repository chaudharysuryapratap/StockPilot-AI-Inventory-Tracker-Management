const CACHE_NAME = "stockpilot-picker-v6";
const OFFLINE_URL = "/static/offline.html";
const APP_SHELL = [
  OFFLINE_URL,
  "/static/app.css",
  "/static/app-shell.js",
  "/static/ui.js",
  "/static/dashboard-viz.js",
  "/static/dashboard-chat.js",
  "/static/procurement.js",
  "/static/picker.js",
  "/static/manifest.webmanifest",
  "/static/stockpilot-icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (!response.ok) return response;
          const copy = response.clone();
          return caches.open(CACHE_NAME).then((cache) =>
            cache.put(event.request, copy).then(() => response)
          );
        })
        .catch(() => caches.match(event.request))
    );
  }
});
