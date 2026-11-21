"""The merge → Tests → Promote chain: a merge made by Auto-merge doesn't count as a push, so it starts Tests
on main itself, and Tests calls Promote (a workflow_run trigger wouldn't fire for that run)."""

import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.test_public_conventions import ATTRIBUTION

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _on_block(text):
    return re.search(r"^on:\n((?:[ #].*\n)+)", text, re.M).group(1)


class AutoMergeChainTests(unittest.TestCase):
    def test_tests_can_be_started_by_hand(self):
        self.assertRegex(_on_block(_read("tests.yml")), r"(?m)^  workflow_dispatch:")

    def test_promote_is_called_by_tests_on_main_after_both_checks(self):
        """A run started by Auto-merge's token can't start Promote through workflow_run."""
        promote = _read("promote.yml")
        self.assertNotIn("workflow_run", _on_block(promote))
        self.assertNotIn("github.event.workflow_run", promote)
        self.assertRegex(_on_block(promote), r"(?m)^  workflow_call:")
        job = re.search(r"(?m)^  promote:\n((?:    .*\n|\n)+)", _read("tests.yml"))
        self.assertIsNotNone(job, "Tests has no promote job")
        job = job.group(1)
        self.assertRegex(job, r"(?m)^    uses: \./\.github/workflows/promote\.yml$")
        needs = re.search(r"(?m)^    needs: \[(.*)\]$", job).group(1)
        self.assertEqual({n.strip() for n in needs.split(",")}, {"test"})  # tests and privacy check: one job
        cond = re.search(r"(?m)^    if: (.*)$", job).group(1)
        self.assertIn("github.ref == 'refs/heads/main'", cond)
        self.assertIn("github.event_name != 'pull_request'", cond)
        self.assertRegex(job, r"(?m)^      contents: write$")

    def test_promote_moves_stable_to_the_commit_tests_checked(self):
        promote = _read("promote.yml")
        self.assertIn('git push origin "$SHA:refs/heads/stable"', promote)
        self.assertIn("SHA: ${{ inputs.sha || github.sha }}", promote)
        self.assertIn("ref: ${{ inputs.sha || github.sha }}", promote)
        self.assertIn("group: promote\n", promote)

    def test_a_merge_releases_nothing(self):
        """Releases are made by hand (Release, tests/test_release_workflow.py): nothing a merge starts tags, publishes
        a release or builds an image."""
        for name in ("promote.yml", "tests.yml", "auto-merge.yml"):
            text = _read(name)
            for word in ("gh release", "buildx", "docker login", "uses: ./.github/workflows/release.yml"):
                self.assertNotIn(word, text, f"{name}: {word}")

    def test_auto_merge_closes_the_issues_a_pr_fixes(self):
        """A merge made with the workflow's own token doesn't close `Fixes #n` issues by itself."""
        text = _read("auto-merge.yml")
        self.assertIn("issues: write", text)
        self.assertRegex(text, r"fix\(e\[sd\]\)\?")
        self.assertRegex(text, r"close\[sd\]\?")
        self.assertRegex(text, r"resolve\[sd\]\?")
        self.assertIn("state_reason=completed", text)
        self.assertNotIn("${{ github.event.workflow_run.pull_requests", text)  # never put PR text in a shell line

    def test_auto_merge_waits_for_tests_and_starts_them_on_main(self):
        text = _read("auto-merge.yml")
        self.assertIn("workflows: [Tests]", _on_block(text))
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertIn("gh workflow run tests.yml --ref main", text)

    def test_auto_merge_only_merges_the_tested_commit_from_this_repo(self):
        text = _read("auto-merge.yml")
        self.assertIn('--match-head-commit "$SHA"', text)
        self.assertIn("head_repository.full_name == github.repository", text)
        self.assertIn('index("hold")', text)

    def test_merged_branches_are_deleted(self):
        text = _read("auto-merge.yml")
        self.assertEqual(text.count('git/refs/heads/$BRANCH"'), 2)


def _jobs(text):
    """The names of a workflow's jobs."""
    block = re.search(r"(?m)^jobs:\n((?:[ #].*\n|\n)+)", text).group(1)
    return re.findall(r"(?m)^  ([\w-]+):$", block)


