// Usher — Status → Storage: one usage bar split by what fills the disk, free space, with Tidy up as its last row.
import { h, icon } from "./dom.js";
import { fmt } from "./util.js";
import { binEmptying } from "./recycle.js";

const PARTS = {
  movies: ["Movies", "--blue"], tv: ["TV", "--green"], family: ["Family libraries", "--purple"],
  downloads: ["Downloads", "--accent"], recycle: ["Recycle bin", "--orange"], other: ["Other", "--label-2"],
};

const legendRow = (colour, name, value, extra) => h("div", { class: "row legend" },
  h("span", { class: "swatch", style: { background: `var(${colour})` }, "aria-hidden": "true" }),
  h("div", { class: "text" }, h("div", { class: "title" }, name), extra || null),
  h("span", { class: "size" }, value));

// What the Tidy up row says: how many watched titles it can free, or what it is while that isn't known yet.
function tidyNote(tidy) {
  if (!tidy) return "Movies and seasons you've finished watching, biggest first";
  const n = tidy.items.length;
  return n ? `${n} title${n === 1 ? "" : "s"} you've finished watching, ${fmt.gb(tidy.bytes)} to free` : "Nothing to tidy: no finished movie or season yet";
}

// `app` supplies what happens on a tap: emptyRecycle(recycle) and tidySheet(). `tidy` is the Tidy up list, or null until it is known.
export function storageSection(storage, app, tidy) {
  const used = storage.total - storage.free;
  const parts = (storage.split || []).filter((p) => p.bytes > 0 && PARTS[p.key]);
  const recycle = storage.recycle;
  const bar = h("div", { class: "usage", role: "img", "aria-label": `${fmt.gb(used)} used of ${fmt.gb(storage.total)}` },
    parts.map((p) => h("i", { style: { "flex-grow": p.bytes, background: `var(${PARTS[p.key][1]})` } })),
    h("i", { class: "free", style: { "flex-grow": Math.max(0, storage.free) } }));
  const rows = parts.map((p) => {
    const note = p.key === "recycle" && recycle?.items
      ? h("div", { class: "sub" }, [`${recycle.items} removed item${recycle.items === 1 ? "" : "s"}`, binEmptying(storage.bin)].filter(Boolean).join(", ")) : null;
    return legendRow(PARTS[p.key][1], PARTS[p.key][0], fmt.gb(p.bytes), note);
  });
  const limit = fmt.gb(storage.lowAt || 0);
  return h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Storage")),
    h("div", { class: `list storage${storage.low ? " low" : ""}` },
      h("div", { class: "headline" }, h("span", { class: "big" }, fmt.gb(used)), ` used of ${fmt.gb(storage.total)}`),
      bar, rows,
      legendRow("--fill-strong", "Free", fmt.gb(storage.free)),
      h("div", { class: "state note" }, storage.low
        ? `Getting full. New downloads may stop when free space reaches ${limit}.` : "Plenty of room for now."),
      recycle?.bytes ? h("button", { class: "link-btn", onclick: () => app.emptyRecycle(recycle) },
        `Empty recycle bin now (${fmt.gb(recycle.bytes)} you can get back)`) : null,
      h("button", { class: "row tidy-row", onclick: () => app.tidySheet() }, h("span", { "aria-hidden": "true" }),
        h("div", { class: "text" }, h("div", { class: "title" }, "Tidy up"), h("div", { class: "sub" }, tidyNote(tidy))),
        h("span", { class: "chev" }, icon("chevron")))));
}
