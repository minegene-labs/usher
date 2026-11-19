// Usher — the setup page: the one-time code from Usher's log, and the first account, who becomes the admin. Connecting
// the apps follows (connect.js).
import { api, lists } from "./data.js";
import { h } from "./dom.js";

export const Setup = (Base) => class extends Base {
  // Asked once the sign-in page is up. Only a server with no account yet says setup is open; every other one answers
  // "not found", so this is a quiet no-op for everyone else.
  async checkSetup() {
    try { await api("/api/setup"); } catch { return; }
    this.setup();
  }

  setup() {
    const field = (props) => h("input", { required: true, ...props });
    const code = field({ name: "code", class: "code", placeholder: "Setup code", "aria-label": "Setup code", autocomplete: "off", autocapitalize: "characters", autocorrect: "off", spellcheck: "false" });
    const name = field({ name: "name", autocomplete: "name", placeholder: "Your name", "aria-label": "Your name" });
    const user = field({ name: "username", autocomplete: "username", placeholder: "Username", "aria-label": "Username", autocapitalize: "none", autocorrect: "off", spellcheck: "false" });
    const pass = field({ name: "password", type: "password", autocomplete: "new-password", placeholder: "Password (at least 12 characters)", "aria-label": "Password" });
    const error = h("p", { class: "error", role: "alert" });
    const btn = h("button", { class: "btn", type: "submit" }, "Create account");
    const form = h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!code.value.trim() || !user.value.trim() || !pass.value) { error.textContent = "Fill in every field."; return; }
        btn.disabled = true; error.textContent = "";
        try {
          lists.set("me", await api("/api/setup", { method: "POST", body: { code: code.value.trim(), name: name.value.trim(), username: user.value.trim(), password: pass.value } }));
          await this.connectApps();
        } catch (err) {
          error.textContent = err.message;
          btn.disabled = false;
        }
      },
    }, h("img", { src: "/icons/icon-192.png", alt: "" }), h("h1", {}, "Set up Usher"),
    h("p", {}, "Enter the setup code from Usher's log, then choose the first account. It becomes the admin."),
    h("div", { class: "fields" }, code, name, user, pass), error, btn);
    this.replaceChildren(h("main", { class: "signin" }, form));
    code.focus();
  }
};
