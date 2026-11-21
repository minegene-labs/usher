"""Usher's own data: accounts and sessions, in one SQLite file."""

import sqlite3
import threading
import time
from pathlib import Path

from . import config
from .files import make_private

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display      TEXT NOT NULL,
    pw           TEXT NOT NULL,
    created      INTEGER NOT NULL,
    failures     INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash   TEXT PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf      TEXT NOT NULL,
    device    TEXT NOT NULL DEFAULT '',
    created   INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    expires   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS requests (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,            -- movie | show
    arr_id   INTEGER NOT NULL,         -- Radarr movie id or Sonarr series id
    ext_id   INTEGER NOT NULL,         -- TMDb id (movie) or TVDB id (show)
    title    TEXT NOT NULL,
    year     INTEGER,
    choice   TEXT NOT NULL DEFAULT '', -- start | latest | new (shows)
    user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created  INTEGER NOT NULL,
    done_at  INTEGER,
    jf_id    TEXT
);
CREATE INDEX IF NOT EXISTS requests_item ON requests(kind, arr_id);
CREATE TABLE IF NOT EXISTS push_subs (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint  TEXT NOT NULL UNIQUE,
    p256dh    TEXT NOT NULL,
    auth      TEXT NOT NULL,
    device    TEXT NOT NULL DEFAULT '',
    created   INTEGER NOT NULL,
    last_ok   INTEGER
);
DROP TABLE IF EXISTS update_subs;  -- the opt-in for update notices, which no longer exist
CREATE TABLE IF NOT EXISTS notify_prefs (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    ready       INTEGER NOT NULL DEFAULT 1,
    stuck       INTEGER NOT NULL DEFAULT 1,
    problem     INTEGER NOT NULL DEFAULT 1,
    space       INTEGER NOT NULL DEFAULT 1,
    server      INTEGER NOT NULL DEFAULT 0,
    tz_offset   INTEGER NOT NULL DEFAULT 0       -- minutes ahead of UTC; set from the device
);
CREATE TABLE IF NOT EXISTS notified (
    key TEXT PRIMARY KEY,
    at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS opened (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jf_id   TEXT NOT NULL,
    at      INTEGER NOT NULL,
    PRIMARY KEY (user_id, jf_id)
);
CREATE TABLE IF NOT EXISTS opinions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,     -- movie | show
    tmdb    INTEGER NOT NULL,  -- TMDb id for both kinds, as on Discover's cards
    liked   INTEGER NOT NULL,  -- 1 like, 0 dislike
    at      INTEGER NOT NULL,
    PRIMARY KEY (user_id, kind, tmdb)
);
"""
PREF_KINDS = ("ready", "problem", "space", "server")  # the old "stuck" column is no longer used

SHOW_DONE_FOR = 2 * 86400  # a finished request stays on Home for two days
FORGET_AFTER = 30 * 86400  # unfinished requests drop off Home after 30 days
DEVICE_IDLE = 60 * 86400  # a device not seen for 60 days drops off the Devices list
OPINIONS_MAX = 500  # likes and dislikes one account can keep; past it a new one is refused, never an old one dropped


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        db = data_dir / "usher.db"
        self._db = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        # SQLite makes its files with the umask; its -wal and -shm files copy the database's mode once it is 0600
        make_private(db, f"{db}-wal", f"{db}-shm")
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._db.execute("DROP TABLE IF EXISTS held")  # held back notifications of the removed Quiet hours
            self._migrate()

    def _migrate(self):
        """Columns added after the first release. Each is added once, if missing."""
        added = {
            "requests": [("hidden", "INTEGER NOT NULL DEFAULT 0"), ("searched", "INTEGER")],
            "users": [("home_seen", "INTEGER NOT NULL DEFAULT 0"), ("admin", "INTEGER NOT NULL DEFAULT 0")],
        }
        for table, cols in added.items():
            have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def q(self, sql: str, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args=()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # users ---------------------------------------------------------------
    def user_by_name(self, username: str):
        return self.one("SELECT * FROM users WHERE username = ?", (username,))

    def user_by_id(self, uid: int):
        return self.one("SELECT * FROM users WHERE id = ?", (uid,))

    def add_user(self, username: str, display: str, pw_hash: str):
        self.run("INSERT INTO users (username, display, pw, created) VALUES (?, ?, ?, ?)", (username, display, pw_hash, int(time.time())))

    def add_first_user(self, username: str, display: str, pw_hash: str) -> bool:
        """The first account, who is the admin. False when an account exists by now, so two setups can't both win."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO users (username, display, pw, created, admin) SELECT ?, ?, ?, ?, 1 WHERE NOT EXISTS (SELECT 1 FROM users)",
                (username, display, pw_hash, int(time.time())),
            )
            return cur.rowcount == 1

    def user_count(self) -> int:
        return self.one("SELECT COUNT(*) FROM users")[0]

    def has_admin(self) -> bool:
        return self.one("SELECT 1 FROM users WHERE admin = 1") is not None

    def set_password(self, username: str, pw_hash: str) -> bool:
        return self.run("UPDATE users SET pw = ?, failures = 0, locked_until = 0 WHERE username = ?", (pw_hash, username)) == 1

    def remove_user(self, username: str) -> bool:
        return self.run("DELETE FROM users WHERE username = ?", (username,)) == 1

    def users(self):
        return self.q("SELECT id, username, display, created, home_seen, admin FROM users ORDER BY username")

    # sessions ------------------------------------------------------------
    def add_session(self, id_hash: str, uid: int, csrf: str, device: str, days: int):
        now = int(time.time())
        device = device[:120]
        if device:  # a device that signs in again replaces its own earlier entry
            self.run("DELETE FROM sessions WHERE user_id = ? AND device = ?", (uid, device))
        self.run(
            "INSERT INTO sessions (id_hash, user_id, csrf, device, created, last_seen, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id_hash, uid, csrf, device[:120], now, now, now + days * 86400),
        )

    def session(self, id_hash: str):
        return self.one(
            "SELECT s.*, u.username, u.display, u.home_seen, u.admin FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id_hash = ? AND s.expires > ?",
            (id_hash, int(time.time())),
        )

    def touch_session(self, id_hash: str, days: int):
        now = int(time.time())
        self.run("UPDATE sessions SET last_seen = ?, expires = ? WHERE id_hash = ?", (now, now + days * 86400, id_hash))

    def drop_session(self, id_hash: str):
        self.run("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))

    def drop_other_sessions(self, uid: int, keep_hash: str) -> int:
        return self.run("DELETE FROM sessions WHERE user_id = ? AND id_hash != ?", (uid, keep_hash))

    def sessions_for(self, uid: int):
        return self.q(
            "SELECT id_hash, device, created, last_seen FROM sessions WHERE user_id = ? AND expires > ? ORDER BY last_seen DESC", (uid, int(time.time()))
        )

    # requests --------------------------------------------------------------
    def add_request(self, kind, arr_id, ext_id, title, year, choice, uid) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO requests (kind, arr_id, ext_id, title, year, choice, user_id, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, arr_id, ext_id, title, year, choice, uid, int(time.time())),
            )
            return cur.lastrowid

    def open_requests(self):
        return self.q("SELECT * FROM requests WHERE done_at IS NULL AND created > ?", (int(time.time()) - FORGET_AFTER,))

    def recent_requests(self):
        now = int(time.time())
        return self.q(
            "SELECT r.*, COALESCE(u.display, 'Someone') AS display FROM requests r "
            "LEFT JOIN users u ON u.id = r.user_id "
            "WHERE r.hidden = 0 AND ((r.done_at IS NULL AND r.created > ?) OR r.done_at > ?) "
            "ORDER BY r.created DESC LIMIT 30",
            (now - FORGET_AFTER, now - SHOW_DONE_FOR),
        )

    def requesters(self) -> dict:
        """(kind, arr_id) -> (display name, created) of the first person who asked for it."""
        rows = self.q(
            "SELECT r.kind, r.arr_id, COALESCE(u.display, 'Someone') AS display, MIN(r.created) AS created "
            "FROM requests r LEFT JOIN users u ON u.id = r.user_id GROUP BY r.kind, r.arr_id"
        )
        return {(r["kind"], r["arr_id"]): (r["display"], r["created"]) for r in rows}

    def hide_request(self, rid: int) -> bool:
        return self.run("UPDATE requests SET hidden = 1 WHERE id = ?", (rid,)) == 1

    # "unread" like Mail: things added after someone started using Usher, until they open that item
    def baseline(self, uid: int) -> int:
        row = self.one("SELECT home_seen FROM users WHERE id = ?", (uid,))
        if row and row["home_seen"]:
            return row["home_seen"]
        now = int(time.time())
        self.run("UPDATE users SET home_seen = ? WHERE id = ? AND home_seen = 0", (now, uid))
        return now

    def mark_all_seen(self, uid: int) -> int:
        """Everything added so far counts as seen: moves the person's baseline to now."""
        now = int(time.time())
        self.run("UPDATE users SET home_seen = ? WHERE id = ?", (now, uid))
        return now

    def mark_opened(self, uid: int, jf_id: str):
        now = int(time.time())
        self.run("INSERT INTO opened (user_id, jf_id, at) VALUES (?, ?, ?) ON CONFLICT(user_id, jf_id) DO UPDATE SET at = excluded.at", (uid, jf_id, now))
        self.run("DELETE FROM opened WHERE at < ?", (now - 90 * 86400,))

    def opened(self, uid: int) -> dict:
        return {r["jf_id"]: r["at"] for r in self.q("SELECT jf_id, at FROM opened WHERE user_id = ?", (uid,))}

    def move_jf(self, old: str, new):
        """A title's media server item changed (moved or rescanned): requests and "opened" marks follow it.
        new None: the item has gone and the requests wait for it to be found again."""
        self.run("UPDATE requests SET jf_id = ? WHERE jf_id = ?", (new, old))
        if new:
            self.run("UPDATE OR IGNORE opened SET jf_id = ? WHERE jf_id = ?", (new, old))
        self.run("DELETE FROM opened WHERE jf_id = ?", (old,))

    def forget_item(self, kind, arr_id):
        self.run("DELETE FROM requests WHERE kind = ? AND arr_id = ?", (kind, arr_id))

    def requested_by(self, uid: int):
        """One account's requests still in the library (a request goes when its title is removed), newest first."""
        return self.q(
            "SELECT kind, arr_id, ext_id, MAX(created) AS created FROM requests WHERE user_id = ? "
            "GROUP BY kind, arr_id ORDER BY MAX(created) DESC, MAX(id) DESC",
            (uid,),
        )

    # likes and dislikes: each account's own, read and changed only through that account's sessions --------------
    def set_opinion(self, uid: int, kind: str, tmdb: int, liked: bool) -> bool:
        """False, and nothing kept, when the account already has OPINIONS_MAX and this title isn't one of them."""
        with self._lock:
            known = self._db.execute("SELECT 1 FROM opinions WHERE user_id = ? AND kind = ? AND tmdb = ?", (uid, kind, tmdb)).fetchone()
            count = self._db.execute("SELECT COUNT(*) FROM opinions WHERE user_id = ?", (uid,)).fetchone()[0]
            if not known and count >= OPINIONS_MAX:
                return False
            self._db.execute(
                "INSERT INTO opinions (user_id, kind, tmdb, liked, at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, kind, tmdb) DO UPDATE SET liked = excluded.liked, at = excluded.at",
                (uid, kind, tmdb, 1 if liked else 0, int(time.time())),
            )
            return True

    def clear_opinion(self, uid: int, kind: str, tmdb: int) -> bool:
        return self.run("DELETE FROM opinions WHERE user_id = ? AND kind = ? AND tmdb = ?", (uid, kind, tmdb)) == 1

    def opinions(self, uid: int, limit: int = OPINIONS_MAX, after=None):
        """kind, tmdb, liked (1 or 0), at and rid (where it sorts), newest first. after: (at, rid) of the last one already
        had, so a page starts after it even when titles before it have gone meanwhile."""
        if after is None:
            return self.q(
                "SELECT kind, tmdb, liked, at, rowid AS rid FROM opinions WHERE user_id = ? ORDER BY at DESC, rowid DESC LIMIT ?",
                (uid, limit),
            )
        return self.q(
            "SELECT kind, tmdb, liked, at, rowid AS rid FROM opinions WHERE user_id = ? AND (at < ? OR (at = ? AND rowid < ?)) "
            "ORDER BY at DESC, rowid DESC LIMIT ?",
            (uid, after[0], after[0], after[1], limit),
        )

    # notifications -------------------------------------------------------------
    def prefs(self, uid: int):
        row = self.one("SELECT * FROM notify_prefs WHERE user_id = ?", (uid,))
        if row is None:
            # Server alerts are on by default only for the first account (usually whoever runs the server).
            first = self.one("SELECT MIN(id) AS id FROM users")
            self.run(
                "INSERT OR IGNORE INTO notify_prefs (user_id, server, tz_offset) VALUES (?, ?, ?)",
                (
                    uid,
                    1 if first and first["id"] == uid else 0,
                    config.get("server.utc_offset_minutes"),
                ),
            )
            row = self.one("SELECT * FROM notify_prefs WHERE user_id = ?", (uid,))
        return row

    def set_prefs(self, uid: int, values: dict):
        self.prefs(uid)
        cols = [k for k in values if k in PREF_KINDS + ("tz_offset",)]
        if cols:
            self.run(f"UPDATE notify_prefs SET {', '.join(f'{c} = ?' for c in cols)} WHERE user_id = ?", tuple(values[c] for c in cols) + (uid,))

    def add_sub(self, uid, endpoint, p256dh, auth, device):
        self.run(
            "INSERT INTO push_subs (user_id, endpoint, p256dh, auth, device, created) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, "
            "auth = excluded.auth, device = excluded.device",
            (uid, endpoint, p256dh, auth, device[:120], int(time.time())),
        )

    def subs(self, uid=None):
        if uid is None:
            return self.q("SELECT * FROM push_subs")
        return self.q("SELECT * FROM push_subs WHERE user_id = ?", (uid,))

    def drop_sub(self, endpoint, uid=None):
        if uid is None:
            self.run("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
        else:
            self.run("DELETE FROM push_subs WHERE endpoint = ? AND user_id = ?", (endpoint, uid))

    def seen(self, key: str) -> bool:
        return self.one("SELECT 1 FROM notified WHERE key = ?", (key,)) is not None

    def mark(self, key: str):
        self.run("INSERT OR IGNORE INTO notified (key, at) VALUES (?, ?)", (key, int(time.time())))
        self.run("DELETE FROM notified WHERE at < ?", (int(time.time()) - 60 * 86400,))

    def purge_expired(self):
        now = int(time.time())
        self.run("DELETE FROM sessions WHERE expires <= ? OR last_seen <= ?", (now, now - DEVICE_IDLE))
