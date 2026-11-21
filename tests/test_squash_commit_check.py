"""Auto-merge reads the subject and message it is about to give a squash commit against the privacy patterns,
because a pull request's text can be edited after Tests passed. A match leaves the pull request open and names
only the part that matched, never the text."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "identifying_details.py"
WORKFLOW = (ROOT / ".github" / "workflows" / "auto-merge.yml").read_text(encoding="utf-8")
DETAIL = "Example-Hamlet"  # made up: stands in for a detail the patterns name
PATTERN = "example-hamlet"

# A stand-in for gh that answers the calls the merge step makes and logs each one.
FAKE_GH = f"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
    for index, arg in enumerate(args):
        if arg == "-F" and args[index + 1].startswith("body=@"):
            log.write("BODY " + open(args[index + 1][6:], encoding="utf-8").read() + "\\n")
if args[:2] == ["pr", "list"]:
    print("7\\tabc\\tfalse\\tfalse")
elif args[0] == "api":
    url = next(a for a in args if a.startswith("repos/"))
    if "contents/tools/identifying_details.py" in url and os.environ.get("FAKE_FETCH_EMPTY"):
        pass
    elif "contents/tools/identifying_details.py?ref=main" in url:
        print(open({str(SCRIPT)!r}, encoding="utf-8").read())
    elif url.endswith("/pulls/7") and "--jq" in args:
        print("merge1")
    elif url.endswith("/pulls/7"):
        print(open(os.environ["FAKE_PR"], encoding="utf-8").read())
    elif "/commits" in url and "--paginate" in args:
        print("a commit message")
    elif "/git/commits/" in url:
        print("tree1")
"""

# Records what the check script is started with, then runs the real interpreter.
FAKE_PYTHON = """#!/bin/sh
echo "token=${GH_TOKEN:-none} patterns=${PATTERNS:-none}" >> "$SEEN"
exec "$REAL_PYTHON" "$@"
"""


def _script():
    step = WORKFLOW.split("        run: |\n", 1)[1].split("\n  promote:", 1)[0]
    return "\n".join(line[10:] for line in step.splitlines()) + "\n"


def _text_files(folder, subject, message, patterns=PATTERN):
    base = Path(folder)
    for name, text in (("patterns", patterns + "\n"), ("subject", subject), ("message", message)):
        (base / name).write_text(text, encoding="utf-8")
    return base


class SquashFlagTests(unittest.TestCase):
    def check(self, subject, message, patterns=PATTERN):
        with tempfile.TemporaryDirectory() as folder:
            base = _text_files(folder, subject, message, patterns)
            return subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "--squash", str(base / "patterns"), str(base / "subject"), str(base / "message")],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_clean_text_passes(self):
        self.assertEqual(self.check("feat(app): add a thing (#7)", "A change.\n\nFixes #3\n").returncode, 0)

    def test_the_part_that_matched_is_named_without_its_text(self):
        subject = self.check(f"feat(app): {DETAIL} (#7)", "fine\n")
        message = self.check("feat(app): fine (#7)", f"Seen at {DETAIL.upper()}.\n")
        both = self.check(DETAIL, DETAIL)
        self.assertIn("squash commit subject.", subject.stdout)
        self.assertIn("squash commit message.", message.stdout)
        self.assertIn("subject and message", both.stdout)
        for result in (subject, message, both):
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(PATTERN, (result.stdout + result.stderr).lower())

    def test_an_invalid_pattern_stops_it_without_quoting_it(self):
        result = self.check("clean", "clean", patterns="example-(place")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("example-(place", result.stdout + result.stderr)


