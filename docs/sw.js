/* Service worker: offline shell + push handling.
   Must be served from the site root so its scope covers the whole app. */

const CACHE = "signals-v2";
// Relative to the worker's scope, so this works at "/" and at "/repo-name/".
const SHELL = [
  "./",
  "./app.js",
  "./styles.css",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // Report data: always try the network first so a fresh run shows up
  // immediately; the client keeps its own copy in localStorage for offline.
  if (url.pathname.includes("/api/") || url.pathname.endsWith("latest.json")) {
    event.respondWith(fetch(request).catch(() => caches.match(request)));
    return;
  }

  // Shell: cache first.
  event.respondWith(
    caches.match(request).then((hit) => hit || fetch(request))
  );
});

self.addEventListener("push", (event) => {
  let data = { title: "Daily Signals", body: "New report available", url: "/" };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (_) {
    if (event.data) data.body = event.data.text();
  }

  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "./icons/icon-192.png",
      badge: "./icons/icon-192.png",
      data: { url: data.url },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
