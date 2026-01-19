const CACHE_NAME = 'ovr-map-tiles-v1';
let tilePrefixes = [];
let cacheEnabled = false;

function isValidPrefix(prefix) {
  return (
    typeof prefix === 'string' &&
    prefix.length > 10 &&
    (prefix.startsWith('http://') || prefix.startsWith('https://'))
  );
}

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'OVR_TILE_CONFIG') {
    const prefixes = Array.isArray(data.tilePrefixes)
      ? data.tilePrefixes
      : [data.tilePrefix].filter(Boolean);
    tilePrefixes = prefixes.filter(isValidPrefix);
    cacheEnabled = tilePrefixes.length > 0 && data.cacheEnabled !== false;
  }

  if (data.type === 'OVR_TILE_CLEAR') {
    event.waitUntil(caches.delete(CACHE_NAME));
  }
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  if (!cacheEnabled || tilePrefixes.length === 0) return;
  const url = event.request.url || '';
  if (!tilePrefixes.some((prefix) => url.startsWith(prefix))) return;

  event.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          cache.put(event.request, response.clone());
          return response;
        });
      })
    )
  );
});
