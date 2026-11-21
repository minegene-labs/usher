"""Tests checks the lines a pull request text must have for what it changes: Fixes #<n>, whether a screen looks
different, `Reviewer: ran` for a sensitive or large change, a test (or a reason) for a change to the app, and a
changelog line (or a reason) for a change a person using Usher could notice."""

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "pr_gates.py"
TESTS = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
STEP = TESTS.split("- name: Check the lines the pull request text must have", 1)[1].split("- name:", 1)[0]
PLAIN = "## Summary\n\nA fix.\n\nChanges how a screen looks: no\n\nFixes #7\n"
GOOD = PLAIN + "Changelog: not needed — nothing a person would notice\n"
REVIEWED = GOOD + "Reviewer: ran\n"
PLAIN_REVIEWED = PLAIN + "Reviewer: ran\n"  # for sensitive paths, but with no changelog reason

spec = importlib.util.spec_from_file_location("pr_gates", SCRIPT)
gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gates)


def check(changed, text, changelog_added=None):
    return gates.problems(changed, text, changelog_added)


class PullRequestTextTests(unittest.TestCase):
    def test_a_complete_text_passes(self):
        self.assertEqual(check({"README.md": 4}, GOOD), [])

    def test_needs_a_fixes_line(self):
        found = check({"README.md": 4}, GOOD.replace("Fixes #7", "Fixes #"))
        self.assertEqual(len(found), 1)
        self.assertIn("`Fixes #<n>`", found[0])
        self.assertEqual(check({"README.md": 4}, GOOD.replace("Fixes #7", "fixes #12")), [])

    def test_needs_a_screen_line_saying_yes_or_no(self):
        for line in ("Changes how a screen looks: yes", "Changes how a screen looks: No"):
            self.assertEqual(check({"README.md": 4}, GOOD.replace("Changes how a screen looks: no", line)), [], line)
        for line in ("", "Changes how a screen looks:", "Changes how a screen looks: maybe", "Screens: no"):
            found = check({"README.md": 4}, GOOD.replace("Changes how a screen looks: no", line))
            self.assertEqual(len(found), 1, line)
            self.assertIn("phone", found[0])

    def test_the_templates_own_instructions_do_not_count(self):
        text = "<!--\nChanges how a screen looks: no\nReviewer: ran\nChangelog: not needed — x\nFixes #7\n-->\n"
        self.assertEqual(len(check({"web/sw.js": 3, "tests/test_x.py": 3}, text)), 4)


class ReviewerLineTests(unittest.TestCase):
    def test_a_sensitive_path_needs_reviewer_ran(self):
        for path in (
            "web/sw.js",
            "web/data.js",
            "web/util.js",
            "usher/auth.py",
            "usher/auth_routes.py",
            "usher/store.py",
            ".github/workflows/tests.yml",
            "Dockerfile",
            "deploy/compose.yaml",
            "deploy/usher-update",
            "compose.yaml",
            "docker-compose.override.yml",
        ):
            changed = {path: 2, "tests/test_x.py": 2}
            found = check(changed, GOOD)
            self.assertEqual(len(found), 1, path)
            self.assertIn("`Reviewer: ran`", found[0])
            self.assertIn(path, found[0])
            self.assertEqual(check(changed, REVIEWED), [], path)

    def test_server_gate_and_security_settings_need_reviewer_ran(self):
        for path in ("usher/server.py", "usher/cli.py", "web/account.js", ".github/scripts/pr_gates.py", ".gitleaks.toml", "requirements.txt"):
            self.assertEqual(len(check({path: 2, "tests/test_x.py": 2}, GOOD)), 1, path)

    def test_other_paths_do_not(self):
        for path in ("web/home.js", "web/app.css", "usher/server_helpers.py", ".github/pull_request_template.md", "docs/compose.md"):
            self.assertEqual(check({path: 2, "tests/test_x.py": 2}, GOOD), [], path)

    def test_a_large_change_needs_reviewer_ran(self):
        self.assertEqual(check({"README.md": 300}, GOOD), [])
        found = check({"README.md": 200, "docs/features.md": 101}, GOOD)
        self.assertEqual(len(found), 1)
        self.assertIn("it changes 301 lines", found[0])
        self.assertEqual(check({"README.md": 200, "docs/features.md": 101}, REVIEWED), [])

    def test_a_printed_path_cannot_start_a_workflow_command(self):
        found = check({"deploy/x\n::error::y": 1}, GOOD)
        self.assertEqual(len(found), 1)
        self.assertNotIn("\n", found[0])


