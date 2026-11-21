"""The things people can do: search, get a movie or show, retry a stuck download, remove something."""

import hashlib
import logging
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .cache import TTLCache
from .clients import ApiError
from .problem import Problem
from .search import SearchActions, _ratings  # noqa: F401  (_ratings: kept importable from here)
from .stop_actions import StopActions, season_of
from .tidy_actions import TidyActions
from .titles import Titles, ident, remote_art
from . import config

log = logging.getLogger("usher.actions")

TV_MONITOR = {  # what "From the start / Latest season / New episodes only" mean to Sonarr
    "start": ("all", True),
    "latest": ("latestSeason", True),
    "new": ("future", False),
}


def season_rows(seasons, episodes, open_reqs, now_iso):
    """Per season (specials left out): episodes out so far, how many are here, and whether it's wanted or asked for."""
    rows = {}
    for e in episodes:
        n = e.get("seasonNumber") or 0
        if n <= 0:
            continue
        r = rows.setdefault(n, {"number": n, "aired": 0, "have": 0, "upcoming": 0})
        air = e.get("airDateUtc")
        if e.get("hasFile"):
            r["have"] += 1
        if e.get("hasFile") or (air and air[:19] <= now_iso[:19]):
            r["aired"] += 1
        else:
            r["upcoming"] += 1
    monitored = {x.get("seasonNumber"): bool(x.get("monitored")) for x in seasons or []}
    asked = {season_of(r["choice"]) for r in open_reqs}
    for n, r in rows.items():
        r["wanted"] = monitored.get(n, False)
        r["asked"] = n in asked
    return [rows[n] for n in sorted(rows)]


SEARCH_TTL = 600  # seconds a lookup is reused for the same query
SEARCH_CACHE_SIZE = 200  # lookups kept


