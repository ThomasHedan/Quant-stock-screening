/*
 * Service worker for the Momentum Gap Scanner.
 *
 * Its only job is push: show the notification and, on click, open the ticker
 * detail page. It deliberately does not cache application shells — the pages
 * this app serves are live market data, and a stale cached table showing
 * yesterday's alerts would be worse than no page at all.
 *
 * On iOS, push only works when the site has been installed to the home screen
 * (see manifest.json and the install instructions in the README).
 */

self.addEventListener('install', (event) => {
  // Take over immediately so a freshly registered worker can receive the very
  // next push rather than waiting for every tab to close.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let payload = { title: 'Momentum Gap Scanner', body: '', url: '/', tag: 'alert' };

  if (event.data) {
    try {
      payload = Object.assign(payload, event.data.json());
    } catch (error) {
      // A malformed payload must still surface something: a silent swallow
      // here looks exactly like push being broken.
      payload.body = event.data.text();
    }
  }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      tag: payload.tag,
      renotify: true,
      requireInteraction: false,
      data: { url: payload.url },
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      // Reuse an open tab when there is one: the trader is likely already
      // looking at the app, and a second window is noise mid-session.
      for (const client of clients) {
        if ('focus' in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
