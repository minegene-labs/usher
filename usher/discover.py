"""Discover: trending, popular, and what's on each streaming service, from TMDb (streaming data by JustWatch).

Only the server talks to TMDb. Devices get posters and logos through Usher's poster proxy.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import config
from .search import _ratings
from .cache import MISSING, TTLCache
from .clients import ApiError
from .discover_rows import DiscoverRows
from .picks import Picks
from .titles import Titles

log = logging.getLogger("usher.discover")
API = os.environ.get("TMDB_API_BASE", "https://api.themoviedb.org/3")  # overridden only in tests
IMG = "https://image.tmdb.org/t/p"
LIST_TTL = 6 * 3600
ITEM_TTL = 24 * 3600


class Discover(Picks, DiscoverRows):
    def __init__(self, actions, state, data_dir=None):
        self.token = config.secret("TMDB_TOKEN")
        self.old_key = bool(re.fullmatch(r"[0-9a-f]{32}", self.token))
        self._asked_for_token = False
        self.region = config.get("discover.region").upper()
        self.language = config.get("discover.language") or "en-US"
        self.upcoming_language = config.get("discover.upcoming_language") or None  # None: every language
        self.upcoming_days = config.get("discover.upcoming_days")
        self.actions, self.state = actions, state
        self._cache = TTLCache(LIST_TTL, 500)  # TMDb answers: no library marks in them, so no title change clears them
        self._lock = threading.Lock()
        self._fallback = TTLCache(6 * 3600, 1000)  # (app, id) -> poster URL or None (a remembered miss)
        self._upcoming = TTLCache(self.UPCOMING_TTL, 1)  # the list upcoming_shows serves, under one key
        if isinstance(getattr(state, "titles", None), Titles):
            state.titles.clears(self._fallback, self._fallback_holds)
        self._upcoming_busy = threading.Lock()
        self._upcoming_pool = ThreadPoolExecutor(max_workers=8)  # its own, so Discover and search never queue behind it
        self._stream_pool = ThreadPoolExecutor(max_workers=8)  # search badges; its own for the same reason
        self._rows_init(data_dir)  # Discover's rows: discover_rows.py
        self._picks_init()  # each account's own Picked for you: picks.py

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # TMDb -----------------------------------------------------------------------------
    def _get(self, path, ttl=LIST_TTL, **params):
        params.setdefault("language", self.language)
        params = {k: v for k, v in params.items() if v is not None}  # language=None: every language
        key = path + "?" + urllib.parse.urlencode(sorted(params.items()))
        hit = self._cache.get(key, MISSING, ttl=ttl)
        if hit is not MISSING:
            return hit
        # TMDb's long "API Read Access Token" goes in a header, out of logs and proxies. The older
        # 32-character "API Key" only works as a query parameter, so it's still accepted, with a nudge to switch.
        headers = {"Accept": "application/json"}
        query = dict(params)
        if self.old_key:
            query["api_key"] = self.token
            if not self._asked_for_token:
                self._asked_for_token = True
                log.warning(
                    "TMDB_TOKEN holds TMDb's older API key; set it to the API Read Access Token from "
                    "TMDb's API settings instead, so the key stays out of web addresses"
                )
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{API}{path}?{urllib.parse.urlencode(query)}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise ApiError(f"tmdb: HTTP {e.code}", status=e.code) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f"tmdb: {e}") from None
        self._cache.set(key, data)
        return data

    def _art(self, path, size="w342"):
        return self.actions._art_key(f"{IMG}/{size}{path}") if path else None

    def _card(self, it, kind=None):
        kind = kind or ("movie" if it.get("media_type") == "movie" else "show" if it.get("media_type") == "tv" else None)
        if kind is None:
            return None
        date = it.get("release_date") if kind == "movie" else it.get("first_air_date")
        return {
            "kind": kind,
            "tmdb": it.get("id"),
            "title": it.get("title") or it.get("name") or "",
            "year": int(date[:4]) if date and date[:4].isdigit() else None,
            "overview": (it.get("overview") or "")[:500],
            "art": self._art(it.get("poster_path")),
            "rating": round(it.get("vote_average") or 0, 1),
            "ratings": self._tmdb_score(it),
        }

    @staticmethod
    def _tmdb_score(it):
        """TMDb's own score, only once enough people have voted for it to mean something."""
        return {"tmdb": round(it["vote_average"], 1)} if it.get("vote_average") and (it.get("vote_count") or 0) >= 20 else {}

    def _streams(self, info):
        """Services in this region that include it with a subscription, free or with ads, in TMDb's order."""
        where = ((info.get("watch/providers") or {}).get("results") or {}).get(self.region) or {}
        seen, out = set(), []
        for p in (where.get("flatrate") or []) + (where.get("free") or []) + (where.get("ads") or []):
            if p.get("provider_name") and p["provider_name"] not in seen:
                seen.add(p["provider_name"])
                out.append({"name": p["provider_name"], "logo": self._art(p.get("logo_path"), "w92")})
        return out, bool(where.get("rent") or where.get("buy"))

    def _details(self, kind, tmdb_id):
        path = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"
        return self._get(path, ttl=ITEM_TTL, append_to_response="external_ids,watch/providers")

    # posters Sonarr or Radarr don't have ----------------------------------------------------
    def poster_for(self, kind, tmdb_id=None, tvdb_id=None):
        """TMDb's poster URL for a title, found by TMDb id (or, for a show, by TheTVDB id). None if TMDb has none."""
        if not self.enabled:
            return None
        if not tmdb_id and kind == "show" and tvdb_id:
            found = self._get(f"/find/{int(tvdb_id)}", ttl=ITEM_TTL, external_source="tvdb_id")
            hit = (found.get("tv_results") or [None])[0]
            if not hit:
                return None
            if hit.get("poster_path"):
                return f"{IMG}/w342{hit['poster_path']}"
            tmdb_id = hit.get("id")
        if not tmdb_id:
            return None
        path = self._details(kind, int(tmdb_id)).get("poster_path")
        if path:
            return f"{IMG}/w342{path}"
        # No main poster yet (common before a show airs): any poster TMDb holds, in any language, then a
        # backdrop (cropped to fit, but better than a blank).
        imgs = self._get(
            f"/{'movie' if kind == 'movie' else 'tv'}/{int(tmdb_id)}/images",
            ttl=ITEM_TTL,
            language=None,
            include_image_language="en,null," + self.language.split("-")[0],
        )
        for group in ("posters", "backdrops"):
            for img in sorted(imgs.get(group) or [], key=lambda i: -(i.get("vote_average") or 0)):
                if img.get("file_path"):
                    return f"{IMG}/w342{img['file_path']}" if group == "posters" else f"{IMG}/w780{img['file_path']}"
        return None

    def arr_fallback(self, app_name, num_id, ttl=6 * 3600):
        """Poster URL from TMDb for a Sonarr series or Radarr movie that has no picture of its own.
        Remembered for 6 hours, misses too, so a page full of blank posters costs one lookup each."""
        key = (app_name, int(num_id))
        hit = self._fallback.get(key, MISSING, ttl=ttl)
        if hit is not MISSING:
            return hit
        url = None
        try:
            if app_name == "sonarr":
                s = self.actions.sonarr.get(f"series/{int(num_id)}")
                url = self.poster_for("show", s.get("tmdbId"), s.get("tvdbId"))
            else:
                m = self.actions.radarr.get(f"movie/{int(num_id)}")
                url = self.poster_for("movie", m.get("tmdbId"))
        except (ApiError, AttributeError, TypeError, ValueError) as e:
            log.info("poster fallback for %s %s: %s", app_name, num_id, e)
        self._fallback.set(key, url)
        return url

    @staticmethod
    def _fallback_holds(key, url, t):
        """A changed title's poster, and a miss remembered for it, are looked up again."""
        return key == ({"show": "sonarr", "movie": "radarr"}.get(t["kind"]), t.get("id"))

    # search results: where it's streaming, and a score for shows ------------------------------
    def annotate(self, results, wait=6.0):
        """Adds `streaming` (service names) to search results, and TMDb's score where Radarr gave none.
        Shares the cache with the detail sheet, so opening a result afterwards is instant. Anything TMDb
        hasn't answered within `wait` seconds simply gets no badge."""
        if not self.enabled:
            return results
        jobs = [(r, self._stream_pool.submit(self._details, r["kind"], r["tmdb"])) for r in results if r.get("tmdb")]
        deadline = time.monotonic() + wait
        for r, fut in jobs:
            try:
                info = fut.result(timeout=max(0.05, deadline - time.monotonic()))
            except Exception as e:  # noqa: BLE001  a badge is optional; the result still shows
                log.info("search badge for %s %s: %s", r["kind"], r["tmdb"], e)
                continue
            r["providers"], r["rentable"] = self._streams(info)
            r["streaming"] = [s["name"] for s in r["providers"]]
            if not r.get("ratings", {}).get("imdb"):
                r.setdefault("ratings", {}).update(self._tmdb_score(info))
            if not r.get("art") and info.get("poster_path"):  # Sonarr or Radarr found no picture
                r["art"] = self._art(info["poster_path"])
        return results

    def _radarr_lookup(self, tmdb_id, wait=5.0):
        """Radarr's lookup for a movie (IMDb and Rotten Tomatoes scores). Kept for Get it, so that is quicker too."""
        try:
            found = self.actions._pool.submit(self.actions._lookup, "movie", tmdb_id).result(timeout=wait)
        except Exception as e:  # noqa: BLE001  scores are optional
            log.info("radarr lookup for scores, tmdb %s: %s", tmdb_id, e)
            return None
        self.actions._remember("movie", tmdb_id, found)
        return found

    def _mark_library(self, items):
        """`have`: in the library to watch; `asked`: requested but nothing downloaded yet."""
        have, asked = set(), set()
        for m in self.state._raw.get("movies", []):
            if m.get("hasFile"):
                have.add(("movie", m.get("tmdbId")))
            elif m.get("monitored"):
                asked.add(("movie", m.get("tmdbId")))
        for x in self.state._raw.get("series", []):
            if not x.get("tmdbId"):
                continue
            files = (x.get("statistics") or {}).get("episodeFileCount") or 0
            (have if files else asked).add(("show", x["tmdbId"]))
        for c in items:
            key = (c["kind"], c["tmdb"])
            c["have"], c["asked"] = key in have, key in asked and key not in have

    # every upcoming show, not just the ones in Sonarr -----------------------------------------
    # TMDb's lists of shows premiering soon and shows starting a new season soon, in discover.upcoming_language
    # (English by default); news and talk shows left out, since they air daily and would crowd the list.
    UPCOMING_PAGES = 2
    SKIP_GENRES = "10763|10767"  # News, Talk

    def _today(self):
        offset = config.get("server.utc_offset_minutes")
        return datetime.now(timezone(timedelta(minutes=offset))).date()

    UPCOMING_TTL = 3 * 3600

    def start(self):
        """Builds Discover's rows soon after start-up and every hour, and All upcoming every 3 hours, so nobody waits."""
        if not self.enabled:
            return

        def rows_loop():
            time.sleep(5)
            while True:
                self._start_rows_build()
                time.sleep(self.ROWS_TTL)

        threading.Thread(target=rows_loop, name="discover-rows", daemon=True).start()

        def loop():
            time.sleep(20)  # let the integrations and the first poll settle first
            while True:
                self._refresh_upcoming()
                time.sleep(self.UPCOMING_TTL)

        threading.Thread(target=loop, name="upcoming", daemon=True).start()

    def _refresh_upcoming(self):
        if not self._upcoming_busy.acquire(blocking=False):
            return  # a build is already running
        try:
            items = self._build_upcoming()
            if items:  # an empty list is usually TMDb failing; keep what we had
                self._upcoming.set("list", items)
        except Exception as e:  # noqa: BLE001  the next run tries again
            log.warning("upcoming shows: %s", e)
        finally:
            self._upcoming_busy.release()

    def upcoming_shows(self):
        """The list built in the background; In library and Requested marks are fresh each time. Only the very first
        request after start-up, before the background build is done, waits for it."""
        if not self.enabled:
            return {"enabled": False, "items": []}
        hit = self._upcoming.peek("list")
        if not hit:
            self._refresh_upcoming()  # waits for a build already running, or runs one
            with self._upcoming_busy:
                pass
            hit = self._upcoming.peek("list")
        elif hit[1] >= self.UPCOMING_TTL:
            threading.Thread(target=self._refresh_upcoming, daemon=True).start()  # serve the old list meanwhile
        items = [dict(c) for c in (hit[0] if hit else [])]
        self._mark_library(items)
        return {"enabled": True, "days": self.upcoming_days, "items": items}

    def _build_upcoming(self):
        today = self._today()
        start, end = today.isoformat(), (today + timedelta(days=self.upcoming_days)).isoformat()
        common = dict(
            with_original_language=self.upcoming_language,
            without_genres=self.SKIP_GENRES,
            sort_by="popularity.desc",
            include_adult="false",
            include_null_first_air_dates="false",
        )
        found = {}  # tmdb id -> list entry, new shows first so a premiere isn't also listed as a new season
        for which, extra in (("new", {"first_air_date.gte": start, "first_air_date.lte": end}), ("season", {"air_date.gte": start, "air_date.lte": end})):
            for page in range(1, self.UPCOMING_PAGES + 1):
                try:
                    res = self._get("/discover/tv", page=page, **common, **extra).get("results") or []
                except ApiError as e:
                    log.warning("upcoming shows (%s, page %s): %s", which, page, e)
                    break
                for it in res:
                    if it.get("id") and it["id"] not in found and (it.get("poster_path") or it.get("overview")):
                        found[it["id"]] = (which, it)
                if len(res) < 20:
                    break

        def fetch(it):
            info = self._details("show", it["id"])
            art = None
            if not info.get("poster_path") and not it.get("poster_path"):
                url = self.poster_for("show", it["id"])  # any poster TMDb holds, then a backdrop
                art = self.actions._art_key(url) if url else None
            return info, art

        jobs = [(which, it, self._upcoming_pool.submit(fetch, it)) for which, it in found.values()]
        items = []
        for which, it, fut in jobs:
            try:
                info, art = fut.result(timeout=60)
            except Exception as e:  # noqa: BLE001  one show TMDb didn't answer for just isn't listed
                log.info("upcoming show %s: %s", it.get("id"), e)
                continue
            nxt = info.get("next_episode_to_air") or {}
            if which == "new":
                when, season = info.get("first_air_date") or it.get("first_air_date"), 1
            elif nxt.get("episode_number") == 1 and (nxt.get("season_number") or 0) > 1:
                when, season = nxt.get("air_date"), nxt["season_number"]
            else:
                continue  # a show already mid-season: Sonarr's own list covers the ones we follow
            if not when or not (start <= when <= end):
                continue
            card = self._card(info, "show")
            if card is None:
                continue
            if not card["art"]:
                card["art"] = self._art(it.get("poster_path")) or art
            if not card["art"]:
                continue  # no poster or backdrop anywhere in TMDb: usually a placeholder listing
            card["providers"], card["rentable"] = self._streams(info)
            card["streaming"] = [s["name"] for s in card["providers"]]
            card.update(
                when=when,
                season=season,
                new=which == "new",
                network=((info.get("networks") or [{}])[0] or {}).get("name"),
                genres=[g["name"] for g in info.get("genres") or [] if g.get("name")][:3],
                popularity=round(it.get("popularity") or 0, 1),
            )
            items.append(card)
        items.sort(key=lambda c: (c["when"], -c["popularity"]))
        return items

    # one title ---------------------------------------------------------------------------
    def item(self, kind: str, tmdb_id: int):
        if not self.enabled:
            raise ApiError("tmdb: not set up")
        info = self._details(kind, tmdb_id)
        unique, rentable = self._streams(info)
        card = self._card(info, kind)
        card["providers"] = unique
        card["rentable"] = rentable
        if kind == "movie":
            card["ext"] = tmdb_id
            card["runtime"] = info.get("runtime")
            lib = next((m for m in self.state._raw.get("movies", []) if m.get("tmdbId") == tmdb_id), None)
            imdb = _ratings((lib or self._radarr_lookup(tmdb_id) or {}).get("ratings"), "movie")
            if imdb:
                card["ratings"] = imdb
            card.update(inLibrary=bool(lib), libId=lib and lib["id"], hasFile=bool(lib and lib.get("hasFile")), monitored=bool(lib and lib.get("monitored")))
        else:
            tvdb = (info.get("external_ids") or {}).get("tvdb_id")
            card["ext"] = tvdb
            card["seasons"] = info.get("number_of_seasons")
            card["network"] = ((info.get("networks") or [{}])[0] or {}).get("name")
            lib = next((x for x in self.state._raw.get("series", []) if tvdb and x.get("tvdbId") == tvdb), None)
            st = (lib or {}).get("statistics") or {}
            card.update(inLibrary=bool(lib), libId=lib and lib["id"], have=st.get("episodeFileCount", 0), total=st.get("episodeCount", 0), noTvdb=not tvdb)
        by = self.actions.store.requesters().get((kind, card.get("libId")))
        card["by"] = by[0] if by else None
        return card
