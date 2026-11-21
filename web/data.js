// Usher — what this device holds: the one way to call the server, and the one loader for every list.
// Nothing here draws anything; app.js says what to redraw and what to do when a list can't be loaded.

export class SignedOut extends Error {}

export const store = { me: null, snap: null, library: null, upcoming: null, tidy: null, upcomingAll: null, discover: null, online: true };

export async function api(path, { method = "GET", body, timeout } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    if (store.me) headers["X-CSRF-Token"] = store.me.csrf;
  }
  // Away from home without Tailscale, the server's address leads nowhere and a request can hang for over a minute;
  // `timeout` gives up sooner so Usher can say so.
  const signal = timeout && AbortSignal.timeout ? AbortSignal.timeout(timeout) : undefined;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin", cache: "no-store", signal });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/api/login" && path !== "/api/setup") throw new SignedOut();   // those two answer 401 for a wrong password or code
  if (!res.ok) throw new Error(data.error || "Something went wrong. Try again.");
  return data;
}

// ---- every list this device keeps -------------------------------------------------------------
// url: where it comes from. fresh: how long a copy is good for, in ms (0: asked for again every time).
// keep: an older copy stays on screen while a new one is fetched. saved: the last copy is also kept on the device,
// for the next time the app opens (see restore). changed: which `changed` live updates drop it ("any" title
// change, or only a change to "all" titles).
export const LISTS = {
  me: { url: "/api/me", fresh: 0 },
  library: { url: "/api/library", fresh: 2 * 60000, keep: true, saved: true, changed: "any" },
  tidy: { url: "/api/tidy", fresh: 60000, keep: true, saved: true, changed: "any" },
  upcoming: { url: "/api/upcoming", fresh: 5 * 60000, keep: true, saved: true, changed: "any" },
  upcomingAll: { url: "/api/upcoming/all", fresh: 30 * 60000, keep: true, changed: "all" },
  discover: { url: "/api/discover", fresh: 30 * 60000, keep: true, saved: true, changed: "any" },
};
const RETRY = 10000;    // after a list couldn't be loaded, how long before a screen asks for it again
const held = {};        // per list: at (when its copy arrived), loading (the request under way), watched, failedAt, gen
const state = (name) => (held[name] ??= { at: 0, loading: null, failedAt: 0, gen: 0, watched: null });

