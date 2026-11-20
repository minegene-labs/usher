// Usher — Settings → Device: notifications and their options.
import { SignedOut, api, store } from "./data.js";
import { h } from "./dom.js";
import { fill } from "./util.js";
import { push } from "./device.js";

// What is wrong and the one step that fixes it, for each reason push.problem() can give.
const PROBLEMS = {
  insecure: ["Notifications need a secure address", "This browser only allows notifications on addresses that start with https://. Open Usher from its https:// address, then come back here."],
  install: ["Add Usher to your Home Screen first", "On iPhone and iPad, notifications only work from the Home Screen app. In Safari, tap Share, then Add to Home Screen, and open Usher from there."],
  unsupported: ["This browser can't show notifications", "Open Usher in an up-to-date version of Safari, Chrome, Edge or Firefox, or update this device, then come back here."],
};

export const Notify = (Base) => class extends Base {
  // ---- notification settings ------------------------------------------------------------------
  notifySection() {
    const body = h("div", { class: "notify-settings" });
    // Coming back to Settings draws the last known rows at once, so the group keeps its size while the answer is
    // fetched again; "Checking…" is only for the very first look.
    if (this.notifyLast) this.drawNotify(body, this.notifyLast.data, this.notifyLast.here);
    else body.append(h("p", { class: "footnote" }, "Checking…"));
    this.fillNotify(body);
    return body;
  }

  async fillNotify(body) {
    const problem = push.problem();
    if (problem) {
      const why = PROBLEMS[problem];
      body.replaceChildren(h("div", { class: "list" }, h("div", { class: "row" }, h("span", {}), h("div", { class: "text" },
        h("div", { class: "title" }, why[0]), h("div", { class: "sub wrap" }, why[1])), h("span", {}))));
      return;
    }
    let data, sub;
    try { [data, sub] = await Promise.all([api("/api/notify"), push.current()]); }
    catch (e) { if (e instanceof SignedOut) return this.signIn(); body.replaceChildren(h("p", { class: "footnote" }, e.message)); return; }
    store.notify = data;
    const here = !!sub && data.devices.some((d) => d.endpoint === sub.endpoint);
    this.notifyLast = { data, here };
    this.drawNotify(body, data, here);
  }

  drawNotify(body, data, here) {
    const denied = Notification.permission === "denied";
    const save = async (values) => {
      try { store.notify = await api("/api/notify", { method: "POST", body: { ...values, tz_offset: push.tz() } }); }
      catch (e) { this.toast(e.message); }
    };
    const toggle = (key, label, hint) => h("label", { class: `row switch-row${here ? "" : " off"}` },
      h("div", { class: "text" }, h("div", { class: "title" }, label), hint ? h("div", { class: "sub wrap" }, hint) : null),
      h("input", { type: "checkbox", switch: true, checked: data.prefs[key], disabled: !here, onchange: (e) => save({ [key]: e.target.checked }) }));
    const thisDevice = h("input", { type: "checkbox", switch: true, checked: here, disabled: denied,
      onchange: async (e) => { e.target.disabled = true; await (e.target.checked ? this.enablePush() : this.disablePush()); this.fillNotify(body); } });
    // One group: the switch for this device is its first row and the options follow, greyed and locked while it is off.
    fill(body,
      h("div", { class: "list" },
        h("label", { class: "row switch-row" }, h("div", { class: "text" }, h("div", { class: "title" }, "Notifications on this device"),
          h("div", { class: "sub wrap" }, denied ? "Turned off in the Settings app, under Notifications → Usher. Turn them on there first."
            : here ? `On${data.devices.length > 1 ? `, and on ${data.devices.length - 1} other device${data.devices.length > 2 ? "s" : ""}` : ""}.` : "Off on this device.")), thisDevice),
        toggle("ready", "Ready to watch", "When something you asked for, or a new episode, is ready."),
        toggle("problem", "Download problems", null),
        toggle("space", "Storage getting full", "At most once a day."),
        toggle("server", "Server needs attention", "When part of the server has been unhappy for half an hour.")),
      here ? h("button", { class: "link-btn", onclick: async () => {
        try { const r = await api("/api/push/test", { method: "POST" }); this.toast(r.sent ? "Sent. It should arrive in a few seconds." : "No devices to send to."); }
        catch (e) { this.toast(e.message); }
      } }, "Send a test notification") : null);
  }

  async enablePush() {
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") { this.toast("Notifications weren't allowed. You can change this in the Settings app, under Notifications → Usher."); return false; }
      const { key } = await api("/api/push/key");
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: push.key(key) });
      store.notify = await api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON(), tz_offset: push.tz() } });
      this.toast("Notifications are on for this device.");
      return true;
    } catch (e) { this.toast(e.message || "Couldn't turn on notifications."); return false; }
  }

  async disablePush() {
    try {
      const sub = await push.current();
      if (sub) { await api("/api/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } }); await sub.unsubscribe(); }
    } catch (e) { this.toast(e.message); }
  }

  // Shown on a request's journey: a one-tap way to be told when it's ready.
  notifyPrompt() {
    if (this.promptDismissed || push.problem() || Notification.permission !== "default") return null;
    return h("div", { class: "prompt" },
      h("div", { class: "title" }, "Want a notification when it's ready?"),
      h("div", { class: "actions-row" },
        h("button", { class: "btn", onclick: async () => { this.promptDismissed = true; await this.enablePush(); this.sheet?.refresh(); } }, "Notify me"),
        h("button", { class: "btn plain", onclick: () => { this.promptDismissed = true; this.sheet?.refresh(); } }, "Not now")));
  }
};
