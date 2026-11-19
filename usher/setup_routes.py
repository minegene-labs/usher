"""The request handler's setup and settings routes: the first account (setup.py), and an admin's settings and secrets
(saved_settings.py)."""

import logging
import re
from http import HTTPStatus

from . import config, saved_settings
from .auth import MIN_PASSWORD, hash_password, token_hash

log = logging.getLogger("usher")

USERNAME = re.compile(r"[a-z][a-z0-9_.-]{1,31}")


class SetupRoutes:
    def setup_open(self):
        """Whether setup is still open, answered for GET /api/setup. Once it is over the page is simply not found."""
        if not self.app.setup.open:
            return self.err(HTTPStatus.NOT_FOUND, "Not found")
        return self.send(200, {"open": True})

    def setup_create(self, data):
        """POST /api/setup: the setup code and the first account's details. The account is signed in at once."""
        if not self.app.setup.open:
            return self.err(HTTPStatus.NOT_FOUND, "Not found")
        if not self.same_origin():
            log.warning("setup refused: origin %r", self.headers.get("Origin"))
            return self.err(HTTPStatus.FORBIDDEN, "Request refused")
        code = str(data.get("code", ""))[:40]
        verdict = self.app.setup.check(code)
        if verdict == "closed":
            return self.err(HTTPStatus.NOT_FOUND, "Not found")
        if verdict == "paused":
            return self.err(HTTPStatus.TOO_MANY_REQUESTS, "Too many wrong codes. Wait a minute, then try again.")
        if verdict != "ok":
            return self.err(HTTPStatus.UNAUTHORIZED, "That setup code isn't right. It is in Usher's log.")
        username = str(data.get("username", "")).strip().lower()
        display = str(data.get("name", "")).strip()[:40] or username.capitalize()
        password = str(data.get("password", ""))[:256]
        if not USERNAME.fullmatch(username):
            return self.err(HTTPStatus.BAD_REQUEST, "A username is 2 to 32 characters: letters, numbers, dot, dash or underscore, starting with a letter.")
        if len(password) < MIN_PASSWORD:
            return self.err(HTTPStatus.BAD_REQUEST, f"Choose a password of at least {MIN_PASSWORD} characters.")
        try:
            done = self.app.setup.finish(code, username, display, hash_password(password))
        except OSError as e:
            log.error("setup can't save its settings file: %s", e.strerror)
            return self.err(HTTPStatus.INTERNAL_SERVER_ERROR, "Usher can't write to its data folder, so it can't be set up. Its log says more.")
        if not done:
            return self.err(HTTPStatus.NOT_FOUND, "Not found")
        token = self.app.auth.open_session(self.app.store.user_by_name(username), self.client_ip(), self.headers.get("User-Agent", ""))
        s = self.app.store.session(token_hash(token))
        return self.send(200, self._me_body(s), headers={"Set-Cookie": self.app.auth.cookie(token)})

    def settings_get(self, s, saved=False):
        if not s["admin"]:
            return self.err(HTTPStatus.FORBIDDEN, "Only an admin can see the settings.")
        try:
            body = saved_settings.describe()
        except config.ConfigError as e:
            return self.err(HTTPStatus.CONFLICT, str(e))
        return self.send(200, {**body, "ok": True, "restartNeeded": True} if saved else body)

    def settings_save(self, s, data):
        if not s["admin"]:
            return self.err(HTTPStatus.FORBIDDEN, "Only an admin can change the settings.")
        settings, secrets = data.get("settings", {}), data.get("secrets", {})
        if not isinstance(settings, dict) or not isinstance(secrets, dict):
            return self.err(HTTPStatus.BAD_REQUEST, "That request didn't make sense.")
        try:
            saved_settings.save(settings, secrets)
        except config.ConfigError as e:
            return self.err(HTTPStatus.BAD_REQUEST, str(e))
        except OSError as e:
            log.error("settings can't be saved: %s", e.strerror)
            return self.err(HTTPStatus.INTERNAL_SERVER_ERROR, "Usher can't write its settings. Its log says more.")
        return self.settings_get(s, saved=True)
