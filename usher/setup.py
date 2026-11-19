"""Setup mode: while Usher has no accounts it prints a one-time setup code to its log, and that code lets one person
create the first account, who becomes the admin. Once the account exists setup is over for good: the code is gone and
no request can bring it back (a restart finds the account and prints no code).

The code is kept in memory only, so it is new at every start. It is compared in constant time, and five wrong tries
are followed by a minute in which every try is refused, so it can't be guessed and nobody can keep it changing.
"""

import hmac
import logging
import secrets
import threading
import time

from . import saved_settings

log = logging.getLogger("usher.setup")

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0, O, 1, I or L, which look alike when read from a log
CODE_LENGTH = 12
MAX_WRONG = 5
PAUSE = 60  # seconds in which every try is refused after MAX_WRONG wrong ones


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + 4] for i in range(0, CODE_LENGTH, 4))


def normalise(code: str) -> str:
    return "".join(c for c in str(code).upper() if c.isalnum())


def is_setting_up(store, source) -> bool:
    """True for an install that has no accounts yet, or whose admin made the account in the browser and hasn't chosen
    the viewing account yet. An install made the old way has neither, so it starts as strictly as before."""
    if store.user_count() == 0:
        return True
    return store.has_admin() and source.values is not None and not source.values.get("media_server.user_id")


class Setup:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._code = None
        self._wrong = 0
        self._paused_until = 0.0

    @property
    def open(self) -> bool:
        with self._lock:
            if self._code is not None and self.store.user_count() > 0:
                self._code = None  # an account was added another way (usher-user): setup is over
            return self._code is not None

    def begin(self):
        """Start-up: with no accounts, make a code and print it."""
        with self._lock:
            if self.store.user_count() == 0:
                self._renew()

    def _renew(self):
        self._code = new_code()
        self._wrong = 0
        log.warning("Setup code: %s  Open Usher in a browser and enter it to create the first account.", self._code)

    def check(self, code: str) -> str:
        """'ok', 'wrong', 'paused' (too many wrong codes: wait a minute) or 'closed'."""
        with self._lock:
            if self._code is None:
                return "closed"
            if time.monotonic() < self._paused_until:
                return "paused"
            if hmac.compare_digest(normalise(code).encode(), normalise(self._code).encode()):
                return "ok"
            self._wrong += 1
            log.warning("setup refused: wrong code (%d of %d)", self._wrong, MAX_WRONG)
            if self._wrong >= MAX_WRONG:
                self._wrong = 0
                self._paused_until = time.monotonic() + PAUSE
                return "paused"
            return "wrong"

    def finish(self, code: str, username: str, display: str, pw_hash: str) -> bool:
        """Create the first account and close setup, if the code is still the right one. False when it isn't, or
        another account got there first."""
        with self._lock:
            if self._code is None or time.monotonic() < self._paused_until or not hmac.compare_digest(normalise(code).encode(), normalise(self._code).encode()):
                return False
            saved_settings.ensure_file()  # raises OSError if usher.toml can't be made: no account without somewhere for settings
            if not self.store.add_first_user(username, display, pw_hash):
                self._code = None
                return False
            self._code = None
        log.info("setup finished: the first account, %s, is the admin", username)
        return True
