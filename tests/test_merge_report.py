"""After each merge, Promote reports on it by itself: a verdict (OK or PROBLEM) and one line per check, as a comment on
the pull request and in the job summary. A PROBLEM opens or updates one issue and turns the run red. The checks live
in .github/scripts/merge_report.py and are tested here without calling GitHub; made-up values only."""

import base64
import contextlib
import datetime
import importlib.util
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_public_conventions import ATTRIBUTION

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "merge_report.py"
WORKFLOWS = ROOT / ".github" / "workflows"

spec = importlib.util.spec_from_file_location("merge_report", SCRIPT)
report_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_mod)

SHA = "0123456789abcdef0123456789abcdef01234567"
EARLIER = "fedcba9876543210fedcba9876543210fedcba98"
NOW = datetime.datetime(2030, 1, 10, 12, 0, tzinfo=datetime.timezone.utc)
OLD = "2020-01-01T00:00:00Z"  # over a day before NOW and before the real clock
BODY = "## Summary\n\nA change.\n\nChanges how a screen looks: no\n\nFixes #7\n"


def merge(**changes):
    """A merge that passes every check, with `changes` applied."""
    data = {
        "number": 42,
        "sha": SHA,
        "promote": "success",
        "dependabot": False,
        "body": BODY,
        "version": "1.2.0",
        "previous_version": "1.1.0",
        "stable": "identical",
        "issues": {7: "closed"},
        "files": [{"filename": "README.md", "patch": "@@ -1 +1 @@\n-a\n+b"}],
        "unlabelled": [],
        "problem_issue": None,
    }
    data.update(changes)
    return data


def run(**changes):
    """(verdict, the report's lines)."""
    verdict, text = report_mod.report(merge(**changes))
    return verdict, text.splitlines()


def line(lines, words):
    """The one check line containing `words`."""
    found = [entry for entry in lines if words in entry]
    assert len(found) == 1, (words, lines)
    return found[0]


class VerdictTests(unittest.TestCase):
    def test_all_ok(self):
        verdict, lines = run()
        self.assertEqual(verdict, "OK")
        self.assertTrue(lines[0].startswith("OK: the merge of #42 (0123456789ab) passed every check."))
        checks = [entry for entry in lines if entry.startswith("- **")]
        self.assertEqual(len(checks), len(report_mod.CHECKS))
        self.assertFalse([entry for entry in checks if entry.startswith("- **problem**")])
        self.assertEqual("- **ok**: The promote job finished.", line(lines, "promote job"))
        self.assertIn("The version changed to 1.2.0.", line(lines, "version"))
        self.assertIn("`stable` is at the merge commit.", line(lines, "`stable`"))
        self.assertIn("The issue it fixes is closed: #7.", line(lines, "fixes"))
        self.assertEqual("- **info**: Files flagged: none.", line(lines, "Files flagged"))
        self.assertIn("**ok**: Every open issue has a priority label", line(lines, "priority label"))

    def test_problem_verdict_counts_the_failed_checks(self):
        verdict, lines = run(stable="behind", unlabelled=[3])
        self.assertEqual(verdict, "PROBLEM")
        self.assertEqual(lines[0], "PROBLEM: the merge of #42 (0123456789ab) needs attention: 2 check(s) failed.")


class AttributionTests(unittest.TestCase):
    """A merge needs no tool boilerplate, and the report doesn't look for it."""

    def test_a_plain_message_and_description_pass(self):
        verdict, lines = run(body="## Summary\n\nA change.\n\nFixes #7\n")
        self.assertEqual(verdict, "OK")
        for entry in lines:
            self.assertNotRegex(entry, ATTRIBUTION)
            self.assertNotRegex(entry.lower(), r"trailer|footer")

    def test_no_check_looks_for_them(self):
        names = [check.__name__ for check in report_mod.CHECKS]
        self.assertNotIn("check_trailer", names)
        self.assertNotIn("check_footer", names)


