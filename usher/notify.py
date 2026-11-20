"""Decides when to notify whom, and sends it. Runs after every poll, on the poller's thread."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .push import Vapid, deliver
from .state.helpers import episode_label

log = logging.getLogger("usher.notify")

HOLD_NUMBERS_FOR = 15 * 60  # a new episode without its season and episode number waits this long for them
SERVER_BAD_FOR = 30 * 60  # the server must be unhappy this long before anyone is told


def unread_count(home_rows, baseline: int, opened: dict) -> int:
    """Like Mail: items in the Latest rows that arrived after this person started using Usher and that they
    haven't opened since. The same rule as the dots on Home."""
    seen_ids, n = set(), 0
    for row in home_rows or []:
        if not row.get("id", "").startswith("latest-"):
            continue
        for card in row.get("items", []):
            jf = card.get("jf")
            if jf in seen_ids:
                continue
            seen_ids.add(jf)
            try:
                added = datetime.fromisoformat(card["added"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, AttributeError):
                continue
            if added > max(baseline, opened.get(jf, 0)):
                n += 1
    return n


class Notifier:
    def __init__(self, store, settings, contact: str):
        self.store = store
        self.vapid = Vapid(settings.data_dir, contact)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self._server_bad_since = None
        self._recent_ready: dict = {}  # title -> when a request's "ready" was sent, to avoid a second ping
        self._held: dict = {}  # media server item id -> when it was first seen without its season or episode number

    # deciding ------------------------------------------------------------------------
    def check(self, snap, raw):
        try:
            events = self._events(snap, raw)
            for e in events:
                log.info("notification built: %s: %s", e["kind"], e["title"])
            self._dispatch(events, snap.get("home") or [])
        except Exception:  # notifications must never stop the poller
            log.exception("notification check failed")

    def _new(self, key):
        if self.store.seen(key):
            return False
        self.store.mark(key)
        return True

    def _waiting_for_numbers(self, it, now):
        """A new episode Jellyfin lists without both numbers is held for the next poll, so the notification can carry
        them; it is only announced without them once it has waited HOLD_NUMBERS_FOR."""
        if it.get("Type") != "Episode" or (it.get("ParentIndexNumber") is not None and it.get("IndexNumber") is not None):
            return False
        first = self._held.setdefault(it.get("Id"), now)
        return now - first < HOLD_NUMBERS_FOR

    def _events(self, snap, raw):
        now = time.time()
        events = []
        requests = snap.get("requests", [])

        for r in requests:
            if r["step"] == 4 and self._new(f"req:{r['rid']}:ready"):
                self._recent_ready[r["title"].lower()] = now
                events.append(
                    dict(
                        kind="ready",
                        title=f"{r['title']} is ready to watch",
                        body="It's in Jellyfin, ready when you are.",
                        url=f"/?open=request:{r['kind']}:{r['id']}",
                        short=r["title"],
                    )
                )
            # No reminder for a request that's still searching or downloading slowly: waiting is normal.

        # New things in Jellyfin that nobody asked for (new episodes of followed shows).
        busy = {r["title"].lower() for r in requests if r["step"] in (2, 3)}
        busy |= {t for t, at in self._recent_ready.items() if now - at < 900}
        jf_items = raw.get("ready") or []
        if not self.store.seen("init:jellyfin"):
            for it in jf_items:  # first run: don't announce the existing library
                self.store.mark(f"jf:{it.get('Id')}")
            self.store.mark("init:jellyfin")
        else:
            groups: dict = {}
            self._held = {k: v for k, v in self._held.items() if k in {it.get("Id") for it in jf_items}}
            for it in jf_items:
                if self.store.seen(f"jf:{it.get('Id')}") or self._waiting_for_numbers(it, now):
                    continue
                self.store.mark(f"jf:{it.get('Id')}")
                name = it.get("SeriesName") if it.get("Type") == "Episode" else it.get("Name")
                if not name or name.lower() in busy:
                    continue
                groups.setdefault(name, []).append(it)
            for name, items in groups.items():
                if len(items) == 1 and items[0].get("Type") == "Episode":
                    it = items[0]
                    label = episode_label(it.get("ParentIndexNumber"), it.get("IndexNumber"))
                    title = f"{name} {label} is ready to watch" if label else f"{name} is ready to watch"
                elif len(items) == 1:
                    title = f"{name} is ready to watch"
                else:
                    title = f"{len(items)} new episodes of {name} are ready"
                events.append(dict(kind="ready", title=title, body="Open Jellyfin to watch.", url="/", short=name))

        for d in snap.get("downloading", []):
            if d["state"] == "problem" and d.get("reason") != "space" and self._new(f"problem:{d['key']}"):
                events.append(
                    dict(
                        kind="problem",
                        title=f"{d['title']} stopped downloading",
                        body=d["message"] + (" Tap to try again." if d.get("canRetry", True) else ""),
                        url="/",
                        short=f"a problem with {d['title']}",
                    )
                )

        storage = (snap.get("status") or {}).get("storage") or {}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if storage.get("low") and self._new(f"space:{today}"):
            events.append(
                dict(
                    kind="space",
                    title="Storage is getting full",
                    body=f"{storage['free'] / 1e9:.0f} GB left. New downloads stop going into the "
                    "library below 25 GB. Removing something and emptying the recycle bin helps.",
                    url="/status",
                    short="storage is getting full",
                )
            )

        status = snap.get("status") or {}
        if status and not status.get("ok"):
            self._server_bad_since = self._server_bad_since or now
            bad = [p for p in status.get("parts", []) if not p["ok"] and p["name"] != "Storage"]
            half = datetime.now(timezone.utc).hour // 12
            if bad and now - self._server_bad_since > SERVER_BAD_FOR and self._new(f"server:{today}:{half}"):
                events.append(
                    dict(
                        kind="server",
                        title="The server needs attention",
                        body=f"{bad[0]['name']}: {bad[0]['text']}",
                        url="/status",
                        short="the server needs attention",
                    )
                )
        else:
            self._server_bad_since = None
        return events

    # sending ---------------------------------------------------------------------------
    def _dispatch(self, events, home_rows):
        for user in self.store.users():
            badge = unread_count(home_rows, self.store.baseline(user["id"]), self.store.opened(user["id"]))
            prefs = self.store.prefs(user["id"])
            mine = [e for e in events if prefs[e["kind"]]]
            if len(mine) == 1:
                e = mine[0]
                self._send(user, {"title": e["title"], "body": e["body"], "url": e["url"], "badge": badge, "tag": e["kind"]}, topic=e["title"])
            elif mine:
                self._send(
                    user,
                    {
                        "title": f"{len(mine)} updates from Usher",
                        "body": "; ".join(e["short"] for e in mine)[:240] + ".",
                        "url": "/",
                        "badge": badge,
                        "tag": "several",
                    },
                )

    def _send(self, user, message, topic=None):
        for sub in self.store.subs(user["id"]):
            self.pool.submit(self._one, dict(sub), message, topic)

    def _one(self, sub, message, topic):
        status = deliver(self.vapid, sub, message, topic=topic)
        if status in (404, 410):
            self.store.drop_sub(sub["endpoint"])
            log.info("a device stopped accepting notifications; removed it")
        elif 200 <= status < 300:
            self.store.run("UPDATE push_subs SET last_ok = ? WHERE id = ?", (int(time.time()), sub["id"]))
        else:
            log.warning("notification not delivered (HTTP %s)", status)

    def test(self, user_row):
        subs = self.store.subs(user_row["user_id"])
        for sub in subs:
            self.pool.submit(
                self._one,
                dict(sub),
                {"title": "Notifications are working", "body": "This is how Usher will tell you something's ready.", "url": "/status", "tag": "test"},
                None,
            )
        return len(subs)
