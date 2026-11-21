"""The Release workflow is run by hand with a version: it gives the changes collected under `## [Unreleased]` that
version, pushes that commit to main, tags it, publishes the release with its notes and an announcement, and publishes
the image. Nothing a merge starts does any of this (tests/test_workflows.py). The workflow is read, not run."""

import re
import unittest
from pathlib import Path

from tests.test_merge_report import _job, _permissions, _step_script

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def _step(job, name):
    found = re.search(rf"(?m)^      - name: {re.escape(name)}\n((?:        .*\n|\n)+)", job)
    assert found, f"no step {name}"
    return found.group(1)


class TriggerTests(unittest.TestCase):
    def test_it_runs_only_by_hand_and_asks_for_a_version(self):
        on = re.search(r"(?m)^on:\n((?:[ #].*\n)+)", TEXT).group(1)
        self.assertEqual(re.findall(r"(?m)^  ([\w-]+):", on), ["workflow_dispatch"])
        self.assertRegex(on, r"(?m)^      version:\n(?:        .*\n)*?        required: true\n")
        self.assertRegex(on, r"(?m)^        type: string$")

    def test_the_jobs_run_in_order_and_each_asks_for_its_own_permissions(self):
        self.assertRegex(TEXT, r"(?m)^permissions: \{\}$")
        self.assertEqual(re.findall(r"(?m)^  ([\w-]+):$", TEXT.split("\njobs:\n", 1)[1]), ["prepare", "publish", "image"])
        self.assertEqual(_permissions(_job(TEXT, "prepare")), {"contents": "write", "actions": "write"})
        self.assertEqual(_permissions(_job(TEXT, "publish")), {"contents": "write", "discussions": "write"})
        self.assertEqual(_permissions(_job(TEXT, "image")), {"contents": "read", "packages": "write", "id-token": "write", "attestations": "write"})
        self.assertRegex(_job(TEXT, "publish"), r"(?m)^    needs: \[prepare\]$")

    def test_one_release_at_a_time(self):
        self.assertRegex(_job(TEXT, "prepare"), r"(?m)^    concurrency:\n      group: release\n      cancel-in-progress: false$")

    def test_no_expression_reaches_a_shell_line(self):
        """The version, a hand-typed input, goes through an environment variable only."""
        for match in re.finditer(r"(?m)^        run: (?:\|\n((?:          .*\n|\n)+)|(.*)\n)", TEXT):
            self.assertNotIn("${{", "".join(match.groups("")))
        self.assertIn("VERSION: ${{ inputs.version }}", _job(TEXT, "prepare"))


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.job = _job(TEXT, "prepare")

    def test_it_runs_only_on_main(self):
        step = _step(self.job, "Check that this runs on main")
        self.assertIn("REF: ${{ github.ref }}", step)
        self.assertIn('[ "$REF" = refs/heads/main ]', step)
        self.assertLess(self.job.index("Check that this runs on main"), self.job.index("actions/checkout"))

    def test_it_releases_only_the_commit_stable_already_holds(self):
        step = _step(self.job, "Check that main is the tested commit")
        self.assertIn('[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/stable)" ]', step)
        self.assertIn("fetch-depth: 0", self.job)  # the tags and stable
        self.assertLess(self.job.index("Check that main is the tested commit"), self.job.index("prepare_release.py"))

    def test_the_version_is_prepared_checked_committed_and_pushed_in_that_order(self):
        order = ["prepare_release.py", "unittest tests.test_versions tests.test_release_notes", "git commit", "git push origin HEAD:refs/heads/main"]
        positions = [self.job.index(word) for word in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('python3 .github/scripts/prepare_release.py "$VERSION"', self.job)

    def test_it_commits_only_the_files_the_script_edits_with_no_force(self):
        step = _step(self.job, "Commit and push to main")
        self.assertIn("git add CHANGELOG.md usher/__init__.py web/sw.js web/updater.js", step)
        self.assertIn('git commit -m "chore(release): version $VERSION"', step)
        self.assertNotRegex(step, r"--force|-f\b|--no-verify|git add (-A|\.)")
        self.assertIn('echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"', step)
        self.assertIn('echo "version=$VERSION" >> "$GITHUB_OUTPUT"', step)

    def test_tests_are_started_on_the_release_commit_because_a_token_push_starts_none(self):
        step = _step(self.job, "Start Tests on the release commit")
        self.assertIn("gh workflow run tests.yml --ref main", step)
        self.assertLess(self.job.index("git push origin HEAD:refs/heads/main"), self.job.index("Start Tests on the release commit"))
        self.assertIn("workflow_dispatch", (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))

    def test_the_token_is_only_in_the_step_that_starts_tests(self):
        self.assertEqual(self.job.count("github.token"), 1)
        self.assertIn("GH_TOKEN: ${{ github.token }}", _step(self.job, "Start Tests on the release commit"))
        self.assertNotIn("secrets.", TEXT)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.job = _job(TEXT, "publish")

    def test_the_release_is_tagged_at_the_pushed_commit_with_the_changelog_notes_and_an_announcement(self):
        create = _step(self.job, "Publish the release")
        self.assertIn('gh release create "v$VERSION" --target "$SHA" --title "Usher $VERSION" --notes-file "$RUNNER_TEMP/release-notes.md"', create)
        self.assertIn("--discussion-category Announcements", create)
        self.assertNotIn("--generate-notes", TEXT)
        self.assertIn("SHA: ${{ needs.prepare.outputs.sha }}", self.job)
        self.assertIn("ref: ${{ needs.prepare.outputs.sha }}", self.job)

    def test_a_version_that_already_has_its_release_is_not_published_twice(self):
        self.assertIn('gh release view "v$VERSION"', _step(self.job, "Publish the release"))

    def test_the_commit_is_checked_out_without_credentials(self):
        self.assertIn("persist-credentials: false", self.job)

    def test_the_script_that_builds_the_notes_is_the_one_the_app_uses(self):
        self.assertIn("release_notes.py", _step(self.job, "Build the release notes from CHANGELOG.md"))


class ImageTests(unittest.TestCase):
    def test_the_image_is_built_from_the_pushed_commit_after_the_release(self):
        job = _job(TEXT, "image")
        self.assertRegex(job, r"(?m)^    needs: \[prepare, publish\]$")
        self.assertIn("needs.publish.result == 'success'", job)
        self.assertIn("ref: ${{ needs.prepare.outputs.sha }}", job)
        self.assertIn("SHA: ${{ needs.prepare.outputs.sha }}", job)

    def test_the_step_that_names_the_image_reads_the_version_the_release_commit_set(self):
        self.assertIn("__version__", _step_script(TEXT, "Name the image"))


if __name__ == "__main__":
    unittest.main()
