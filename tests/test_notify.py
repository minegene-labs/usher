"""Which notifications fire, for whom, and when."""

import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from usher.notify import Notifier
from usher.store import Store


class Settings:
    def __init__(self, d):
        self.data_dir = Path(d)


def snap(requests=(), downloading=(), low=False, ok=True):
    return {
        "requests": list(requests),
        "downloading": list(downloading),
        "badge": 2,
        "status": {"ok": ok, "parts": [{"name": "TV", "ok": ok, "text": "Not responding."}], "storage": {"free": 20e9, "total": 243e9, "low": low}},
    }


def req(rid, step, title="Example Show", stuck=False):
    return {"rid": rid, "kind": "show", "id": 7, "title": title, "step": step, "stuck": stuck}


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        zone = mock.patch.dict(os.environ, {"TZ": "Etc/GMT-10"})  # UTC+10, no clock changes
        zone.start()
        self.addCleanup(zone.stop)
        self.store = Store(Path(self.tmp.name))
        self.store.add_user("alex", "Alex", "x")
        self.store.add_user("sam", "Sam", "x")
        for uid in (1, 2):  # UTC+10
            self.store.prefs(uid)
        self.n = Notifier(self.store, Settings(self.tmp.name), "https://usher.example")
        self.sent = []
        self.n._send = lambda user, msg, topic=None: self.sent.append((user["username"], msg))
        self.daytime = mock.patch("usher.notify.datetime", wraps=datetime)

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, s, raw=None, utc_hour=2):  # 02:xx UTC = 12:xx at UTC+10
        with mock.patch("usher.notify.datetime") as dt:
            dt.now.return_value = datetime(2026, 11, 5, utc_hour, 0, tzinfo=timezone.utc)
            self.n.check(s, raw or {"ready": []})

    def test_request_ready_once_for_both(self):
        self.run_check(snap([req(1, 2)]))
        self.assertEqual(self.sent, [])
        self.run_check(snap([req(1, 4)]))
        self.assertEqual(sorted((u, m["title"]) for u, m in self.sent), [("alex", "Example Show is ready to watch"), ("sam", "Example Show is ready to watch")])
        self.assertEqual(self.sent[0][1]["url"], "/?open=request:show:7")
        self.sent.clear()
        self.run_check(snap([req(1, 4)]))
        self.assertEqual(self.sent, [])  # never twice

    def test_each_item_has_its_own_tag_and_the_same_item_keeps_it(self):
        self.run_check(snap([req(1, 2), req(2, 2, title="Other Show")]))
        self.run_check(snap([req(1, 4)]))
        self.run_check(snap([req(2, 4, title="Other Show")]))
        tags = {m["title"]: m["tag"] for u, m in self.sent if u == "alex"}
        self.assertEqual(len(tags), 2)
        self.assertEqual(len(set(tags.values())), 2)
        self.assertEqual(tags["Example Show is ready to watch"], "ready:Example Show is ready to watch")

    def test_existing_library_not_announced_then_new_episodes_are(self):
        old = [{"Id": "a", "Type": "Episode", "SeriesName": "Bluey", "ParentIndexNumber": 3, "IndexNumber": 1}]
        self.run_check(snap(), {"ready": old})
        self.assertEqual(self.sent, [])
        new = old + [{"Id": "b", "Type": "Episode", "SeriesName": "Bluey", "ParentIndexNumber": 3, "IndexNumber": 2}]
        self.run_check(snap(), {"ready": new})
        self.assertEqual(self.sent[0][1]["title"], "Bluey S3E2 is ready to watch")

    def test_unwatched_item_still_listed_after_60_days_is_not_announced_again(self):
        old = [{"Id": "a", "Type": "Movie", "Name": "Old Film"}]
        self.run_check(snap(), {"ready": old})
        day = 86400
        with mock.patch("usher.store.time.time", return_value=time.time() + 40 * day):
            self.run_check(snap(), {"ready": old})  # seen again on day 40
        with mock.patch("usher.store.time.time", return_value=time.time() + 61 * day):
            new = old + [{"Id": "b", "Type": "Movie", "Name": "New Film"}]
            self.run_check(snap(), {"ready": new})
        self.assertEqual({m["title"] for _, m in self.sent}, {"New Film is ready to watch"})
        self.assertTrue(self.store.seen("init:jellyfin"))

    def test_first_run_marker_is_never_pruned(self):
        self.run_check(snap(), {"ready": []})
        with mock.patch("usher.store.time.time", return_value=time.time() + 400 * 86400):
            self.store.mark("jf:other")
        self.assertTrue(self.store.seen("init:jellyfin"))

    def test_episodes_of_a_request_in_progress_are_not_announced_separately(self):
        self.run_check(snap(), {"ready": []})
        ep = [{"Id": "m1", "Type": "Episode", "SeriesName": "Example Show", "ParentIndexNumber": 2, "IndexNumber": 1}]
        self.run_check(snap([req(1, 3)]), {"ready": ep})
        self.assertEqual(self.sent, [])
        self.run_check(snap([req(1, 4)]), {"ready": ep})
        self.assertEqual(len(self.sent), 2)  # one "ready" each, not an extra episode ping

    def test_notifications_arrive_at_any_time_of_day(self):
        self.run_check(snap([req(1, 4)]), utc_hour=14)  # 00:00 at UTC+10
        self.assertEqual(sorted((u, m["title"]) for u, m in self.sent), [("alex", "Example Show is ready to watch"), ("sam", "Example Show is ready to watch")])

    def test_stored_quiet_hours_values_are_ignored(self):
        self.store.run("ALTER TABLE notify_prefs ADD COLUMN quiet_on INTEGER NOT NULL DEFAULT 1")
        self.store.run("ALTER TABLE notify_prefs ADD COLUMN quiet_start TEXT NOT NULL DEFAULT '22:00'")
        self.store.run("ALTER TABLE notify_prefs ADD COLUMN quiet_end TEXT NOT NULL DEFAULT '07:00'")
        self.run_check(snap([req(1, 4)]), utc_hour=14)
        self.assertEqual(len(self.sent), 2)

    def test_held_notifications_table_is_removed_on_start(self):
        self.store.run("CREATE TABLE held (id INTEGER PRIMARY KEY)")
        self.store._db.close()
        store = Store(Path(self.tmp.name))
        self.addCleanup(store._db.close)
        self.assertIsNone(store.one("SELECT name FROM sqlite_master WHERE name = 'held'"))

    def test_settings_route_has_no_quiet_hours(self):
        from usher.notify_routes import NotifyRoutes

        class Handler(NotifyRoutes):
            app = mock.Mock(store=self.store)

        h = Handler()
        h.save_prefs({"user_id": 1}, {"quiet_on": False, "quiet_start": "21:00", "ready": False})
        prefs = h.notify_body({"user_id": 1})["prefs"]
        self.assertEqual(sorted(prefs), ["problem", "ready", "server", "space"])
        self.assertIs(prefs["ready"], False)

    def test_preferences_respected(self):
        self.store.set_prefs(2, {"ready": 0})
        self.run_check(snap([req(1, 4)]))
        self.assertEqual([u for u, _ in self.sent], ["alex"])

    def test_server_alert_only_after_half_an_hour_and_only_for_the_first_account(self):
        with mock.patch("usher.notify.time") as t:
            t.time.return_value = 1000
            self.run_check(snap(ok=False))
            self.assertEqual(self.sent, [])
            t.time.return_value = 1000 + 31 * 60
            self.run_check(snap(ok=False))
        self.assertEqual([(u, m["title"]) for u, m in self.sent], [("alex", "The server needs attention")])

    def test_space_problem_not_sent_as_download_problem(self):
        d = {"key": "k1", "title": "Example Show", "state": "problem", "reason": "space", "message": "out of room"}
        self.run_check(snap(downloading=[d], low=True))
        titles = [m["title"] for _, m in self.sent]
        self.assertIn("Storage is getting full", titles)
        self.assertFalse(any("stopped downloading" in x for x in titles))


if __name__ == "__main__":
    unittest.main()
