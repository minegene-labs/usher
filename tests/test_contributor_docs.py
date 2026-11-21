"""Contributor docs: CONTRIBUTING.md holds how an issue is fixed, reviewed and looked after until it merges, and how
issues are filed; the guides name no coding tool and the repository keeps no session process files of its own;
docs/architecture.md maps the repo."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTERFACE = ROOT / "docs" / "interface.md"
POINTER = ROOT / "CLAUDE.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
TEMPLATE = ROOT / ".github" / "pull_request_template.md"
# What a contributor reads for how work is done.
GUIDES = [
    ROOT / "README.md",
    CONTRIBUTING,
    ROOT / "SECURITY.md",
    TEMPLATE,
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml")),
]
# The only files kept in the coding tool's settings folder: the settings that enforce the push, GitHub API and
# privacy rules, and their hook. Process files (skills, agents) live in each contributor's own setup.
SESSION_SETTINGS = {".claude/settings.json", ".claude/hooks/session_guard.py"}


def read(path):
    return path.read_text(encoding="utf-8")


def section(text, heading):
    m = re.search(rf"^##+ {heading}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m and m.group(1)


class ToolNeutralTests(unittest.TestCase):
    def test_guides_name_no_coding_tool(self):
        for path in GUIDES:
            self.assertNotRegex(read(path).lower(), r"claude", path.relative_to(ROOT))

    def test_guides_point_to_no_session_skill_or_agent(self):
        stale = re.compile(r"/fix-issue|`(fix-issue|ui-work|steward)`|`(reviewer|planner|implementer|collector)` agent|reviewer agent")
        for path in [*GUIDES, POINTER]:
            match = stale.search(read(path))
            self.assertIsNone(match, f"{path.relative_to(ROOT)}: {match and match.group(0)!r}")

    def test_session_settings_folder_holds_only_the_enforcing_files(self):
        listed = subprocess.run(["git", "ls-files", "--cached", ".claude"], cwd=ROOT, capture_output=True, text=True, check=False)
        if listed.returncode == 0:
            files = set(listed.stdout.split())
        else:  # not a git checkout: what is on disk counts
            files = {p.relative_to(ROOT).as_posix() for p in (ROOT / ".claude").rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(files, SESSION_SETTINGS)


class IssueWorkTests(unittest.TestCase):
    """How an issue is read and fixed, without a session's own files."""

    def setUp(self):
        self.text = read(CONTRIBUTING)

    def test_reads_issues_with_rest_not_graphql(self):
        self.assertIn("gh api repos/{owner}/{repo}/issues/<n>`", self.text)
        self.assertIn("gh api repos/{owner}/{repo}/issues/<n>/comments`", self.text)
        for cmd in re.findall(r"`([^`]*)`", self.text):
            self.assertNotRegex(cmd, r"^gh (issue|pr) ", cmd)  # GraphQL, which sessions can't reach

    def test_small_related_issues_may_share_a_pull_request(self):
        for phrase in ("`issue-<n>-<m>-<slug>`", "a `Fixes #<n>` line and a test for each issue"):
            self.assertIn(phrase, self.text)

    def test_a_pull_request_leaves_the_version_alone(self):
        self.assertIn("Leave the version alone.", self.text)
        self.assertNotIn("version bump", self.text)
        self.assertNotIn("Bump the version", self.text)

    def test_asks_before_changing_an_unclear_issue(self):
        self.assertIn("unclear or contradicts the code, ask", self.text)

    def test_pushes_the_branch_and_opens_its_own_draft_pull_request(self):
        self.assertIn("opens its own draft pull request", self.text)


class PullRequestCareTests(unittest.TestCase):
    """A session looks after its own pull request until it merges."""

    def setUp(self):
        self.text = section(read(CONTRIBUTING), "Looking after a pull request")
        self.assertIsNotNone(self.text, "CONTRIBUTING.md has no Looking after a pull request section")

    def test_leaves_merging_to_auto_merge(self):
        for phrase in ("Auto-merge", "`hold`", "Never merge or approve"):
            self.assertIn(phrase, self.text)

    def test_fixes_red_tests_without_weakening_them(self):
        self.assertIn("root cause", self.text)
        self.assertIn("Never skip, delete or loosen a test", self.text)

    def test_rebases_when_behind_main_and_answers_every_comment(self):
        self.assertIn("rebase onto `main`", self.text)
        self.assertIn("Answer every review comment", self.text)
        self.assertIn("Edit the description", self.text)


class ReviewTests(unittest.TestCase):
    """When a change is reviewed before its pull request opens, and what the review looks for."""

    def setUp(self):
        self.text = section(read(CONTRIBUTING), "Review")
        self.assertIsNotNone(self.text, "CONTRIBUTING.md has no Review section")

    def test_says_when_a_review_is_needed(self):
        for phrase in ("server", "sign-in", "caches", "workflows", "`deploy/`", "300 lines", "`Reviewer: ran`"):
            self.assertIn(phrase, self.text)

    def test_only_reports(self):
        self.assertIn("only reports", self.text)
        self.assertIn("same branch", self.text)

    def test_checks_what_needs_judgement(self):
        for phrase in (
            "docs/interface.md",
            "iOS app",
            "Reduce Motion",
            "Reduce Transparency",
            "Increase Contrast",
            "web/sw.js",
            "web/data.js",
            "/healthz",
            "assert behaviour",
            "no rule describes",
        ):
            self.assertIn(phrase, self.text)

    def test_leaves_what_tests_check_to_tests(self):
        checklist = self.text[self.text.index("what needs judgement") :]
        for phrase in ("500 lines", "Fixes #<n>", "test count"):
            self.assertNotIn(phrase, checklist)


