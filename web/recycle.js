// Usher — what happens to removed files, in words, from each app's own recycle bin setting (status.bin).
// `bin` is { tv, movie }, each { on, days } or null while it hasn't been read; `app` is "tv" or "movie".
const days = (n) => `${n} day${n === 1 ? "" : "s"}`;

// The sentence in a Remove confirmation that says whether the files can be got back.
export function afterRemoving(bin, app) {
  const p = bin?.[app];
  if (!p) return "Usher couldn't check the recycle bin, so it may be deleted for good.";
  if (!p.on) return "It is deleted for good and can't be got back.";
  if (!p.days) return "It stays in the recycle bin until the recycle bin is emptied on the Status screen.";
  return `It goes to the recycle bin for ${days(p.days)}; to get the space back sooner, empty the recycle bin on the Status screen.`;
}

// The line under Recycle bin in Storage: when what is in it goes by itself.
export function binEmptying(bin) {
  const on = Object.values(bin || {}).filter((p) => p?.on);
  if (!on.length) return "";
  const kept = on.filter((p) => !p.days).length;
  const counts = on.map((p) => p.days).filter(Boolean);
  if (!counts.length) return "kept until you empty it";
  const [lo, hi] = [Math.min(...counts), Math.max(...counts)];
  const after = lo === hi ? `after ${days(lo)}` : `after ${lo} to ${days(hi)}`;
  return kept ? `some emptied by itself ${after}, the rest kept until you empty it` : `emptied by itself ${after}`;
}

// Which app deletes a title's files: a show or season is Sonarr's, a movie Radarr's.
export const appOf = (kind) => (kind === "movie" ? "movie" : "tv");
