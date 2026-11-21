"""The update script reports how an update ended and, on every run, the newest release it saw.

Usher keeps the last result for Settings → Advanced and the latest check for Settings → About → Updates."""

import json
import logging
import re
import time
from http import HTTPStatus

from . import __version__
from .changelog import changelog
from .files import write_private

log = logging.getLogger("usher.updates")

RESULTS = ("built", "rolled_back")
VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
# The timer runs once a day (up to an hour late); a check older than two days means the server has stopped looking
# (or can't reach GitHub).
STALE_AFTER = 2 * 24 * 3600


def local_only(client_ip: str, forwarded: str) -> bool:
    """Reached from the server itself, not through the reverse proxy (the rule /healthz uses for its details)."""
    return client_ip in ("127.0.0.1", "::1") and not forwarded


def valid_check(check) -> bool:
    """A check as _report_check stores it; a file edited or damaged by hand is ignored rather than breaking Settings."""
    if not isinstance(check, dict):
        return False
    at = check.get("at")
    return (
        isinstance(check.get("newest"), str)
        and VERSION.fullmatch(check["newest"]) is not None
        and isinstance(check.get("held"), bool)
        and isinstance(check.get("failed"), bool)
        and isinstance(at, int)
        and not isinstance(at, bool)
    )


def version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


class UpdateResult:
    def __init__(self, data_dir):
        self.path = data_dir / "update.json"
        self.last, self.check = self._load()

    def _load(self):
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None, None
        except ValueError:
            log.warning("update.json can't be read; the last update result is forgotten")
            return None, None
        if not isinstance(saved, dict):
            return None, None
        if "result" in saved:  # written before the checks were kept: the file was the last result itself
            saved = {"last": saved}
        last, check = saved.get("last"), saved.get("check")
        if isinstance(last, dict) and last.get("result") in RESULTS:  # an older file also holds a commit id, which is dropped
            last = {key: last.get(key) for key in ("result", "version", "at")}
        else:
            last = None
        return last, check if valid_check(check) else None

    def _save(self):
        write_private(self.path, json.dumps({"last": self.last, "check": self.check}))

    def report(self, data: dict) -> dict:
        """Record what the update script says. Raises ValueError with a reason when it can't be used."""
        if data.get("result") == "checked":
            return self._report_check(data)
        result, version = data.get("result"), data.get("version")
        if result not in RESULTS:
            raise ValueError("result must be built, rolled_back or checked")
        if version != __version__:  # the script reports after the running version answered, so they agree
            raise ValueError("version isn't the one running")
        self.last = {"result": result, "version": version, "at": int(time.time())}
        self._save()
        return self.last

    def _report_check(self, data: dict) -> dict:
        newest, held, failed = data.get("newest"), data.get("held"), data.get("failed")
        if not isinstance(newest, str) or not VERSION.fullmatch(newest):
            raise ValueError("newest must be a version such as 1.2.3")
        if not isinstance(held, bool) or not isinstance(failed, bool):
            raise ValueError("held and failed must be true or false")
        self.check = {"newest": newest, "held": held, "failed": failed, "at": int(time.time())}
        self._save()
        return self.check

    def state(self, now: float | None = None) -> str:
        """What Settings → About says about updates: up_to_date, waiting, held, rolled_back or unknown (can't tell)."""
        check = self.check
        if check is None or (now if now is not None else time.time()) - check["at"] > STALE_AFTER:
            return "unknown"
        if version_key(check["newest"]) <= version_key(__version__):
            return "up_to_date"
        if check["failed"]:
            return "rolled_back"
        return "held" if check["held"] else "waiting"

    def view(self) -> dict:
        """What the signed-in app reads: nothing that names the server."""
        check = self.check and {"newest": self.check["newest"], "at": self.check["at"]}
        return {"last": self.last, "check": check, "state": self.state()}


class UpdateRoutes:
    """The request handler's part: the update state and last result, the Changelog, and the script's local-only report."""

    def update_get(self, path):
        if path == "/api/changelog":
            return self.send(200, {"versions": changelog()})
        return self.send(200, {**self.app.updates.view(), "release": self.app.releases.view()})

    def update_report(self, data):
        if not local_only(self.client_address[0], self.headers.get("X-Forwarded-For")):
            return self.err(HTTPStatus.NOT_FOUND, "Not found")
        try:
            self.app.updates.report(data)
        except ValueError as e:
            return self.err(HTTPStatus.BAD_REQUEST, str(e))
        return self.send(200, {"ok": True})