class FixedIssueTests(unittest.TestCase):
    def test_issue_still_open(self):
        verdict, lines = run(issues={7: "open"})
        self.assertEqual(verdict, "PROBLEM")
        self.assertIn("**problem**: Not every issue it fixes is closed (open: #7).", line(lines, "fixes"))

    def test_several_issues_one_missing(self):
        verdict, lines = run(issues={7: "closed", 8: "missing", 9: "closed"})
        self.assertEqual(verdict, "PROBLEM")
        self.assertIn("(missing: #8)", line(lines, "fixes"))
        self.assertIn("The issues it fixes are closed: #7, #9.", line(run(issues={7: "closed", 9: "closed"})[1], "fixes"))

    def test_no_fixes_line(self):
        verdict, lines = run(issues={})
        self.assertEqual(verdict, "PROBLEM")
        self.assertIn("names no issue with a `Fixes #<n>` line", line(lines, "Fixes #<n>"))

    def test_fixed_issues_are_read_from_the_description(self):
        text = "Fixes #7\nCloses #8, resolves: #9\n<!-- Fixes #1 is only an example -->\nfixed #7\nSee #10\n"
        self.assertEqual(report_mod.fixed_issues(text), [7, 8, 9])


class PriorityLabelTests(unittest.TestCase):
    def test_missing_priority_label(self):
        verdict, lines = run(unlabelled=[3, 12])
        self.assertEqual(verdict, "PROBLEM")
        self.assertIn("**problem**: Open issues without a priority label (P1, P2 or P3), opened over a day ago: #3, #12.", line(lines, "priority label"))

    def issue(self, *labels, created=OLD):
        return {"number": 1, "labels": [{"name": name} for name in labels], "created_at": created}

    def test_an_old_issue_without_a_priority_needs_one(self):
        self.assertTrue(report_mod.needs_priority(self.issue("bug"), NOW))
        self.assertTrue(report_mod.needs_priority(self.issue(), NOW))
        self.assertFalse(report_mod.needs_priority(self.issue("bug", "P3"), NOW))

    def test_issues_the_repository_opens_by_itself_are_left_out(self):
        # The upstream check's issues, its standing "Upstream betas" issue, its issue for each new release and the
        # weekly vulnerabilities issue.
        for label in ("upstream", "upstream-beta", "upstream-release", "vulnerabilities"):
            self.assertFalse(report_mod.needs_priority(self.issue(label), NOW), label)

    def test_an_issue_younger_than_a_day_is_not_triaged_yet(self):
        self.assertFalse(report_mod.needs_priority(self.issue("bug", created="2030-01-10T00:00:00Z"), NOW))
        self.assertFalse(report_mod.needs_priority(self.issue(created="2030-01-09T12:00:01Z"), NOW))
        self.assertTrue(report_mod.needs_priority(self.issue(created="2030-01-09T12:00:00Z"), NOW))


class PromoteTests(unittest.TestCase):
    def test_a_failed_promote_is_a_problem(self):
        for result in ("failure", "skipped"):
            verdict, lines = run(promote=result, stable="behind")
            self.assertEqual(verdict, "PROBLEM", result)
            self.assertIn(f"**problem**: The promote job didn't finish ({result}): `stable` may not have moved.", line(lines, "promote job"))


