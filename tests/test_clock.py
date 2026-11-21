"""The server shows its local time from the container's TZ, following clock changes."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from usher import clock, config
from usher.state.helpers import local_time
from usher.store import Store


def at(y, m, d, h):
    return datetime(y, m, d, h, 0, tzinfo=timezone.utc).timestamp()


class ClockTests(unittest.TestCase):
    def test_local_hour_follows_the_clock_change(self):
        with mock.patch.dict(os.environ, {"TZ": "America/Adak"}):
            self.assertEqual(local_time(at(2026, 3, 7, 12)), "02:00")  # winter, UTC-10
            self.assertEqual(local_time(at(2026, 3, 9, 12)), "03:00")  # after the clocks change, UTC-9
            self.assertEqual(clock.to_local(datetime(2026, 7, 1, 12, tzinfo=timezone.utc)).hour, 3)

    def test_utc_without_tz_or_with_a_bad_name(self):
        for env in ({}, {"TZ": "Not/AZone"}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(local_time(at(2026, 7, 1, 12)), "12:00")

    def test_a_bad_name_is_said_in_the_log_notes(self):
        source = config.Source("usher.toml (made up)", {"media_server.user_id": "a" * 32})
        with mock.patch.dict(os.environ, {"TZ": "Not/AZone"}):
            self.assertTrue(any("Not/AZone" in n for n in config.check(source)))
        with mock.patch.dict(os.environ, {"TZ": "America/Adak"}):
            self.assertEqual(config.check(source), [])

    def test_the_old_offset_key_starts_normally_and_is_noted(self):
        text = '[server]\nutc_offset_minutes = 120\n[media_server]\nuser_id = "' + "a" * 32 + '"\n'
        ignored = []
        values = config.parse(text, "usher.toml (made up)", ignored)
        self.assertNotIn("server.utc_offset_minutes", values)
        source = config.Source("usher.toml (made up)", values, tuple(ignored))
        self.assertTrue(any("server.utc_offset_minutes" in n and "no longer used" in n for n in config.check(source)))
        self.assertNotIn("server.utc_offset_minutes", config.KEYS)

    def test_an_old_database_loses_the_device_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store._db.execute("ALTER TABLE notify_prefs ADD COLUMN tz_offset INTEGER NOT NULL DEFAULT 0")
            store._db.close()
            store = Store(Path(tmp))
            self.addCleanup(store._db.close)
            cols = {r[1] for r in store._db.execute("PRAGMA table_info(notify_prefs)")}
            self.assertNotIn("tz_offset", cols)
