"""What people do on Tidy up: remove a watched title, keep one off the list, put a kept one back."""

import logging
import time

from . import tidy
from .clients import ApiError
from .problem import Problem
from .titles import ident

log = logging.getLogger("usher.actions")


class TidyActions:
    def tidy_remove(self, kind: str, item_id: int, season, user):
        """Remove one watched movie or season picked from the tidy-up list: unmonitored so it isn't downloaded
        again, files to the recycle bin (if Sonarr or Radarr has one). Only what's still on the list can go."""
        self.state.drop_cache()
        listed = self.state.tidy()["items"]
        if not any(i["kind"] == kind and i["id"] == item_id and i["season"] == season for i in listed):
            raise Problem("That isn't on the tidy-up list any more (someone may still be watching it).")
        app = self.radarr if kind == "movie" else self.sonarr
        try:
            record = app.get(f"movie/{item_id}" if kind == "movie" else f"series/{item_id}")
        except ApiError:
            record = {"id": item_id}
        title = tidy.remove(self.sonarr, self.radarr, kind, item_id, season)
        # Its request (if any) is finished; keep it off Home so it isn't looked for again.
        # (A show's other requests still waiting for a different season are left alone.)
        if kind == "movie":
            self.store.run("UPDATE requests SET hidden = 1, done_at = COALESCE(done_at, ?) WHERE kind = 'movie' AND arr_id = ?", (int(time.time()), item_id))
        else:
            self.store.run(
                "UPDATE requests SET hidden = 1, done_at = COALESCE(done_at, ?) WHERE kind = 'show' AND arr_id = ? AND (done_at IS NOT NULL OR choice = ?)",
                (int(time.time()), item_id, f"season:{season}"),
            )
        self.state.drop_cache()
        log.info("%s tidied away %s", user["username"], title)
        # The movie or show stays in Radarr or Sonarr; its files have gone, so the media server rescans its folder,
        # and until then Home leaves it (or that season) out.
        t = ident("movie" if kind == "movie" else "show", record)
        self.state.title_changed("removed", [t], hide=True, season=season, paths=[t["path"]], how="Modified")
        return {"ok": True, "title": title}

    def tidy_call(self, op: str, data: dict, user):
        """The POST routes under /api/tidy/: remove, keep (both name a movie or one season) and put-back (a key)."""
        if op == "put-back":
            return self.tidy_put_back(str(data.get("key", "")), user)
        kind = data.get("kind")
        if op not in ("remove", "keep") or kind not in ("movie", "season"):
            raise ValueError(f"unknown tidy-up request {op} {kind}")
        season = int(data["season"]) if kind == "season" else None
        act = self.tidy_remove if op == "remove" else self.tidy_keep
        return act(kind, int(data.get("id", 0)), season, user)

    def tidy_keep(self, kind: str, item_id: int, season, user):
        """Take a title off the tidy-up list for good (every season of a show). Nothing is deleted; Put back undoes it."""
        self.state.drop_cache()
        listed = self.state.tidy()["items"]
        found = next((i for i in listed if i["kind"] == kind and i["id"] == item_id and i["season"] == season), None)
        if found is None:
            raise Problem("That isn't on the tidy-up list any more.")
        if not found.get("key"):
            raise Problem("This title can't be kept: it has no catalogue ID yet.")
        self.state.kept.keep(found["key"])
        self.state.drop_cache()
        log.info("%s kept %s out of tidy-up", user["username"], found["title"])
        return {"ok": True, "title": found["title"]}

    def tidy_put_back(self, key: str, user):
        """Return a kept title to tidy-up; it is listed there again if it still qualifies."""
        if key not in self.state.kept.keys():
            raise Problem("That title isn't kept.")
        self.state.kept.put_back(key)
        self.state.drop_cache()
        log.info("%s put a kept title back on tidy-up", user["username"])
        return {"ok": True}