class FlaggedFileTests(unittest.TestCase):
    def files(self, *paths, patch="@@ -1 +1 @@\n-a\n+b"):
        return [{"filename": p, "patch": patch} for p in paths]

    def test_a_flagged_path_is_shown_but_is_not_a_problem(self):
        verdict, lines = run(files=self.files("README.md", "deploy/compose.yaml", ".github/workflows/release.yml"))
        self.assertEqual(verdict, "OK")
        self.assertIn("**info**: Files flagged", line(lines, "Files flagged"))
        self.assertIn("`.github/workflows/release.yml`, `deploy/compose.yaml`.", line(lines, "Files flagged"))
        self.assertNotIn("README.md", line(lines, "Files flagged"))

    def test_every_kind_of_flagged_path(self):
        flagged = ["Dockerfile", ".claude/settings.json", "web/data.js", "web/sw.js", "deploy/usher-update", ".github/workflows/tests.yml"]
        for path in flagged:
            self.assertTrue(report_mod.is_flagged({"filename": path, "patch": None}), path)
        for path in ("web/app.js", "usher/server.py", ".github/scripts/pr_gates.py", "docs/deploy.md", "tests/test_x.py"):
            self.assertFalse(report_mod.is_flagged({"filename": path, "patch": None}), path)

    def test_a_service_worker_version_bump_alone_is_not_flagged(self):
        bump = '@@ -4 +4 @@\n-const VERSION = "usher-1.1.0";\n+const VERSION = "usher-1.2.0";'
        self.assertFalse(report_mod.is_flagged({"filename": "web/sw.js", "patch": bump}))
        self.assertTrue(report_mod.is_flagged({"filename": "web/sw.js", "patch": bump + "\n+self.skipWaiting();"}))
        self.assertTrue(report_mod.is_flagged({"filename": "web/sw.js", "patch": None}))  # a diff too large to show

    def test_paths_are_made_safe_and_the_list_is_capped(self):
        verdict, lines = run(files=self.files("deploy/a`b<c>.sh", *[f"deploy/f{n:02}" for n in range(25)]))
        self.assertEqual(verdict, "OK")
        flagged = line(lines, "Files flagged")
        self.assertIn("`deploy/a?b?c?.sh`", flagged)
        self.assertTrue(flagged.endswith(" and 6 more."))


class VersionTests(unittest.TestCase):
    """The version is the build number that makes a device drop its saved copy of the app."""

    def test_a_bump_is_ok(self):
        verdict, lines = run(files=[{"filename": "usher/server.py"}])
        self.assertEqual(verdict, "OK")
        self.assertEqual("- **ok**: The version changed to 1.2.0.", line(lines, "version"))

    def test_an_unchanged_version_is_a_problem_when_the_app_changed(self):
        for path in ("usher/server.py", "web/app.css", "deploy/compose.yaml", "Dockerfile", "tests/test_basics.py", "requirements.txt", "CHANGELOG.md"):
            verdict, lines = run(previous_version="1.2.0", files=[{"filename": "README.md"}, {"filename": path}])
            self.assertEqual(verdict, "PROBLEM", path)
            self.assertIn("**problem**: The version is still 1.2.0: it wasn't bumped", line(lines, "version"))

    def test_a_merge_that_changes_no_app_code_needs_no_new_version(self):
        for paths in (["README.md"], ["docs/architecture.md", ".claude/settings.json", ".github/workflows/promote.yml", "LICENSE"]):
            verdict, lines = run(previous_version="1.2.0", files=[{"filename": p} for p in paths])
            self.assertEqual(verdict, "OK", paths)
            self.assertIn("no new version is needed", line(lines, "version"))

    def test_moving_a_file_out_of_the_app_counts_as_changing_it(self):
        files = [{"filename": "docs/old-server.md", "previous_filename": "usher/server.py"}]
        self.assertEqual(run(previous_version="1.2.0", files=files)[0], "PROBLEM")

    def test_dependabots_updates_keep_the_version(self):
        verdict, lines = run(dependabot=True, issues={}, previous_version="1.2.0", files=[{"filename": "requirements.txt"}])
        self.assertEqual(verdict, "OK")
        self.assertIn("**info**: The version stays 1.2.0: Dependabot's updates keep it.", line(lines, "version"))

    def test_no_version_found(self):
        self.assertEqual(run(version=None)[0], "PROBLEM")

    def test_a_first_commit_with_no_parent_is_not_compared(self):
        self.assertEqual(run(previous_version=None)[0], "OK")


class StableTests(unittest.TestCase):
    def test_stable_past_the_merge_is_fine_behind_is_not(self):
        verdict, lines = run(stable="ahead")
        self.assertEqual(verdict, "OK")
        self.assertIn("past the merge commit", line(lines, "`stable`"))
        for status in ("behind", "diverged", "missing"):
            verdict, lines = run(stable=status)
            self.assertEqual(verdict, "PROBLEM", status)
            self.assertIn(f"`stable` is not at the merge commit ({status}).", line(lines, "`stable`"))


