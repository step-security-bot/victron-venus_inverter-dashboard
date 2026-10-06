/* Notifications only: deliberately no fetch handler, offline cache or SPA cache. */
const DATABASE = 'inverter-system-notifications-v1'
const MAX_EVENTS = 4096
const RETENTION_MS = 30 * 24 * 60 * 60 * 1000
const MAX_EVENT_AGE_MS = 5 * 60 * 1000
const DEFAULT_SETTINGS = { enabled: false, generation: 0 }

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1)
    request.onupgradeneeded = () => {
      const db = request.result
      db.createObjectStore('settings')
      const events = db.createObjectStore('events', { keyPath: 'key' })
      events.createIndex('expires', 'expires')
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(new Error('Notification storage unavailable'))
  })
}

async function settings(update) {
  const db = await openDatabase()
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('settings', update ? 'readwrite' : 'readonly')
      const store = tx.objectStore('settings')
      const request = store.get('delivery')
      let result
      request.onsuccess = () => {
        const saved = request.result || DEFAULT_SETTINGS
        const current = { enabled: saved.enabled === true, generation: Number.isSafeInteger(saved.generation) ? saved.generation : 0 }
        const next = update?.(current)
        if (next) store.put(next, 'delivery')
        result = { ...(next || current), applied: !update || !!next }
      }
      tx.oncomplete = () => resolve(result)
      tx.onerror = () => reject(new Error('Notification settings unavailable'))
      tx.onabort = tx.onerror
    })
  } finally { db.close() }
}

function changeSettings(request) {
  // Persist intent before browser/server awaits; another tab's Disable retires it.
  return settings((current) => {
    if (request.type === 'notification-settings-begin' || request.enabled === true) {
      if (!Number.isSafeInteger(request.generation) || request.generation !== current.generation) return null
    }
    if (request.type === 'notification-settings-begin' || request.enabled === false) {
      if (current.generation >= Number.MAX_SAFE_INTEGER) return null
      return { enabled: false, generation: current.generation + 1 }
    }
    return { enabled: true, generation: current.generation }
  })
}

function normalizedEvent(value) {
  if (!value || typeof value !== 'object' || value.schemaVersion !== 1 || value.url !== '/') return null
  if (!['native', 'ev', 'water', 'lowBattery', 'test'].includes(value.kind)) return null
  if (!['victron', 'ha', 'system'].includes(value.source)) return null
  if (value.kind === 'native' && !['victron', 'system'].includes(value.source)) return null
  if (typeof value.eventKey !== 'string' || !/^[a-f0-9]{64}$/.test(value.eventKey)) return null
  if (typeof value.title !== 'string' || !value.title || Array.from(value.title).length > 120) return null
  if (typeof value.body !== 'string' || Array.from(value.body).length > 1000) return null
  const now = Date.now()
  for (const timestamp of [value.sourceTimestampMs, value.observedAtMs]) {
    if (!Number.isSafeInteger(timestamp) || timestamp <= 0 || timestamp > now + 30_000 || now - timestamp > MAX_EVENT_AGE_MS) return null
  }
  return { key: value.eventKey, title: value.title, body: value.body, timestamp: value.sourceTimestampMs }
}

function pruneEvents(store) {
  const count = store.count()
  count.onsuccess = () => {
    let excess = Math.max(0, count.result - MAX_EVENTS)
    const oldest = store.index('expires').openCursor()
    oldest.onsuccess = () => {
      const cursor = oldest.result
      if (cursor && (excess > 0 || cursor.value.expires <= Date.now())) {
        cursor.delete(); excess--; cursor.continue()
      }
    }
  }
}

// A read/write transaction serializes claims made by push events and every tab.
async function claimEvent(event) {
  const db = await openDatabase()
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('events', 'readwrite')
      const store = tx.objectStore('events')
      let claimed = false
      const request = store.get(event.key)
      request.onsuccess = () => {
        if (!request.result) {
          store.put({ key: event.key, expires: Date.now() + RETENTION_MS })
          claimed = true
        }
        pruneEvents(store)
      }
      tx.oncomplete = () => resolve(claimed)
      tx.onerror = () => reject(new Error('Notification deduplication unavailable'))
      tx.onabort = tx.onerror
    })
  } finally { db.close() }
}

async function releaseClaim(key) {
  const db = await openDatabase()
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction('events', 'readwrite')
      tx.objectStore('events').delete(key)
      tx.oncomplete = resolve
      tx.onerror = () => reject(new Error('Notification storage unavailable'))
      tx.onabort = tx.onerror
    })
  } finally { db.close() }
}

async function deliver(value) {
  const event = normalizedEvent(value)
  if (!event) return false
  const config = await settings()
  if (!config.enabled) return false
  if (!await claimEvent(event)) return false
  const current = await settings()
  if (!current.enabled || current.generation !== config.generation) return false
  try {
    const bodyPrefix = event.body ? `${event.body}\n` : ''
    await self.registration.showNotification(event.title, {
      body: `${bodyPrefix}Event: ${new Date(event.timestamp).toLocaleString(undefined, { timeZoneName: 'short' })}`,
      tag: event.key,
      timestamp: event.timestamp,
      icon: '/notification-icon.svg',
      data: { path: '/' },
      // OS dismissal never acknowledges an inverter alarm.
    })
    return true
  } catch {
    await releaseClaim(event.key)
    throw new Error('The browser could not display the notification')
  }
}

self.addEventListener('install', (event) => { event.waitUntil(self.skipWaiting()) })
self.addEventListener('activate', (event) => { event.waitUntil(self.clients.claim()) })
self.addEventListener('message', (event) => {
  // Only controlled same-origin dashboard windows may configure this worker.
  if (event.origin !== self.location.origin) return
  if (!event.source?.url || new URL(event.source.url).origin !== self.location.origin) return
  const request = event.data
  event.waitUntil((async () => {
    try {
      let result
      if (request?.type === 'notification-settings-get') result = await settings()
      else if (request?.type === 'notification-settings-set' || request?.type === 'notification-settings-begin') {
        if (request.type === 'notification-settings-set' && typeof request.enabled !== 'boolean') return
        result = await changeSettings(request)
        if (result.applied) {
          const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
          for (const client of windows) client.postMessage({ type: 'notification-settings-changed' })
        }
      } else return
      event.ports?.[0]?.postMessage({ ok: true, result })
    } catch {
      event.ports?.[0]?.postMessage({ ok: false, error: 'Notification delivery or storage unavailable' })
    }
  })())
})
self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    try { await deliver(event.data?.json()) } catch { /* Delivery failure must not crash the worker. */ }
  })())
})
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const dashboard = windows.find((client) => {
      const url = new URL(client.url)
      return url.origin === self.location.origin && url.pathname === '/'
    })
    if (dashboard) await dashboard.focus()
    else await self.clients.openWindow('/')
  })())
})
