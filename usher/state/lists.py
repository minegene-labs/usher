"""Library, Upcoming and Tidy up: built on demand and cached briefly."""

from datetime import timedelta

from .. import tidy
from ..cache import MISSING
from ..titles import arr_art
from .helpers import episode_subtitle, iso, utcnow


class Lists:
    def tidy(self):
        """Watched movies and seasons that could be removed to free space, biggest first (see tidy.py)."""

        def build():
            busy_movies = {r.get("movieId") for r in self._raw["queue_movies"]}
            busy_series = {r.get("seriesId") for r in self._raw["queue_tv"]}
            items = tidy.candidates(
                self.radarr.get("movie"),
                self.sonarr.get("series"),
                *tidy.jellyfin_items(self.jf),
                busy_movies=busy_movies,
                busy_series=busy_series,
                kept=self._kept_keys(),
            )
            return {"items": items, "bytes": sum(i["size"] for i in items)}

        return self._cached("tidy", 60, build)

    def _kept_keys(self):
        return self.kept.keys() if self.kept else frozenset()

    def kept_titles(self):
        """Titles kept out of Tidy up that are still on the server, for Put back."""
        return self._cached("kept", 60, lambda: {"items": tidy.kept_titles(self.radarr.get("movie"), self.sonarr.get("series"), self._kept_keys())})

    # publishing -------------------------------------------------------------

    # on-demand data, cached briefly -----------------------------------------
    def _cached(self, key, ttl, fn):
        hit = self._cache.get(key, MISSING, ttl=ttl)
        if hit is not MISSING:
            return hit
        value = fn()
        self._cache.set(key, value)
        return value

    def drop_cache(self):
        self._cache.clear()

    def library(self):
        def build():
            asked = self.store.requesters() if self.store else {}
            movies = []
            for m in self.radarr.get("movie"):
                if m.get("hasFile"):
                    state = "ready"
                elif not m.get("monitored"):
                    state = "not_wanted"
                elif not m.get("isAvailable"):
                    state = "not_out"
                else:
                    state = "looking"
                movies.append(
                    {
                        "kind": "movie",
                        "id": m["id"],
                        "title": m.get("title", ""),
                        "sort": m.get("sortTitle") or m.get("title", "").lower(),
                        "year": m.get("year"),
                        "added": m.get("added"),
                        "state": state,
                        "size": m.get("sizeOnDisk", 0),
                        "runtime": m.get("runtime"),
                        "overview": (m.get("overview") or "")[:600],
                        "art": arr_art("movie", m),
                        "by": (asked.get(("movie", m["id"])) or [None])[0],
                    }
                )
            shows = []
            for s in self.sonarr.get("series"):
                st = s.get("statistics") or {}
                have, total = st.get("episodeFileCount", 0), st.get("episodeCount", 0)
                shows.append(
                    {
                        "kind": "show",
                        "id": s["id"],
                        "title": s.get("title", ""),
                        "sort": s.get("sortTitle") or s.get("title", "").lower(),
                        "year": s.get("year"),
                        "added": s.get("added"),
                        "ended": s.get("ended"),
                        "seasons": st.get("seasonCount") or len(s.get("seasons") or []),
                        "have": have,
                        "total": total,
                        "size": st.get("sizeOnDisk", 0),
                        "network": s.get("network"),
                        "overview": (s.get("overview") or "")[:600],
                        "art": arr_art("show", s),
                        "by": (asked.get(("show", s["id"])) or [None])[0],
                    }
                )
            return {"movies": movies, "shows": shows}

        return self._cached("library", 60, build)

    def upcoming(self, days=14):
        def build():
            today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=12)
            end = today + timedelta(days=days + 1)
            items = []
            for ep in self.sonarr.get("calendar", start=iso(today), end=iso(end), includeSeries="true"):
                series = ep.get("series") or {}
                items.append(
                    {
                        "kind": "show",
                        "id": ep.get("seriesId"),
                        "when": ep.get("airDateUtc"),
                        "title": series.get("title", "TV show"),
                        "subtitle": episode_subtitle(ep.get("seasonNumber"), ep.get("episodeNumber"), ep.get("title")),
                        "here": ep.get("hasFile", False),
                        "art": arr_art("show", {**series, "id": ep.get("seriesId")}),
                    }
                )
            for m in self.radarr.get("calendar", start=iso(today), end=iso(end)):
                when = m.get("digitalRelease") or m.get("physicalRelease")
                if not when or not (iso(today) <= when <= iso(end)):
                    continue
                items.append(
                    {
                        "kind": "movie",
                        "id": m.get("id"),
                        "when": when,
                        "title": m.get("title", "Movie"),
                        "subtitle": "Out to watch at home",
                        "here": m.get("hasFile", False),
                        "art": arr_art("movie", m),
                    }
                )
            items.sort(key=lambda x: x["when"] or "")
            return {"items": items, "days": days}

        return self._cached("upcoming", 300, build)
