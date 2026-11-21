// Usher — updates. The server writes its own build (the version and a fingerprint of the app's files) over the placeholder
// when it serves this file (usher/stamp.py), so BUILD is the build of the server that sent it and no pull request edits it.
export const BUILD = "__USHER_BUILD__";
export const updater = {
  // True when the server runs a different build of Usher from the one on screen, unless a reload to that
  // build already happened this session (so a mismatch can never loop).
  stale(serverBuild) {
    if (!serverBuild || serverBuild === BUILD) return false;
    try { return sessionStorage.getItem("usher.reloadedFor") !== serverBuild; } catch { return true; }
  },
  // Drop the cached app files, ask for the new service worker, then reload from the server.
  async apply(serverBuild) {
    try { sessionStorage.setItem("usher.reloadedFor", serverBuild || ""); } catch { /* ignore */ }
    try {
      const reg = await navigator.serviceWorker?.getRegistration();
      if (reg) await Promise.race([reg.update(), new Promise((r) => setTimeout(r, 4000))]);
      if (window.caches) await Promise.all((await caches.keys()).map((k) => caches.delete(k)));
    } catch { /* reload anyway */ }
    location.reload();
  },
};
