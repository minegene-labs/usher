// Usher — the Status screen, its details and logs.
import { SignedOut, api, lists, store } from "./data.js";
import { h, icon } from "./dom.js";
import { storageSection } from "./storage.js";

export const Status = (Base) => class extends Base {
  // ---- Status ------------------------------------------------------------------------------
  // Answers "is everything working?", laid out like iOS Settings: the summary card first (how things are, and what needs
  // attention), then Health and Storage. What people change for themselves is in Settings, opened from the button at the top right.
  status() {
    const s = store.snap;
    const wrap = h("div", { class: "content status" }, this.offlineBanner());
    if (s && !s.loading) {
      const st = s.status;
      wrap.append(this.summaryCard(st), ...(st.notes || []).map((n) => h("p", { class: "footnote" }, n)), this.healthSection(st));
      if (st.storage) wrap.append(storageSection(st.storage, this, lists.show("tidy")));
    } else if (!this.offlineBanner()) {
      wrap.append(this.summaryCard(null));   // same shape as the answer, so nothing jumps when it arrives
    }
    return wrap;
  }

  // One card: the headline, then a row for each part that is not ok. Storage has no details screen: its section below says more.
  // The dot and symbol are decoration; the words carry the meaning.
  summaryCard(st) {
    const bad = st ? st.parts.filter((p) => !p.ok) : [];
    const n = bad.length;
    const working = !!st && n === 0 && st.ok;
    const lead = !st ? null : working ? h("span", { class: "dot", "aria-hidden": "true" }) : icon("warning");
    const words = !st ? "Checking…" : working ? "Everything's working."
      : `${n || 1} thing${n > 1 ? "s" : ""} need${n > 1 ? "" : "s"} attention. Usher keeps working around it where it can.`;
    return h("section", { class: "section status-summary" }, h("div", { class: "list summary" },
      h("div", { class: "row headline" }, h("span", { class: `lead${working || !st ? "" : " warn"}`, "aria-hidden": "true" }, lead),
        h("div", { class: "text" }, h("div", { class: "title" }, words)), h("span", {})),
      bad.map((p) => {
        const body = [h("span", { class: "dot warn", "aria-hidden": "true" }),
          h("div", { class: "text" }, h("div", { class: "title" }, p.name, h("span", { class: "sr-only" }, ", needs attention")), h("div", { class: "sub" }, p.text))];
        return p.name === "Storage" ? h("div", { class: "row" }, ...body, h("span", {}))
          : h("button", { class: "row", onclick: () => this.statusDetails(p) }, ...body, h("span", { class: "chev" }, icon("chevron")));
      })));
  }

  healthSection(st) {
    return h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Health")), h("div", { class: "list" },
      st.parts.filter((p) => p.name !== "Storage").map((p) => h("button", { class: "row", onclick: () => this.statusDetails(p) },
        h("span", { class: `dot${p.ok ? "" : " warn"}`, "aria-hidden": "true" }),
        h("div", { class: "text" }, h("div", { class: "title" }, p.name)),
        h("span", { class: "meta" }, p.ok ? "Working" : "Needs attention"),
        h("span", { class: "chev" }, icon("chevron")))),
      h("button", { class: "row", onclick: () => this.logsSheet() }, h("span", { "aria-hidden": "true" }),
        h("div", { class: "text" }, h("div", { class: "title" }, "Logs")),
        h("span", { class: "chev" }, icon("chevron")))));
  }

  // ---- Status details and logs -------------------------------------------------------------------
  async statusDetails(part) {
    let data = null, error = null;
    const sheet = this.openSheet(() => {
      const d = data?.[part.name];
      return [
        h("h2", { id: "sheet-title", class: "sheet-title" }, part.name),
        h("p", { class: "overview" }, part.text),
        !data && !error ? h("p", { class: "footnote" }, "Asking the server…") : null,
        error ? h("p", { class: "footnote" }, error) : null,
        d?.items?.length ? h("div", { class: "list" }, d.items.map((it) => h("div", { class: "row" },
          h("span", { class: `dot${it.ok === false ? " warn" : it.ok === null ? " off" : ""}`, "aria-hidden": "true" }),
          h("div", { class: "text" }, h("div", { class: "title" }, it.name), h("div", { class: "sub wrap" }, it.text)), h("span", {})))) : null,
        d?.messages?.length ? h("div", { class: "messages" }, h("div", { class: "footnote" }, "What it reports, word for word:"),
          d.messages.map((m) => h("p", {}, m))) : null,
        d?.facts?.length ? h("div", { class: "list facts", style: { "margin-top": "14px" } }, d.facts.map(([k, v]) =>
          h("div", { class: "row" }, h("span", {}), h("div", { class: "title" }, k), h("span", { class: "meta" }, v)))) : null,
        h("div", { class: "actions" }, h("button", { class: "btn plain", onclick: () => this.logsSheet({ Search: "prowlarr", TV: "sonarr", Movies: "radarr", Watching: "jellyfin" }[part.name] || "usher") }, "Show its logs")),
      ];
    });
    try { data = await api("/api/status/details"); } catch (e) { if (e instanceof SignedOut) return this.signIn(); error = e.message; }
    if (this.sheet === sheet) sheet.refresh();
  }

  logsSheet(source = "usher") {
    const sources = [["usher", "Usher"], ["sonarr", "TV"], ["radarr", "Movies"], ["prowlarr", "Search"], ["jellyfin", "Jellyfin"]];
    let level = "warn", lines = null, error = null, timer = null;
    const load = async () => {
      try { lines = (await api(`/api/logs?source=${source}&level=${level}`)).lines; error = null; }
      catch (e) { if (e instanceof SignedOut) return this.signIn(); error = e.message; }
      if (this.sheet === sheet) sheet.refresh();
    };
    const time = (t) => { const d = new Date(t); return isNaN(d) ? String(t || "").slice(0, 19) : d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
    const sheet = this.openSheet(() => [
      h("h2", { id: "sheet-title", class: "sheet-title" }, "Logs"),
      h("div", { class: "log-controls" },
        h("div", { class: "segmented", role: "group", "aria-label": "Which app" }, sources.map(([v, label]) =>
          h("button", { "aria-pressed": String(v === source), onclick: () => { source = v; lines = null; sheet.refresh(); load(); } }, label))),
        h("select", { class: "sort", "aria-label": "Show", onchange: (e) => { level = e.target.value; lines = null; sheet.refresh(); load(); } },
          h("option", { value: "all", selected: level === "all" }, "Everything"),
          h("option", { value: "warn", selected: level === "warn" }, "Warnings and errors"),
          h("option", { value: "error", selected: level === "error" }, "Errors only"))),
      error ? h("p", { class: "footnote" }, error) : null,
      lines === null && !error ? h("p", { class: "footnote" }, "Loading…") : null,
      lines?.length === 0 ? h("p", { class: "empty" }, level === "all" ? "Nothing logged recently." : "Nothing at this level recently. That's good.") : null,
      lines?.length ? h("div", { class: "log", role: "log", "aria-live": "off" }, lines.map((l) =>
        h("div", { class: `line ${l.level}` }, h("span", { class: "t" }, time(l.time)), h("span", { class: "lv" }, l.level.toUpperCase()),
          l.source ? h("span", { class: "src" }, `${l.source}: `) : null, l.message))) : null,
      h("p", { class: "footnote" }, "Newest first. Updates every 10 seconds while open. Keys and passwords are hidden."),
    ]);
    sheet.dialog.classList.add("big");
    timer = setInterval(load, 10000);
    sheet.dialog.addEventListener("close", () => clearInterval(timer));
    load();
  }
};
