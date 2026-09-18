/*
 * Push subscription helper, loaded by the settings page.
 *
 * Kept as plain ES modules with no build step (CLAUDE.md 3). Everything here
 * is best-effort and reports its failures in the page rather than the console:
 * "I enabled notifications and nothing happened" is the single most likely
 * support question, and the answer is almost always one of the states below.
 */

export function isSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window;
}

export function isStandalone() {
  // iOS only delivers push to a site installed to the home screen.
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  return Uint8Array.from([...raw].map((char) => char.charCodeAt(0)));
}

export async function enablePush(vapidPublicKey) {
  if (!isSupported()) {
    throw new Error('This browser does not support Web Push.');
  }

  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new Error(`Notification permission was ${permission}.`);
  }

  const registration = await navigator.serviceWorker.register('/static/sw.js');
  await navigator.serviceWorker.ready;

  const existing = await registration.pushManager.getSubscription();
  const subscription =
    existing ||
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
    }));

  const response = await fetch('/api/push/subscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(subscription.toJSON()),
  });
  if (!response.ok) {
    throw new Error(`The server rejected the subscription (HTTP ${response.status}).`);
  }
  return subscription;
}

export async function disablePush() {
  if (!isSupported()) return false;
  const registration = await navigator.serviceWorker.getRegistration('/static/sw.js');
  if (!registration) return false;
  const subscription = await registration.pushManager.getSubscription();
  if (!subscription) return false;

  await fetch('/api/push/unsubscribe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint: subscription.endpoint }),
  });
  return subscription.unsubscribe();
}

export async function sendTestNotification() {
  const response = await fetch('/api/push/test', { method: 'POST' });
  if (!response.ok) {
    throw new Error(`Test push failed (HTTP ${response.status}).`);
  }
  return response.json();
}
