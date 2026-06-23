// service-worker.js
const CACHE_NAME = 'rockabywifi-v1';
const STATIC_CACHE_URLS = [
  '/',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/manifest.json',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'
];

// Install event – cache static assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_CACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// Activate event – clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.filter(name => name !== CACHE_NAME)
          .map(name => caches.delete(name))
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch event – serve from cache, fallback to network
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  
  // Only handle same-origin requests or CDN resources
  if (url.origin === location.origin || url.origin === 'https://cdnjs.cloudflare.com' || url.origin === 'https://cdn.jsdelivr.net') {
    event.respondWith(
      caches.match(event.request)
        .then(response => {
          if (response) {
            return response; // Serve from cache
          }
          // Fetch from network and cache
          return fetch(event.request)
            .then(networkResponse => {
              const clonedResponse = networkResponse.clone();
              caches.open(CACHE_NAME)
                .then(cache => cache.put(event.request, clonedResponse));
              return networkResponse;
            })
            .catch(() => {
              // Offline fallback – serve a simple offline page if needed
              return new Response('Offline – Please check your internet connection.', { status: 503 });
            });
        })
    );
  }
});

// Handle push notifications (optional – for later)
self.addEventListener('push', event => {
  const data = event.data ? event.data.json() : {};
  const options = {
    body: data.body || 'RockabyWiFi notification',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    vibrate: [200, 100, 200]
  };
  event.waitUntil(
    self.registration.showNotification(data.title || 'RockabyWiFi', options)
  );
});