export const lists = {
  redraw() {},    // (name): a list has a new copy, or is due another try. Set by the app.
  failed() {},    // (name, error): a list a screen is waiting for couldn't be loaded. Set by the app.

  fresh(name) { return store[name] != null && Date.now() - state(name).at < LISTS[name].fresh; },
  // Good enough to draw: fresh, or an older copy of a list that stays up while it is fetched again.
  usable(name) { return this.fresh(name) || (!!LISTS[name].keep && store[name] != null); },

  // The list: its copy if that's fresh, otherwise from the server. One request at a time per list; later
  // callers share it. Rejects (SignedOut, or an Error to show) when it can't be had; the caller decides.
  load(name, { timeout } = {}) {
    if (this.fresh(name)) return Promise.resolve(store[name]);
    const s = state(name);
    if (s.loading) return s.loading;
    const gen = s.gen;
    const run = async () => {
      try {
        const d = await api(LISTS[name].url, { timeout });
        if (s.gen !== gen) return d;                 // dropped meanwhile: the copy is already out of date
        const same = JSON.stringify(d) === JSON.stringify(store[name]);
        this.set(name, d);
        if (!same) this.redraw(name);
        return d;
      } finally { if (s.loading === p) s.loading = null; }
    };
    const p = run();
    s.loading = p;
    return p;
  },

  // For a screen being drawn: the copy to draw now, or null while it loads (the screen is redrawn when it
  // arrives). Signed out and can't-reach-home are handled here, for every list.
  show(name) {
    const s = state(name);
    if (!this.fresh(name) && Date.now() - s.failedAt >= RETRY) {
      const p = this.load(name), gen = s.gen;
      if (s.watched !== p) {
        s.watched = p;
        p.catch((e) => {
          if (held[name] !== s || s.gen !== gen) return;      // dropped or signed out meanwhile
          s.failedAt = Date.now();
          const out = e instanceof SignedOut;
          if (out || !this.usable(name)) this.failed(name, e);
          if (!out) setTimeout(() => { if (held[name] === s && !this.usable(name)) this.redraw(name); }, RETRY);
        });
      }
    }
    return this.usable(name) ? store[name] : null;
  },

  // True when the last try for a list failed (it is tried again every few seconds while a screen wants it).
  tried(name) { return state(name).failedAt > 0; },

  set(name, data) {
    const s = state(name); store[name] = data; s.at = Date.now(); s.failedAt = 0;
    if (LISTS[name]?.saved) this.save(name, data);
  },
  drop(...names) {
    for (const name of names) { const s = state(name); store[name] = null; s.at = 0; s.failedAt = 0; s.loading = null; s.gen += 1; }
  },
  // A `changed` live update: drop every list the change touches.
  changed(n) {
    this.drop(...Object.keys(LISTS).filter((name) => LISTS[name].changed === "any" || (LISTS[name].changed === "all" && n.all)));
    details.clear();   // "In your library" and "Already asked for" may have changed
  },
  // Signed out: nothing of the account stays on the device, in memory or saved, artwork included.
  clear() {
    this.drop(...Object.keys(LISTS)); for (const name of Object.keys(held)) delete held[name];
    details.clear();
    return this.forget();
  },

  // ---- the last copies, kept on the device ----------------------------------------------------
  // The saved lists and the live state (`snap`: Home and Status) are written to Cache Storage, at most once every
  // SAVE_EVERY per list, so the next time the app opens they are drawn at once and refreshed in the background.
  // Each copy names its account and build. It is shown only once the server has confirmed the session (`me`), only to
  // the same account and build, and signing out (or another account) deletes every copy, artwork included.
  save(name, data) {
    if (typeof caches === "undefined" || store.me?.account == null) return;
    const first = !unsaved.has(name);
    unsaved.set(name, { account: store.me.account, build: store.me.build, at: Date.now(), data });
    if (first) setTimeout(() => this.write(name), SAVE_EVERY);
  },

  // Writes what is waiting now (the app is going to the background, where it may be closed). Without a name: all of it.
  async write(name) {
    const names = name ? [name] : [...unsaved.keys()];
    const gen = savedGen;
    for (const n of names) {
      const copy = unsaved.get(n);
      unsaved.delete(n);
      if (!copy) continue;
      try {
        const cache = await caches.open(CACHES.data);
        if (gen !== savedGen) return;
        await cache.put(`/saved/${n}`, new Response(JSON.stringify(copy), { headers: { "Content-Type": "application/json" } }));
        if (gen !== savedGen) return caches.delete(CACHES.data);   // signed out while writing
      } catch {
        // Storage full or turned off: the app works as before, from the network; the next copy tries again.
      }
    }
  },

  // After `me`: the last copies of this account and build, drawn until the server's arrive. Never fresh, so the first
  // screen that shows one asks for it again at once.
  async restore() {
    const me = store.me;
    if (typeof caches === "undefined" || me?.account == null) return;
    const gen = savedGen;
    const names = [...Object.keys(LISTS).filter((n) => LISTS[n].saved), "snap"];
    let copies;
    try {
      const cache = await caches.open(CACHES.data);
      copies = await Promise.all(names.map(async (n) => (await cache.match(`/saved/${n}`))?.json()));
    } catch {
      return;   // unreadable: drawn from the network as before
    }
    if (gen !== savedGen || store.me !== me) return;
    if (copies.some((c) => c && c.account !== me.account)) return this.forget();   // another account's: never shown
    names.forEach((name, i) => {
      const c = copies[i];
      if (!c || c.build !== me.build || Date.now() - c.at > SAVED_FOR || store[name] != null) return;
      store[name] = c.data;
      if (name !== "snap") state(name).at = 0;
    });
  },

  // Deletes every copy on the device and tells the service worker to forget the artwork.
  async forget() {
    savedGen += 1;
    unsaved.clear();
    if (typeof navigator !== "undefined") navigator.serviceWorker?.controller?.postMessage({ type: "forget" });
    if (typeof caches === "undefined") return;
    await Promise.all(Object.values(CACHES).map((name) => caches.delete(name).catch(() => false)));
  },
};

export const CACHES = { data: "usher-data", art: "usher-art" };   // the same names as in sw.js
const SAVE_EVERY = 10000;                  // live updates come every few seconds; one write per list at most this often
const SAVED_FOR = 7 * 24 * 3600 * 1000;   // an older copy is not worth drawing
const unsaved = new Map();                 // name -> copy waiting to be written
let savedGen = 0;                          // goes up when copies are deleted, so a write under way doesn't bring one back

// ---- details asked for ahead ----------------------------------------------------------------------
// A Discover title's details, asked for while its card is on screen and the device is idle, so its sheet opens at once.
// Two at a time at most, kept for a short while, and dropped with the lists (a title change, signing out).
const DETAILS_FRESH = 15 * 60000;
const DETAILS_MAX = 60;
export const details = {
  held: new Map(),   // url -> { at, p }
  queue: [],
  busy: 0,

  load(url) {
    const had = this.held.get(url);
    if (had && Date.now() - had.at < DETAILS_FRESH) return had.p;
    const entry = { at: Date.now(), p: api(url) };
    this.held.set(url, entry);
    entry.p.catch(() => { if (this.held.get(url) === entry) this.held.delete(url); });   // asked for again next time
    while (this.held.size > DETAILS_MAX) this.held.delete(this.held.keys().next().value);
    return entry.p;
  },

  ahead(url) {
    if (this.held.has(url) || this.queue.includes(url)) return;
    this.queue.push(url);
    this.next();
  },

  next() {
    while (this.busy < 2 && this.queue.length) {
      this.busy += 1;
      // Failing here is fine: the sheet asks again when it opens, and says so if it still can't.
      this.load(this.queue.shift()).catch(() => null).finally(() => { this.busy -= 1; this.next(); });
    }
  },

  clear() { this.held.clear(); this.queue.length = 0; },
};
