self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => event.respondWith(fetch(event.request)));

// Dock / taskbar badge with the unread count. (This used to post an empty
// notification on every change as well, which did nothing for the badge.)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SET_BADGE') {
    const count = event.data.count || 0;
    if (!self.navigator.setAppBadge) return;
    (count > 0 ? self.navigator.setAppBadge(count) : self.navigator.clearAppBadge()).catch(() => {});
  }
});

// Clicking a message notification brings the app forward on that conversation.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const chatId = event.notification.data && event.notification.data.chatId;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const win = wins[0];
    if (win) {
      try { await win.focus(); } catch (_) {}
      if (chatId) win.postMessage({ type: 'OPEN_CHAT', chatId });
    } else if (self.clients.openWindow) {
      await self.clients.openWindow('/');
    }
  })());
});
