// Usher — updates: BUILD must match usher/__init__.py and sw.js (a test checks).
// ---- updates: this file's version must match usher/__init__.py and sw.js (a test checks) ------
export const BUILD = "1.4.9";
export const updater = {
  // True when the server runs a different Usher from the one on screen, unless a reload to that
  // version already happened this session (so a mismatch can never loop).
  stale(serverVersion) {
    if (!serverVersion || serverVersion === BUILD) return false;
    try { return sessionStorage.getItem("usher.reloadedFor") !== serverVersion; } catch { return true; }
  },
  // Drop the cached app files, ask for the new service worker, then reload from the server.
  async apply(serverVersion) {
    try { sessionStorage.setItem("usher.reloadedFor", serverVersion || ""); } catch { /* ignore */ }
    try {
      const reg = await navigator.serviceWorker?.getRegistration();
      if (reg) await Promise.race([reg.update(), new Promise((r) => setTimeout(r, 4000))]);
      if (window.caches) await Promise.all((await caches.keys()).map((k) => caches.delete(k)));
    } catch { /* reload anyway */ }
    location.reload();
  },
};
