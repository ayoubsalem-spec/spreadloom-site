// Minimal service worker -- required by browsers to allow "Add to Home Screen"
// installation. This intentionally does NOT cache pages, so every page load
// still goes straight to the live Railway server, same as opening it in a
// browser tab. That's deliberate: SitePulse's data changes constantly
// (equipment status, locations), so an offline cache would show stale
// information instead of failing loudly. If offline support is wanted later,
// this is the file to extend with a proper cache strategy.

self.addEventListener('install', function(event) {
    self.skipWaiting();
});

self.addEventListener('activate', function(event) {
    self.clients.claim();
});

self.addEventListener('fetch', function(event) {
    // Pass every request straight through to the network -- no caching.
    event.respondWith(fetch(event.request));
});
