// Usher — Settings: a screen pushed over Status (the account button at its top right, or the sidebar's footer on a wide screen), laid out like
// iOS Settings: who you are first, Sign out last.
import { api, store } from "./data.js";
import { h, icon } from "./dom.js";
import { titleRow } from "./rows.js";
import { fmt, prefs } from "./util.js";
import { OPEN_IN } from "./watch.js";
import { BUILD, updater } from "./updater.js";
import { away, push } from "./device.js";

export const Settings = (Base) => class extends Base {
  // Settings is pushed over Status, so from any other screen (the sidebar's footer) Status comes first and back returns to it.
  openSettings() {
    if (this.pushed.includes("settings")) return;
    if (this.current !== "/status") {
      history.pushState({}, "", "/status");
      this.show("/status", false);
    }
    this.pushScreen("settings");
  }

  // What render() draws while Settings is open: the title, the back button (named for the screen below) and the body.
  settingsView(backLabel) {
    const back = h("button", { class: "nav-back", onclick: () => this.popScreen() }, icon("chevron"), backLabel);
    return { title: "Settings", back, body: h("div", { class: "content settings" }, this.offlineBanner(), this.settingsGroups()) };
  }

  settingsGroups() {
    return [this.accountSection(), this.deviceSection(), this.devicesSection(), this.aboutSection(), this.advancedSection() ?? "", this.signOutSection()];
  }

  accountSection() {
    const me = store.me;
    return h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Account")),
      h("div", { class: "list" },
        h("div", { class: "row" }, h("span", {}), h("div", { class: "text" }, h("div", { class: "title" }, me.name), h("div", { class: "sub" }, `Signed in as ${me.username}`)), h("span", {})),
        h("button", { class: "row", onclick: () => this.opinionsSheet() }, h("span", { "aria-hidden": "true" }),
          h("div", { class: "text" }, h("div", { class: "title" }, "Likes and dislikes"), h("div", { class: "sub" }, "What Picked for you follows, and what Discover leaves out")),
          h("span", { class: "chev" }, icon("chevron")))));
  }

  // Every title this account liked or disliked, newest first, each with Undo. Only this account's own. A page at a
  // time (Show more for the next), so opening it never asks for every title's details at once. Each page starts after
  // the last title of the one before (`next`, from the server), so an Undo meanwhile never shifts it.
  opinionsSheet() {
    let items = [], next = "", loading = false, error = null, focusTo = null;   // next: "" the first page, null no more
    let title = null;
    const undos = [];          // the Undo buttons in the order drawn, so focus can move to the one now in a removed row's place
    const undoOf = new Map();  // title -> its Undo button, as last drawn
    // Drawing replaces the buttons, so focus is put back afterwards: VoiceOver stays in the list rather than at the top.
    const refresh = () => {
      this.sheet?.refresh();
      focusTo?.()?.focus();
      focusTo = null;
    };
    const load = async (moveFocus) => {
      if (loading || next === null) return;
      loading = true;
      try {
        const d = await api(next ? `/api/opinions?after=${encodeURIComponent(next)}` : "/api/opinions");
        items = [...items, ...d.items]; next = d.next; error = null;
        if (moveFocus && d.items.length) focusTo = () => undoOf.get(d.items[0]) ?? title;   // the first title of the new page
      } catch (e) { error = e.message; }
      loading = false;
      refresh();
    };
    const undo = async (it, button) => {
      button.disabled = true;
      try {
        await this.setOpinion(it, "clear");
        const at = undos.indexOf(button);
        focusTo = () => undos[Math.min(at, undos.length - 1)] ?? title;
        items = items.filter((x) => x !== it);
        refresh();
        if (!items.length && next !== null) load(false);   // every one shown was undone: the next page, if any
      } catch (e) { this.toast(e.message); button.disabled = false; }
    };
    const row = (it) => {
      const name = it.title || (it.kind === "movie" ? "A movie" : "A TV show");
      const button = h("button", { class: "btn plain small", "aria-label": `Undo ${it.opinion} of ${name}`, onclick: () => undo(it, button) }, "Undo");
      undos.push(button);
      undoOf.set(it, button);
      return titleRow({ art: it.art, title: name, subtitle: [it.year, it.kind === "movie" ? "Movie" : "TV show"].filter(Boolean).join(" · "), trailing: button });
    };
    const group = (which, name, note) => {
      const list = items.filter((it) => it.opinion === which);
      return list.length ? [h("h3", { class: "sheet-subtitle" }, name), h("p", { class: "footnote" }, note), h("div", { class: "list" }, list.map(row))] : null;
    };
    this.openSheet(() => {
      undos.length = 0;
      undoOf.clear();
      title = h("h2", { id: "sheet-title", class: "sheet-title", tabindex: "-1" }, "Likes and dislikes");
      if (error && !items.length) {
        // Changed in place rather than redrawn, so focus stays on the button while the page loads.
        const retry = h("button", { class: "link-btn", onclick: () => { retry.textContent = "Loading…"; focusTo = () => title; load(false); } }, loading ? "Loading…" : "Try again");
        return [title, h("p", { class: "empty" }, error), retry];
      }
      if (!items.length) return [title, h("p", { class: "empty" }, loading || next === "" ? "Loading…" : "Nothing yet. Open a title in Search or Discover and tap Like or Dislike.")];
      return [title, h("p", { class: "footnote" }, "Only you see these."),
        group("like", "Liked", "Picked for you in Discover follows these."),
        group("dislike", "Disliked", "Kept out of Discover for you. Search still finds them."),
        error ? h("p", { class: "footnote" }, error) : null,
        next !== null ? h("button", { class: "link-btn", onclick: () => load(true) }, loading ? "Loading…" : "Show more") : null];
    });
    load(false);
  }

  // Settings that only apply to this device (kept here, not on the server).
  deviceSection() {
    return h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Device")),
      h("div", { class: "group-head" }, h("h3", {}, "Notifications")),
      this.notifySection(),
      h("div", { class: "group-head" }, h("h3", {}, "Open titles in")),
      h("div", { class: "list" }, this.openInRow()),
      this.connectShortcutRow());
  }

  // One tap installs the Shortcut the Connect button runs. Only offered on iPhone and iPad, and only when the server has a link for it.
  connectShortcutRow() {
    const link = away.installLink(store.me);
    if (!link || !push.ios()) return null;
    return [h("div", { class: "group-head" }, h("h3", {}, "Away from home")),
      h("div", { class: "list" }, h("button", { class: "row", onclick: () => { location.href = link; } }, h("span", { "aria-hidden": "true" }),
        h("div", { class: "text" }, h("div", { class: "title" }, "Add the Connect shortcut"),
          h("div", { class: "sub" }, "Connects Tailscale, then reopens Usher")),
        h("span", { class: "chev" }, icon("chevron"))))];
  }

  // Every device signed in to this account, this one marked.
  devicesSection() {
    const me = store.me;
    return h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Devices")),
      h("div", { class: "list" },
        me.devices.map((d) => h("div", { class: "row" }, h("span", {}), h("div", { class: "text" },
          h("div", { class: "title" }, d.current ? `${d.device} (this one)` : d.device),
          h("div", { class: "sub" }, `Last used ${fmt.ago(d.lastSeen * 1000)}`)), h("span", {}))),
        me.devices.length > 1 ? h("button", { class: "link-btn", onclick: () => this.signOutOthers() }, "Sign out other devices") : null));
  }

  // The label always shows in full; the control wraps below it (full width) when it does not fit beside it, and both choices stay the same size.
  // Per device, so it is kept here and not on the server: what Watch does on a title that is in Jellyfin.
  openInRow() {
    const now = prefs.get(OPEN_IN, "app") === "web" ? "web" : "app";
    const seg = (value, label) => h("button", { type: "button", "aria-pressed": String(now === value), onclick: () => { prefs.set(OPEN_IN, value); this.render(false); } }, label);
    return h("div", { class: "row choice-row" }, h("div", { class: "text" }, h("div", { class: "title" }, "Open titles in"),
      h("div", { class: "sub" }, "What Watch does on this device")),
      h("div", { class: "segmented", role: "group", "aria-label": "Open titles in" }, seg("app", "Jellyfin app"), seg("web", "Website")));
  }

  // Credits TMDb's own terms ask for: its approved logo, unaltered and smaller than Usher's, with its notice.
  aboutSection() {
    return h("section", { class: "section about" }, h("div", { class: "section-head" }, h("h2", {}, "About")),
      h("div", { class: "about-app" }, h("img", { src: "/icons/icon-192.png", alt: "", class: "about-icon" }),
        h("div", {}, h("div", { class: "title" }, "Usher"), h("div", { class: "footnote" }, `Version ${BUILD}`))),
      h("div", { class: "list" }, this.updatesRow(),
        h("button", { class: "row", onclick: () => this.openChangelog() }, h("span", { "aria-hidden": "true" }),
          h("div", { class: "text" }, h("div", { class: "title" }, "Changelog")),
          h("span", { class: "chev" }, icon("chevron")))),
      h("button", { class: "link-btn", onclick: () => updater.apply(store.me.version) }, "Reload the app"),
      h("div", { class: "about-credit" }, h("img", { src: "/icons/tmdb.svg", alt: "TMDB", class: "about-tmdb" }),
        h("p", { class: "footnote" }, "This product uses the TMDB API but is not endorsed or certified by TMDB.")),
      h("p", { class: "footnote" }, "Where to watch: streaming data by JustWatch."));
  }

  // Its own group, last and in red, as in iOS Settings.
  signOutSection() {
    return h("section", { class: "section" }, h("div", { class: "list" },
      h("button", { class: "link-btn danger", onclick: () => this.signOut() }, "Sign out")));
  }
};