class TestsLineTests(unittest.TestCase):
    def test_an_app_change_needs_a_test_or_a_reason(self):
        found = check({"web/home.js": 5}, GOOD)
        self.assertEqual(len(found), 1)
        self.assertIn("web/home.js", found[0])
        self.assertIn("`Tests: not needed — <reason>`", found[0])
        self.assertEqual(check({"web/home.js": 5, "tests/test_new_row.py": 5}, GOOD), [])
        for line in ("Tests: not needed — only a comment changes", "Tests: not needed - only a comment changes"):
            self.assertEqual(check({"web/home.js": 5}, GOOD + line + "\n"), [], line)

    def test_a_reason_must_be_given(self):
        for line in ("Tests: not needed", "Tests: not needed —", "Tests: needed — later"):
            self.assertEqual(len(check({"usher/tidy.py": 5}, GOOD + line + "\n")), 1, line)

    def test_names_how_many_more_app_files_there_are(self):
        found = check({"web/home.js": 5, "web/app.css": 1, "usher/tidy.py": 2}, GOOD)
        self.assertIn("and 2 more app file(s)", found[0])

    def test_docs_and_ci_changes_need_no_test(self):
        self.assertEqual(check({"README.md": 5, "docs/features.md": 5, ".github/pull_request_template.md": 3}, GOOD), [])


class ChangelogTests(unittest.TestCase):
    """CHANGELOG.md lists only what a person using or installing Usher would notice."""

    def test_a_tests_only_change_that_edits_the_changelog_fails(self):
        found = check({"tests/test_x.py": 5, "CHANGELOG.md": 3}, PLAIN)
        self.assertEqual(len(found), 1)
        self.assertIn("tests/test_x.py", found[0])
        self.assertIn("CHANGELOG.md stays unchanged", found[0])
        self.assertEqual(check({"tests/test_x.py": 5}, PLAIN), [])

    def test_a_change_to_how_the_project_is_run_or_its_documentation_leaves_the_changelog_alone(self):
        for paths in (
            {".github/workflows/tests.yml": 2},
            {".github/scripts/pr_gates.py": 2},
            {".claude/settings.json": 2},
            {"docs/architecture.md": 2},
            {"README.md": 2},
            {"CONTRIBUTING.md": 2, "tests/test_x.py": 2},
            {"tools/identifying_details.py": 2, ".githooks/pre-push": 2},
        ):
            self.assertEqual(len(check({**paths, "CHANGELOG.md": 3}, PLAIN_REVIEWED)), 1, paths)  # some of these paths are sensitive

    def test_a_documentation_only_change_that_leaves_the_changelog_alone_passes(self):
        self.assertEqual(check({"README.md": 5, "docs/features.md": 5, "CONTRIBUTING.md": 2}, PLAIN), [])

    def test_a_change_to_the_app_needs_a_line_or_a_reason(self):
        for path in ("usher/tidy.py", "web/home.js", "deploy/usher.example.toml", "deploy/README.md", "Dockerfile"):
            changed = {path: 5, "tests/test_x.py": 5}
            found = check(changed, PLAIN_REVIEWED, changelog_added=False)  # deploy/ and Dockerfile need the reviewer too
            self.assertEqual(len(found), 1, path)
            self.assertIn(path, found[0])
            self.assertIn("CHANGELOG.md", found[0])
            self.assertIn("`Changelog: not needed — <reason>`", found[0])

    def test_a_line_in_the_changelog_passes(self):
        for path in ("usher/tidy.py", "web/home.js", "deploy/usher.example.toml", "Dockerfile"):
            changed = {path: 5, "tests/test_x.py": 5, "CHANGELOG.md": 3}
            self.assertEqual(check(changed, REVIEWED), [], path)  # REVIEWED: deploy/ and Dockerfile are sensitive

    def test_a_reason_passes_and_must_be_given(self):
        changed = {"usher/tidy.py": 5, "tests/test_x.py": 5}
        for line in ("Changelog: not needed — an internal refactor", "Changelog: not needed - an internal refactor"):
            self.assertEqual(check(changed, PLAIN + line + "\n"), [], line)
        for line in ("Changelog: not needed", "Changelog: not needed —", "Changelog: needed — later"):
            self.assertEqual(len(check(changed, PLAIN + line + "\n")), 1, line)

    def test_a_deletion_alone_is_not_a_line_added(self):
        changed = {"web/home.js": 5, "tests/test_x.py": 5, "CHANGELOG.md": 2}
        self.assertEqual(len(check(changed, PLAIN, changelog_added=False)), 1)
        self.assertEqual(check(changed, PLAIN, changelog_added=True), [])

    def test_the_changelog_alone_or_with_other_files_is_not_forbidden(self):
        self.assertEqual(check({"CHANGELOG.md": 3}, PLAIN), [])
        self.assertEqual(check({"requirements.txt": 1, "CHANGELOG.md": 3}, PLAIN_REVIEWED), [])

    def test_the_message_names_the_rule_and_the_files_never_the_text(self):
        found = check({"tests/test_x.py": 5, "CHANGELOG.md": 3}, PLAIN + "secret words\n")
        self.assertNotIn("secret words", found[0])
        self.assertIn("CONTRIBUTING.md, Changelog", found[0])
        injected = check({"web/x\n::error::y": 5, "tests/test_x.py": 5}, PLAIN, changelog_added=False)[0]
        self.assertNotIn("\n", injected)
        self.assertNotIn("::", injected)  # neither the newline nor the colons survive, so it cannot start a workflow command
        self.assertIn("web/x???error??y", injected)


