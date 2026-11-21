// Usher — Settings → About → Updates: whether the server is keeping up with new versions. Read-only, shown to every signed-in account.
import { SignedOut, api } from "./data.js";
import { h } from "./dom.js";
import { fill } from "./util.js";

// How the server's state reads. Each line says the state in words, since the dot's colour alone must not carry it.
export function updatesText(view, nowMs = Date.now()) {
  const newest = view.check?.newest;
  const release = view.release;
  if (view.state === "unknown" && release) {
    // No update script reports in (automatic updates are off): the answer comes from the daily check for a newer release.
    if (!release.enabled) return { label: "Not checking", detail: "Checking for new versions is turned off", dot: " off" };
    if (release.failed) return { label: "Can't check right now", detail: "Usher couldn't look for a new version just now", dot: " off" };
    if (release.available) return { label: "Update available", detail: `Version ${release.newest} is out`, dot: " warn", release };
    if (release.newest) return { label: "Up to date", detail: `Checked ${minutesAgo(release.at, nowMs)}`, dot: "" };
  }
  switch (view.state) {
    case "up_to_date": return { label: "Up to date", detail: `Checked ${minutesAgo(view.check.at, nowMs)}`, dot: "" };
    case "waiting": return { label: "Update waiting", detail: `Version ${newest} is out and isn't installed yet`, dot: " off" };
    case "held": return { label: "Held", detail: `Version ${newest} needs a step on the server first, so it won't install by itself`, dot: " warn" };
    case "rolled_back": return { label: "Last update rolled back", detail: `Version ${newest} didn't work, so the previous version is still running`, dot: " warn" };
    default: return { label: "Can't tell", detail: "The server hasn't reported in over two days, so it may have stopped checking", dot: " off" };
  }
}

function minutesAgo(seconds, nowMs) {
  const minutes = Math.max(0, Math.floor((nowMs / 1000 - seconds) / 60));
  if (minutes < 1) return "just now";
  return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
}

// The notes link and the command that updates, which the person copies and runs on the host: Usher installs nothing.
function updateHow(release) {
  const copy = h("button", { type: "button", class: "link-btn", onclick: async () => {
    try { await navigator.clipboard.writeText(release.command); copy.textContent = "Copied"; }
    catch { copy.textContent = "Select the command to copy it"; }
  } }, "Copy");
  return h("div", { class: "update-how" },
    h("a", { href: release.notes, target: "_blank", rel: "noopener" }, `What's new in ${release.newest}`),
    h("code", { class: "update-command" }, release.command), copy);
}

export const Updates = (Base) => class extends Base {
  // The last answer is kept for the next time Settings draws, so the row doesn't flash "Checking…" on every redraw.
  updatesRow() {
    const row = h("div", { class: "row" });
    this.fillUpdates(row, this.updatesView);
    this.refreshUpdates(row);
    return row;
  }

  fillUpdates(row, view, error) {
    const text = view ? updatesText(view) : null;
    fill(row, h("span", { class: `dot${text ? text.dot : " off"}`, "aria-hidden": "true" }),
      h("div", { class: "text" }, h("div", { class: "title" }, "Updates"),
        h("div", { class: "sub wrap" }, error ?? (text ? `${text.label} · ${text.detail}` : "Checking…")),
        text?.release ? updateHow(text.release) : null),
      h("span", {}));
  }

  async refreshUpdates(row) {
    try { this.updatesView = await api("/api/updates"); }
    catch (e) {
      if (e instanceof SignedOut) return this.signIn();
      return this.fillUpdates(row, null, e.message);
    }
    this.fillUpdates(row, this.updatesView);
  }
};
