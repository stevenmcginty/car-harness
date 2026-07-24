/* Car Harness service worker.
 *
 * Deliberately network-FIRST, not cache-first. The cache exists so the app
 * still opens when the backend is stopped — not to make a LAN request faster.
 * Cache-first on app.js is exactly how a deployed fix ends up invisible behind
 * a stale worker, so we never do it: the network always wins when it answers,
 * and the cache is only ever the fallback.
 *
 * /api/* is never touched at all. Diagnostics served from cache would be a
 * lie about the car in front of you.
 */

const VERSION = 'car-harness-v2.7';
const SHELL = [
  './',
  'index.html',
  'styles.css',
  'app.js',
  'manifest.webmanifest',
  'icons/icon-192.png',
  'icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      // addAll is all-or-nothing; one missing file must not break install.
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return; // always live, never cached

  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(VERSION).then((cache) => cache.put(req, copy));
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((hit) => {
          if (hit) return hit;
          // A navigation with nothing cached for that exact URL still deserves
          // the app shell rather than the browser's offline error page.
          if (req.mode === 'navigate') {
            return caches.match('index.html');
          }
          return Response.error();
        })
      )
  );
});
