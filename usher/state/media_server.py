"""Checks against the media server: titles reaching it, what was watched, missing pictures and Home's rows."""

import logging
import time

from ..titles import jf_art
from .helpers import episode_subtitle

log = logging.getLogger("usher.state")


class MediaServer:
    def _played(self, items):
        """Something left the unplayed list (it was watched): Tidy up and every view of it are out of date."""
        now = {it.get("Id"): it for it in items or [] if isinstance(it, dict)}
        before, self._unplayed = self._unplayed, now
        if before is None:
            return
        oldest = min((it.get("DateCreated") or "" for it in now.values()), default="")
        # Only what left while newer than the oldest still listed: older ones were just pushed off the list.
        left = [it for i, it in before.items() if i not in now and (it.get("DateCreated") or "") >= oldest]
        if left:
            self._cache.pop("tidy", None)
            self.titles.changed("played", [self.titles.identify(it) for it in left])

    def art_missing(self, jf_id):
        """A picture of a media server item wasn't there: if the item itself has gone, Home drops its card."""
        now = time.time()
        if jf_id in self._gone_jf or now - self._art_checked.get(jf_id, 0) < 600:
            return
        self._art_checked[jf_id] = now
        if len(self._art_checked) > 2000:
            self._art_checked.clear()
        have = self._try("jellyfin", lambda: self.jf.existing([jf_id]))
        if have is not None and jf_id not in have:
            self._gone_jf[jf_id] = time.time()
            log.info("media server item %s has gone; leaving it off Home", jf_id)
            with self._build_lock:
                self._publish(self._build())

    # Home rows, mirroring Jellyfin's own home screen for the shared viewing account ------------------
    def _jellyfin_home(self):
        def card(it, wide=False):
            kind = it.get("Type")
            ud = it.get("UserData") or {}
            tags = it.get("ImageTags") or {}
            series_art = jf_art(it.get("SeriesId") or it["Id"], it.get("SeriesPrimaryImageTag") or tags.get("Primary"), "Primary")
            if kind == "Episode":
                title = it.get("SeriesName", "")
                sub = episode_subtitle(it.get("ParentIndexNumber"), it.get("IndexNumber"), it.get("Name"))
                art = jf_art(it["Id"], tags.get("Primary"), "Primary") if wide else series_art
            else:
                title = it.get("Name", "")
                if kind == "Series":
                    n = ud.get("UnplayedItemCount") or 0
                    sub = f"{n} new episode{'s' if n != 1 else ''}" if n else str(it.get("ProductionYear") or "")
                else:
                    sub = str(it.get("ProductionYear") or "")
                art = jf_art(it["Id"], (it.get("BackdropImageTags") or [None])[0], "Backdrop") if wide else jf_art(it["Id"], tags.get("Primary"), "Primary")
            # _item: what's needed to match the card to its title; taken off before the snapshot is sent.
            return {
                "jf": it["Id"],
                "type": kind,
                "title": title,
                "subtitle": sub,
                "art": art,
                "fallback": series_art,
                "_item": {k: it.get(k) for k in ("Id", "Type", "SeriesId", "ProviderIds", "Path", "ParentIndexNumber", "IndexNumber")},
                "progress": round((ud.get("PlayedPercentage") or 0) / 100, 3) or None,
                "played": bool(ud.get("Played")),
                "unplayed": ud.get("UnplayedItemCount") or 0,
                "overview": (it.get("Overview") or "")[:600],
                "added": it.get("DateLastMediaAdded") or it.get("DateCreated"),
            }

        rows = []
        resume = [card(i, wide=True) for i in self.jf.resume(12)]
        if resume:
            rows.append({"id": "resume", "name": "Continue watching", "wide": True, "items": resume})
        nxt = [card(i, wide=True) for i in self.jf.next_up(12)]
        if nxt:
            rows.append({"id": "nextup", "name": "Next up", "wide": True, "items": nxt})
        for v in self.jf.views():
            if v.get("CollectionType") in ("playlists", "boxsets", "livetv", "music"):
                continue
            items = [card(i) for i in self.jf.latest(v["Id"], 16)]
            if items:
                rows.append({"id": f"latest-{v['Id']}", "name": f"Latest {v.get('Name', '')}", "wide": False, "items": items})
        return rows

    def _confirm_in_jellyfin(self):
        """A request only reaches 'Ready to watch' once Jellyfin itself has the movie or show. A media server ID
        saved earlier is checked again on every look: a move (into the family folders, say) or a rescan gives the
        title a new item, and the request and the "opened" marks then follow it to the new one."""
        if not self.store:
            return
        movies = {m["id"]: m for m in self._raw["movies"] if m.get("hasFile")}
        shows = {s["id"]: s for s in self._raw["series"] if (s.get("statistics") or {}).get("episodeFileCount")}
        saved = self.store.q("SELECT * FROM requests WHERE jf_id IS NOT NULL AND (done_at IS NULL OR done_at > ?)", (int(time.time()) - 30 * 86400,))
        have = self._try("jellyfin", lambda: self.jf.existing([r["jf_id"] for r in saved])) if saved else set()
        recheck, self._recheck = self._recheck, set()
        if have is None:  # the media server didn't answer: check again next time
            have, recheck = {r["jf_id"] for r in saved}, set()
        stale = [r for r in saved if r["jf_id"] not in have or (r["kind"], r["arr_id"]) in recheck]
        for req in [r for r in self.store.open_requests() if not r["jf_id"]] + stale:
            item = (movies if req["kind"] == "movie" else shows).get(req["arr_id"])
            if not item:
                if req["jf_id"] and req["jf_id"] not in have:
                    self.store.move_jf(req["jf_id"], None)  # gone, and nothing to find it again by
                continue
            if req["kind"] == "movie":
                ids = {"Tmdb": item.get("tmdbId") or req["ext_id"], "Imdb": item.get("imdbId")}
            else:
                ids = {"Tvdb": item.get("tvdbId") or req["ext_id"], "Tmdb": item.get("tmdbId"), "Imdb": item.get("imdbId")}
            jf_id = self._try("jellyfin", lambda: self.jf.find(req["kind"], req["title"], ids, item.get("path") or ""))
            if req["jf_id"]:
                if jf_id != req["jf_id"] and (jf_id or req["jf_id"] not in have):
                    self.store.move_jf(req["jf_id"], jf_id)
                    log.info("%s has a new media server ID", req["title"])
            elif jf_id:
                self.store.run("UPDATE requests SET jf_id = ? WHERE id = ?", (jf_id, req["id"]))
                log.info("in Jellyfin: %s", req["title"])