@unittest.skipUnless(shutil.which("git"), "needs git")
class HeadMessageTests(unittest.TestCase):
    def check(self, message):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", "-q", folder], check=True)
            subprocess.run(
                ["git", "-C", folder, "-c", "user.name=a", "-c", "user.email=a@example.invalid", "commit", "-q", "--allow-empty", "-m", message],
                check=True,
            )
            (Path(folder) / "patterns").write_text(PATTERN + "\n", encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "--head-message", str(Path(folder) / "patterns")],
                cwd=folder,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_a_clean_message_passes_and_a_match_is_reported_without_its_text(self):
        self.assertEqual(self.check("feat(app): add a thing").returncode, 0)
        result = self.check(f"feat(app): {DETAIL}\n\nbody\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("newest commit's message", result.stdout)
        self.assertNotIn(PATTERN, (result.stdout + result.stderr).lower())

    def test_a_repository_without_a_commit_fails_instead_of_passing_unchecked(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", "-q", folder], check=True)
            (Path(folder) / "patterns").write_text(PATTERN + "\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "--head-message", str(Path(folder) / "patterns")],
                cwd=folder,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)


@unittest.skipUnless(shutil.which("jq") and shutil.which("grep"), "needs jq and grep")
class MergeStepTests(unittest.TestCase):
    """The Auto-merge step itself, run as the runner runs it, with a fake gh."""

    def run_step(self, title, body, patterns=PATTERN, author="someone", tested_by=None, fetch_empty=False):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as tools:
            gh = Path(tools) / "gh"
            gh.write_text(FAKE_GH.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1), encoding="utf-8")
            gh.chmod(0o755)
            (Path(tools) / "python3").write_text(FAKE_PYTHON, encoding="utf-8")
            (Path(tools) / "python3").chmod(0o755)
            (Path(folder) / "fake-pr.json").write_text(json.dumps({"title": title, "body": body, "user": {"login": author}}), encoding="utf-8")
            log = Path(folder) / "gh.log"
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _script()],
                cwd=folder,
                env={
                    "PATH": ":".join([tools, str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
                    "GH_LOG": str(log),
                    "FAKE_PR": str(Path(folder) / "fake-pr.json"),
                    "RUNNER_TEMP": folder,
                    "GITHUB_OUTPUT": str(Path(folder) / "output"),
                    "GH_REPO": "example/repo",
                    "SHA": "abc",
                    "BRANCH": "issue-7-thing",
                    "TESTED_BY": tested_by or author,
                    "REAL_PYTHON": sys.executable,
                    "SEEN": str(Path(folder) / "seen"),
                    "FAKE_FETCH_EMPTY": "1" if fetch_empty else "",
                    "PATTERNS": patterns,
                    "GH_TOKEN": "made-up",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            result.log = log.read_text(encoding="utf-8") if log.exists() else ""
            seen = Path(folder) / "seen"
            result.seen = seen.read_text(encoding="utf-8").splitlines() if seen.exists() else []
            return result

    def test_a_clean_pull_request_still_merges(self):
        result = self.run_step("feat(app): add a thing", "## Summary\n\nFine.\n\nFixes #3\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pr merge 7 --squash", result.log)

    def test_a_title_edited_to_match_is_not_merged_and_the_comment_names_only_the_title(self):
        result = self.run_step(f"feat(app): {DETAIL}", "## Summary\n\nFine.\n")
        everything = result.stdout + result.stderr + result.log
        self.assertEqual(result.returncode, 1, everything)
        self.assertNotIn("pr merge", result.log)
        self.assertNotIn("pr ready", result.log)
        self.assertIn("BODY Identifying details found in the squash commit subject.", result.log)
        self.assertNotIn(PATTERN, everything.lower())

    def test_a_description_that_matches_is_named_as_the_message(self):
        result = self.run_step("feat(app): add a thing", f"## Summary\n\nNear {DETAIL}.\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("pr merge", result.log)
        self.assertIn("squash commit message.", result.log)
        self.assertNotIn(PATTERN, (result.stdout + result.stderr + result.log).lower())

    def test_text_outside_the_summary_never_reaches_the_commit_so_it_is_not_checked_here(self):
        # Tests reads the whole description; the merge reads what the squash commit will hold.
        result = self.run_step("feat(app): add a thing", f"## Summary\n\nFine.\n\n## What changed\n\n{DETAIL}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pr merge 7 --squash", result.log)

    def test_without_patterns_nothing_is_merged(self):
        result = self.run_step("feat(app): add a thing", "## Summary\n\nFine.\n", patterns="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("PRIVACY_PATTERNS is not set", result.stdout)
        self.assertNotIn("pr merge", result.log)

    def test_an_invalid_pattern_merges_nothing_and_is_not_quoted(self):
        result = self.run_step("feat(app): add a thing", "## Summary\n\nFine.\n", patterns="example-(place")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("pr merge", result.log)
        self.assertNotIn("example-(place", result.stdout + result.stderr + result.log)

    def test_dependabots_own_run_is_not_checked(self):
        result = self.run_step(f"chore(deps): {DETAIL}", "x\n", author="dependabot[bot]")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pr merge 7 --squash", result.log)

    def test_a_dependabot_pull_request_edited_by_a_person_is_checked(self):
        result = self.run_step(f"chore(deps): {DETAIL}", "x\n", author="dependabot[bot]", tested_by="someone")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("pr merge", result.log)

    def test_the_check_never_sees_the_token_or_the_secret(self):
        result = self.run_step("feat(app): add a thing", "## Summary\n\nFine.\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.seen)
        self.assertEqual(set(result.seen), {"token=none patterns=none"})

    def test_an_empty_download_of_the_check_merges_nothing(self):
        result = self.run_step("feat(app): add a thing", "## Summary\n\nFine.\n", fetch_empty=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("could not be fetched", result.stdout)
        self.assertNotIn("pr merge", result.log)

    def test_patterns_that_are_only_blank_lines_merge_nothing(self):
        result = self.run_step(f"feat(app): {DETAIL}", "## Summary\n\nFine.\n", patterns="\n  \n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PRIVACY_PATTERNS is not set", result.stdout)
        self.assertNotIn("pr merge", result.log)

    def test_a_blocked_pull_request_is_not_labelled(self):
        result = self.run_step(f"feat(app): {DETAIL}", "## Summary\n\nFine.\n\nFixes #3\n")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("/labels", result.log)


class WorkflowTests(unittest.TestCase):
    def test_the_check_runs_before_the_merge_with_the_secret(self):
        self.assertIn("PATTERNS: ${{ secrets.PRIVACY_PATTERNS }}", WORKFLOW)
        self.assertLess(WORKFLOW.index("check --squash"), WORKFLOW.index("gh pr merge"))

    def test_the_secret_is_given_to_the_one_step_not_the_job(self):
        job_env = WORKFLOW.split("    env:\n", 1)[1].split("    outputs:", 1)[0]
        self.assertNotIn("PRIVACY_PATTERNS", job_env)

    def test_the_script_comes_from_main_not_from_the_pull_request(self):
        # The fake gh serves the script only for a request that names ref=main (see MergeStepTests).
        self.assertIn("contents/tools/identifying_details.py?ref=main", WORKFLOW)
        self.assertNotIn("actions/checkout", WORKFLOW)
        self.assertNotIn("$GITHUB_WORKSPACE", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
