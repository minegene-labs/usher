"""The plan job of the Release workflow (.github/scripts/release_gate.py): the daily run makes a release only when
everything planned for it is done and says which condition was not met when it isn't; a hand run passes its version
through. Each case builds a made-up repository (a changelog, a version, dated commits, tags and a `stable`) and a fake
GitHub, so no network is used and every name and number is invented."""

import datetime
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_merge_report import _job, _permissions
from tests.test_release_workflow import TEXT, _step

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_gate", ROOT / ".github" / "scripts" / "release_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

NOW = datetime.datetime(2026, 3, 10, 12, 0, tzinfo=datetime.timezone.utc)
HOURS = datetime.timedelta(hours=1)
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- A fix.\n\n## [1.2.3]\n\n### Added\n- Something old.\n"
WITH_ADDED = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- A fix.\n\n### Added\n- A new row.\n\n## [1.2.3]\n\n### Fixed\n- Older.\n"
EMPTY = "# Changelog\n\n## [Unreleased]\n\n## [1.2.3]\n\n### Fixed\n- Older.\n"


def issue(number, *names, pull=None):
    item = {"number": number, "labels": [{"name": name} for name in names]}
    if pull is not None:
        item["pull_request"] = {"merged_at": pull}
    return item


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Answers the calls the gate makes from lists made up for the case, and notes what was asked."""

    def __init__(self, open_issues=(), merged=(), runs=()):
        self.open_issues, self.merged, self.runs, self.asked = list(open_issues), list(merged), list(runs), []

    def pages(self, path):
        self.asked.append(path)
        if path.startswith("issues?state=open&labels="):
            label = path.rsplit("=", 1)[1]
            return [i for i in self.open_issues if label in [x["name"] for x in i["labels"]]]
        if path.startswith("issues?state=closed&labels=needs-server"):
            return self.merged
        raise AssertionError(path)

    def get(self, path):
        self.asked.append(path)
        assert path.startswith("actions/workflows/release.yml/runs"), path
        return {"workflow_runs": self.runs}


@unittest.skipUnless(shutil.which("git"), "needs git")
class Repo(unittest.TestCase):
    """A made-up repository at `self.root`, which the script reads as its own."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        patch = mock.patch.object(gate, "ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.run_git("init", "-q", "-b", "main")
        (self.root / "usher").mkdir()
        (self.root / "usher" / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")

    def run_git(self, *args, when=None):
        env = {"PATH": os.defpath, "HOME": str(self.root), "GIT_CONFIG_GLOBAL": os.devnull}
        env |= {"GIT_AUTHOR_NAME": "Example", "GIT_AUTHOR_EMAIL": "example@example.invalid"}
        env |= {"GIT_COMMITTER_NAME": "Example", "GIT_COMMITTER_EMAIL": "example@example.invalid"}
        if when:
            env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = stamp(when)
        done = subprocess.run(["git", "-C", str(self.root), *args], env=env, capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def commit(self, when, message="change"):
        self.run_git("add", "-A")
        self.run_git("commit", "-q", "--allow-empty", "-m", message, when=when)

    def stable_at_head(self):
        self.run_git("update-ref", "refs/remotes/origin/stable", "HEAD")

    def history(self, changelog=CHANGELOG, last=NOW - 30 * HOURS, released=NOW - 100 * HOURS, tested=True):
        """A release (1.2.3) a while ago, then a change `last`, with `stable` where the case wants it."""
        (self.root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        self.commit(released, "release")
        self.run_git("tag", "v1.2.3")
        self.commit(last, "merged change")
        if tested:
            self.stable_at_head()


class DecideTests(Repo):
    def decide(self, api=None, event="schedule", typed="", run_id="500"):
        return gate.decide(event, typed, api or FakeGitHub(), run_id, NOW)

    def test_everything_planned_is_done_so_a_patch_is_released(self):
        self.history()
        version, lines = self.decide()
        self.assertEqual(version, "1.2.4")
        self.assertIn("releasing 1.2.4", lines[0])

    def test_something_added_makes_it_a_minor_release(self):
        self.history(WITH_ADDED)
        self.assertEqual(self.decide()[0], "1.3.0")

    def test_a_major_version_is_never_chosen(self):
        self.assertEqual(gate.next_version("1.9.9", ["v1.9.9"], True), "1.10.0")
        self.assertEqual(gate.next_version("1.9.9", ["v1.9.9"], False), "1.9.10")

    def test_the_version_starts_from_the_newer_of_the_app_and_the_latest_release(self):
        self.assertEqual(gate.next_version("1.2.3", ["v1.2.3", "v1.4.0"], False), "1.4.1")  # a release made by hand ahead of the file
        self.assertEqual(gate.next_version("1.5.0", ["v1.2.3"], False), "1.5.1")
        self.assertEqual(gate.next_version("0.1.0", [], True), "0.2.0")

    def test_an_open_next_release_or_p1_issue_stops_it_and_the_log_names_it(self):
        self.history()
        for label in ("next-release", "P1"):
            version, lines = self.decide(FakeGitHub(open_issues=[issue(41, label, "feature")]))
            self.assertIsNone(version, label)
            self.assertIn("#41", "\n".join(lines))

    def test_open_p2_and_p3_issues_without_the_label_do_not_stop_it(self):
        self.history()
        api = FakeGitHub(open_issues=[issue(42, "P2", "bug"), issue(43, "P3", "feature")])
        self.assertEqual(self.decide(api)[0], "1.2.4")

    def test_issues_on_hold_and_pull_requests_do_not_count(self):
        self.history()
        api = FakeGitHub(open_issues=[issue(44, "next-release", "hold"), issue(45, "P1", "hold"), issue(46, "next-release", pull="")])
        self.assertEqual(self.decide(api)[0], "1.2.4")

    def test_a_p1_issue_that_is_also_labelled_next_release_is_named_once(self):
        self.history()
        version, lines = self.decide(FakeGitHub(open_issues=[issue(47, "P1", "next-release")]))
        self.assertIsNone(version)
        self.assertEqual("\n".join(lines).count("#47"), 1)

    def test_an_empty_unreleased_releases_nothing_and_does_not_fail(self):
        self.history(EMPTY)
        version, lines = self.decide()
        self.assertIsNone(version)
        self.assertIn("Nothing is collected under [Unreleased]", "\n".join(lines))

    def test_a_pull_request_merged_since_the_release_that_needs_a_server_step_stops_it(self):
        self.history()
        merged = [issue(50, "needs-server", pull=stamp(NOW - 40 * HOURS))]
        version, lines = self.decide(FakeGitHub(merged=merged))
        self.assertIsNone(version)
        self.assertIn("needs-server", "\n".join(lines))
        self.assertIn("#50", "\n".join(lines))

    def test_a_needs_server_pull_request_merged_before_the_release_or_never_merged_does_not(self):
        self.history()
        merged = [issue(51, "needs-server", pull=stamp(NOW - 200 * HOURS)), issue(52, "needs-server", pull=None), issue(53, "needs-server")]
        self.assertEqual(self.decide(FakeGitHub(merged=merged))[0], "1.2.4")

    def test_only_what_changed_since_the_release_is_asked_for(self):
        self.history()
        api = FakeGitHub()
        self.decide(api)
        self.assertIn(f"issues?state=closed&labels=needs-server&since={stamp(NOW - 100 * HOURS)}", api.asked)

    def test_a_latest_commit_under_24_hours_old_stops_it(self):
        self.history(last=NOW - 23 * HOURS)
        version, lines = self.decide()
        self.assertIsNone(version)
        self.assertIn("at least 24 hours old", "\n".join(lines))

    def test_a_latest_commit_exactly_24_hours_old_is_old_enough(self):
        self.history(last=NOW - 24 * HOURS)
        self.assertEqual(self.decide()[0], "1.2.4")

    def test_a_main_that_stable_has_not_reached_stops_it(self):
        self.history(tested=False)
        self.run_git("update-ref", "refs/remotes/origin/stable", "HEAD~1")
        version, lines = self.decide()
        self.assertIsNone(version)
        self.assertIn("main is not at stable", "\n".join(lines))

    def test_another_release_run_in_progress_stops_it_but_this_run_and_finished_ones_do_not(self):
        self.history()
        done = [{"id": 400, "status": "completed"}, {"id": 500, "status": "in_progress"}]
        self.assertEqual(self.decide(FakeGitHub(runs=done))[0], "1.2.4")
        for status in ("in_progress", "queued", "waiting"):
            version, lines = self.decide(FakeGitHub(runs=[*done, {"id": 401, "status": status}]))
            self.assertIsNone(version, status)
            self.assertIn("401", "\n".join(lines))

    def test_every_unmet_condition_is_listed(self):
        self.history(EMPTY, last=NOW - HOURS)
        version, lines = self.decide(FakeGitHub(open_issues=[issue(41, "P1")]))
        self.assertIsNone(version)
        self.assertEqual(len(lines), 4)  # the heading and three reasons

    def test_with_no_release_yet_every_needs_server_pull_request_counts(self):
        (self.root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        self.commit(NOW - 30 * HOURS)
        self.stable_at_head()
        api = FakeGitHub(merged=[issue(60, "needs-server", pull=stamp(NOW - 900 * HOURS))])
        version, _ = self.decide(api)
        self.assertIsNone(version)
        self.assertIn("issues?state=closed&labels=needs-server", api.asked)

    def test_a_hand_run_passes_its_version_through_without_asking_github(self):
        api = FakeGitHub(open_issues=[issue(41, "P1")])
        version, lines = self.decide(api, event="workflow_dispatch", typed="1.5.0")
        self.assertEqual(version, "1.5.0")
        self.assertEqual(api.asked, [])
        self.assertIn("by hand", lines[0])

    def test_a_hand_typed_version_that_is_not_a_version_is_refused(self):
        for typed in ("", "1.5", "v1.5.0", "1.5.0\nrelease=yes", "1.5.0 ", "latest"):
            with self.assertRaises(ValueError, msg=repr(typed)):
                self.decide(event="workflow_dispatch", typed=typed)

    def test_a_missing_workflow_is_an_error_not_a_crash(self):
        self.history()
        api = FakeGitHub()
        api.get = lambda path: None  # GitHub's 404
        with self.assertRaisesRegex(RuntimeError, "no workflow release.yml"):
            self.decide(api)

    def test_any_other_event_is_refused(self):
        with self.assertRaises(ValueError):
            self.decide(event="push")


class MainTests(Repo):
    """What the workflow step sees: the exit code and the two outputs."""

    def run_main(self, api, event="schedule", typed=""):
        output = self.root / "output"
        env = {"EVENT_NAME": event, "TYPED_VERSION": typed, "GH_TOKEN": "made-up", "GITHUB_REPOSITORY": "example/usher"}
        env |= {"GITHUB_RUN_ID": "500", "GITHUB_OUTPUT": str(output)}
        with mock.patch.dict(os.environ, env), mock.patch.object(gate, "GitHub", lambda *args, **kwargs: api):
            code = gate.main()
        return code, output.read_text(encoding="utf-8") if output.exists() else ""

    def test_a_release_writes_yes_and_the_version(self):
        self.history()
        self.assertEqual(self.run_main(FakeGitHub()), (0, "release=yes\nversion=1.2.4\n"))

    def test_no_release_ends_green_with_no(self):
        self.history()
        self.assertEqual(self.run_main(FakeGitHub(open_issues=[issue(41, "P1")])), (0, "release=no\nversion=\n"))

    def test_a_hand_run_writes_its_version(self):
        self.assertEqual(self.run_main(FakeGitHub(), "workflow_dispatch", "2.0.0"), (0, "release=yes\nversion=2.0.0\n"))

    def test_a_state_that_cannot_be_read_fails_the_run_instead_of_releasing_or_skipping(self):
        """No history to read, so the run is red: a failure is never mistaken for 'nothing to release'."""
        shutil.rmtree(self.root / ".git")
        self.assertEqual(self.run_main(FakeGitHub()), (1, ""))


class ChangelogTests(unittest.TestCase):
    def test_what_is_collected(self):
        self.assertEqual(gate.collected(CHANGELOG), (True, False))  # the Added entry of 1.2.3 doesn't count
        self.assertEqual(gate.collected(WITH_ADDED), (True, True))
        self.assertEqual(gate.collected(EMPTY), (False, False))
        self.assertEqual(gate.collected("# Changelog\n\n## [1.0.0]\n\n### Added\n- Old.\n"), (False, False))


class PlanJobTests(unittest.TestCase):
    """The workflow's text around the script."""

    def setUp(self):
        self.job = _job(TEXT, "plan")

    def test_it_runs_the_script_on_main_with_no_secret_and_no_environment(self):
        self.assertIn("run: python3 .github/scripts/release_gate.py", _step(self.job, "Decide whether to release, and which version"))
        self.assertNotIn("secrets.", self.job)
        self.assertNotIn("environment:", self.job)
        self.assertIn("persist-credentials: false", self.job)
        self.assertIn("fetch-depth: 0", self.job)
        self.assertIn('[ "$REF" = refs/heads/main ]', self.job)

    def test_it_only_reads(self):
        for scope, level in _permissions(self.job).items():
            self.assertEqual(level, "read", scope)

    def test_the_release_job_waits_for_its_yes_and_takes_its_version(self):
        prepare = _job(TEXT, "prepare")
        self.assertIn("if: ${{ needs.plan.outputs.release == 'yes' }}", prepare)
        self.assertIn("VERSION: ${{ needs.plan.outputs.version }}", prepare)
        self.assertIn("release: ${{ steps.gate.outputs.release }}", self.job)
        self.assertIn("version: ${{ steps.gate.outputs.version }}", self.job)

    def test_the_hand_typed_version_reaches_the_script_through_the_environment(self):
        self.assertIn("TYPED_VERSION: ${{ inputs.version }}", self.job)
        self.assertIn("EVENT_NAME: ${{ github.event_name }}", self.job)

    def test_the_label_that_plans_a_release_exists(self):
        labels = (ROOT / ".github" / "workflows" / "labels.yml").read_text(encoding="utf-8")
        self.assertRegex(labels, r"gh label create next-release .*--force")


if __name__ == "__main__":
    unittest.main()
