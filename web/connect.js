// Usher — the Connect your apps step of setup: for each app its address and key, a Test button that says whether Usher
// supports the version found, and Save. The keys are sent to the server and never shown again.
import { api } from "./data.js";
import { h } from "./dom.js";

const GOOD = ["supported", "untested"];   // a version Usher works with; "untested" carries a note

export const Connect = (Base) => class extends Base {
  // After the first account: connect the apps, or leave it for later. Anything that goes wrong here opens the app.
  async connectApps() {
    try { await this.connectStep(); } catch { this.start(); }   // a screen that can't be drawn must not strand a new account
  }

  async connectStep() {
    const apps = (await api("/api/connect")).apps;
    const rows = apps.map((app) => this.connectRow(app));
    const note = h("p", { class: "connect-note", role: "status" });
    const next = h("button", { class: "btn plain", type: "button", onclick: () => this.start() }, "Skip for now");
    const save = h("button", { class: "btn", type: "button" }, "Save");
    save.addEventListener("click", async () => {
      const done = rows.filter((r) => r.good());
      if (!done.length) { note.textContent = "Test an app first. Only apps that connect are saved."; return; }
      save.disabled = true; note.textContent = "";
      const settings = {}; const secrets = {};
      for (const r of done) Object.assign(settings, r.settings()), Object.assign(secrets, r.secrets());
      try {
        await api("/api/settings", { method: "POST", body: { settings, secrets } });
        note.textContent = "Saved. Usher uses these after it next restarts.";
        next.textContent = "Continue";
      } catch (err) {
        note.textContent = err.message;
      }
      save.disabled = false;
    });
    this.replaceChildren(h("main", { class: "signin connect" }, h("div", { class: "connect-page" },
      h("h1", {}, "Connect your apps"),
      h("p", {}, "Usher works with the apps you already run. Enter where each one is and its key, then press Test. You can skip this and do it later."),
      ...rows.map((r) => r.el), note, h("div", { class: "connect-actions" }, save, next))));
  }

  connectRow(app) {
    const field = (props) => h("input", { autocomplete: "off", autocapitalize: "none", autocorrect: "off", spellcheck: "false", ...props });
    const first = app.suggestions[0];
    const url = app.hasAddress ? field({ name: `${app.id}-url`, inputmode: "url", "aria-label": `${app.label} address`, placeholder: "Address", value: first || app.url, list: `${app.id}-found` }) : null;
    const user = app.hasUser ? field({ name: `${app.id}-user`, "aria-label": `${app.label} username`, placeholder: "Username", value: app.user }) : null;
    const key = field({ name: `${app.id}-key`, type: "password", "aria-label": `${app.label} ${app.credential}`, placeholder: app.keySaved ? `${app.credential} saved, leave blank to keep it` : app.credential[0].toUpperCase() + app.credential.slice(1) });
    const found = app.hasAddress ? h("datalist", { id: `${app.id}-found` }, app.suggestions.map((s) => h("option", { value: s }))) : null;
    const result = h("p", { class: "connect-result", role: "status" });
    const button = h("button", { class: "btn small", type: "button" }, "Test");
    let last = null;   // the answer to the latest Test, for the address and key as they were then
    const edited = () => { last = null; result.className = "connect-result"; result.textContent = ""; };   // what was tested is no longer what is typed
    for (const input of [url, user, key]) input?.addEventListener("input", edited);
    button.addEventListener("click", async () => {
      button.disabled = true; last = null; result.className = "connect-result"; result.textContent = "Testing…";
      try {
        last = await api("/api/connect/test", { method: "POST", body: { app: app.id, url: url?.value ?? "", key: key.value, user: user?.value ?? "" } });
        if (url && last.url) url.value = last.url;
        result.textContent = last.message;
        result.className = `connect-result ${GOOD.includes(last.verdict) ? last.verdict : "problem"}`;
      } catch (err) {
        result.textContent = err.message;
        result.className = "connect-result problem";
      }
      button.disabled = false;
    });
    const el = h("section", { class: "connect-app" },
      h("h2", {}, app.label, app.optional ? h("span", {}, " (optional)") : null),
      h("p", { class: "connect-find" }, app.find),
      h("div", { class: "fields" }, url, user, key), found, h("div", { class: "connect-test" }, button, result));
    return {
      el,
      good: () => !!last && GOOD.includes(last.verdict),
      settings: () => ({ ...(app.setting ? { [app.setting]: last.url } : {}), ...(user && app.userSetting ? { [app.userSetting]: user.value.trim() } : {}) }),
      secrets: () => (key.value.trim() ? { [app.secret]: key.value.trim() } : {}),
    };
  }
};