class SupersededMergeTests(unittest.TestCase):
    """A merge whose report comes late, after a later merge moved `stable` past it: promote's fast-forward push was
    refused, so its promote job failed, though `stable` holds the merge."""

    def test_is_information_and_not_a_problem(self):
        for result in ("cancelled", "failure"):
            verdict, lines = run(stable="ahead", promote=result)
            self.assertEqual(verdict, "OK", result)
            self.assertEqual(lines[0], "OK: the merge of #42 (0123456789ab) passed every check.")
            self.assertFalse([entry for entry in lines if entry.startswith("- **problem**")])
            self.assertIn(f"**info**: The promote job didn't finish ({result}), but `stable` is already past the merge commit", line(lines, "The promote job"))
            self.assertIn("**info**: `stable` is past the merge commit: a later merge was promoted since.", line(lines, "`stable` is past"))

    def test_without_stable_past_it_the_failure_is_a_problem(self):
        for status in ("behind", "diverged", "missing", "identical"):
            verdict, lines = run(stable=status, promote="failure")
            self.assertEqual(verdict, "PROBLEM", status)
            self.assertIn("**problem**: The promote job didn't finish (failure)", line(lines, "The promote job"))

    def test_other_checks_still_count(self):
        verdict, lines = run(stable="ahead", promote="failure", issues={7: "open"}, unlabelled=[3])
        self.assertEqual(verdict, "PROBLEM")
        self.assertEqual(lines[0], "PROBLEM: the merge of #42 (0123456789ab) needs attention: 2 check(s) failed.")
        self.assertIn("**problem**: Not every issue it fixes is closed", line(lines, "fixes"))

    def test_a_merge_that_was_promoted_itself_is_still_ok_when_stable_moved_on(self):
        verdict, lines = run(stable="ahead")
        self.assertEqual(verdict, "OK")
        self.assertEqual("- **ok**: The promote job finished.", line(lines, "The promote job"))


class DependabotTests(unittest.TestCase):
    def test_an_update_needs_no_issue(self):
        verdict, lines = run(dependabot=True, body="Bumps example from 1.0 to 1.1.", issues={}, previous_version="1.2.0")
        self.assertEqual(verdict, "OK")
        self.assertIn("**info**", line(lines, "Fixed issue"))

    def test_an_update_still_needs_stable_and_priority_labels(self):
        self.assertEqual(run(dependabot=True, stable="behind", issues={}, previous_version="1.2.0")[0], "PROBLEM")
        self.assertEqual(run(dependabot=True, unlabelled=[5], issues={}, previous_version="1.2.0")[0], "PROBLEM")


class FakeGitHub:
    """Answers from a table of made-up REST responses; anything else is a 404."""

    def __init__(self, answers, lists):
        self.answers, self.lists, self.asked = answers, lists, []

    def get(self, path):
        self.asked.append(path)
        return self.answers.get(path)

    def pages(self, path):
        self.asked.append(path)
        return self.lists[path]


def fake_github(**overrides):
    def init(version):
        return {"content": base64.b64encode(f'"""Usher."""\n\n__version__ = "{version}"\n'.encode()).decode(), "encoding": "base64"}

    answers = {
        f"commits/{SHA}/pulls": [
            {"number": 41, "merge_commit_sha": EARLIER, "merged_at": "2030-01-01T00:00:00Z"},
            {"number": 42, "merge_commit_sha": SHA, "merged_at": "2030-01-02T00:00:00Z", "body": BODY, "user": {"login": "example-user"}},
        ],
        f"commits/{SHA}": {"sha": SHA, "parents": [{"sha": EARLIER}]},
        f"contents/usher/__init__.py?ref={SHA}": init("1.2.0"),
        f"contents/usher/__init__.py?ref={EARLIER}": init("1.1.0"),
        f"compare/{SHA}...stable": {"status": "identical"},
        "issues/7": {"number": 7, "state": "closed"},
    }
    lists = {
        "pulls/42/files": [{"filename": "README.md", "patch": "@@ -1 +1 @@\n-a\n+b"}, {"filename": "web/data.js"}],
        "issues?state=open": [
            {"number": 3, "title": "An example", "labels": [{"name": "enhancement"}, {"name": "P2"}], "created_at": OLD},
            {"number": 4, "title": "Without priority", "labels": [{"name": "bug"}], "created_at": OLD},
            {"number": 5, "title": "A pull request", "labels": [], "pull_request": {}, "created_at": OLD},
            {"number": 8, "title": "Merge problem: PR #39, by hand", "labels": [{"name": "bug"}], "created_at": OLD},
            {"number": 6, "title": "Merge problem: PR #40", "labels": [{"name": "bug"}, {"name": "P1"}], "created_at": OLD},
            {"number": 9, "title": "Latest releases fail a call", "labels": [{"name": "upstream"}], "created_at": OLD},
            {"number": 10, "title": "Just opened", "labels": [{"name": "feature"}], "created_at": "2030-01-10T11:00:00Z"},
        ],
    }
    answers.update(overrides)
    return FakeGitHub(answers, lists)


