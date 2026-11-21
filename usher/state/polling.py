"""The poller: reads the integrations on a schedule and searches again for requests still waiting for a copy."""

import logging
import threading
import time

from ..actions import season_of
from ..clients import ApiError
from ..kept import Kept
from ..missing import MissingSearcher
from .helpers import couldnt_find_after, recycle_policy, recycle_usage, research_every, sites_not_responding

log = logging.getLogger("usher.state")

FAST, SLOW, IDLE = 2, 30, 15  # seconds: Transmission while downloading, arr apps, Transmission idle
# Sonarr and Radarr only watch the new-release feeds after the first search, and those never carry older titles,
# so Usher searches again for each waiting request on the [retries] schedule (hourly at first, then less often).


class Polling:
    def start(self):
        self.missing = MissingSearcher(self.sonarr, self.s.data_dir)
        self.kept = Kept(self.s.data_dir)
        threading.Thread(target=self._loop, name="poller", daemon=True).start()

    def _try(self, name, fn, default=None):
        try:
            value = fn()
            self._raw["ok"][name] = True
            return value
        except (ApiError, KeyError, TypeError, ValueError) as e:
            if self._raw["ok"].get(name, True):
                log.warning("%s unavailable: %s", name, e)
            self._raw["ok"][name] = False
            return default

    def _poll_slow(self):
        r = self._raw
        q = dict(pageSize=200, includeUnknownSeriesItems="false")
        r["queue_tv"] = self._try("sonarr", lambda: self.sonarr.get("queue", includeSeries="true", includeEpisode="true", **q)["records"], r["queue_tv"])
        r["missing_tv"] = self._try(
            "sonarr",
            lambda: self.sonarr.get(
                "wanted/missing", page=1, pageSize=100, sortKey="airDateUtc", sortDirection="descending", includeSeries="true", monitored="true"
            )["records"],
            r["missing_tv"],
        )
        r["queue_movies"] = self._try("radarr", lambda: self.radarr.get("queue", includeMovie="true", pageSize=200)["records"], r["queue_movies"])
        r["movies"] = self._try("radarr", lambda: self.radarr.get("movie"), r["movies"])
        r["series"] = self._try("sonarr", lambda: self.sonarr.get("series"), r["series"])
        self._outside(self.titles.sync("movie", r["movies"]) + self.titles.sync("show", r["series"]))
        self._close_orphans()
        r["health"] = {
            "TV": self._try("sonarr", lambda: self.sonarr.get("health"), []),
            "Movies": self._try("radarr", lambda: self.radarr.get("health"), []),
            "Search": self._try("prowlarr", lambda: self.prowlarr.get("health"), []),
        }
        r["sites_down"] = self._try("prowlarr", lambda: sites_not_responding(self.prowlarr.get("indexer"), self.prowlarr.get("indexerstatus")), None)
        r["disk"] = self._try("sonarr", lambda: self.sonarr.get("diskspace"), r["disk"])
        r["ready"] = self._try("jellyfin", lambda: self.jf.unplayed(60), r["ready"])
        self._played(r["ready"])
        r["ok"]["jellyfin_up"] = self.jf.healthy()
        r["recycle"] = recycle_usage(self.s.recycle_dir)
        self._poll_recycle_policy()
        r["home"] = self._try("jellyfin", self._jellyfin_home, r.get("home"))
        self._confirm_in_jellyfin()

    def _poll_recycle_policy(self):
        """Read each app's recycle bin setting, so people are only told they can get something back when they can."""
        r = self._raw
        policy = {}
        for key, app in (("tv", self.sonarr), ("movie", self.radarr)):
            policy[key] = self._try(app.name, lambda app=app: recycle_policy(app.get("config/mediamanagement")), (r.get("bin") or {}).get(key))
        r["bin"] = policy

    def _poll_fast(self):
        ts = self._try("transmission", self.tr.torrents, None)
        if ts is not None:
            self._raw["torrents"] = {t["hashString"].lower(): t for t in ts}

    def _loop(self):
        last_slow = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            # Poll the arr apps more often while someone is waiting on a request.
            slow = 10 if self._active_requests() else SLOW
            if started - last_slow >= slow or self._kick.is_set():
                self._kick.clear()
                self._poll_slow()
                last_slow = started
            self._poll_fast()
            with self._build_lock:
                snap = self._build()
                self._publish(snap)
            self._research(snap)
            if self.notifier:
                self.notifier.check(snap, self._raw)
            for worker in filter(None, (self.replacer, self.missing)):
                worker.tick(self._raw)
            busy = any(t.get("status") in (3, 4) for t in self._raw["torrents"].values())
            self._kick.wait(max(0.5, (FAST if busy else IDLE) - (time.monotonic() - started)))
            if self._stop.is_set():
                break

    def _research(self, snap):
        """Search again for requests that are still waiting for a copy (see [retries] in usher.toml)."""
        if not self.store:
            return
        now = time.time()
        waiting = {r["rid"] for r in snap.get("requests", []) if r.get("searching")}
        lists = {"movie": {m.get("id") for m in self._raw["movies"]}, "show": {x.get("id") for x in self._raw["series"]}}
        for req in self.store.open_requests():
            pool = lists.get(req["kind"])
            if req["id"] not in waiting or (pool and req["arr_id"] not in pool):
                continue  # not waiting, or no longer in Sonarr or Radarr: nothing to search for
            last = req["searched"]
            age = now - req["created"]
            due = research_every(last is not None, age >= couldnt_find_after())
            if now - (last or req["created"]) < due:
                continue
            if req["kind"] == "movie":
                app, body = self.radarr, {"name": "MoviesSearch", "movieIds": [req["arr_id"]]}
            elif season_of(dict(req).get("choice")) is not None:
                app, body = self.sonarr, {"name": "SeasonSearch", "seriesId": req["arr_id"], "seasonNumber": season_of(dict(req).get("choice"))}
            else:
                app, body = self.sonarr, {"name": "SeriesSearch", "seriesId": req["arr_id"]}
            ok = self._try(app.name, lambda: app.send("POST", "command", body) or True)
            if ok is not None:
                self.store.run("UPDATE requests SET searched = ? WHERE id = ?", (int(now), req["id"]))
                log.info("searching again: %s", req["title"])

    def _close_orphans(self):
        """Requests whose movie or show is no longer in Radarr or Sonarr are closed, not looked for again."""
        if not self.store:
            return
        lists = {"movie": self._raw["movies"], "show": self._raw["series"]}
        now = time.time()
        for req in self.store.open_requests():
            pool = lists.get(req["kind"]) or []
            if pool and now - req["created"] > 300 and not any(x.get("id") == req["arr_id"] for x in pool):
                self.store.forget_item(req["kind"], req["arr_id"])
                log.info("closed request for %s: no longer in %s", req["title"], "Radarr" if req["kind"] == "movie" else "Sonarr")

    def refresh_now(self):
        """Ask the poller to check everything straight away, after an action."""
        self._kick.set()

    def stop(self):
        self._stop.set()
        self._kick.set()

    def _active_requests(self) -> bool:
        return bool(self.store and self.store.open_requests())