class ActionsMinutesTests(unittest.TestCase):
    """Each job sets up a runner of its own, so CI runs as few jobs as it can."""

    def test_tests_and_privacy_check_are_one_job(self):
        text = _read("tests.yml")
        self.assertEqual(_jobs(text), ["test", "promote"])
        test_job = re.search(r"(?m)^  test:\n((?:    .*\n|\n)+)", text).group(1)
        self.assertIn("coverage run -m unittest discover -s tests -t .", test_job)
        self.assertIn("PRIVACY_PATTERNS", test_job)
        self.assertIn("if: github.actor != 'dependabot[bot]'", test_job)

    def test_pip_installs_from_cache(self):
        text = _read("tests.yml")
        self.assertRegex(text, r"(?m)^          cache: pip$")
        self.assertIn("requirements.txt\n            requirements-dev.txt", text)

    def test_new_push_cancels_pull_request_run_but_never_main(self):
        text = _read("tests.yml")
        block = re.search(r"(?m)^concurrency:\n((?:  .*\n)+)", text)
        self.assertIsNotNone(block, "Tests has no workflow-level concurrency")
        block = block.group(1)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", block)
        # Runs on main get a group each (run_id), so a later one can't cancel or replace a pending promotion.
        self.assertIn("github.event_name == 'pull_request' && github.ref || github.run_id", block)

    def test_dependabot_is_monthly_and_grouped(self):
        text = (WORKFLOWS.parent / "dependabot.yml").read_text(encoding="utf-8")
        ecosystems = re.findall(r"(?m)^  - package-ecosystem: (\S+)$", text)
        self.assertEqual(sorted(ecosystems), ["docker", "github-actions", "pip"])
        self.assertEqual(text.count("interval: monthly"), len(ecosystems))
        self.assertNotIn("interval: weekly", text)
        self.assertNotIn("interval: daily", text)
        self.assertEqual(len(re.findall(r'(?m)^        patterns: \["\*"\]$', text)), len(ecosystems))

    def test_auto_merge_promotes_without_a_second_run_when_it_is_safe(self):
        text = _read("auto-merge.yml")
        self.assertIn("promote_sha=$merged", text)
        # Safe only if the squash commit holds exactly the tree of the head commit Tests passed on.
        self.assertIn('git/commits/$merged" --jq \'.tree.sha\'', text)
        self.assertIn('git/commits/$SHA" --jq \'.tree.sha\'', text)
        self.assertIn('[ "$merged_tree" = "$tested_tree" ]', text)
        self.assertNotIn("parents", text)
        # Dependabot's runs skip the privacy check, so their merges are always tested on main.
        self.assertIn("TESTED_BY: ${{ github.event.workflow_run.actor.login }}", text)
        self.assertIn('[ "$TESTED_BY" != "dependabot[bot]" ]', text)
        job = re.search(r"(?m)^  promote:\n((?:    .*\n|\n)+)", text)
        self.assertIsNotNone(job, "Auto-merge has no promote job")
        job = job.group(1)
        self.assertRegex(job, r"(?m)^    needs: \[merge\]$")
        self.assertIn("if: needs.merge.outputs.promote_sha != ''", job)
        self.assertRegex(job, r"(?m)^    uses: \./\.github/workflows/promote\.yml$")
        self.assertIn("sha: ${{ needs.merge.outputs.promote_sha }}", job)
        self.assertRegex(_on_block(_read("promote.yml")), r"(?m)^      sha:\n")