class CollectTests(unittest.TestCase):
    def test_reads_the_merge_from_github(self):
        data = report_mod.collect(fake_github(), SHA, now=NOW)
        self.assertEqual(data["number"], 42)
        self.assertEqual(data["promote"], "success")
        self.assertFalse(data["dependabot"])
        self.assertEqual(data["stable"], "identical")
        self.assertEqual((data["version"], data["previous_version"]), ("1.2.0", "1.1.0"))
        self.assertEqual(data["issues"], {7: "closed"})
        self.assertEqual(data["unlabelled"], [4, 8])  # not pull requests, the upstream issue or the one just opened
        self.assertEqual(data["problem_issue"], 6)  # the title alone isn't enough: #8 lacks the P1 label
        self.assertEqual([f["filename"] for f in data["files"]], ["README.md", "web/data.js"])
        verdict, text = report_mod.report(data)
        self.assertEqual(verdict, "PROBLEM")
        self.assertIn("#4", text)
        self.assertIn("`web/data.js`", text)

    def test_a_push_without_a_pull_request_has_nothing_to_report(self):
        self.assertIsNone(report_mod.collect(fake_github(**{f"commits/{SHA}/pulls": []}), SHA))
        unmerged = [{"number": 42, "merge_commit_sha": SHA, "merged_at": None}]
        self.assertIsNone(report_mod.collect(fake_github(**{f"commits/{SHA}/pulls": unmerged}), SHA))

    def test_no_version_or_parent_is_read_as_none(self):
        data = report_mod.collect(fake_github(**{f"contents/usher/__init__.py?ref={SHA}": None, f"commits/{SHA}": None}), SHA, now=NOW)
        self.assertEqual((data["version"], data["previous_version"]), (None, None))

    def test_missing_stable_and_issue(self):
        data = report_mod.collect(fake_github(**{f"compare/{SHA}...stable": None, "issues/7": None}), SHA)
        self.assertEqual(data["stable"], "missing")
        self.assertEqual(data["issues"], {7: "missing"})

    def test_dependabot_is_told_apart_by_its_login(self):
        pulls = [{"number": 42, "merge_commit_sha": SHA, "merged_at": "2030-01-02T00:00:00Z", "body": None, "user": {"login": "dependabot[bot]"}}]
        data = report_mod.collect(fake_github(**{f"commits/{SHA}/pulls": pulls}), SHA)
        self.assertTrue(data["dependabot"])
        self.assertEqual(data["issues"], {})


