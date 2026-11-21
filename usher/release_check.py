"""Asks GitHub for the newest release at most once a day, for Settings → About → Updates.

Usher never installs anything: updating a container is the host's job. The check is a single request to GitHub's
public API, sent only while `updates.check_releases` is "on". The last answer is kept in release.json so a restart
doesn't ask again before a day has passed."""

import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from . import __version__, config
from .files import write_private
from .update_result import VERSION, version_key

log = logging.getLogger("usher.releases")

LATEST = "https://api.github.com/repos/minegene-labs/usher/releases/latest"
NOTES = "https://github.com/minegene-labs/usher/releases/tag/v"
UPDATE_COMMAND = "docker compose pull && docker compose up -d"
EVERY = 24 * 3600
RETRY_AFTER = 3600  # when GitHub can't be reached, ask again sooner than a day
MAX_REPLY = 1_000_000
TAG = re.compile(r"v?([0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})")


def newest_from(reply) -> str:
    """The version a GitHub 'latest release' reply names. Raises ValueError when it names none we can use."""
    tag = reply.get("tag_name") if isinstance(reply, dict) else None
    found = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not found or reply.get("draft") or reply.get("prerelease"):
        raise ValueError("the reply names no release version")
    return found[1]


def fetch_newest() -> str:
    """The newest release's version. Raises OSError or ValueError, with a reason that holds nothing private."""
    req = urllib.request.Request(LATEST, headers={"Accept": "application/vnd.github+json", "User-Agent": "Usher"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return newest_from(json.loads(r.read(MAX_REPLY)))


class ReleaseCheck:
    def __init__(self, data_dir, fetch=fetch_newest):
        self.path = data_dir / "release.json"
        self.fetch = fetch
        self.newest, self.at, self.failed = self._load()
        self._wake = threading.Event()
        self._stop = False

    def _load(self):
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None, 0, False
        except ValueError:
            log.warning("release.json can't be read; the last release check is forgotten")
            return None, 0, False
        newest, at = saved.get("newest") if isinstance(saved, dict) else None, saved.get("at") if isinstance(saved, dict) else None
        ok = isinstance(newest, str) and VERSION.fullmatch(newest) and isinstance(at, int) and not isinstance(at, bool)
        return (newest, at, bool(saved.get("failed"))) if ok else (None, 0, False)

    @staticmethod
    def enabled() -> bool:
        return config.get("updates.check_releases") == "on"

    def due(self, now: float | None = None) -> bool:
        wait = RETRY_AFTER if self.failed else EVERY
        return (now if now is not None else time.time()) - self.at >= wait

    def check(self, now: float | None = None) -> bool:
        """Ask GitHub if the check is on and a day has passed. True when it asked."""
        if not self.enabled() or not self.due(now):
            return False
        try:
            self.newest = self.fetch()
            self.failed = False
        except (OSError, ValueError, http.client.HTTPException) as e:  # the message names the kind of failure, never a request or a reply
            log.warning("can't check for a newer release: %s", type(e).__name__)
            self.failed = True
        self.at = int(now if now is not None else time.time())
        write_private(self.path, json.dumps({"newest": self.newest, "at": self.at, "failed": self.failed}))
        return True

    def view(self) -> dict:
        """What the signed-in app reads."""
        out = {"enabled": self.enabled(), "failed": self.failed, "command": UPDATE_COMMAND}
        if self.newest:
            out.update(newest=self.newest, at=self.at, notes=NOTES + self.newest, available=version_key(self.newest) > version_key(__version__))
        return out

    def available(self) -> str | None:
        """The newest version when it is newer than the one running and the check is on."""
        if self.enabled() and self.newest and version_key(self.newest) > version_key(__version__):
            return self.newest
        return None

    # running --------------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, name="releases", daemon=True).start()

    def stop(self):
        self._stop = True
        self._wake.set()

    def _loop(self):
        while not self._stop:
            try:
                self.check()
            except Exception:  # the check must never take the server down
                log.exception("release check failed")
            self._wake.wait(900)
