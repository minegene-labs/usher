"""A new episode is never announced or labelled with a season or episode number Usher doesn't have."""

import unittest
from unittest import mock

from tests import test_notify
from tests.test_notify import snap
from usher import notify
from usher.state.helpers import episode_label, episode_subtitle


def ep(i, season=None, number=None):
    return {"Id": i, "Type": "Episode", "SeriesName": "Bluey", "ParentIndexNumber": season, "IndexNumber": number}


class NotifyNumbers(unittest.TestCase):
    # the same fixtures as test_notify, without re-running its tests
    setUp = test_notify.NotifyTests.setUp
    tearDown = test_notify.NotifyTests.tearDown
    run_check = test_notify.NotifyTests.run_check

    def titles(self):
        return sorted({m["title"] for _, m in self.sent})

    def check_at(self, items, t):
        with mock.patch("usher.notify.time.time", return_value=t):
            self.run_check(snap(), {"ready": items})

    def start(self):
        self.check_at([], 1000)  # first run: nothing announced

    def test_episode_without_numbers_is_held_then_announced_with_them(self):
        self.start()
        self.check_at([ep("a")], 1010)
        self.assertEqual(self.sent, [])
        self.check_at([ep("a", 3, 4)], 1070)
        self.assertEqual(self.titles(), ["Bluey S3E4 is ready to watch"])
        self.sent.clear()
        self.check_at([ep("a", 3, 4)], 1130)
        self.assertEqual(self.sent, [])  # once only

    def test_episode_still_without_numbers_after_the_limit_is_announced_by_title(self):
        self.start()
        self.check_at([ep("a", None, 4)], 1010)
        self.assertEqual(self.sent, [])
        self.check_at([ep("a", None, 4)], 1010 + notify.HOLD_NUMBERS_FOR - 1)
        self.assertEqual(self.sent, [])
        self.check_at([ep("a", None, 4)], 1010 + notify.HOLD_NUMBERS_FOR)
        self.assertEqual(self.titles(), ["Bluey is ready to watch"])

    def test_specials_episode_keeps_season_zero(self):
        self.start()
        self.check_at([ep("a", 0, 3)], 1010)
        self.assertEqual(self.titles(), ["Bluey S0E3 is ready to watch"])

    def test_held_episode_that_disappears_is_forgotten(self):
        self.start()
        self.check_at([ep("a")], 1010)
        self.check_at([], 1020)
        self.assertEqual(self.n._held, {})


class Labels(unittest.TestCase):
    def test_label_is_left_out_when_a_number_is_missing(self):
        self.assertEqual(episode_label(None, None), "")
        self.assertEqual(episode_label(1, None), "")
        self.assertEqual(episode_label(None, 2), "")
        self.assertEqual(episode_label(1, 2), "S1E2")
        self.assertEqual(episode_label(0, 3), "S0E3")

    def test_subtitle_keeps_the_name_without_a_label(self):
        self.assertEqual(episode_subtitle(None, None, "Hide and Seek"), "Hide and Seek")
        self.assertEqual(episode_subtitle(None, None), "")
        self.assertEqual(episode_subtitle(2, 4, "Hide and Seek"), "S2E4 · Hide and Seek")


if __name__ == "__main__":
    unittest.main()