class Actions(SearchActions, StopActions, TidyActions):
    def __init__(self, sonarr, radarr, store, state):
        self.sonarr, self.radarr, self.store, self.state = sonarr, radarr, store, state
        self.quality = config.get("quality.profile_name")
        self.quality_fallback = config.get("quality.fallback_contains")
        self.tv_root = config.get("library.tv_folder")
        self.movie_root = config.get("library.movie_folder")
        # Optional separate library folders for family-friendly titles, chosen from the rating. Empty = off.
        self.family_tv_root = config.get("family.tv_folder")
        self.family_movie_root = config.get("family.movie_folder")
        self.family_ratings = {r.upper() for r in config.get("family.ratings")}
        self._lookups: OrderedDict = OrderedDict()  # (kind, ext_id) -> lookup object, for Get it
        self.remote_art: OrderedDict = OrderedDict()  # key -> poster URL, so devices fetch posters via Usher
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2)
        # Search's own pool: room for a few searches in flight, so one slow lookup can't hold up the next search.
        self._search_pool = ThreadPoolExecutor(max_workers=6)
        self._search_cache = TTLCache(SEARCH_TTL, SEARCH_CACHE_SIZE)  # (kind, lowercased query) -> lookup results
        self._inflight: dict = {}  # (kind, lowercased query) -> [future, clients waiting]
        self._latest: OrderedDict = OrderedDict()  # client -> the query it searched for last
        # The title identities (titles.py): a change to a title drops its lookups, so Search shows it as it is now.
        self.titles = state.titles if isinstance(getattr(state, "titles", None), Titles) else Titles()
        self.titles.listeners.append(self._forget_title)
        self.titles.clears(self._search_cache, self._lookup_holds)

    @staticmethod
    def _lookup_holds(key, found, t):
        """Does this cached lookup hold the title? Its "in your library" comes from the lookup itself."""
        field = {"show": "tvdbId", "movie": "tmdbId"}
        return key[0] == t["kind"] and any(
            (t.get("ext") and x.get(field[t["kind"]]) == t["ext"]) or (t.get("id") and x.get("id") == t["id"]) for x in found if isinstance(x, dict)
        )

    def _forget_title(self, what, titles):
        """Drop the lookups Get it needs fresh for these titles."""
        with self._lock:
            for t in titles:
                self._lookups.pop((t["kind"], t.get("ext")), None)

    def _changed(self, what, kind, item, **kw):
        """Tell State a title changed (see State.title_changed)."""
        self.state.title_changed(what, [ident(kind, item)], **kw)

    # helpers ---------------------------------------------------------------------
    def _remember(self, kind, ext_id, obj):
        with self._lock:
            self._lookups[(kind, ext_id)] = obj
            self._lookups.move_to_end((kind, ext_id))
            while len(self._lookups) > 300:
                self._lookups.popitem(last=False)

    def _art_key(self, url):
        if not url:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        with self._lock:
            self.remote_art[key] = url
            self.remote_art.move_to_end(key)
            while len(self.remote_art) > 2000:
                self.remote_art.popitem(last=False)
        return remote_art(key)

    @staticmethod
    def profile_env(app):
        """The usher.toml key holding this PVR's quality profile ID."""
        return "pvr.tv.quality_profile_id" if app.name == "sonarr" else "pvr.movies.quality_profile_id"

    def profile_choice(self, app):
        """(profile id, how it was found). A profile ID from the settings is used as is, so renaming the profile
        changes nothing, and a deleted one is an error rather than a silent switch to another profile.
        Without an ID, quality.profile_name is looked up ("name"), then the first profile whose name contains
        quality.fallback_contains, then the first profile ("fallback")."""
        profiles = app.get("qualityprofile")
        wanted = str(config.get(self.profile_env(app)) or "")
        if wanted:
            for p in profiles:
                if str(p.get("id")) == wanted:
                    return p["id"], "id"
            raise Problem(f"{app.name.title()}: quality profile {wanted} ({self.profile_env(app)}) no longer exists")
        for p in profiles:
            if p.get("name", "").lower() == self.quality.lower():
                return p["id"], "name"
        for p in profiles:
            if self.quality_fallback and self.quality_fallback.lower() in p.get("name", "").lower():
                return p["id"], "fallback"
        if profiles:
            return profiles[0]["id"], "fallback"
        raise Problem("No quality setting found. Check the app's profiles.")

    def _profile(self, app):
        return self.profile_choice(app)[0]

    def family_title(self, obj) -> bool:
        """Ratings in family.ratings (G, PG and the TV equivalents by default) count as family-friendly; decided from
        Sonarr's or Radarr's rating."""
        return str(obj.get("certification") or "").strip().upper() in self.family_ratings

    def family_enabled(self):
        return {"show": bool(self.family_tv_root), "movie": bool(self.family_movie_root)}

    def _family_root(self, app, wanted):
        """A family-friendly folder must exist exactly; never fall back to the main library."""
        roots = [r.get("path", "").rstrip("/") for r in app.get("rootfolder")]
        if wanted.rstrip("/") not in roots:
            raise Problem(f"The family-friendly folder isn't set up in {app.name.title()} yet.")
        return wanted

    def _root(self, app, wanted):
        roots = [r.get("path", "").rstrip("/") for r in app.get("rootfolder")]
        if wanted.rstrip("/") in roots:
            return wanted
        if roots:
            return roots[0]
        raise Problem("No library folder is set up.")

    @staticmethod
    def search_ttl():
        """Seconds a lookup is reused; read from here each time so it can be changed for one run."""
        return SEARCH_TTL

    # get it ----------------------------------------------------------------------
    def get(self, kind: str, ext_id: int, choice: str, user, family=None):
        """family: None = decide from the rating (the normal case); True/False only from tests or scripts."""
        if kind == "show":
            return self._get_show(ext_id, choice if choice in TV_MONITOR else "latest", user, family)
        if kind == "movie":
            return self._get_movie(ext_id, user, family)
        raise Problem("Unknown kind of item.")

    def _lookup(self, kind, ext_id):
        with self._lock:
            obj = self._lookups.get((kind, ext_id))
        if obj:
            return obj
        if kind == "show":
            found = self.sonarr.get("series/lookup", term=f"tvdb:{ext_id}")
        else:
            found = self.radarr.get("movie/lookup", term=f"tmdb:{ext_id}")
        if not found:
            raise Problem("Couldn't find that any more. Try searching again.")
        return found[0]

    def _known(self, kind, ext_id):
        """The library id if Sonarr or Radarr already has this, using what the poller last saw."""
        if self.titles.is_hidden(f"{kind}:{ext_id}"):
            return None  # removed a moment ago; the poller hasn't looked since
        pool, field = (self.state._raw["series"], "tvdbId") if kind == "show" else (self.state._raw["movies"], "tmdbId")
        return next((x["id"] for x in pool if x.get(field) == ext_id), None)

    def _forget_lookup(self, kind, ext_id):
        with self._lock:
            self._lookups.pop((kind, ext_id), None)

    def _get_show(self, tvdb_id, choice, user, family=False):
        known = self._known("show", tvdb_id)
        s = dict(self._lookup("show", tvdb_id))
        if known or s.get("id"):
            return {"ok": True, "already": True, "kind": "show", "id": known or s["id"]}
        monitor, search = TV_MONITOR[choice]
        if family is None:
            family = self.family_title(s)
        root = self._family_root(self.sonarr, self.family_tv_root) if family and self.family_tv_root else self._root(self.sonarr, self.tv_root)
        s.update(
            qualityProfileId=self._profile(self.sonarr),
            rootFolderPath=root,
            monitored=True,
            seasonFolder=True,
            seriesType=s.get("seriesType") or "standard",
            addOptions={"monitor": monitor, "searchForMissingEpisodes": search, "searchForCutoffUnmetEpisodes": False},
        )
        s.pop("id", None)
        try:
            added = self.sonarr.send("POST", "series", s)
        except ApiError as e:
            if "already" in str(e).lower():
                raise Problem("That show is already in your library.") from None
            raise
        self._forget_lookup("show", tvdb_id)
        rid = self.store.add_request("show", added["id"], tvdb_id, added.get("title", s.get("title", "")), added.get("year"), choice, user["user_id"])
        log.info("%s asked for show %s (%s%s)", user["username"], added.get("title"), choice, ", family-friendly" if family else "")
        self._changed("added", "show", {**s, **added})
        return {"ok": True, "kind": "show", "id": added["id"], "rid": rid}

    def _get_movie(self, tmdb_id, user, family=None):
        m = dict(self._lookup("movie", tmdb_id))
        if family is None:
            family = self.family_title(m)
        family_root = self._family_root(self.radarr, self.family_movie_root) if family and self.family_movie_root else None
        known = self._known("movie", tmdb_id)
        if known and not m.get("id"):
            m = dict(self.radarr.get(f"movie/{known}"))
        if m.get("id"):
            # Already known to Radarr. If it has no file (the existing library is unmonitored), ask for it now.
            if m.get("hasFile"):
                return {"ok": True, "already": True, "kind": "movie", "id": m["id"]}
            current = self.radarr.get(f"movie/{m['id']}")
            current["monitored"] = True
            if family_root and not (current.get("path") or "").startswith(family_root + "/"):
                # No file yet, so only the record's folder changes; nothing on disk moves.
                current["path"] = f"{family_root}/{(current.get('path') or '').rstrip('/').split('/')[-1]}"
                current["rootFolderPath"] = family_root
            self.radarr.send("PUT", f"movie/{m['id']}", current)
            self.radarr.send("POST", "command", {"name": "MoviesSearch", "movieIds": [m["id"]]})
            movie_id, title, year = m["id"], current.get("title"), current.get("year")
            record = current
        else:
            m.update(
                qualityProfileId=self._profile(self.radarr),
                rootFolderPath=family_root or self._root(self.radarr, self.movie_root),
                monitored=True,
                minimumAvailability="released",
                addOptions={"searchForMovie": True, "monitor": "movieOnly"},
            )
            m.pop("id", None)
            try:
                added = self.radarr.send("POST", "movie", m)
            except ApiError as e:
                if "already" in str(e).lower():
                    raise Problem("That movie is already in your library.") from None
                raise
            movie_id, title, year = added["id"], added.get("title", m.get("title", "")), added.get("year")
            record = {**m, **added}
        self._forget_lookup("movie", tmdb_id)
        open_req = [r for r in self.store.open_requests() if r["kind"] == "movie" and r["arr_id"] == movie_id]
        rid = open_req[0]["id"] if open_req else self.store.add_request("movie", movie_id, tmdb_id, title, year, "", user["user_id"])
        log.info("%s asked for movie %s%s", user["username"], title, " (family-friendly)" if family_root else "")
        self._changed("added", "movie", record)
        return {"ok": True, "kind": "movie", "id": movie_id, "rid": rid}

    # seasons of a show already in the library ------------------------------------
    def seasons(self, series_id: int):
        s = self.sonarr.get(f"series/{series_id}")
        eps = self.sonarr.get("episode", seriesId=series_id)
        mine = [r for r in self.store.open_requests() if r["kind"] == "show" and r["arr_id"] == series_id]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return {"id": series_id, "title": s.get("title", ""), "seasons": season_rows(s.get("seasons"), eps, mine, now)}

    def get_season(self, series_id: int, number: int, user):
        """Download one season of a show already in the library, and follow it on Home like any request."""
        s = self.sonarr.get(f"series/{series_id}")
        season = next((x for x in s.get("seasons") or [] if x.get("seasonNumber") == number), None)
        if number <= 0 or season is None:
            raise Problem("That season isn't in the TV listings.")
        season["monitored"] = True
        s["monitored"] = True
        self.sonarr.send("PUT", f"series/{series_id}", s)
        # Sonarr usually follows the season's switch, but older episodes can stay unwanted, so set them too.
        ids = [
            e["id"]
            for e in self.sonarr.get("episode", seriesId=series_id)
            if e.get("seasonNumber") == number and not e.get("hasFile") and not e.get("monitored")
        ]
        if ids:
            self.sonarr.send("PUT", "episode/monitor", {"episodeIds": ids, "monitored": True})
        self.sonarr.send("POST", "command", {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": number})
        choice = f"season:{number}"
        same = [r for r in self.store.open_requests() if r["kind"] == "show" and r["arr_id"] == series_id and r["choice"] == choice]
        title = f"{s.get('title', 'TV show')} · Season {number}"
        rid = same[0]["id"] if same else self.store.add_request("show", series_id, s.get("tvdbId") or 0, title, s.get("year"), choice, user["user_id"])
        self.state.drop_cache()
        log.info("%s asked for %s", user["username"], title)
        self._changed("added", "show", s, season=number)
        return {"ok": True, "kind": "show", "id": series_id, "rid": rid, "title": title}

    # remove ----------------------------------------------------------------------
    def remove(self, kind: str, item_id: int, user):
        app = self.sonarr if kind == "show" else self.radarr
        field = "seriesId" if kind == "show" else "movieId"
        try:
            queue = app.get("queue", pageSize=200)["records"]
        except ApiError:
            queue = []
        for rec in queue:
            if rec.get(field) == item_id:
                app.send("DELETE", f"queue/{rec['id']}", removeFromClient="true", blocklist="false")
        path = f"series/{item_id}" if kind == "show" else f"movie/{item_id}"
        item = app.get(path)
        title = item.get("title", "")
        exclusion = "addImportListExclusion" if kind == "show" else "addImportExclusion"
        app.send("DELETE", path, deleteFiles="true", **{exclusion: "false"})
        self.store.forget_item(kind, item_id)
        self.state.drop_cache()
        log.info("%s removed %s %s", user["username"], kind, title)
        # Gone everywhere at once: off Home until the media server has caught up (it's told now), out of Search,
        # Library and Upcoming, and every open device is told.
        t = ident(kind, item)
        self.state.title_changed("removed", [t], hide=True, gone=True, paths=[t["path"]])
        return {"ok": True}

    # recycle bin -------------------------------------------------------------------
    def empty_recycle(self, user, recycle_dir):
        """Delete everything in the recycle bin now, instead of waiting for the app to empty it."""
        import shutil

        root = os.path.realpath(recycle_dir)
        if not os.path.isdir(root) or root in ("/", "/srv", "/srv/media"):
            raise Problem("The recycle bin isn't set up on this server.")
        removed = 0
        for entry in os.scandir(root):
            path = os.path.join(root, entry.name)
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    continue
            removed += 1
        log.info("%s emptied the recycle bin (%d item(s))", user["username"], removed)
        self.state.refresh_now()
        return {"ok": True, "removed": removed}
