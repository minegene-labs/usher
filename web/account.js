// Usher — sign-in, sign-out and being away from home.
import { SignedOut, api, lists, store } from "./data.js";
import { h, icon } from "./dom.js";
import { away } from "./device.js";

export const Account = (Base) => class extends Base {
  // ---- account ------------------------------------------------------------------------------
  async signOut() {
    try { await api("/api/logout", { method: "POST" }); } catch { /* signed out either way */ }
    if ("clearAppBadge" in navigator) navigator.clearAppBadge().catch(() => {});
    this.signIn();
  }

  async signOutOthers() {
    try {
      await api("/api/sessions/sign-out-others", { method: "POST" });
      await lists.load("me");
      this.render(false);
    } catch (e) { this.handle(e); }
  }

  handle(e) {
    if (e instanceof SignedOut) return this.signIn();
    store.online = false;
    this.render(false);
  }

  // Every way of being signed out ends here (Sign out, a session ended on the server, another device signing this one
  // out), so this is where everything of the account is let go: lists in memory, saved copies and artwork.
  signIn() {
    this.events?.close(); this.events = null;   // so no live update brings anything back
    lists.clear(); store.snap = null;
    history.replaceState({}, "", "/");
    const error = h("p", { class: "error", role: "alert" });
    const user = h("input", { name: "username", autocomplete: "username", placeholder: "Username", "aria-label": "Username", autocapitalize: "none", autocorrect: "off", spellcheck: "false", required: true });
    const pass = h("input", { name: "password", type: "password", autocomplete: "current-password", placeholder: "Password", "aria-label": "Password", required: true });
    const btn = h("button", { class: "btn", type: "submit" }, "Sign in");
    const form = h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!user.value.trim() || !pass.value) { error.textContent = "Enter your username and password."; return; }
        btn.disabled = true; error.textContent = "";
        try {
          lists.set("me", await api("/api/login", { method: "POST", body: { username: user.value.trim(), password: pass.value } }));
          this.start();
        } catch (err) {
          error.textContent = err instanceof SignedOut ? "That username or password isn't right." : err.message;
          btn.disabled = false; pass.select();
        }
      },
    }, h("img", { src: "/icons/icon-192.png", alt: "" }), h("h1", {}, "Usher"), h("p", {}, "Sign in to see what's ready to watch."),
    h("div", { class: "fields" }, user, pass), error, btn);
    this.replaceChildren(h("main", { class: "signin" }, form));
    user.focus();
    this.checkSetup();   // a server with no account yet swaps this for the setup page
  }

  // Shown when Usher opens but can't reach the server: almost always Tailscale being off away from home.
  // It keeps trying by itself, so it clears as soon as Tailscale connects.
  offline() {
    const first = !this.offlineShown;
    this.offlineShown = true;
    if (!first && this.querySelector(".away")) return this.scheduleRetry();
    this.replaceChildren(h("main", { class: "signin away" }, h("div", { class: "empty" }, icon("wifi"), h("h3", {}, "Can't reach home"),
      h("p", {}, "Usher works at home, or anywhere with Tailscale turned on."),
      h("div", { class: "away-actions" },
        h("a", { class: "btn", href: away.connectLink() }, "Connect Tailscale"),
        h("button", { class: "btn plain", onclick: () => this.boot() }, "Try again")),
      h("p", { class: "footnote" }, "Usher checks again by itself every few seconds."),
      away.help())));
    this.scheduleRetry();
  }

  scheduleRetry() {
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => { if (this.offlineShown && document.visibilityState === "visible") this.boot(); }, 5000);
  }
};