class UpstreamCheckTests(unittest.TestCase):
    """The weekly check against the latest releases is one job, and only ever opens one issue."""

    def setUp(self):
        self.text = _read("upstream-check.yml")

    def test_runs_weekly_monthly_and_by_hand_in_one_job(self):
        on = _on_block(self.text)
        self.assertEqual(len(re.findall(r"- cron:", on)), 2)
        self.assertRegex(on, r"(?m)^  workflow_dispatch:")
        self.assertEqual(_jobs(self.text), ["upstream"])

    def test_issue_is_opened_updated_and_closed_by_label(self):
        self.assertIn("labels=$label&state=open", self.text)
        self.assertIn("state=closed -f state_reason=completed", self.text)
        self.assertRegex(_read("labels.yml"), r"gh label create upstream .*--force")
        self.assertRegex(_read("labels.yml"), r"gh label create upstream-beta .*--force")

    def test_betas_only_comment(self):
        beta = self.text[self.text.index('if [ "$CHANNEL" = beta ]; then\n            # Betas only') :]
        beta = beta[: beta.index("elif")]
        self.assertNotIn("state=closed", beta)
        self.assertNotIn("PATCH", beta)

    def test_new_releases_get_their_own_issues_on_the_latest_images_only(self):
        step = self.text[self.text.index("- name: Open or close the release issues") :]
        step = step[: step.index("- name:", 1)]
        self.assertIn("env.CHANNEL != 'beta'", step)
        self.assertIn("always()", step)  # also when the contract check fails, so the issue links the failure
        self.assertIn('python3 -m tests.upstream_releases "$RUNNER_TEMP/out/results.json"', step)
        self.assertLess(self.text.index("- name: Open, update or close the issue"), self.text.index("- name: Open or close the release issues"))
        self.assertRegex(_read("labels.yml"), r"gh label create upstream-release .*--force")

    def test_image_overrides_are_checked_and_never_in_a_shell_line(self):
        self.assertNotIn("${{ inputs.", re.sub(r"(?m)^      \w+: \$\{\{ inputs\.\w+ \}\}$", "", self.text))
        self.assertIn("Not an image name", self.text)

    def test_seeded_jellyfin_key_reaches_the_later_steps(self):
        # A step's environment file is read when that step ends, so only the seeding step's own file carries the key on.
        seed = self.text[self.text.index("- name: Seed minimal settings") :]
        seed = seed[: seed.index("- name:", 1)]
        self.assertIn('UPSTREAM_ENV_FILE="$GITHUB_ENV" python3 -m tests.upstream_seed', seed)
        self.assertNotRegex(self.text[: self.text.index("- name: Seed minimal settings")], r"UPSTREAM_ENV_FILE=")

    def test_runs_only_the_contract_checks(self):
        self.assertIn("python3 -m tests.upstream_seed", self.text)
        self.assertIn("python3 -m tests.upstream_check", self.text)
        self.assertNotIn("secrets.", self.text)


class IncompatibleVersionFormTests(unittest.TestCase):
    def test_form_asks_for_app_versions_and_status_details(self):
        text = (WORKFLOWS.parent / "ISSUE_TEMPLATE" / "incompatible.yml").read_text(encoding="utf-8")
        self.assertIn("name: Doesn't work with my version of", text)
        self.assertIn("labels: [bug]", text)
        for field in ("app_version", "usher_version", "details"):
            self.assertIn(f"id: {field}", text)
        self.assertIn("Status", text)


