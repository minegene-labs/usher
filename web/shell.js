// Usher — the app element's start-up, live updates and screen switching.
import { SignedOut, api, lists, store } from "./data.js";
import { h, icon } from "./dom.js";
import { away } from "./device.js";
import { whenIdle } from "./prefetch.js";
import { updater } from "./updater.js";
import { initials } from "./util.js";

// The round account picture: the person's initials, or a person glyph when the name has no letters. The button around it
// carries the name for VoiceOver, so the picture itself is hidden from it.
function avatar() {
  const letters = initials(store.me.name);
  return h("span", { class: "avatar", "aria-hidden": "true" }, letters || icon("person"));
}

export const SCREENS = [
  { path: "/", name: "Home", icon: "home" },
  { path: "/search", name: "Search", icon: "search" },
  { path: "/downloads", name: "Downloads", icon: "downloads" },
  { path: "/upcoming", name: "Upcoming", icon: "upcoming" },
  { path: "/library", name: "Library", icon: "library" },
  { path: "/status", name: "Status", icon: "status" },
];

// Screens pushed over Status, by the name kept in history.state.pushed (a stack, bottom first). The title names the screen
// in its own bar and on the back button of the one above it.
const PUSHED_TITLES = { settings: "Settings", changelog: "Changelog" };

