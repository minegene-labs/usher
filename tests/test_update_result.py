"""The update script's reports: Usher keeps the last result for Settings → Advanced and the latest check for
Settings → About → Updates, and sends nothing."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from usher import __version__
from usher.release_check import ReleaseCheck
from usher.store import Store
from usher.update_result import UpdateResult, UpdateRoutes

COMMIT = "0123456789abcdef0123456789abcdef01234567"


class Request(UpdateRoutes):
    """Just enough of the request handler to run the routes: it records the answer instead of writing it."""

    def __init__(self, updates, ip="127.0.0.1", forwarded=None):
        releases = ReleaseCheck(updates.path.parent, lambda: "1.0.0") if updates else None
        self.app = mock.Mock(updates=updates, releases=releases)
        self.client_address = (ip, 50000)
        self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        self.answer = None

    def send(self, status, body, headers=None):
        self.answer = (int(status), body)

    def err(self, status, message):
        self.answer = (int(status), {"error": message})


class UpdateResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.updates = UpdateResult(self.dir)

    def report(self, result="built", **extra):
        return self.updates.report({"result": result, "version": __version__, "commit": COMMIT, **extra})

    def test_a_report_is_kept_and_survives_a_restart(self):
        self.report()
        last = self.updates.last
        self.assertEqual((last["result"], last["version"], last["commit"]), ("built", __version__, COMMIT[:12]))
        self.assertEqual(UpdateResult(self.dir).last, last)
        self.assertEqual(oct((self.dir / "update.json").stat().st_mode & 0o777), "0o600")

    def test_a_later_report_replaces_the_last_one(self):
        self.report("built")
        self.report("rolled_back")
        self.assertEqual(self.updates.last["result"], "rolled_back")

    def test_a_damaged_result_file_is_ignored(self):
        (self.dir / "update.json").write_text("{not json")
        with self.assertLogs("usher.updates", "WARNING"):
            self.assertIsNone(UpdateResult(self.dir).last)

    def test_bad_reports_are_refused_and_change_nothing(self):
        for bad in ({"result": "done"}, {"version": "9.9.9"}, {"commit": "not-a-commit"}, {"commit": ""}, {"commit": "../etc"}):
            with self.assertRaises(ValueError, msg=bad):
                self.report(**bad)
        self.assertIsNone(self.updates.last)

    def post(self, **kw):
        req = Request(self.updates, **kw)
        req.update_report({"result": "built", "version": __version__, "commit": COMMIT})
        return req.answer

    def test_the_report_is_taken_from_the_server_itself(self):
        status, body = self.post()
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(self.updates.last["result"], "built")

    def test_the_report_is_refused_from_anywhere_else(self):
        self.assertEqual(self.post(ip="192.0.2.7")[0], 404)
        self.assertEqual(self.post(forwarded="192.0.2.7")[0], 404)  # through the reverse proxy
        self.assertIsNone(self.updates.last)

    def test_a_bad_report_is_answered_with_the_reason(self):
        req = Request(self.updates)
        req.update_report({"result": "built", "version": "0.0.1", "commit": COMMIT})
        self.assertEqual(req.answer[0], 400)

    def test_the_last_result_is_what_advanced_shows_and_holds_no_secrets(self):
        self.report("rolled_back")
        req = Request(self.updates)
        req.update_get("/api/updates")
        self.assertEqual(req.answer[1]["last"]["result"], "rolled_back")
        self.assertEqual(set(req.answer[1]), {"last", "check", "state", "release"})
        self.assertEqual(set(req.answer[1]["last"]), {"result", "version", "commit", "at"})
        self.assertNotIn("auth", json.dumps(req.answer[1]))

    # The check the script sends on every run ------------------------------------------------------------
    def check(self, newest="9.9.9", held=False, failed=False, **extra):
        return self.updates.report({"result": "checked", "newest": newest, "held": held, "failed": failed, **extra})

    def test_a_check_is_kept_with_its_time_and_survives_a_restart(self):
        with mock.patch("usher.update_result.time.time", return_value=1000):
            self.check("1.2.3")
        self.assertEqual(self.updates.check, {"newest": "1.2.3", "held": False, "failed": False, "at": 1000})
        self.assertEqual(UpdateResult(self.dir).check, self.updates.check)
        self.assertEqual(oct((self.dir / "update.json").stat().st_mode & 0o777), "0o600")

    def test_a_check_and_a_result_are_kept_together_and_neither_replaces_the_other(self):
        self.report()
        self.check()
        self.report("rolled_back")
        again = UpdateResult(self.dir)
        self.assertEqual((again.last["result"], again.check["newest"]), ("rolled_back", "9.9.9"))

    def test_a_result_file_from_before_the_checks_is_still_read(self):
        old = {"result": "built", "version": __version__, "commit": COMMIT[:12], "at": 5}
        (self.dir / "update.json").write_text(json.dumps(old))
        updates = UpdateResult(self.dir)
        self.assertEqual((updates.last, updates.check), (old, None))

    def test_bad_checks_are_refused_and_change_nothing(self):
        for bad in ({"newest": "latest"}, {"newest": ""}, {"newest": 3}, {"newest": "1.2"}, {"held": "yes"}, {"failed": 1}, {"held": None}):
            with self.assertRaises(ValueError, msg=bad):
                self.check(**bad)
        self.assertIsNone(self.updates.check)

    def test_versions_that_are_not_plain_numbers_are_refused(self):
        for bad in ("١.٢.٣", "1.2.3.4", "1.2.3-beta", "1.2.3\n", f"1.2.{'9' * 7}", f"{'9' * 5000}.0.0"):
            with self.assertRaises(ValueError, msg=bad):
                self.check(newest=bad)
        self.assertIsNone(self.updates.check)

    def test_a_damaged_stored_check_is_ignored_and_settings_still_answers(self):
        good = {"newest": "9.9.9", "held": False, "failed": False, "at": 5}
        damaged = (
            {"newest": "9.9.9"},
            {**good, "at": "yesterday"},
            {**good, "at": True},
            {**good, "held": "no"},
            {**good, "failed": None},
            {**good, "newest": f"{'9' * 5000}.0.0"},
            {**good, "newest": 7},
            ["not", "a", "check"],
        )
        for check in damaged:
            (self.dir / "update.json").write_text(json.dumps({"last": None, "check": check}))
            updates = UpdateResult(self.dir)
            self.assertIsNone(updates.check, check)
            req = Request(updates)
            req.update_get("/api/updates")
            self.assertEqual((req.answer[0], req.answer[1]["state"]), (200, "unknown"), check)
        (self.dir / "update.json").write_text(json.dumps({"last": None, "check": good}))
        self.assertEqual(UpdateResult(self.dir).check, good)

    def test_a_check_is_taken_from_the_server_itself_only(self):
        body = {"result": "checked", "newest": "9.9.9", "held": False, "failed": False}
        for kw in ({"ip": "192.0.2.7"}, {"forwarded": "192.0.2.7"}):
            req = Request(self.updates, **kw)
            req.update_report(body)
            self.assertEqual(req.answer[0], 404)
        self.assertIsNone(self.updates.check)
        req = Request(self.updates)
        req.update_report(body)
        self.assertEqual(req.answer[0], 200)
        self.assertEqual(self.updates.check["newest"], "9.9.9")

    def state(self, age=0, **check):
        now = 100_000
        with mock.patch("usher.update_result.time.time", return_value=now - age):
            self.check(**check)
        return self.updates.state(now)

    def test_no_report_at_all_is_cant_tell(self):
        self.assertEqual(self.updates.state(), "unknown")

    def test_the_newest_version_running_is_up_to_date(self):
        self.assertEqual(self.state(newest=__version__), "up_to_date")

    def test_a_newer_version_on_stable_is_waiting(self):
        self.assertEqual(self.state(newest="999.0.0"), "waiting")

    def test_a_newer_version_that_needs_a_step_on_the_server_is_held(self):
        self.assertEqual(self.state(newest="999.0.0", held=True), "held")

    def test_a_newer_version_that_failed_its_health_check_is_rolled_back(self):
        self.assertEqual(self.state(newest="999.0.0", failed=True), "rolled_back")

    def test_a_check_older_than_an_hour_is_cant_tell_whatever_it_said(self):
        for flags in ({}, {"held": True}, {"failed": True}):
            self.assertEqual(self.state(age=3601, newest="999.0.0", **flags), "unknown", flags)
        self.assertEqual(self.state(age=3601, newest=__version__), "unknown")
        self.assertEqual(self.state(age=3600, newest=__version__), "up_to_date")  # the hour itself is still recent

    def test_flags_about_a_version_already_running_are_ignored(self):
        """A held or failed flag from before the running version caught up must not show."""
        self.assertEqual(self.state(newest=__version__, held=True, failed=True), "up_to_date")

    def test_versions_compare_as_numbers_not_as_text(self):
        with mock.patch("usher.update_result.__version__", "0.9.0"):
            self.assertEqual(self.state(newest="0.10.0"), "waiting")
        with mock.patch("usher.update_result.__version__", "0.10.0"):
            self.assertEqual(self.state(newest="0.9.0"), "up_to_date")

    def test_what_settings_reads_has_the_state_and_the_check_and_names_no_server(self):
        self.report()
        self.check("999.0.0", held=True)
        req = Request(self.updates)
        req.update_get("/api/updates")
        body = req.answer[1]
        self.assertEqual(body["state"], "held")
        self.assertEqual(set(body["check"]), {"newest", "at"})
        self.assertEqual(body["check"]["newest"], "999.0.0")
        self.assertNotIn(COMMIT[:12], json.dumps(body["check"]))

    def test_no_notice_is_sent_and_the_old_opt_in_table_is_gone(self):
        self.assertFalse(hasattr(UpdateResult, "pool"))
        store = Store(self.dir)
        names = {r["name"] for r in store.q("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("update_subs", names)

    def test_a_database_from_before_loses_the_opt_in_table(self):
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(Path(d) / "usher.db")
            db.execute("CREATE TABLE update_subs (id INTEGER PRIMARY KEY, endpoint TEXT)")
            db.commit()
            db.close()
            store = Store(Path(d))
            names = {r["name"] for r in store.q("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("update_subs", names)


if __name__ == "__main__":
    unittest.main()
