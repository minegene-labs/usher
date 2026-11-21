"""Remove and Tidy up only promise the recycle bin when Sonarr or Radarr has one, and name its own number of days."""

import json
import shutil
import subprocess
import unittest
from unittest import mock

from tests.webjs import WEB
from usher.clients import ApiError
from usher.state import State
from usher.state.helpers import recycle_policy

SCRIPT = """
import { afterRemoving, binEmptying } from "%(recycle)s";
const on = (days) => ({ on: true, days });
const off = { on: false, days: null };
console.log(JSON.stringify({
  seven: afterRemoving({ tv: on(7), movie: on(7) }, "movie"),
  fourteen: afterRemoving({ tv: on(14), movie: on(7) }, "tv"),
  forever: afterRemoving({ tv: on(null), movie: on(7) }, "tv"),
  none: afterRemoving({ tv: on(7), movie: off }, "movie"),
  unknown: afterRemoving({ tv: null, movie: null }, "tv"),
  emptyingSeven: binEmptying({ tv: on(7), movie: on(7) }),
  emptyingMixed: binEmptying({ tv: on(14), movie: on(7) }),
  emptyingNone: binEmptying({ tv: off, movie: off }),
  emptyingForever: binEmptying({ tv: on(null), movie: on(null) }),
}));
"""


def make_state(sonarr_conf, radarr_conf):
    settings = mock.Mock(media_path="/srv/media", low_space_gb=30, recycle_dir="/nonexistent", family_tv_folder="", family_movie_folder="")
    sonarr, radarr = mock.Mock(), mock.Mock()
    sonarr.name, radarr.name = "sonarr", "radarr"
    for app, conf in ((sonarr, sonarr_conf), (radarr, radarr_conf)):
        if isinstance(conf, Exception):
            app.get.side_effect = conf
        else:
            app.get.return_value = conf
    return State(settings, sonarr, radarr, mock.Mock(), mock.Mock(), mock.Mock())


class ReadingTheSetting(unittest.TestCase):
    def test_a_bin_with_days(self):
        self.assertEqual(recycle_policy({"recycleBin": "/recycle", "recycleBinCleanupDays": 14}), {"on": True, "days": 14})

    def test_a_bin_that_is_never_emptied(self):
        self.assertEqual(recycle_policy({"recycleBin": "/recycle", "recycleBinCleanupDays": 0}), {"on": True, "days": None})

    def test_no_bin(self):
        self.assertEqual(recycle_policy({"recycleBin": "", "recycleBinCleanupDays": 7}), {"on": False, "days": None})
        self.assertEqual(recycle_policy({"recycleBinCleanupDays": 7}), {"on": False, "days": None})

    def test_polling_reads_each_app_and_keeps_the_last_answer_when_one_fails(self):
        st = make_state({"recycleBin": "/recycle", "recycleBinCleanupDays": 14}, {"recycleBin": "", "recycleBinCleanupDays": 7})
        st._poll_recycle_policy()
        self.assertEqual(st._raw["bin"], {"tv": {"on": True, "days": 14}, "movie": {"on": False, "days": None}})
        st.sonarr.get.side_effect = ApiError("down")
        st._poll_recycle_policy()
        self.assertEqual(st._raw["bin"]["tv"], {"on": True, "days": 14})


class StatusNote(unittest.TestCase):
    def status(self, bin_):
        st = make_state({}, {})
        st._raw["ok"].update(transmission=True, prowlarr=True, sonarr=True, radarr=True, jellyfin_up=True)
        st._raw["bin"] = bin_
        return st._status(st._raw)

    def test_a_note_names_each_app_without_a_bin_and_nothing_turns_red(self):
        out = self.status({"tv": {"on": False, "days": None}, "movie": {"on": False, "days": None}})
        self.assertEqual(len(out["notes"]), 2)
        self.assertIn("TV shows", out["notes"][0])
        self.assertIn("movies", out["notes"][1])
        self.assertTrue(out["ok"])

    def test_no_note_with_a_bin_or_while_it_is_unknown(self):
        self.assertEqual(self.status({"tv": {"on": True, "days": 7}, "movie": None})["notes"], [])
        self.assertEqual(self.status({"tv": None, "movie": None})["notes"], [])


class Wording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("needs Node to run the client's code")
        run = subprocess.run(
            ["node", "--input-type=module"], input=SCRIPT % {"recycle": (WEB / "recycle.js").as_uri()}, capture_output=True, text=True, timeout=30
        )
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.out = json.loads(run.stdout)

    def test_the_confirmation_names_the_apps_own_days(self):
        self.assertIn("for 7 days", self.out["seven"])
        self.assertIn("for 14 days", self.out["fourteen"])

    def test_a_bin_that_is_never_emptied_says_so(self):
        self.assertIn("until the recycle bin is emptied", self.out["forever"])

    def test_without_a_bin_it_says_deleted_for_good(self):
        self.assertIn("deleted for good", self.out["none"])
        self.assertNotIn("days", self.out["none"])

    def test_unknown_promises_nothing(self):
        self.assertNotIn("goes to the recycle bin", self.out["unknown"])

    def test_storage_line(self):
        self.assertEqual(self.out["emptyingSeven"], "emptied by itself after 7 days")
        self.assertEqual(self.out["emptyingMixed"], "emptied by itself after 7 to 14 days")
        self.assertEqual(self.out["emptyingNone"], "")
        self.assertEqual(self.out["emptyingForever"], "kept until you empty it")


class ScreensUseIt(unittest.TestCase):
    def test_no_screen_still_promises_seven_days(self):
        for name in ("sheets.js", "storage.js"):
            src = (WEB / name).read_text(encoding="utf-8")
            self.assertNotIn("7 days", src, name)
            self.assertIn("recycle.js", src, name)