export const Shell = (Base) => class extends Base {
  pushed = [];   // names of the screens pushed over Status, bottom first

  connectedCallback() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
      navigator.serviceWorker.addEventListener("message", (e) => { if (e.data?.type === "open") this.openUrl(e.data.url); });
    }
    addEventListener("popstate", () => this.show(location.pathname, false));
    addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key >= "1" && e.key <= String(SCREENS.length)) {
        e.preventDefault();
        this.go(SCREENS[Number(e.key) - 1].path);
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return void lists.write();   // it may be closed in the background
      if (this.offlineShown) return this.boot();     // back from the Shortcuts or Tailscale app
      if (!store.me) return;
      if (!this.events) this.listen();
      this.checkVersion(true);
    });
    lists.redraw = (name) => this.listLoaded(name);
    lists.failed = (name, e) => this.listFailed(name, e);
    this.boot();
  }

  async boot() {
    try {
      await lists.load("me", { timeout: 6000 });
      this.offlineShown = false;
      if (updater.stale(store.me.version)) return updater.apply(store.me.version);
      // The last copies of this account's lists and Home, so the first screen paints at once; never held up for long.
      await Promise.race([lists.restore(), new Promise((resolve) => setTimeout(resolve, 300))]);
      this.start();
    } catch (e) {
      if (e instanceof SignedOut) this.signIn();
      else this.offline();
    }
  }

  start() {
    this.listen();
    this.layout();
    this.show(location.pathname, false);
    if (location.search) { this.openUrl(location.href); history.replaceState({}, "", location.pathname); }
  }

  // Brought back to the screen: reload straight away if nothing is open, otherwise offer it.
  async checkVersion(resumed = false) {
    let me;
    try { me = await lists.load("me"); } catch { return; }
    if (!updater.stale(me.version)) return;
    if (resumed && !this.sheet && !document.activeElement?.matches?.("input, select, textarea")) return updater.apply(me.version);
    this.updateBanner(me.version);
  }

  updateBanner(version) {
    if (document.querySelector(".update-banner")) return;
    const bar = h("div", { class: "update-banner", role: "status" }, h("span", {}, `Usher ${version} is ready.`),
      h("button", { class: "link", onclick: () => updater.apply(version) }, "Reload"));
    document.body.append(bar);
  }

  // Opened from a notification: "/?open=request:movie:16" shows that request's journey.
  openUrl(href) {
    const url = new URL(href, location.origin);
    if (url.origin !== location.origin) return;
    const open = url.searchParams.get("open") || "";
    const path = SCREENS.some((x) => x.path === url.pathname) ? url.pathname : "/";
    if (path !== this.current) this.go(path);
    const m = open.match(/^request:(movie|show):(\d+)$/);
    if (m) setTimeout(() => this.journey(m[1], Number(m[2])), 300);
  }

  // live updates over Server-Sent Events -----------------------------------------------------
  listen() {
    this.events?.close();
    // since: the last title change this device saw, so after a reconnect it catches up on what it missed.
    const es = new EventSource(`/api/events${store.changeSeq != null ? `?since=${store.changeSeq}` : ""}`);
    this.events = es;
    // A title changed (got, removed, tidied, moved, watched, here or on another device, or in the apps
    // themselves): drop this device's copies of it before the new state arrives.
    es.addEventListener("changed", (e) => this.titlesChanged(JSON.parse(e.data)));
    es.addEventListener("state", (e) => {
      store.snap = JSON.parse(e.data);
      store.online = true;
      lists.save("snap", store.snap);
      store.changeSeq ??= store.snap.changes;
      this.updateBadges();
      if (this.current === "/") this.liveHome();
      if (this.current === "/downloads") this.liveDownloads();
      if (this.current === "/status" && !this.pushed.length) {
        // Only redraw Status when its own facts change, so coming back to the app doesn't flash. A screen pushed over it
        // (Settings, Changelog) is never redrawn by a live update, so nothing there is reset mid-tap.
        if (this.statusShape() !== this.lastStatus) this.render(false);
      }
      if (this.sheet?.live) this.sheet.refresh();
    });
    es.addEventListener("signed-out", () => { es.close(); this.events = null; this.signIn(); });
    es.onerror = async () => {
      if (es.readyState !== EventSource.CLOSED) return; // the browser is retrying by itself
      this.events = null;
      try { await lists.load("me"); setTimeout(() => this.listen(), 3000); this.checkVersion(); }
      catch (err) {
        if (err instanceof SignedOut) return this.signIn();
        store.online = false; this.render(false); setTimeout(() => this.listen(), 10000);
      }
    };
  }

  // What Status shows, minus the sizes that move by a few bytes.
  statusShape() {
    return JSON.stringify(store.snap?.status, (k, v) => (k === "free" || k === "bytes" ? Math.round(v / 1e9) : v));
  }

  // Every list this device keeps that shows a title, dropped and fetched again when the title changes.
  titlesChanged(n) {
    store.changeSeq = n.seq;
    const titles = n.titles || [];
    const same = (t, x) => !!x && x.kind === t.kind && ((t.key && x.key === t.key) || (t.id != null && x.id === t.id)
      || (t.ext != null && x.ext === t.ext));
    lists.changed(n);   // Library, Upcoming, Discover's have/asked marks; All upcoming when everything changed
    const seasons = this.seasonCache || {};
    for (const id of Object.keys(seasons)) {
      if (n.all || titles.some((t) => t.kind === "show" && t.id === Number(id))) delete seasons[id];
    }
    // An open details sheet for that title: closed if the title has gone, otherwise drawn again.
    const open = this.sheet?.title;
    if (open && (n.all || titles.some((t) => same(t, open)))) {
      if (n.what === "removed") {
        this.sheet.dialog.close();
        this.toast(`${open.name || "That"} was removed.`);
      } else this.sheet.refresh();
    }
    // Search: look again, so "In your library" and Get it are right (the server has dropped its old lookup).
    if (this.current === "/search" && this.searchResults?.results?.length && !this.searching
        && (n.all || this.searchResults.results.some((r) => titles.some((t) => same(t, { kind: r.kind, id: r.libId, ext: r.ext }))))) {
      this.runSearch();
    }
    for (const name of ["library", "upcoming", "discover", "tidy"]) this.listLoaded(name);   // on screen: fetched again
  }

  // The loader (data.js) has a new copy of a list, or is due another try: redraw the screen that shows it.
  listLoaded(name) {
    const on = { library: "/library", upcoming: "/upcoming", upcomingAll: "/upcoming", discover: "/search", tidy: "/status" }[name];
    if (!on || this.current !== on || !this.main?.isConnected || (name === "tidy" && this.pushed.length)) return;
    if (name !== "discover") this.render(false);
    else if (!this.searchResults) this.showResults();
  }

  // A list a screen is waiting for couldn't be loaded. Discover just shows the Search introduction instead.
  listFailed(name, e) {
    if ((name === "discover" || name === "tidy") && !(e instanceof SignedOut)) return this.listLoaded(name);   // the row falls back to its plain description
    this.handle(e);
  }

  updateBadges() {
    const n = this.unreadCount();
    const unseen = this.downloadsUnseen();
    for (const b of this.querySelectorAll("[data-badge]")) {
      const count = b.dataset.badge === "downloads" ? unseen : n;
      b.textContent = count > 99 ? "99+" : count; b.hidden = count === 0;
      b.setAttribute("aria-label", b.dataset.badge === "downloads" ? `${count} new in Downloads` : `${count} ready to watch`);
    }
    if ("setAppBadge" in navigator) (n ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => {});
  }

  // shell: sidebar + tab bar ---------------------------------------------------------------
  layout() {
    const link = (s, cls) => h("a", { class: cls, href: s.path, onclick: (e) => { e.preventDefault(); this.go(s.path); } },
      icon(s.icon), h("span", {}, s.name),
      s.path === "/" || s.path === "/downloads" ? h("span", { class: "badge", "data-badge": s.path === "/" ? "home" : "downloads", hidden: true }) : null);
    this.main = h("main", { class: "main", id: "main" });
    this.replaceChildren(h("div", { class: "shell" },
      h("aside", { class: "sidebar" },
        h("div", { class: "brand" }, h("img", { src: "/icons/icon-192.png", alt: "" }), "Usher"),
        h("nav", { "aria-label": "Screens" }, SCREENS.map((s) => link(s, "side-link"))),
        // On a wide screen the way into Settings is where people look for their account, as in iPad apps.
        h("button", { class: "who", "aria-label": `Signed in as ${store.me.name}, opens Settings`, onclick: () => this.openSettings() },
          avatar(), h("span", {}, `Signed in as ${store.me.name}`))),
      this.main,
      h("nav", { class: "tabbar", "aria-label": "Screens" }, SCREENS.map((s) => link(s, "tab")))));
    this.updateBadges();
  }

  go(path) {
    if (path === this.current) { this.main.scrollTo({ top: 0, behavior: "smooth" }); return; }
    history.pushState({}, "", path);
    this.show(path, true);
  }

  // If the screen can't be drawn, the stack and history go back to what they were: a stack that kept the name would make
  // openSettings() ignore every later tap.
  pushScreen(name) {
    const below = this.pushed;
    history.pushState({ pushed: [...this.pushed, name] }, "", "/status");
    this.pushed = [...this.pushed, name];
    try {
      this.render(true);
    } catch (e) {
      console.error(`Couldn't draw ${name}`, e);
      this.pushed = below;
      history.back();
      this.render(true);
      this.toast(`Couldn't open ${PUSHED_TITLES[name]}.`);
      return;
    }
    this.main.scrollTo(0, 0);
  }

  // Back to the screen below: the browser's own back when the stack is in the history, otherwise by hand.
  popScreen() {
    if (history.state?.pushed?.length) return history.back();
    this.pushed = this.pushed.slice(0, -1);
    this.render(true);
  }

  show(path, animate) {
    this.current = SCREENS.some((s) => s.path === path) ? path : "/";
    const kept = [history.state?.pushed].flat().filter((name) => name in PUSHED_TITLES);   // the stack stays across back, forward and reload
    this.pushed = this.current === "/status" ? kept : [];
    for (const a of this.querySelectorAll(".tab, .side-link")) {
      a.toggleAttribute("aria-current", a.getAttribute("href") === this.current);
      if (a.getAttribute("href") === this.current) a.setAttribute("aria-current", "page");
    }
    const swap = () => { this.render(true); this.main.scrollTo(0, 0); };
    if (animate && document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      document.startViewTransition(swap);
    } else swap();
  }

  // The other tabs' lists, fetched while the device is idle, so switching to one paints at once. One with a copy to
  // show is left to refresh when its screen opens; one that can't be had is asked for again (and explained) there.
  aheadLists() {
    if (this.aheadDue) return;
    this.aheadDue = true;
    whenIdle(() => {
      this.aheadDue = false;
      if (!store.me) return;
      for (const name of ["library", "upcoming", "discover"]) if (!lists.usable(name)) lists.load(name).catch(() => null);
    });
  }

  render(full) {
    this.aheadLists();
    const screen = SCREENS.find((s) => s.path === this.current);
    document.title = screen.path === "/" ? "Usher" : `${PUSHED_TITLES[this.pushed.at(-1)] || screen.name} · Usher`;
    if (this.current === "/status") this.lastStatus = this.statusShape();   // so a live update with nothing new leaves it alone
    const top = this.pushed.at(-1);
    const back = PUSHED_TITLES[this.pushed.at(-2)] || screen.name;   // the back button names the screen below, as in iOS
    const pushed = { settings: () => this.settingsView(back), changelog: () => this.changelogView(back) }[top]?.() || null;   // a screen pushed over Status, with a back button
    const body = pushed?.body || { "/": () => this.home(), "/search": () => this.searchScreen(), "/downloads": () => this.downloads(), "/upcoming": () => this.upcoming(), "/library": () => this.library(), "/status": () => this.status() }[this.current]();
    if (!full && this.main.firstChild) {
      // Replacing the content can let the browser clamp or reset the scroll position; only the top bar and switching screens move it.
      const top = this.main.scrollTop;
      this.main.querySelector(".content")?.replaceWith(body);
      if (this.main.scrollTop !== top) this.main.scrollTop = top;
      return;
    }
    const name = pushed?.title || screen.name;
    const small = h("div", { class: "small", "aria-hidden": "true" }, name);
    // Status has the account button at the top right, as the App Store and Apple TV do; it opens Settings.
    const account = !pushed && this.current === "/status"
      ? h("button", { class: "nav-action", "aria-label": `Signed in as ${store.me.name}, opens Settings`, onclick: () => this.openSettings() }, avatar()) : null;
    const bar = h("div", { class: "navbar" }, pushed?.back || null, small, account);
    const title = h("h1", { class: "large-title" }, name);
    this.main.replaceChildren(bar, h("div", { class: "page" }, title, body));
    this.observer?.disconnect();
    this.observer = new IntersectionObserver(([e]) => bar.classList.toggle("scrolled", !e.isIntersecting), { rootMargin: "-50px 0px 0px 0px" });
    this.observer.observe(title);
  }

  offlineBanner() {
    return store.online ? null : h("div", { class: "banner offline", role: "status" }, icon("wifi"),
      h("span", {}, "Can't reach home. If you're out, turn on Tailscale. ",
        h("a", { class: "link", href: away.connectLink() }, "Connect")));
  }
};
