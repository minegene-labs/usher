// Usher — what this device can do: notifications support, and the away-from-home help.
import { h } from "./dom.js";
// ---- notifications support ------------------------------------------------------------------
export const push = {
  supported: () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window,
  ios: () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1),
  installed: () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true,
  async current() {
    if (!push.supported()) return null;
    const reg = await navigator.serviceWorker.ready;
    return reg.pushManager.getSubscription();
  },
  key(b64) {
    const raw = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  },
  // Why notifications can't work here, or null when they can. A page on a plain http:// address (other than this
  // computer) gets no service worker at all, so it is checked first: adding to the Home Screen wouldn't fix it.
  problem() {
    if (window.isSecureContext === false) return "insecure";
    if (push.supported() && (!push.ios() || push.installed())) return null;
    return push.ios() && !push.installed() ? "install" : "unsupported";
  },
  tz: () => -new Date().getTimezoneOffset(),
};

// ---- away from home: Usher is only reachable on the home network or through Tailscale ------------
// Safari can't tell whether Tailscale is on, so Usher just notices it can't reach the server, says the likely
// fix, and offers a button that runs a Shortcut (Tailscale's own Connect action, then back to Usher).
export const away = {
  shortcut: "Usher Connect",
  connectLink: () => `shortcuts://run-shortcut?name=${encodeURIComponent(away.shortcut)}`,
  // The link that installs the ready-made Shortcut, when the server has one set; null otherwise.
  installLink: (me) => (me?.connectShortcut ? me.connectShortcut : null),
  help() {
    return h("details", { class: "away-help" }, h("summary", {}, "Set up the Connect button (once per device)"),
      h("ol", {},
        h("li", {}, "Open the Shortcuts app and make a new shortcut."),
        h("li", {}, "Add Tailscale's Connect action."),
        h("li", {}, "Add Wait, for 3 seconds, so Tailscale has time to connect."),
        h("li", {}, "Add Open URLs, with ", h("strong", {}, location.origin), ". It reopens Usher once the connection is up."),
        h("li", {}, `Name the shortcut ${away.shortcut}.`)),
      h("p", {}, "Better still: in Tailscale's settings, turn on VPN On Demand, connecting on every Wi-Fi except home's and on mobile data. Then this message rarely appears."));
  },
};
