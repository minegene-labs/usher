"""Passwords, sessions and sign-in throttling."""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time

log = logging.getLogger("usher.auth")

# scrypt: memory-hard, in the standard library, so Usher has no third-party dependencies.
N, R, P = 2**15, 8, 1  # about 32 MB and ~100 ms per check on an older laptop CPU
MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD = 12
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
IP_WINDOW = 15 * 60
IP_LIMIT = 20

COOKIE = "__Host-usher"  # __Host- forces Secure, Path=/ and no Domain


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, maxmem=MAXMEM, dklen=32)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(dk)}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=MAXMEM, dklen=len(_unb64(dk)))
        return hmac.compare_digest(got, _unb64(dk))
    except (ValueError, TypeError):
        return False


# A real hash to check against when the username doesn't exist, so timing doesn't reveal it.
_DUMMY = hash_password(secrets.token_urlsafe(16))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, store, session_days: int):
        self.store = store
        self.days = session_days
        self._ip_hits: dict = {}
        self._ip_lock = threading.Lock()

    def _ip_allowed(self, ip: str) -> bool:
        now = time.time()
        with self._ip_lock:
            hits = [t for t in self._ip_hits.get(ip, []) if now - t < IP_WINDOW]
            self._ip_hits[ip] = hits
            return len(hits) < IP_LIMIT

    def _ip_fail(self, ip: str):
        with self._ip_lock:
            self._ip_hits.setdefault(ip, []).append(time.time())

    def sign_in(self, username: str, password: str, ip: str, device: str):
        """Returns (token, None) on success or (None, message) on failure."""
        username = (username or "").strip()
        if not self._ip_allowed(ip):
            log.warning("sign-in blocked: too many attempts from %s", ip)
            return None, "Too many attempts. Try again in 15 minutes."
        user = self.store.user_by_name(username) if username else None
        if user and user["locked_until"] > time.time():
            check_password(password or "", _DUMMY)
            log.warning("sign-in refused: %s is locked (from %s)", username, ip)
            return None, "Too many attempts. Try again in 15 minutes."
        ok = check_password(password or "", user["pw"] if user else _DUMMY) and user is not None
        if not ok:
            self._ip_fail(ip)
            if user:
                failures = user["failures"] + 1
                locked = int(time.time()) + LOCK_SECONDS if failures >= MAX_FAILURES else 0
                self.store.run("UPDATE users SET failures = ?, locked_until = ? WHERE id = ?", (0 if locked else failures, locked, user["id"]))
            log.warning("sign-in failed for %r from %s", username[:40], ip)
            return None, "That username or password isn't right."
        self.store.run("UPDATE users SET failures = 0, locked_until = 0 WHERE id = ?", (user["id"],))
        return self.open_session(user, ip, device), None

    def open_session(self, user, ip: str, device: str) -> str:
        """A new session for a person already known to be who they say (a good password, or the setup code); its token."""
        token = secrets.token_urlsafe(32)  # 256 bits
        self.store.add_session(token_hash(token), user["id"], secrets.token_urlsafe(24), device, self.days)
        self.store.purge_expired()
        log.info("signed in: %s from %s (%s)", user["username"], ip, device[:60])
        return token

    def session(self, token: str):
        if not token or len(token) > 100:
            return None
        h = token_hash(token)
        s = self.store.session(h)
        if s and time.time() - s["last_seen"] > 3600:  # slide the 90 days at most hourly
            self.store.touch_session(h, self.days)
            self.store.purge_expired()  # idle devices go without waiting for someone to sign in
        return s

    def cookie(self, token: str, max_age: int | None = None) -> str:
        age = self.days * 86400 if max_age is None else max_age
        return f"{COOKIE}={token}; Path=/; Max-Age={age}; Secure; HttpOnly; SameSite=Strict"