class MainTests(unittest.TestCase):
    def run_main(self, github, extra=None):
        with tempfile.TemporaryDirectory() as folder:
            outputs = Path(folder) / "outputs"
            env = {"GITHUB_REPOSITORY": "example/usher", "GH_TOKEN": "made-up", "GITHUB_OUTPUT": str(outputs)}
            env.update(extra or {})
            with mock.patch.dict(os.environ, env), mock.patch.object(report_mod, "GitHub", lambda *args: github):
                printed = io.StringIO()
                with contextlib.redirect_stdout(printed):
                    report_mod.main([SHA])
            return printed.getvalue(), outputs.read_text(encoding="utf-8") if outputs.exists() else ""

    def test_prints_the_report_and_sets_the_step_outputs(self):
        printed, outputs = self.run_main(fake_github())
        self.assertTrue(printed.startswith("PROBLEM: the merge of #42"))
        self.assertEqual(outputs, "number=42\nverdict=PROBLEM\nopen_issue=6\n")

    def test_reports_a_failed_promote(self):
        printed, outputs = self.run_main(fake_github(), {"PROMOTE": "failure"})
        self.assertIn("**problem**: The promote job didn't finish (failure)", printed)
        self.assertIn("verdict=PROBLEM\n", outputs)

    def test_no_pull_request_sets_no_outputs(self):
        printed, outputs = self.run_main(fake_github(**{f"commits/{SHA}/pulls": []}))
        self.assertIn("no merge to report", printed)
        self.assertEqual(outputs, "")

    def test_refuses_anything_but_a_commit_hash(self):
        for argv in ([], ["main"], [SHA, "extra"], ["$(id)"]):
            with self.assertRaises(SystemExit):
                report_mod.main(argv)


class PagesTests(unittest.TestCase):
    def test_follows_next_links(self):
        github = report_mod.GitHub("example/usher", "made-up", "https://api.example.com/")
        pages = {
            "https://api.example.com/repos/example/usher/issues?state=open&per_page=100": ([1, 2], '<https://api.example.com/next>; rel="next"'),
            "https://api.example.com/next": ([3], '<https://api.example.com/first>; rel="first"'),
        }
        with mock.patch.object(github, "_fetch", side_effect=lambda url: pages[url]):
            self.assertEqual(github.pages("issues?state=open"), [1, 2, 3])


def _job(text, name):
    found = re.search(rf"(?m)^  {name}:\n((?:    .*\n|\n)+)", text)
    assert found, f"no {name} job"
    return found.group(1)


def _permissions(job):
    block = re.search(r"(?m)^    permissions:.*\n((?:      .*\n)+)", job).group(1)
    return {key: value for key, value in re.findall(r"(?m)^      ([\w-]+): (\w+)", block)}


