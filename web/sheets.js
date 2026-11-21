// Usher — sheets: details, seasons, tidy-up and the journey of one request.
import { api, details, lists, store } from "./data.js";
import { titleRow } from "./rows.js";
import { h, icon } from "./dom.js";
import { fmt } from "./util.js";
import { afterRemoving, appOf } from "./recycle.js";
import { poster } from "./posters.js";
import { away } from "./device.js";
import { dragToClose } from "./sheetdrag.js";

const saving = new WeakSet();   // titles whose like or dislike is on its way, so a quick second tap waits for it

export const Sheets = (Base) => class extends Base {
  // ---- details sheet -------------------------------------------------------------------------
  // ---- sheets ---------------------------------------------------------------------------------
  // A sheet's content is rebuilt from `build` whenever live state arrives, so progress moves on its own. Groups may nest to any depth; a list left in would be drawn as text.
  openSheet(build) {
    this.sheet?.dialog.close();
    const inner = h("div", { class: "sheet-inner" });
    const dialog = h("dialog", { class: "sheet", "aria-labelledby": "sheet-title" }, inner);
    const sheet = {
      dialog, build,
      refresh: () => {
        const head = [h("div", { class: "grabber", "aria-hidden": "true" }),
          h("button", { class: "sheet-close", "aria-label": "Close", onclick: () => dialog.close() }, icon("close"))];
        inner.replaceChildren(...head, ...[sheet.build()].flat(Infinity).filter(Boolean));
      },
    };
    dialog.addEventListener("close", () => { dialog.remove(); if (this.sheet === sheet) this.sheet = null; });
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
    dragToClose(dialog, inner);
    this.sheet = sheet;
    sheet.refresh();
    document.body.append(dialog);
    dialog.showModal();
    return sheet;
  }

  head(art, title, sub, pill) {
    return h("div", { class: "detail-head" }, poster(art, title),
      h("div", {}, h("h2", { id: "sheet-title" }, title), sub ? h("div", { class: "sub" }, sub) : null,
        pill ? h("span", { class: `pill ${pill[1]}` }, pill[0]) : null));
  }

  facts(full) {
    const facts = [];
    if (full.kind === "movie") {
      if (full.year) facts.push(["Year", full.year]);
      if (full.runtime) facts.push(["Length", `${Math.floor(full.runtime / 60) ? `${Math.floor(full.runtime / 60)} hr ` : ""}${full.runtime % 60} min`]);
    } else if (full.kind === "show") {
      if (full.seasons) facts.push(["Seasons", full.seasons]);
      if (full.total) facts.push(["Episodes here", `${full.have} of ${full.total}`]);
      if (full.network) facts.push(["Network", full.network]);
      if (full.ended !== undefined && full.ended !== null) facts.push(["Status", full.ended ? "Finished" : "Still airing"]);
    }
    if (full.size) facts.push(["Space used", fmt.gb(full.size)]);
    if (full.by) facts.push(["Asked for by", full.by]);
    if (full.added) facts.push(["Added", fmt.added(full.added)]);
    return facts.length ? h("div", { class: "list facts" }, facts.map(([k, v]) => h("div", { class: "row" }, h("span", {}), h("div", { class: "title" }, k), h("span", { class: "meta" }, v)))) : null;
  }

  async details(item, from) {
    let full = item;
    if ((from === "looking" || from === "downloading" || from === "upcoming") && item.id) {
      const held = lists.show("library") || await lists.load("library").catch(() => null);
      const list = held?.[item.kind === "movie" ? "movies" : "shows"] || [];
      const lib = list.find((x) => x.id === item.id);
      full = { ...lib, ...item };
      if (lib) for (const k of ["have", "total", "seasons", "size"]) full[k] = lib[k];
    }
    const pill = {
      ready: ["Ready to watch", "ready"], looking: [item.stuck ? "Still looking" : "Looking for it", "looking"],
      downloading: [item.state === "problem" ? "Problem" : item.state === "waiting" ? "Waiting to start" : item.state === "preparing" ? "Getting it ready" : item.racing ? `Trying a better copy · ${Math.floor((item.progress || 0) * 100)}%` : item.stalled ? `Waiting for someone to share it · ${Math.floor((item.progress || 0) * 100)}%` : `Downloading ${Math.floor((item.progress || 0) * 100)}%`, item.state === "problem" ? "problem" : "looking"],
      upcoming: [item.here ? "Already here" : `Due ${fmt.dayLabel(item.when).toLowerCase()}`, item.here ? "ready" : ""],
      library: full.kind === "movie" ? ({ ready: ["Ready to watch", "ready"], looking: ["Looking for it", "looking"], not_out: ["Not out yet", ""], not_wanted: ["Not downloaded", ""] }[full.state])
        : [full.have >= full.total && full.total ? "All episodes here" : `${full.have} of ${full.total} episodes`, full.have ? "ready" : "looking"],
    }[from];
    const canRemove = from === "library" || ((from === "looking" || from === "downloading" || from === "upcoming") && Number.isInteger(item.id));
    const sheet = this.openSheet(() => [
      this.head(full.art, full.title, item.subtitle || full.year || "", pill),
      item.message ? h("p", { class: "overview" }, item.message) : null,
      from === "downloading" && item.wrongTitle ? h("p", { class: "overview warning" }, `The copy downloading is called “${item.wrongTitle}”, which doesn't look like ${full.title}. If it's the wrong thing, stop it.`) : null,
      from === "downloading" && item.state === "problem" && item.canRetry !== false ? this.retryButton(item.key) : null,
      from === "ready" ? h("p", { class: "overview" }, "It's in Jellyfin, ready to watch.") : null,
      full.overview ? h("p", { class: "overview" }, full.overview) : null,
      this.facts(full),
      full.kind === "show" && Number.isInteger(full.id) && (from === "library" || from === "upcoming") ? this.seasonsBlock(full.id) : null,
      from === "downloading" && item.key || canRemove ? h("div", { class: "actions" },
        from === "downloading" && item.key ? this.stopButton(item.key, full.title, item.kind === "show" ? (item.episodes > 1 ? "these episodes" : "this episode") : "it") : null,
        canRemove ? h("button", { class: `btn ${from === "downloading" ? "plain" : "danger"}`, onclick: () => this.remove(full) }, "Remove from library…") : null,
        from === "downloading" && item.key ? h("p", { class: "footnote" }, `Remove from library deletes ${item.kind === "show" ? "the whole show" : "it"} as well.`) : null) : null,
    ]);
    if (Number.isInteger(full.id)) sheet.title = { kind: full.kind, id: full.id, name: full.title, from };
  }

  // Like or dislike, for the signed-in account only. Picked for you in Discover follows likes; a disliked title leaves
  // this account's Discover at once (Search still finds it). Tapping the chosen one again clears it. The buttons change
  // in place, so focus stays on the one tapped and VoiceOver reads its new state.
  opinionButtons(r) {
    if (!Number.isInteger(r.tmdb) || r.tmdb <= 0 || (r.kind !== "movie" && r.kind !== "show")) return null;
    const note = h("p", { class: "footnote", "aria-live": "polite" });
    const buttons = {};
    const show = () => {
      for (const [which, btn] of Object.entries(buttons)) btn.setAttribute("aria-pressed", String(r.opinion === which));
      note.textContent = { like: "Picked for you in Discover will follow this.", dislike: "Kept out of Discover for you. Tap Dislike again to undo." }[r.opinion] || "";
      note.hidden = !r.opinion;
    };
    const button = (which, label) => (buttons[which] = h("button", { type: "button", class: "opinion-btn", "aria-label": label,
      onclick: async () => {
        if (saving.has(r)) return;
        saving.add(r);
        try { await this.setOpinion(r, r.opinion === which ? "clear" : which); } catch (e) { this.toast(e.message); }
        saving.delete(r);
        show();
      } }, icon(which), h("span", {}, label)));
    const group = h("div", { class: "opinions", role: "group", "aria-label": "Like or dislike" }, button("like", "Like"), button("dislike", "Dislike"));
    show();
    return [group, note];
  }

  // Saves an opinion (like, dislike or clear) and brings every copy that shows it in step: search results still on
  // screen, the details asked for ahead, and Discover, fetched again so a dislike leaves its rows at once.
  async setOpinion(r, opinion) {
    const res = await api("/api/opinion", { method: "POST", body: { kind: r.kind, tmdb: r.tmdb, opinion } });
    r.opinion = res.opinion;
    for (const x of this.searchResults?.results || []) if (x.kind === r.kind && x.tmdb === r.tmdb) x.opinion = res.opinion;
    details.clear();
    lists.drop("discover");
    lists.load("discover").catch(() => null);   // a screen that shows Discover asks again, and says so if it can't
    return res.opinion;
  }

  // Seasons of a show already in the library, each with Get it for the ones that aren't all here.
  seasonsBlock(id) {
    const cache = (this.seasonCache ||= {});
    const entry = cache[id];
    if (!entry || (!entry.loading && Date.now() - entry.at > 60000)) {
      cache[id] = { ...(entry || {}), loading: true };
      api(`/api/show/seasons?id=${id}`)
        .then((d) => { cache[id] = { data: d, at: Date.now() }; })
        .catch((e) => { cache[id] = { error: e.message, at: Date.now() }; })
        .finally(() => this.sheet?.refresh());
    }
    const head = h("div", { class: "footnote seasons-head" }, "Seasons");
    if (!entry?.data) return h("div", { class: "seasons" }, head,
      h("p", { class: "footnote" }, entry?.error || "Checking which seasons are here…"));
    const rows = entry.data.seasons.map((x) => {
      const missing = x.aired - x.have;
      let sub, action = null;
      if (!x.aired) sub = x.upcoming ? "Not out yet" : "No episodes listed yet";
      else if (!missing) sub = x.upcoming ? `All ${x.aired} out so far are here · more coming` : `All ${x.aired} episodes here`;
      else if (x.asked) sub = `${x.have} of ${x.aired} here · asked for, follow it on Home`;
      else {
        sub = x.have ? `${x.have} of ${x.aired} episodes here` : `Not downloaded · ${x.aired} episodes`;
        if (x.wanted && x.have) sub += " · still looking for the rest";
        const label = x.wanted ? "Search again" : "Get it";
        const btn = h("button", { class: "btn small", "aria-label": `${label}: season ${x.number}`, onclick: async (e) => {
          e.stopPropagation();
          btn.disabled = true; btn.textContent = "Asking…";
          try {
            const res = await api("/api/show/season", { method: "POST", body: { id, season: x.number } });
            delete cache[id]; lists.drop("library");
            this.toast(`Asked for ${res.title}. Follow it on Home.`);
          } catch (err) { this.toast(err.message); btn.disabled = false; btn.textContent = label; }
          this.sheet?.refresh();
        } }, label);
        action = btn;
      }
      return h("div", { class: "row" }, h("span", {}),
        h("div", { class: "text" }, h("div", { class: "title" }, `Season ${x.number}`), h("div", { class: "sub" }, sub)),
        action || h("span", { class: "meta" }, missing || !x.aired ? "" : icon("check")));
    });
    return h("div", { class: "seasons" }, head, rows.length ? h("div", { class: "list" }, rows)
      : h("p", { class: "footnote" }, "No seasons listed yet."));
  }

  retryButton(key) {
    const btn = h("button", { class: "btn", onclick: async () => {
      btn.disabled = true; btn.textContent = "Trying again…";
      try {
        await api("/api/retry", { method: "POST", body: { key } });
        btn.textContent = "Looking for another copy";
      } catch (e) { this.toast(e.message); btn.disabled = false; btn.textContent = "Try again"; }
    } }, "Try again");
    return h("div", { class: "actions" }, btn,
      h("p", { class: "footnote" }, "Skips this copy and looks for a different one."));
  }

  // Stop a download that has started: nothing more is fetched, and it isn't looked for again.
  stopButton(keys, title, what) {
    return h("button", { class: "btn danger", onclick: async () => {
      if (!confirm(`Stop downloading ${title}?\n\nThe download is cancelled and ${what} won't be looked for again. Anything already in your library stays.`)) return;
      try {
        await api("/api/cancel", { method: "POST", body: { keys: [keys].flat() } });
        this.sheet?.dialog.close();
        lists.drop("library");
        this.toast(`Stopped downloading ${title}.`);
      } catch (e) { this.toast(e.message); }
    } }, "Stop downloading…");
  }

  async remove(item) {
    const size = item.size ? ` (${fmt.gb(item.size)})` : "";
    const what = item.kind === "show" ? "every episode of it" : "it";
    if (!confirm(`Remove ${item.title}?\n\nThis deletes ${what}${size}. ${afterRemoving(store.snap?.status?.bin, appOf(item.kind))}`)) return;
    try {
      await api("/api/remove", { method: "POST", body: { kind: item.kind, id: item.id } });
      this.sheet?.dialog.close();
      lists.drop("library");
      this.toast(`${item.title} was removed.`);
      if (this.current === "/library") this.render(false);
    } catch (e) { this.toast(e.message); }
  }

  // Watched movies and whole seasons, biggest first. Nothing goes unless someone taps Remove.
  tidySheet() {
    let data = null, kept = [], error = null;
    const load = () => Promise.all([api("/api/tidy"), api("/api/tidy/kept")])
      .then(([d, k]) => { data = d; kept = k.items; error = null; }).catch((e) => { error = e.message; })
      .finally(() => this.sheet?.refresh());
    const keepOrBack = async (path, body, done, button) => {
      button.disabled = true;
      try {
        await api(path, { method: "POST", body });
        this.toast(done);
        lists.drop("tidy");
        await load();
      } catch (err) { this.toast(err.message); button.disabled = false; }
    };
    load();
    const sheet = this.openSheet(() => {
      if (error) return [h("h2", { id: "sheet-title", class: "sheet-title" }, "Tidy up"), h("p", { class: "empty" }, error)];
      if (!data) return [h("h2", { id: "sheet-title", class: "sheet-title" }, "Tidy up"), h("p", { class: "empty" }, "Checking what's been watched…")];
      return [
        h("h2", { id: "sheet-title", class: "sheet-title" }, "Tidy up"),
        h("p", { class: "footnote" }, data.items.length
          ? `${data.items.length} watched title${data.items.length === 1 ? "" : "s"} using ${fmt.gb(data.bytes)}. Only movies and whole seasons that have been watched to the end on Home are listed. Removing one won't download it again.`
          : "Nothing to tidy: no movie or whole season has been watched to the end on Home yet."),
        data.items.length ? h("div", { class: "list" }, data.items.map((it) => titleRow({ art: it.art, title: it.title,
          subtitle: [it.subtitle, fmt.gb(it.size)].filter(Boolean).join(" · "),
          state: it.watched ? `Finished ${fmt.ago(it.watched)}` : null,
          trailing: h("div", { class: "row-actions" },
            h("button", { class: "btn plain small", "aria-label": `Keep ${it.title}`, onclick: (e) =>
              keepOrBack("/api/tidy/keep", { kind: it.kind, id: it.id, season: it.season }, `${it.title} will stay.`, e.currentTarget) }, "Keep"),
            h("button", { class: "btn danger small", "aria-label": `Remove ${it.title} ${it.subtitle}`, onclick: async (e) => {
            const what = it.kind === "season" ? `${it.title}, ${it.subtitle.split(" · ")[0]}` : it.title;
            if (!confirm(`Remove ${what}?\n\nThis deletes ${fmt.gb(it.size)} and stops it downloading again. ${afterRemoving(store.snap?.status?.bin, appOf(it.kind))}`)) return;
            e.target.disabled = true;
            try {
              const r = await api("/api/tidy/remove", { method: "POST", body: { kind: it.kind, id: it.id, season: it.season } });
              this.toast(`${r.title || what} was removed.`);
              data.items = data.items.filter((x) => x !== it);
              data.bytes -= it.size;
              lists.drop("library", "tidy");
              this.sheet?.refresh();
            } catch (err) { this.toast(err.message); e.target.disabled = false; }
          } }, "Remove…")) }))) : null,
        kept.length ? [
          h("h3", { class: "sheet-subtitle" }, "Kept titles"),
          h("p", { class: "footnote" }, "These stay as they are and aren't suggested above."),
          h("div", { class: "list" }, kept.map((it) => titleRow({ art: it.art, title: it.title,
            subtitle: [it.subtitle, fmt.gb(it.size)].filter(Boolean).join(" · "),
            trailing: h("button", { class: "btn plain small", "aria-label": `Put back ${it.title}`, onclick: (e) =>
              keepOrBack("/api/tidy/put-back", { key: it.key }, `${it.title} is back on the list.`, e.currentTarget) }, "Put back") }))),
        ] : null,
      ];
    });
    // Status shows how many titles Tidy up can free, so it is drawn again with the new count once the sheet is closed.
    sheet.dialog.addEventListener("close", () => { if (this.current === "/status" && !this.pushed.length) this.render(false); });
  }

  async emptyRecycle(rb) {
    const gain = rb.bytes ? ` This frees ${fmt.gb(rb.bytes)} straight away.` : "";
    if (!confirm(`Empty the recycle bin?\n\nThe ${rb.items} removed item${rb.items === 1 ? "" : "s"} in it will be gone for good.${gain}`)) return;
    try {
      await api("/api/recycle/empty", { method: "POST" });
      this.toast("Recycle bin emptied. Free space updates in a few seconds.");
    } catch (e) { this.toast(e.message); }
  }

  toast(message) {
    const t = h("div", { class: "toast", role: "status" }, message);
    document.body.append(t);
    setTimeout(() => t.classList.add("gone"), 3200);
    setTimeout(() => t.remove(), 3700);
  }

  // ---- the journey of one request --------------------------------------------------------------
  journey(kind, id, rid) {
    const sheet = this.openSheet(() => {
      const pending = this.pending?.kind === kind && this.pending?.id === id ? this.pending : null;
      const all = store.snap?.requests || [];
      const r = (rid && all.find((x) => x.rid === rid)) || all.find((x) => x.kind === kind && x.id === id) || pending;
      if (!r) return h("p", { class: "empty" }, "Checking…");
      const pct = r.progress !== null && r.progress !== undefined ? Math.floor(r.progress * 100) : null;
      return [
        this.head(r.art, r.title, `${r.year ? `${r.year} · ` : ""}${r.kind === "movie" ? "Movie" : "TV show"}`, null),
        h("p", { class: "footnote" }, `${r.by} asked for this ${fmt.ago(r.asked)}.`),
        r.warning ? h("p", { class: "overview warning" }, r.warning) : null,
        h("ol", { class: "journey" }, r.steps.map((name, i) => {
          const now = i === r.step;
          const complete = r.step === 4 && !r.problem;
          return h("li", { class: complete ? "done ok" : i < r.step ? "done" : now ? (r.problem && r.canRetry !== false ? "now bad" : "now") : "", "aria-current": now ? "step" : null },
            h("span", { class: "marker", "aria-hidden": "true" }, i < r.step || (now && i === 4) ? icon("check") : null),
            h("div", {},
              h("div", { class: "step-name" }, now ? r.label : name),
              now && r.detail ? h("div", { class: "step-detail" }, r.detail) : null,
              now && i === 2 && pct !== null ? h("div", { class: "progress" }, h("i", { style: { "--p": `${pct}%` } })) : null,
              now && i === 2 && r.eta !== null && r.eta !== undefined ? h("div", { class: "step-detail" }, `${fmt.eta(r.eta)} left`) : null));
        })),
        r.step < 4 ? this.notifyPrompt() : null,
        r.problem && r.key && r.canRetry !== false ? this.retryButton(r.key) : null,
        r.keys?.length && !r.problem ? h("div", { class: "actions" }, this.stopButton(r.keys, r.title, "it")) : null,
        r.rid && r.step < 2 && !r.keys?.length && !r.done ? h("div", { class: "actions" }, h("button", { class: "btn danger", onclick: async () => {
          if (!confirm(`Stop looking for ${r.title}?\n\nNothing has downloaded, so nothing is deleted. You can ask for it again any time.`)) return;
          try {
            await api("/api/requests/stop", { method: "POST", body: { rid: r.rid } });
            sheet.dialog.close();
            store.snap.requests = (store.snap.requests || []).filter((x) => x.rid !== r.rid);
            lists.drop("library");
            if (this.current === "/") this.render(false);
            this.toast(`Stopped looking for ${r.title}.`);
          } catch (e) { this.toast(e.message); }
        } }, "Stop looking for it…")) : null,
        r.step === 4 ? h("p", { class: "overview" }, "It's in Jellyfin, ready to watch.") : null,
        r.rid && (r.done || r.step === 4) ? h("div", { class: "actions" }, h("button", { class: "btn plain", onclick: async () => {
          try {
            await api("/api/requests/clear", { method: "POST", body: { rid: r.rid } });
            sheet.dialog.close();
            store.snap.requests = (store.snap.requests || []).filter((x) => x.rid !== r.rid);
            if (this.current === "/") this.render(false);
            this.toast(`${r.title} cleared from Downloads.`);
          } catch (e) { this.toast(e.message); }
        } }, "Clear from Downloads"), h("p", { class: "footnote" }, "Only hides it here. It stays in your library, and clears by itself after two days.")) : null,
      ];
    });
    sheet.live = true;
  }
};