class SquashMergeTests(unittest.TestCase):
    """Each pull request lands on main as one commit: its title, Summary and Fixes lines, nothing collected from its commits."""

    def setUp(self):
        self.text = _read("auto-merge.yml")

    def test_merges_as_one_squash_commit(self):
        merges = re.findall(r"gh pr merge .*", self.text)
        self.assertEqual(len(merges), 1)
        self.assertIn("--squash", merges[0])
        self.assertNotRegex(self.text, r"--merge\b|--rebase\b")

    def test_message_is_the_title_and_a_short_body(self):
        merge = re.search(r"gh pr merge .*", self.text).group(0)
        self.assertIn('--subject "$title (#$number)"', merge)
        self.assertIn('--body-file "$RUNNER_TEMP/message"', merge)
        self.assertIn('keep = ($0 == "## Summary")', self.text)  # the description's Summary, not all of it

    def test_no_author_address_is_passed_or_printed(self):
        # GitHub rejects an author address that isn't the token's own account; the author is the pull request's.
        self.assertNotIn("--author-email", self.text)
        self.assertNotIn("COMMIT_AUTHOR_EMAIL", self.text)
        self.assertNotIn("AUTHOR_EMAIL", self.text)
        self.assertNotRegex(self.text, r"[\w.+-]+@[\w-]+\.\w+")  # never an address written in the repo

    def _message(self, description, fixed):
        """Runs the workflow's own message build on this description and these fixed issues."""
        start = self.text.index("summary=$(")
        end = self.text.index('> "$RUNNER_TEMP/message"') + len('> "$RUNNER_TEMP/message"')
        script = textwrap.dedent("          " + self.text[start:end])
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "description").write_text(description, encoding="utf-8")
            subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                env={"PATH": "/usr/bin:/bin", "RUNNER_TEMP": folder, "fixed": fixed},
                check=True,
            )
            return (Path(folder) / "message").read_text(encoding="utf-8")

    def test_message_is_the_summary_then_the_fixes_lines(self):
        description = "## Summary\n\n<!-- the template's note -->\nA change.\n\n## What changed\n\nDetails.\n\nFixes #7\nFixes #9\n"
        self.assertEqual(self._message(description, "7 9"), "A change.\n\nFixes #7\nFixes #9\n")

    def test_collects_no_trailers_from_the_commits(self):
        self.assertNotRegex(self.text, ATTRIBUTION)
        self.assertNotIn("co_authors", self.text)
        build = self.text[self.text.index("summary=$(") : self.text.index("gh pr merge")]
        self.assertIn('echo "Fixes #$issue"', build)
        self.assertIn('> "$RUNNER_TEMP/message"', build)
        self.assertNotIn("RUNNER_TEMP/commits", build)

    def test_tests_check_the_text_again_when_it_is_edited(self):
        text = _read("tests.yml")
        self.assertRegex(_on_block(text), r"types: \[[^\]]*\bedited\b")  # a fixed text is checked again
        self.assertNotRegex(text, r"run: \|[^\n]*\n[^\n]*\$\{\{ github\.event\.pull_request\.body")

    def test_pull_request_text_check_reads_the_current_description(self):
        """The event carries the description as it was when the pull request opened, so the check asks GitHub
        for the text now."""
        text = _read("tests.yml")
        step = text.split("- name: Check the pull request text", 1)[1].split("- name:", 1)[0]
        self.assertNotIn("github.event.pull_request.body", step)
        self.assertNotIn("PR_BODY", step)
        self.assertIn('gh api "repos/$GITHUB_REPOSITORY/' + 'pulls/$NUMBER" --jq .body', step)
        self.assertIn("NUMBER: ${{ github.event.pull_request.number }}", step)
        self.assertIn("pull-requests: read", text)
        self.assertLess(text.index("coverage run"), text.index("- name: Check the pull request text"))

    def test_no_pull_request_text_in_a_shell_line(self):
        self.assertNotIn("${{ github.event.pull_request.title", self.text)
        self.assertNotIn("${{ github.event.pull_request.body", self.text)


class ReleaseNotesTests(unittest.TestCase):
    """The release's notes are its section of CHANGELOG.md (tests/test_release_notes.py has the builder's cases)."""

    def test_pull_request_takes_its_issues_type_labels(self):
        text = _read("auto-merge.yml")
        self.assertIn("grep -xE 'bug|enhancement|feature'", text)
        self.assertIn('issues/$number/labels" -f "labels[]=$label"', text)
        # Labelled before merging, so the merged pull request can be filtered by type at once.
        self.assertLess(text.index('issues/$number/labels"'), text.index("gh pr merge"))

    def test_release_doesnt_read_pull_request_titles(self):
        release = _read("release.yml")
        self.assertIn('--title "Usher $VERSION"', release)
        self.assertNotIn(".title", release)


def _dependabot_commit(*updates):
    """A Dependabot commit message in its own format, with one metadata entry per (name, update type)."""
    entries = "".join(
        f"- dependency-name: {name}\n"
        "  dependency-version: 2.0.0\n"
        "  dependency-type: direct:production\n"
        f"  update-type: version-update:semver-{kind}\n"
        "  dependency-group: actions\n"
        for name, kind in updates
    )
    subject = "chore(deps): bump the actions group with updates"
    return f"{subject}\n\nUpdates `example/action` from 1.0.0 to 2.0.0\n\n---\nupdated-dependencies:\n{entries}...\n"


