const CACHE = 'simple-v201';
const URLS = [
  'index.html',
  'help.html',
  'help-ru.html',
  'style.css',
  'sim.png',
  'favicon.svg',
  'icon-192.png',
  'icon-512.png',
  'manifest.json',
  'des-bundle.js',
  'aes-bundle.js',
  'sim.svg',
  'sim_anim.svg',
  'nosim.svg',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(URLS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => clients.claim())
  );
});

const OFFLINE_RESPONSE = new Response('Offline: page not cached', {
  status: 503,
  statusText: 'Offline',
  headers: { 'Content-Type': 'text/plain' },
});

self.addEventListener('fetch', e => {
  if (!e.request.url.startsWith('http')) return;
  const path = new URL(e.request.url).pathname;
  if (path.startsWith('/api/')) return;  // live data, never cache
  if (e.request.method !== 'GET') return;
  const isNavigate = e.request.mode === 'navigate';
  const isSwScript = path.endsWith('/sw.js');
  if (isNavigate) {
    e.respondWith(
      fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }).catch(() =>
        caches.match(e.request)
          .then(r => r || caches.match('index.html'))
          .then(r => r || OFFLINE_RESPONSE)
      )
    );
  } else if (isSwScript) {
    e.respondWith(fetch(e.request).catch(() => OFFLINE_RESPONSE));
  } else {
    e.respondWith(
      caches.match(e.request).then(r => r || fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }))
    );
  }
});