class PromoteWorkflowTests(unittest.TestCase):
    """The report is one short job after promote, with only the permissions it needs."""

    def setUp(self):
        self.text = (WORKFLOWS / "promote.yml").read_text(encoding="utf-8")

    def test_only_promote_can_write_to_stable(self):
        self.assertRegex(self.text, r"(?m)^permissions: \{\}$")
        self.assertEqual(re.findall(r"(?m)^  ([\w-]+):$", self.text.split("\njobs:\n", 1)[1]), ["promote", "report"])
        self.assertEqual(_permissions(_job(self.text, "promote")), {"contents": "write"})
        self.assertEqual(_permissions(_job(self.text, "report")), {"contents": "read", "pull-requests": "write", "issues": "write"})

    def test_report_runs_after_promote_even_when_it_failed_and_reads_github_only(self):
        job = _job(self.text, "report")
        self.assertRegex(job, r"(?m)^    needs: \[promote\]$")
        self.assertRegex(job, r"(?m)^    if: \$\{\{ !cancelled\(\) \}\}$")
        self.assertRegex(job, r"(?m)^      PROMOTE: \$\{\{ needs\.promote\.result \}\}$")
        self.assertNotIn("IMAGE", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("sparse-checkout: .github/scripts", job)
        self.assertIn('python3 .github/scripts/merge_report.py "$SHA"', job)
        self.assertIn('>> "$GITHUB_STEP_SUMMARY"', job)
        self.assertNotIn("secrets.", self.text)

    def test_comments_and_keeps_one_problem_issue(self):
        job = _job(self.text, "report")
        self.assertIn('issues/$NUMBER/comments" -F body=@"$report"', job)
        self.assertIn(f"title={report_mod.PROBLEM_TITLE}$NUMBER", job)
        self.assertEqual(job.count('-f "labels[]=bug" -f "labels[]=P1"'), 2)  # opened, or updated
        self.assertIn("state=closed -f state_reason=completed", job)
        self.assertIn('if [ "$VERDICT" = OK ]; then', job)
        self.assertLess(job.index("state=closed"), job.index("exit 1"))  # an OK never fails the run

    def test_one_report_at_a_time(self):
        job = _job(self.text, "report")
        self.assertRegex(job, r"(?m)^    concurrency:\n      group: promote-report\n      cancel-in-progress: false$")

    def test_no_expression_in_a_shell_line(self):
        runs = re.findall(r"(?m)^        run: (?:\|\n((?:          .*\n|\n)+)|(.*)\n)", self.text)
        self.assertTrue(runs)
        for block in runs:
            self.assertNotIn("${{", "".join(block))

    def test_callers_grant_what_promote_needs(self):
        for name in ("tests.yml", "auto-merge.yml"):
            job = _job((WORKFLOWS / name).read_text(encoding="utf-8"), "promote")
            self.assertEqual(_permissions(job), {"contents": "write", "pull-requests": "write", "issues": "write"}, name)


def _step_script(text, name):
    """The shell of the step called `name`, as GitHub runs it."""
    found = re.search(rf"(?m)^      - name: {re.escape(name)}\n(?:        (?!run:).*\n)*        run: \|\n((?:          .*\n|\n)+)", text)
    assert found, f"no step {name}"
    return re.sub(r"(?m)^          ", "", found.group(1))


STUB_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$GH_LOG"
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class CommentStepTests(unittest.TestCase):
    """The comment step's shell, run with a stand-in for gh that only records what it was asked."""

    def setUp(self):
        text = (WORKFLOWS / "promote.yml").read_text(encoding="utf-8")
        self.script = _step_script(text, "Comment on the pull request, and open, update or close the problem issue")

    def run_step(self, verdict, open_issue=""):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "bin").mkdir()
            gh = folder / "bin" / "gh"
            gh.write_text(STUB_GH, encoding="utf-8")
            gh.chmod(0o755)
            (folder / "merge-report.md").write_text(f"{verdict}: the merge of #42.\n", encoding="utf-8")
            env = {
                "PATH": f"{folder / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                "GH_LOG": str(folder / "gh.log"),
                "GH_REPO": "example/usher",
                "RUNNER_TEMP": str(folder),
                "NUMBER": "42",
                "VERDICT": verdict,
                "OPEN_ISSUE": open_issue,
            }
            done = subprocess.run(["bash", "-e", "-c", self.script], env=env, capture_output=True, text=True, timeout=30)
            log = folder / "gh.log"
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            issue = (folder / "issue.md").read_text(encoding="utf-8") if (folder / "issue.md").exists() else ""
            return done.returncode, calls, done.stdout, issue

    def test_ok_without_a_problem_issue_only_comments(self):
        code, calls, _, _ = self.run_step("OK")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("api repos/example/usher/issues/42/comments", calls[0])

    def test_ok_closes_the_problem_issue(self):
        code, calls, _, _ = self.run_step("OK", "6")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("issues/6/comments", calls[1])
        self.assertIn("-X PATCH repos/example/usher/issues/6 -f state=closed -f state_reason=completed", calls[2])

    def test_problem_opens_the_issue(self):
        code, calls, out, issue = self.run_step("PROBLEM")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("api repos/example/usher/issues -f title=Merge problem: PR #42 -f labels[]=bug -f labels[]=P1", calls[1])
        self.assertNotIn("PATCH", calls[1])
        self.assertIn("::error::The merge of #42 has a problem", out)
        self.assertTrue(issue.startswith("PROBLEM: the merge of #42."))
        self.assertIn("closes by itself when the next merge reports OK", issue)

    def test_problem_updates_the_open_issue(self):
        code, calls, _, _ = self.run_step("PROBLEM", "6")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("-X PATCH repos/example/usher/issues/6 -f title=Merge problem: PR #42 -f labels[]=bug -f labels[]=P1", calls[1])


if __name__ == "__main__":
    unittest.main()
