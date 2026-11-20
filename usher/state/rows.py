"""Builds the snapshot's rows for Home: downloads, still looking, ready to watch and the media server's own rows."""

import time

from ..releases import import_reason, title_matches
from ..titles import arr_art, jf_art
from .helpers import READY_WINDOW, STALLED_AFTER, couldnt_find_after, episode_label, episode_subtitle, hms_seconds, iso, parse_time, utcnow


class Rows:
    # building the snapshot --------------------------------------------------
    def _build(self):
        r = self._raw
        now = utcnow()
        downloading, in_queue_eps, in_queue_movies = self._downloads(r)
        looking = self._looking(r, now, in_queue_eps, in_queue_movies)
        ready = self._ready(r, now)
        status = self._status(r)
        requests = self._requests(r, now, downloading)
        # When each recently added, unwatched movie or episode arrived: each person's badge counts the ones
        # newer than the last time they looked at Home.
        new_items = [it.get("DateCreated") for it in r["ready"] if it.get("DateCreated") and self._shown(it)]
        return {
            "ready": ready,
            "downloading": downloading,
            "looking": looking,
            "status": status,
            "requests": requests,
            "home": self._home_rows(r.get("home") or []),
            "newItems": new_items,
            "changes": self.titles.seq,
        }

    def _shown(self, it):
        """Not a title just removed, and not an item the media server no longer has."""
        cutoff = time.time() - 24 * 3600
        for jid, when in list(self._gone_jf.items()):
            if when < cutoff:
                self._gone_jf.pop(jid, None)
        if it.get("Id") in self._gone_jf or (it.get("SeriesId") and it.get("SeriesId") in self._gone_jf):
            return False
        return not self.titles.hides(it)

    def _home_rows(self, rows):
        """Home's rows as the media server last listed them, less removed titles; each card names its title."""
        out = []
        for row in rows:
            items = []
            for card in row.get("items") or []:
                it = card.get("_item") or {"Id": card.get("jf")}
                if not self._shown(it):
                    continue
                t = self.titles.identify(it)
                items.append({**{k: v for k, v in card.items() if k != "_item"}, "key": t["key"] if t else None})
            if items:
                out.append({**row, "items": items})
        return out

    def _downloads(self, r):
        groups: dict = {}
        in_eps, in_movies = set(), set()
        for rec in r["queue_tv"]:
            if rec.get("episodeId"):
                in_eps.add(rec["episodeId"])
            key = (rec.get("downloadId") or f"tv{rec.get('id')}").lower()
            g = groups.setdefault(key, {"kind": "show", "recs": [], "id": rec.get("seriesId")})
            g["recs"].append(rec)
        for rec in r["queue_movies"]:
            if rec.get("movieId"):
                in_movies.add(rec["movieId"])
            key = (rec.get("downloadId") or f"mv{rec.get('id')}").lower()
            groups.setdefault(key, {"kind": "movie", "recs": [], "id": rec.get("movieId")})["recs"].append(rec)

        out = []
        for key, g in groups.items():
            rec = g["recs"][0]
            if g["kind"] == "show":
                series = rec.get("series") or {}
                title = series.get("title") or rec.get("title", "TV episode")
                eps = sorted({(e.get("episode") or {}).get("seasonNumber", 0) * 1000 + (e.get("episode") or {}).get("episodeNumber", 0) for e in g["recs"]})
                if len(eps) == 1:
                    ep = rec.get("episode") or {}
                    subtitle = episode_subtitle(ep.get("seasonNumber"), ep.get("episodeNumber"), ep.get("title"))
                else:
                    seasons = sorted({e // 1000 for e in eps})
                    subtitle = f"Season {seasons[0]} · {len(eps)} episodes" if len(seasons) == 1 else f"{len(eps)} episodes"
                art = arr_art("show", {**series, "id": g["id"]}) if g["id"] else None
            else:
                movie = rec.get("movie") or {}
                title = movie.get("title") or rec.get("title", "Movie")
                subtitle = str(movie.get("year") or "")
                art = arr_art("movie", {**movie, "id": g["id"]}) if g["id"] else None

            size = sum(x.get("size") or 0 for x in g["recs"][:1])
            left = sum(x.get("sizeleft") or 0 for x in g["recs"][:1])
            progress = (1 - left / size) if size else 0
            eta = hms_seconds(rec.get("timeleft"))
            t = r["torrents"].get(key)
            if t:
                progress = t.get("percentDone", progress)
                eta = t["eta"] if (t.get("eta") or -1) >= 0 else None

            tstate = rec.get("trackedDownloadState", "")
            tstatus = rec.get("trackedDownloadStatus", "")
            status = (rec.get("status") or "").lower()
            message, reason, can_retry = "", None, True
            warned = [x for x in g["recs"] if x.get("trackedDownloadStatus") == "warning" or x.get("trackedDownloadState") == "importBlocked"]
            if tstatus == "error" or status == "failed" or tstate in ("failed", "failedPending") or (t and t.get("error") in (2, 3)):
                state, message, reason = "problem", "The download stopped. It will look for another copy by itself.", "failed"
            elif warned and (rec.get("status") or "").lower() == "completed" or tstate == "importBlocked":
                state = "problem"
                reason, message, can_retry = import_reason(warned)
            elif tstate in ("importPending", "importing") or progress >= 0.999:
                state = "preparing"
            elif status in ("queued", "delay", "paused") or (t and t.get("status") in (0, 1, 2, 3)):
                state = "waiting"
            else:
                state = "downloading"
            # Stalled: downloading, but no progress for hours (usually nobody is sharing it right now).
            stalled_for = None
            if state == "downloading":
                was = self._progress.get(key)
                now_s = time.time()
                if was is None or progress > was[0] + 0.0005:
                    self._progress[key] = (progress, now_s)
                elif now_s - was[1] >= STALLED_AFTER:
                    stalled_for = int(now_s - was[1])
            else:
                self._progress.pop(key, None)
            if stalled_for:
                hours = stalled_for // 3600
                message = (
                    f"Waiting for someone to share it (no progress for {hours} hour{'s' if hours != 1 else ''}). "
                    "That's normal for some copies; Usher keeps trying by itself."
                )
            racing = bool(self.replacer and self.replacer.racing(key))
            if racing:
                message = "Trying a better copy alongside this one. Whichever finishes first goes into the library, and the other is removed."
            # A release whose name doesn't mention the title is probably the wrong show or movie.
            item = (rec.get("series") if g["kind"] == "show" else rec.get("movie")) or {}
            releases = {x.get("title") for x in g["recs"] if x.get("title")}
            wrong = next((rn for rn in releases if item and not title_matches(rn, item)), None)
            self.queue_ids[key] = ("sonarr" if g["kind"] == "show" else "radarr", [x.get("id") for x in g["recs"]])
            out.append(
                {
                    "key": key,
                    "kind": g["kind"],
                    "id": g["id"],
                    "title": title,
                    "subtitle": subtitle,
                    "art": art,
                    "state": state,
                    "progress": round(max(0.0, min(1.0, progress)), 3),
                    "eta": eta,
                    "message": message,
                    "episodes": len(g["recs"]),
                    "reason": reason,
                    "canRetry": can_retry,
                    "stalled": bool(stalled_for),
                    "stalledFor": stalled_for,
                    "racing": racing,
                    "wrongTitle": (wrong or "")[:160] or None,
                }
            )
        for gone in set(self._progress) - set(groups):
            self._progress.pop(gone, None)
        order = {"problem": 0, "downloading": 1, "preparing": 2, "waiting": 3}
        out.sort(key=lambda x: (order[x["state"]], x["stalled"], -x["progress"]))
        return out, in_eps, in_movies

    def _looking(self, r, now, in_eps, in_movies):
        out = []
        shows: dict = {}
        for rec in r["missing_tv"]:
            if rec.get("id") in in_eps:
                continue
            aired = parse_time(rec.get("airDateUtc"))
            if aired and aired > now:
                continue
            sid = rec.get("seriesId")
            s = shows.setdefault(sid, {"series": rec.get("series") or {}, "eps": [], "oldest": aired})
            s["eps"].append(rec)
            if aired and (not s["oldest"] or aired < s["oldest"]):
                s["oldest"] = aired
        for sid, s in shows.items():
            eps = s["eps"]
            if len(eps) == 1:
                sub = episode_label(eps[0].get("seasonNumber"), eps[0].get("episodeNumber"))
            else:
                sub = f"{len(eps)} episodes"
            out.append(
                {
                    "kind": "show",
                    "id": sid,
                    "title": s["series"].get("title", "TV show"),
                    "subtitle": sub,
                    "art": arr_art("show", {**s["series"], "id": sid}),
                    "since": iso(s["oldest"]),
                    "stuck": bool(s["oldest"] and (now - s["oldest"]).total_seconds() > couldnt_find_after()),
                }
            )
        for m in r["movies"]:
            if not m.get("monitored") or m.get("hasFile") or m.get("id") in in_movies or not m.get("isAvailable"):
                continue
            added = parse_time(m.get("added"))
            out.append(
                {
                    "kind": "movie",
                    "id": m["id"],
                    "title": m.get("title", "Movie"),
                    "subtitle": str(m.get("year") or ""),
                    "art": arr_art("movie", m),
                    "since": iso(added),
                    "stuck": bool(added and (now - added).total_seconds() > couldnt_find_after()),
                }
            )
        out.sort(key=lambda x: x["since"] or "", reverse=True)
        return out

    def _ready(self, r, now):
        out, shows = [], {}
        for it in r["ready"]:
            if not self._shown(it):
                continue
            created = parse_time(it.get("DateCreated"))
            if created and (now - created).total_seconds() > READY_WINDOW:
                continue
            if it.get("Type") == "Episode":
                sid = it.get("SeriesId") or it.get("Id")
                if sid in shows:
                    g = shows[sid]
                    g["count"] += 1
                    g["subtitle"] = f"{g['count']} new episodes"
                    continue
                g = {
                    "kind": "episode",
                    "id": it.get("Id"),
                    "title": it.get("SeriesName", "TV show"),
                    "subtitle": episode_subtitle(it.get("ParentIndexNumber"), it.get("IndexNumber"), it.get("Name")),
                    "art": jf_art(sid, it.get("SeriesPrimaryImageTag")),
                    "added": iso(created),
                    "count": 1,
                }
                shows[sid] = g
                out.append(g)
            elif it.get("Type") == "Movie":
                out.append(
                    {
                        "kind": "movie",
                        "id": it.get("Id"),
                        "title": it.get("Name", "Movie"),
                        "subtitle": str(it.get("ProductionYear") or ""),
                        "art": jf_art(it.get("Id"), (it.get("ImageTags") or {}).get("Primary")),
                        "added": iso(created),
                        "count": 1,
                    }
                )
        return out[:20]
