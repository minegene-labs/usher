"""The daily check for a newer release: when it asks, when it doesn't, what it keeps, and who is told."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from usher import config
from usher.notify import Notifier
from usher.release_check import UPDATE_COMMAND, ReleaseCheck, newest_from
from usher.store import Store

DAY = 24 * 3600
T0 = 10 * DAY


def settings(check="on"):
    return mock.patch.object(config, "_active", config.Source("usher.toml (made up)", {"updates.check_releases": check}))


class Fetch:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        running = mock.patch("usher.release_check.__version__", "1.0.0")
        running.start()
        self.addCleanup(running.stop)

    def test_it_asks_at_most_once_a_day_and_keeps_the_answer_across_a_restart(self):
        fetch = Fetch("1.1.0", "1.2.0")
        with settings():
            check = ReleaseCheck(self.dir, fetch)
            self.assertTrue(check.check(now=T0))
            self.assertFalse(check.check(now=T0 + DAY - 1))
            self.assertEqual(fetch.calls, 1)
            again = ReleaseCheck(self.dir, fetch)  # a restart doesn't ask again inside the day
            self.assertFalse(again.check(now=T0 + 60))
            self.assertEqual(again.newest, "1.1.0")
            self.assertTrue(again.check(now=T0 + DAY))
            self.assertEqual(again.newest, "1.2.0")
        self.assertEqual(oct((self.dir / "release.json").stat().st_mode & 0o777), "0o600")

    def test_turned_off_it_makes_no_request(self):
        fetch = Fetch("1.1.0")
        with settings("off"):
            check = ReleaseCheck(self.dir, fetch)
            self.assertFalse(check.check(now=10 * DAY))
            self.assertEqual(fetch.calls, 0)
            self.assertFalse(check.view()["enabled"])
            self.assertIsNone(check.available())

    def test_a_newer_release_is_available_and_an_equal_or_older_one_is_not(self):
        with settings():
            for newest, available in (("1.0.1", True), ("1.10.0", True), ("1.0.0", False), ("0.9.9", False)):
                check = ReleaseCheck(Path(tempfile.mkdtemp(dir=self.dir)), Fetch(newest))
                check.check(now=T0)
                view = check.view()
                self.assertEqual(view["available"], available, newest)
                self.assertEqual(check.available(), newest if available else None)
                self.assertEqual(view["command"], UPDATE_COMMAND)
                self.assertTrue(view["notes"].endswith(f"/v{newest}"))

    def test_when_github_cant_be_reached_it_says_so_and_asks_again_within_the_hour(self):
        fetch = Fetch(OSError("no route"), "1.1.0")
        with settings(), self.assertLogs("usher.releases", "WARNING") as logged:
            check = ReleaseCheck(self.dir, fetch)
            check.check(now=T0)
            self.assertTrue(check.view()["failed"])
            self.assertNotIn("no route", "".join(logged.output))
            self.assertFalse(check.check(now=T0 + 60))
            self.assertTrue(check.check(now=T0 + 3600))
            self.assertFalse(check.view()["failed"])

    def test_an_http_failure_mid_reply_counts_as_a_failed_check_not_an_endless_retry(self):
        import http.client

        with settings(), self.assertLogs("usher.releases", "WARNING"):
            check = ReleaseCheck(self.dir, Fetch(http.client.IncompleteRead(b"")))
            self.assertTrue(check.check(now=T0))
            self.assertTrue(check.failed)
            self.assertFalse(check.check(now=T0 + 60))

    def test_a_failed_check_keeps_the_last_known_release(self):
        with settings():
            check = ReleaseCheck(self.dir, Fetch("1.1.0", ValueError("bad")))
            check.check(now=T0)
            check.check(now=T0 + DAY)
            self.assertEqual((check.newest, check.failed), ("1.1.0", True))

    def test_only_a_real_release_version_is_taken_from_the_reply(self):
        self.assertEqual(newest_from({"tag_name": "v2.3.4"}), "2.3.4")
        self.assertEqual(newest_from({"tag_name": "2.3.4"}), "2.3.4")
        for bad in (
            {},
            [],
            None,
            {"tag_name": "latest"},
            {"tag_name": "v1.2"},
            {"tag_name": "v1.2.3-rc1"},
            {"tag_name": 7},
            {"tag_name": "v1.2.3", "draft": True},
            {"tag_name": "v1.2.3", "prerelease": True},
        ):
            with self.assertRaises(ValueError, msg=bad):
                newest_from(bad)

    def test_a_damaged_file_is_ignored(self):
        for text in ("{not json", "[]", json.dumps({"newest": "x", "at": 1}), json.dumps({"newest": "1.1.0", "at": True})):
            (self.dir / "release.json").write_text(text)
            with settings(), self.assertNoLogs("usher.releases", "ERROR"):
                self.assertIsNone(ReleaseCheck(self.dir, Fetch()).newest, text)


class Settings:
    def __init__(self, d):
        self.data_dir = Path(d)


class AdminsAreToldOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        running = mock.patch("usher.release_check.__version__", "1.0.0")
        running.start()
        self.addCleanup(running.stop)
        self.store = Store(Path(self.tmp.name))
        self.store.add_user("alex", "Alex", "x")
        self.store.add_user("sam", "Sam", "x")
        self.store.run("UPDATE users SET admin = 1 WHERE username = 'alex'")
        for uid in (1, 2):
            self.store.prefs(uid)
        self.notifier = Notifier(self.store, Settings(self.tmp.name), "https://usher.example")
        self.sent = []
        self.notifier._send = lambda user, msg, topic=None: self.sent.append((user["username"], msg["title"]))
        self.releases = ReleaseCheck(Path(self.tmp.name), Fetch("1.1.0", "1.1.0", "1.2.0"))
        self.notifier.releases = self.releases

    def poll(self):
        self.notifier.check({"requests": [], "downloading": [], "status": {"ok": True}}, {})

    def test_an_admin_is_told_once_about_each_new_release_and_other_accounts_are_not(self):
        with settings():
            self.releases.check(now=T0)
            self.poll()
            self.poll()
            self.assertEqual(self.sent, [("alex", "A new version of Usher is out")])
            self.releases.check(now=T0 + DAY)  # the same release again: nothing new
            self.poll()
            self.assertEqual(len(self.sent), 1)
            self.releases.check(now=T0 + 2 * DAY)
            self.poll()
            self.assertEqual(len(self.sent), 2)

    def test_an_admin_with_the_switch_off_is_not_told_and_does_not_hold_it_up_for_a_new_admin(self):
        self.store.set_prefs(1, {"server": 0})
        with settings():
            self.releases.check(now=T0)
            self.poll()
            self.assertEqual(self.sent, [])
            self.store.add_user("kim", "Kim", "x")
            self.store.run("UPDATE users SET admin = 1 WHERE username = 'kim'")
            self.store.set_prefs(3, {"server": 1})
            self.poll()
            self.poll()
            self.assertEqual(self.sent, [("kim", "A new version of Usher is out")])

    def test_nobody_is_told_when_the_check_is_off_or_nothing_is_newer(self):
        with settings("off"):
            self.releases.check(now=T0)
            self.poll()
        running = mock.patch("usher.release_check.__version__", "1.1.0")
        with settings(), running:
            self.releases.check(now=T0)
            self.poll()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
