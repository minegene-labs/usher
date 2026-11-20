// Usher service worker: keeps the app shell and the artwork on the device so the app opens instantly.
// Of /api/ it keeps only artwork (/api/art/); lists and account data go straight to the network (the app keeps its
// own last copies, per account: see `lists.restore` in data.js). Signing out empties both.
const VERSION = "usher-1.4.6";
const SHELL = ["/", "/index.html", "/app.css", "/account.css", "/sheets.css", "/discover.css", "/media.css", "/menu.css", "/notify.css", "/storage.css", "/status.css", "/changelog.css", "/app.js", "/data.js", "/dom.js", "/util.js", "/posters.js", "/prefetch.js", "/rows.js", "/filters.js", "/device.js", "/menu.js", "/storage.js", "/watch.js",
  "/updater.js", "/shell.js", "/home.js", "/downloads.js", "/upcoming.js", "/status.js", "/settings.js", "/notify.js", "/advanced.js", "/updates.js", "/sheets.js", "/sheetdrag.js", "/search.js",
  "/discover.js", "/account.js", "/setup.js", "/connect.js", "/changelog.js", "/startup.js", "/manifest.webmanifest",
  "/icons/icon-192.png", "/icons/apple-touch-icon.png", "/icons/favicon.png", "/icons/tmdb.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      // Only old shells go: the artwork and the saved lists outlive an update.
      .then((keys) => Promise.all(keys.filter((k) => ![VERSION, ART, DATA].includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/art/")) {
    // Should the cache itself fail (storage full or unavailable), the picture still comes from the server.
    event.respondWith(artwork(event).catch(() => fetch(event.request)));
    return;
  }
  if (url.pathname.startsWith("/api/")) return; // straight to the network
  if (event.request.mode === "navigate") {
    // Network first so updates show at once; the cached shell only when the server can't be reached.
    // Away from home without Tailscale the server's address can hang rather than fail, so after 4 s the
    // cached shell opens instead and tells the person what to do.
    const net = fetch(event.request);
    const slow = new Promise((_, reject) => setTimeout(reject, 4000));
    event.respondWith(Promise.race([net, slow]).catch(async () => (await caches.match("/index.html")) || net));
    return;
  }
  // Shell files: answered from this version's cache, never refreshed into it. A copy swapped in file by file could pair
  // a new module with an old one and leave the page on the start-up screen; each version's files stay one set, and the
  // next version brings its own cache.
  event.respondWith(caches.open(VERSION).then(async (cache) => (await cache.match(event.request)) || fetch(event.request)));
});

// ---- artwork --------------------------------------------------------------------------------
// Pictures are answered from the device at once and refreshed in the background once older than the server said they
// may be kept (versioned ones for a month). They have a cache of their own, capped in bytes and in pictures: past
// either, the least recently shown go first, so artwork never fills the phone. A 401 (the session has ended) or the
// app's "forget" message (someone signed out) empties it, with the saved lists.
const ART = "usher-art";
const DATA = "usher-data";
const ART_BYTES = 40 * 1024 * 1024;
const ART_ITEMS = 800;
const ART_INDEX = "/art-index";   // kept with the pictures: each one's size and age, least recently shown first
const art = { ready: null, gen: 0, saving: null };

// One copy per picture: a retry (?r=1, from posters.js) replaces the copy rather than adding one.
function artKey(href) {
  const u = new URL(href);
  u.searchParams.delete("r");
  return u.href;
}

function keepFor(res) {
  const m = (res.headers.get("Cache-Control") || "").match(/max-age=(\d+)/);
  return (m ? Number(m[1]) : 86400) * 1000;
}

// The index, read once per start of the worker. A picture without an entry (or an entry without a picture) is
// dropped, so the two always agree and the cap counts everything kept.
function artIndex() {
  art.ready ??= (async () => {
    const cache = await caches.open(ART);
    const index = new Map();
    const saved = await cache.match(ART_INDEX);
    try {
      for (const [key, entry] of saved ? await saved.json() : []) index.set(key, entry);
    } catch {
      index.clear();   // unreadable: start again; the pictures it named are dropped just below
    }
    const kept = new Set((await cache.keys()).map((r) => r.url).filter((k) => new URL(k).pathname.startsWith("/api/art/")));
    for (const key of kept) if (!index.has(key)) await cache.delete(key);
    for (const key of index.keys()) if (!kept.has(key)) index.delete(key);
    return index;
  })().catch((e) => {
    art.ready = null;   // tried again on the next picture; this one comes straight from the server
    throw e;
  });
  return art.ready;
}

function saveIndex() {
  art.saving ??= new Promise((resolve) => setTimeout(resolve, 1000)).then(async () => {
    art.saving = null;
    const gen = art.gen;
    const index = await artIndex();
    const cache = await caches.open(ART);
    if (gen !== art.gen) return;
    await cache.put(ART_INDEX, new Response(JSON.stringify([...index]), { headers: { "Content-Type": "application/json" } }));
  });
  return art.saving;
}

async function artwork(event) {
  const gen = art.gen;
  const key = artKey(event.request.url);
  const retry = new URL(event.request.url).searchParams.has("r");
  const index = await artIndex();
  const entry = index.get(key);
  const cached = entry && !retry ? await (await caches.open(ART)).match(key) : null;
  if (cached) {
    index.delete(key);
    index.set(key, entry);   // now the most recently shown
    if (Date.now() - entry.stored > entry.keep) event.waitUntil(refreshArt(key, gen));
    event.waitUntil(saveIndex());
    return cached;
  }
  const res = await fetch(event.request);
  if (res.ok) event.waitUntil(keepArt(key, res.clone(), gen));
  else if (res.status === 401) event.waitUntil(forget());
  return res;
}

async function refreshArt(key, gen) {
  let res;
  try {
    res = await fetch(key, { credentials: "same-origin" });
  } catch {
    return;   // offline: the copy stays and is tried again next time it's shown
  }
  if (res.ok) return keepArt(key, res, gen);
  if (res.status === 401) return forget();
}

async function keepArt(key, res, gen) {
  const size = (await res.clone().blob()).size;
  const index = await artIndex();
  const cache = await caches.open(ART);
  if (gen !== art.gen) return;   // signed out meanwhile: not for the next person
  await cache.put(key, res);
  if (gen !== art.gen) return cache.delete(key);
  index.delete(key);
  index.set(key, { size, stored: Date.now(), keep: keepFor(res) });
  let bytes = 0;
  for (const e of index.values()) bytes += e.size;
  for (const [old, e] of index) {
    if (bytes <= ART_BYTES && index.size <= ART_ITEMS) break;
    index.delete(old);
    bytes -= e.size;
    await cache.delete(old);
  }
  await saveIndex();
}

async function forget() {
  art.gen += 1;
  art.ready = null;
  await Promise.all([caches.delete(ART), caches.delete(DATA)]);
}

self.addEventListener("message", (event) => {
  if (event.data?.type === "forget") event.waitUntil(forget());
});

// ---- notifications ------------------------------------------------------------------------
// The message arrives encrypted from the server; the browser decrypts it before this runs.
self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch { msg = {}; }
  const title = msg.title || "Usher";
  const shown = self.registration.showNotification(title, {
    body: msg.body || "",
    tag: msg.tag || "usher",
    data: { url: typeof msg.url === "string" && msg.url.startsWith("/") ? msg.url : "/" },
    icon: "/icons/icon-192.png",
  });
  const badge = Number.isInteger(msg.badge) && "setAppBadge" in self.navigator
    ? (msg.badge ? self.navigator.setAppBadge(msg.badge) : self.navigator.clearAppBadge()).catch(() => {})
    : Promise.resolve();
  event.waitUntil(Promise.all([shown, badge]));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) {
        await c.focus();
        c.postMessage({ type: "open", url });
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