class WrittenDownTests(unittest.TestCase):
    """The template and the guides tell a session which lines to write."""

    def test_a_filled_in_template_passes(self):
        template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertEqual(len(check({"README.md": 4}, template)), 2)  # the issue number and yes or no are left to fill
        filled = template.replace("Changes how a screen looks:", "Changes how a screen looks: no").replace("Fixes #", "Fixes #7")
        self.assertEqual(check({"README.md": 4}, filled), [])

    def test_template_and_contributing_name_every_line(self):
        for path in (".github/pull_request_template.md", "CONTRIBUTING.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            for line in ("Changes how a screen looks:", "Reviewer: ran", "Tests: not needed — <reason>", "Changelog: not needed — <reason>", "Fixes #"):
                self.assertIn(line, text, path)

    def test_the_missing_reviewer_line_points_to_how_a_review_is_done(self):
        found = check({"deploy/usher-update": 4}, GOOD)
        self.assertEqual(len(found), 1)
        self.assertIn("CONTRIBUTING.md, Review", found[0])
        self.assertIn("## Review\n", (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"))


class DeviceStorageListTests(unittest.TestCase):
    def test_lists_every_script_that_keeps_data_on_the_device(self):
        uses = re.compile(r"\bcaches\.|\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b")
        found = {f"web/{p.name}" for p in (ROOT / "web").glob("*.js") if uses.search(p.read_text(encoding="utf-8"))}
        self.assertEqual(found, set(gates.ON_DEVICE), "keep ON_DEVICE in .github/scripts/pr_gates.py in step")

    def test_sign_in_modules_exist(self):
        for path in gates.SIGN_IN:
            self.assertTrue((ROOT / path).is_file(), path)


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "example@example.invalid",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "example@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@unittest.skipUnless(shutil.which("git"), "needs git")
class StepTests(unittest.TestCase):
    """Runs the step's own shell in a real git repository, with a fake `gh` holding the pull request text."""

    @classmethod
    def setUpClass(cls):
        cls.script = textwrap.dedent(STEP.split("run: |\n", 1)[1]).strip() + "\n"

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        self.repo = self.folder / "repo"
        for name, text in {
            "web/sw.js": 'const VERSION = "usher-__USHER_VERSION__";\nconst SHELL = ["/", "/app.js"];\n',
            "web/updater.js": 'export const BUILD = "__USHER_VERSION__";\n',
            "usher/__init__.py": '__version__ = "1.0.0"\n',
            "README.md": "Example\n",
        }.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text, encoding="utf-8")
        (self.repo / ".github" / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPT, self.repo / ".github" / "scripts" / "pr_gates.py")  # in the base: not part of the diff
        self.git("init", "-q", "-b", "main")
        self.base = self.commit()

    def git(self, *args):
        env = {**os.environ, **GIT_ENV}
        return subprocess.run(["git", *args], cwd=self.repo, env=env, capture_output=True, text=True, check=True).stdout

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD").strip()

    def edit(self, name, old, new):
        path = self.repo / name
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def run_step(self, text):
        head = self.commit()
        fake = self.folder / "bin" / "gh"
        fake.parent.mkdir(exist_ok=True)
        fake.write_text('#!/bin/sh\ncat "$FAKE_BODY"\n', encoding="utf-8")
        fake.chmod(0o755)
        (self.folder / "body.md").write_text(text, encoding="utf-8")
        env = {
            **GIT_ENV,
            "PATH": f"{fake.parent}:{os.environ['PATH']}",
            "FAKE_BODY": str(self.folder / "body.md"),
            "GITHUB_REPOSITORY": "example/usher",
            "NUMBER": "7",
            "BASE": self.base,
            "HEAD": head,
            "RUNNER_TEMP": str(self.folder),
            "GH_TOKEN": "made-up",
        }
        cmd = ["bash", "-e", "-o", "pipefail", "-c", self.script]
        return subprocess.run(cmd, cwd=self.repo, env=env, capture_output=True, text=True, check=False)

    def test_a_service_worker_change_without_reviewer_ran_fails(self):
        self.edit("web/sw.js", '"/app.js"]', '"/app.js", "/home.js"]')
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text("# a test\n", encoding="utf-8")
        result = self.run_step(GOOD)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::No `Reviewer: ran` line, and it touches web/sw.js:", result.stdout)
        self.assertNotIn("updater.js", result.stdout)  # that one is unchanged

    def test_the_same_change_with_reviewer_ran_passes_without_touching_the_version(self):
        self.edit("web/sw.js", '"/app.js"]', '"/app.js", "/home.js"]')
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text("# a test\n", encoding="utf-8")
        result = self.run_step(REVIEWED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_docs_change_needs_neither_line(self):
        self.edit("README.md", "Example", "Example, explained")
        result = self.run_step(GOOD)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("has the lines this change needs", result.stdout)

    def test_a_rename_counts_both_of_its_paths(self):
        (self.repo / "deploy").mkdir()
        (self.repo / "deploy" / "notes.md").write_text("".join(f"line {n}\n" for n in range(20)), encoding="utf-8")
        self.base = self.commit()
        self.git("mv", "deploy/notes.md", "docs-notes.md")
        result = self.run_step(GOOD)
        self.assertEqual(result.returncode, 1)
        self.assertIn("it touches deploy/notes.md", result.stdout)

    def test_a_tests_only_change_that_edits_the_changelog_fails(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text("# a test\n", encoding="utf-8")
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n- A line.\n", encoding="utf-8")
        result = self.run_step(PLAIN)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::It changes only files that describe how the project is run", result.stdout)

    def test_a_tests_only_change_that_leaves_the_changelog_alone_passes(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text("# a test\n", encoding="utf-8")
        result = self.run_step(PLAIN)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_app_change_needs_a_changelog_line_that_is_really_added(self):
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n- Old line.\n", encoding="utf-8")
        self.base = self.commit()
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text("# a test\n", encoding="utf-8")
        self.edit("web/sw.js", '"/app.js"]', '"/app.js", "/home.js"]')
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")  # only a deletion
        result = self.run_step(PLAIN_REVIEWED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("adds no line to CHANGELOG.md", result.stdout)
        (self.repo / "CHANGELOG.md").write_text("# Changelog\n- A new line.\n", encoding="utf-8")
        result = self.run_step(PLAIN_REVIEWED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_missing_fixes_line_fails(self):
        self.edit("README.md", "Example", "Example, explained")
        result = self.run_step(GOOD.replace("Fixes #7\n", ""))
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::No `Fixes #<n>` line", result.stdout)

    def test_the_token_does_not_reach_the_script(self):
        self.assertIn("env -u GH_TOKEN python3 .github/scripts/pr_gates.py", self.script)


class WorkflowTests(unittest.TestCase):
    def test_runs_on_pull_requests_except_dependabots(self):
        self.assertIn("if: github.event_name == 'pull_request' && github.event.pull_request.user.login != 'dependabot[bot]'", STEP)
        # The test count check still runs for every pull request, Dependabot's included.
        text_step = TESTS.split("- name: Check the pull request text", 1)[1].split("- name:", 1)[0]
        self.assertIn("if: github.event_name == 'pull_request'\n", text_step)

    def test_reads_the_text_now_and_compares_the_pull_requests_own_commits(self):
        self.assertIn("gh api \"repos/$GITHUB_REPOSITORY/pulls/$NUMBER\" --jq .body", STEP)
        self.assertIn("BASE: ${{ github.event.pull_request.base.sha }}", STEP)
        self.assertIn("HEAD: ${{ github.event.pull_request.head.sha }}", STEP)
        self.assertNotIn("${{", STEP.split("run: |", 1)[1])  # event data reaches the shell through env only
        self.assertIn("fetch-depth: 0", TESTS)


if __name__ == "__main__":
    unittest.main()