class DependabotMajorHoldTests(unittest.TestCase):
    """Patch and minor Dependabot updates merge by themselves; a major one waits for a person."""

    def setUp(self):
        self.text = _read("auto-merge.yml")

    def _is_held(self, message):
        """Runs the workflow's own check on a pull request whose commits have this message."""
        pattern = re.search(r"grep -qE '([^']+)' \"\$RUNNER_TEMP/commits\"", self.text)
        self.assertIsNotNone(pattern, "Auto-merge doesn't check the commits for a major update")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt") as commits:
            commits.write(message)
            commits.flush()
            return subprocess.run(["grep", "-qE", pattern.group(1), commits.name], check=False).returncode == 0

    def test_a_major_update_is_held(self):
        self.assertTrue(self._is_held(_dependabot_commit(("example/action", "major"))))

    def test_one_major_update_in_a_group_holds_the_group(self):
        self.assertTrue(self._is_held(_dependabot_commit(("example/one", "minor"), ("example/two", "major"))))

    def test_patch_and_minor_updates_merge(self):
        self.assertFalse(self._is_held(_dependabot_commit(("example/one", "minor"), ("example/two", "patch"))))
        self.assertFalse(self._is_held("fix(ci): mention version-update:semver-major in prose\n"))

    def test_reads_every_commit_of_a_dependabot_pull_request(self):
        self.assertIn("""[ "$(jq -r '.user.login' "$RUNNER_TEMP/pr.json")" = "dependabot[bot]" ]""", self.text)
        self.assertLess(self.text.index('"$RUNNER_TEMP/commits"'), self.text.index('"dependabot[bot]" ]'))
        # Saved to a file first: grep -q stopping early would fail the pipe under pipefail and hide a match.
        self.assertIn(
            """gh api --paginate "repos/$GH_REPO/pulls/$number/commits" --jq '.[].commit.message' """
            """> "$RUNNER_TEMP/commits\"""",
            self.text,
        )

    def test_held_with_the_label_and_a_comment_before_any_merge(self):
        hold = self.text.index('issues/$number/labels" -f "labels[]=hold"')
        comment = self.text.index('issues/$number/comments" -F "body=@$RUNNER_TEMP/major"')
        self.assertLess(self.text.index('"$RUNNER_TEMP/commits"'), hold)
        self.assertLess(hold, self.text.index("gh pr merge"))
        self.assertLess(comment, self.text.index("gh pr merge"))
        self.assertLess(hold, self.text.index('issues/$number/labels" -f "labels[]=$label"'))
        self.assertIn("major version update", self.text)

    def test_hold_label_is_kept(self):
        self.assertRegex(_read("labels.yml"), r"gh label create hold .*--force")

    def test_rule_is_written_next_to_the_dependabot_notes(self):
        notes = (WORKFLOWS.parent / "dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("major", notes)
        self.assertIn("hold", notes)


class PriorityLabelTests(unittest.TestCase):
    """The Labels workflow creates the priority labels the issue rules use, so a new repository has them."""

    def test_labels_workflow_creates_the_priority_labels(self):
        text = _read("labels.yml")
        for label in ("P1", "P2", "P3"):
            self.assertRegex(text, rf"gh label create {label} .*--description \"Priority \d: .*--force")


class NeedsServerLabelTests(unittest.TestCase):
    """A pull request that needs a step on the server is labelled needs-server by itself."""

    SERVER_FILES = ("Dockerfile", "deploy/compose.yaml", "deploy/usher-update", "deploy/usher-autodeploy.*")

    def test_label_is_created(self):
        self.assertRegex(_read("labels.yml"), r"gh label create needs-server .*--force")

    def test_workflow_labels_prs_touching_server_files(self):
        text = _read("needs-server.yml")
        on = _on_block(text)
        self.assertIn("pull_request:", on)
        for path in self.SERVER_FILES:
            self.assertIn(f"- {path}", on)
        self.assertIn("pull-requests: write", text)
        self.assertIn("/labels", text)
        self.assertIn("needs-server", text)
        self.assertNotIn("${{ github.event.pull_request.title", text)  # never put PR text in a shell line

    def test_the_label_stays_and_the_release_notes_come_from_the_changelog(self):
        self.assertIn("needs-server", _read("labels.yml"))
        self.assertFalse((WORKFLOWS.parent / "release.yml").exists())  # the label grouping of generated notes
        self.assertNotIn("--generate-notes", _read("release.yml"))


if __name__ == "__main__":
    unittest.main()