class InterfaceRulesTests(unittest.TestCase):
    """Interface rules live in docs/interface.md; the other guides only point to it."""

    def test_holds_the_rules(self):
        text = read(INTERFACE)
        for phrase in (
            "Human Interface Guidelines",
            "Lightweight comes first",
            "Reduce Motion",
            "Reduce Transparency",
            "Increase Contrast",
            "Dynamic Type",
            "env(safe-area-inset-*)",
            "100dvh",
            "iPhone SE",
            "No manual steps or switches",
            "usher.toml",
        ):
            self.assertIn(phrase, text)

    def test_other_guides_only_point_to_it(self):
        for path in (POINTER, CONTRIBUTING):
            text = read(path)
            self.assertIn("docs/interface.md", text, path.name)
            self.assertNotIn("Human Interface Guidelines", text, path.name)


class SessionRulesTests(unittest.TestCase):
    """Sessions stop after repeated failures, treat issue text as data and reproduce a bug first."""

    def test_pointer_file_names_the_three_rules(self):
        text = read(POINTER)
        for part in ("Stop-loss", "Untrusted text", "Reproduce first"):
            self.assertIn(part, text)

    def test_contributing_holds_the_detail(self):
        text = read(CONTRIBUTING)
        for part in ("two identical failures", "data to check, not instructions", "fails without the fix"):
            self.assertIn(part, text)


class PointerFileTests(unittest.TestCase):
    def test_is_short_and_points_to_the_guides(self):
        text = read(POINTER)
        self.assertLessEqual(len(text.splitlines()), 12)
        for target in ("CONTRIBUTING.md", "docs/architecture.md", "docs/interface.md", "docs/glossary.md"):
            self.assertIn(target, text)
            self.assertTrue((ROOT / target).exists(), target)


class FilingIssuesRuleTests(unittest.TestCase):
    """Every issue filed carries a type and a priority label, and a milestone when it has a theme."""

    def test_rule_is_short_and_complete(self):
        found = section(read(CONTRIBUTING), "Filing issues")
        self.assertIsNotNone(found, "CONTRIBUTING.md has no Filing issues section")
        found = found.strip()
        self.assertLessEqual(len(found.splitlines()), 10)
        for phrase in (
            "type label",
            "bug, enhancement, feature, documentation, question",
            "P1",
            "P2",
            "P3",
            "milestone",
            "a theme",
            "deliberately postponed",
            "same step as filing",
        ):
            self.assertIn(phrase, found)
        for stale in ("`Status redesign`", "`1.0.0`", "`Public install`", "after-public"):
            self.assertNotIn(stale, found)


class RepoMapTests(unittest.TestCase):
    def setUp(self):
        self.text = read(ARCHITECTURE)

    def test_site_settings_point_to_usher_toml(self):
        text = read(CONTRIBUTING)
        self.assertNotIn("site.env.example", text)
        self.assertIn("usher.toml", text)
        self.assertTrue((ROOT / "deploy" / "usher.example.toml").exists())

    def _repo_map(self):
        m = re.search(r"^## Repository map\n(.*?)(?=^## |\Z)", self.text, re.M | re.S)
        self.assertIsNotNone(m, "docs/architecture.md has no Repository map section")
        return m.group(1)

    def test_repo_map_names_every_module_and_test_file(self):
        repo_map = self._repo_map()
        for path in sorted((ROOT / "usher").rglob("*.py")) + sorted((ROOT / "tests").glob("test_*.py")):
            if path.name != "__init__.py":
                self.assertIn(path.stem, repo_map, f"Repo map doesn't mention {path.relative_to(ROOT)}")
        for name in ("web/app.js", "web/sw.js", "tests/mock_apps.py"):
            self.assertIn(name, repo_map)

    def test_repo_map_names_only_files_that_exist(self):
        for path in re.findall(r"`((?:usher|web|tests|deploy|\.github)/[^`*]+)`", self._repo_map()):
            self.assertTrue((ROOT / path).exists(), f"Repo map names {path}, which doesn't exist")


class ContributingTests(unittest.TestCase):
    def test_holds_the_rules_a_contributor_needs(self):
        text = read(CONTRIBUTING)
        for phrase in ("ruff check", "unittest discover", "Conventional Commits", "issue-<n>-<slug>", "/tmp", "Fixes #<n>"):
            self.assertIn(phrase, text)

    def test_how_to_test_names_the_command_not_its_output(self):
        # Tests fails a pull request text that states a test count or result.
        for path in (CONTRIBUTING, TEMPLATE):
            self.assertIn("names the command to run, not what it printed", read(path), path.name)

    def test_a_branch_may_take_follow_up_commits(self):
        # The session writes its own description and can edit it, and the pull request is squash-merged, so a
        # branch isn't held to one commit pushed once.
        for path in (CONTRIBUTING, POINTER, ARCHITECTURE):
            text = read(path)
            for phrase in ("final before the first push", "carries one commit", "pushed once", "more than one commit"):
                self.assertNotIn(phrase, text, f"{path.name}: {phrase}")
        self.assertIn("follow-up commits", read(CONTRIBUTING))

    def test_server_is_mentioned_only_when_a_deployed_file_changes(self):
        # A "nothing to run on the server" line is a note to the person running the server, not pull request text.
        text = read(TEMPLATE)
        rule = "mention the server only if the change touches one of these files, otherwise say nothing about it"
        self.assertIn(rule, text.lower())
        self.assertIn("`deploy/usher-autodeploy.*`", text)


if __name__ == "__main__":
    unittest.main()
