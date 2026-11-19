"""The request handler's sign-in and session routes: sign in, who am I, sign out, sign out other devices."""

import logging
from http import HTTPStatus

from . import __version__, config
from .auth import token_hash

log = logging.getLogger("usher")


def describe_device(ua: str) -> str:
    """'iPhone', 'iPad' or 'Mac' from a user agent, for the list of signed-in devices."""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Macintosh" in ua:
        return "Mac or iPad"  # iPadOS Safari reports itself as a Mac
    return "Other device"


class AuthRoutes:
    def login(self, data):
        if not data:
            return self.err(HTTPStatus.BAD_REQUEST, "Enter your username and password.")
        device = self.headers.get("User-Agent", "")
        token, problem = self.app.auth.sign_in(str(data.get("username", ""))[:64], str(data.get("password", ""))[:256], self.client_ip(), device)
        if not token:
            return self.err(HTTPStatus.UNAUTHORIZED, problem)
        s = self.app.store.session(token_hash(token))
        return self.send(200, self._me_body(s), headers={"Set-Cookie": self.app.auth.cookie(token)})

    def _me_body(self, s):
        devices = [
            {"device": describe_device(d["device"]), "lastSeen": d["last_seen"], "current": d["id_hash"] == s["id_hash"]}
            for d in self.app.store.sessions_for(s["user_id"])
        ]
        return {
            "account": s["user_id"],
            "admin": bool(s["admin"]),
            "username": s["username"],
            "name": s["display"],
            "csrf": s["csrf"],
            "devices": devices,
            "baseline": self.app.store.baseline(s["user_id"]),
            "opened": self.app.store.opened(s["user_id"]),
            "jellyfinWeb": config.get("media_server.public_url"),
            "connectShortcut": config.get("server.shortcut_url"),
            "jellyfinApp": self.app.jf.app_link(),
            "familyFriendly": self.app.actions.family_enabled(),
            "version": __version__,
        }

    def me(self, s):
        return self.send(200, self._me_body(s))

    def logout(self, s):
        self.app.store.drop_session(s["id_hash"])
        log.info("signed out: %s", s["username"])
        # Clear-Site-Data: the browser's own copies of pictures go too (the app deletes what it keeps itself).
        return self.send(200, {"ok": True}, headers={"Set-Cookie": self.app.auth.cookie("", 0), "Clear-Site-Data": '"cache"'})

    def sign_out_others(self, s):
        n = self.app.store.drop_other_sessions(s["user_id"], s["id_hash"])
        log.info("%s signed out %d other device(s)", s["username"], n)
        return self.send(200, {"ok": True, "signedOut": n})
