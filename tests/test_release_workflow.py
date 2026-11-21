"""The Release workflow is run by hand with a version: it gives the changes collected under `## [Unreleased]` that
version, pushes that commit to main, waits for Tests to pass on it, tags it, publishes the release with its notes and an
announcement, and publishes the image. Nothing a merge starts does any of this (tests/test_workflows.py). The
workflow's text is checked, and the shell of its guards is run with stand-ins for gh and sleep: no GitHub is called."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_merge_report import _job, _permissions, _step_script

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
WAIT_ROUNDS = 70


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
        self.assertEqual(re.findall(r"(?m)^  ([\w-]+):$", TEXT.split("\njobs:\n", 1)[1]), ["prepare", "tested", "publish", "image"])
        self.assertEqual(_permissions(_job(TEXT, "prepare")), {"contents": "write"})
        self.assertEqual(_permissions(_job(TEXT, "tested")), {"contents": "read", "actions": "write"})
        self.assertEqual(_permissions(_job(TEXT, "publish")), {"contents": "write", "discussions": "write"})
        self.assertEqual(_permissions(_job(TEXT, "image")), {"contents": "read", "packages": "write", "id-token": "write", "attestations": "write"})
        self.assertRegex(_job(TEXT, "tested"), r"(?m)^    needs: \[prepare\]$")
        self.assertRegex(_job(TEXT, "publish"), r"(?m)^    needs: \[prepare, tested\]$")

    def test_one_release_at_a_time(self):
        self.assertRegex(_job(TEXT, "prepare"), r"(?m)^    concurrency:\n      group: release\n      cancel-in-progress: false$")

    def test_no_expression_reaches_a_shell_line(self):
        """The version, a hand-typed input, goes through an environment variable only."""
        for match in re.finditer(r"(?m)^        run: (?:\|\n((?:          .*\n|\n)+)|(.*)\n)", TEXT):
            self.assertNotIn("${{", "".join(match.groups("")))
        self.assertIn("VERSION: ${{ inputs.version }}", _job(TEXT, "prepare"))

    def test_the_token_is_in_the_jobs_that_need_it_only(self):
        self.assertNotIn("github.token", _job(TEXT, "prepare"))  # it pushes with the checkout's own credentials
        self.assertEqual(_job(TEXT, "tested").count("github.token"), 1)
        self.assertEqual(_job(TEXT, "publish").count("github.token"), 1)
        self.assertEqual(_job(TEXT, "image").count("github.token"), 1)  # the registry sign-in
        self.assertNotIn("secrets.", TEXT)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.job = _job(TEXT, "prepare")

    def test_the_guards_come_before_anything_is_changed(self):
        self.assertLess(self.job.index("Check that this runs on main"), self.job.index("actions/checkout"))
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

    def test_it_does_not_start_tests_itself(self):
        """A failure of that call would fail the job that carries the commit's outputs; the tested job does it."""
        self.assertNotIn("gh workflow run", self.job)


class TestedTests(unittest.TestCase):
    """Nothing is tagged, published or built until Tests have passed on the release commit."""

    def setUp(self):
        self.job = _job(TEXT, "tested")

    def test_tests_are_started_because_a_token_push_starts_none(self):
        self.assertIn("gh workflow run tests.yml --ref main", _step(self.job, "Start Tests on the release commit"))
        self.assertIn("workflow_dispatch", (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
        self.assertLess(self.job.index("Start Tests"), self.job.index("Wait until stable"))

    def test_publish_and_image_come_after_it(self):
        self.assertLess(TEXT.index("  tested:"), TEXT.index("  publish:"))
        self.assertLess(TEXT.index("  publish:"), TEXT.index("  image:"))
        self.assertRegex(_job(TEXT, "image"), r"(?m)^    needs: \[prepare, publish\]$")


def _stubs(folder, **scripts):
    """A PATH whose first folder holds stand-ins for commands."""
    (Path(folder) / "bin").mkdir(exist_ok=True)
    for name, body in scripts.items():
        stub = Path(folder) / "bin" / name
        stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        stub.chmod(0o755)
    return f"{Path(folder) / 'bin'}{os.pathsep}{os.defpath}"


def _bash(script, folder, extra=None, cwd=None):
    base = {"PATH": os.defpath, "HOME": str(folder), "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["bash", "-e", "-c", script], env={**base, **(extra or {})}, capture_output=True, text=True, timeout=60, cwd=cwd)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class RefGuardTests(unittest.TestCase):
    def test_only_main_may_run_it(self):
        script = _step_script(TEXT, "Check that this runs on main")
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(_bash(script, folder, {"REF": "refs/heads/main"}).returncode, 0)
            for ref in ("refs/heads/some-branch", "refs/heads/main2", "refs/tags/main", ""):
                done = _bash(script, folder, {"REF": ref})
                self.assertEqual(done.returncode, 1, ref)
                self.assertIn("::error::Run Release on the main branch.", done.stdout)


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "needs bash and git")
class StableGuardTests(unittest.TestCase):
    """The commit a release starts from is the one `stable` holds, in a made-up repository."""

    def git(self, folder, *args):
        who = {"GIT_AUTHOR_NAME": "Example", "GIT_AUTHOR_EMAIL": "example@example.invalid"}
        who |= {"GIT_COMMITTER_NAME": "Example", "GIT_COMMITTER_EMAIL": "example@example.invalid", "PATH": os.defpath}
        subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, env=who)

    def clone(self, folder):
        remote, work = Path(folder) / "remote.git", Path(folder) / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
        self.git(work, "commit", "-q", "--allow-empty", "-m", "first")
        self.git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.git(work, "push", "-q", "origin", "HEAD:refs/heads/stable")
        self.git(work, "fetch", "-q", "origin")
        return work

    def test_it_starts_only_from_the_commit_stable_holds(self):
        script = _step_script(TEXT, "Check that main is the tested commit")
        with tempfile.TemporaryDirectory() as folder:
            work = self.clone(folder)
            self.assertEqual(_bash(script, folder, cwd=work).returncode, 0)
            self.git(work, "commit", "-q", "--allow-empty", "-m", "merged, not yet tested")
            self.git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
            self.git(work, "fetch", "-q", "origin")
            done = _bash(script, folder, cwd=work)
            self.assertEqual(done.returncode, 1)
            self.assertIn("main is not at stable yet", done.stdout)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class WaitStepTests(unittest.TestCase):
    """The tested job's shell, with a gh that answers a made-up compare status and a sleep that doesn't wait."""

    def run_steps(self, statuses):
        wait = _step_script(TEXT, "Wait until stable holds the release commit")
        with tempfile.TemporaryDirectory() as folder:
            asked = Path(folder, "asked")
            asked.write_text("", encoding="utf-8")
            Path(folder, "answers").write_text("\n".join(statuses) + "\n", encoding="utf-8")
            gh = (
                f'echo "$*" >> "{asked}"\n'
                f'n=$(wc -l < "{asked}")\n'
                f'total=$(wc -l < "{folder}/answers")\n'
                '[ "$n" -le "$total" ] || n=$total\n'
                f'sed -n "${{n}}p" "{folder}/answers"\n'
            )
            path = _stubs(folder, gh=gh, sleep="exit 0\n")
            done = _bash(wait, folder, {"PATH": path, "GH_REPO": "example/usher", "SHA": "0" * 40})
            return done, asked.read_text(encoding="utf-8").splitlines()

    def test_it_goes_on_once_stable_is_at_or_past_the_release_commit(self):
        for status in ("identical", "ahead"):
            done, asked = self.run_steps([status])
            self.assertEqual(done.returncode, 0, status)
            self.assertEqual(asked, [f"api repos/example/usher/compare/{'0' * 40}...stable --jq .status"])

    def test_it_keeps_asking_while_tests_run(self):
        done, asked = self.run_steps(["behind", "behind", "ahead"])
        self.assertEqual((done.returncode, len(asked)), (0, 3))

    def test_it_stops_when_stable_never_gets_there(self):
        """A red Tests run leaves stable behind: the job fails, so nothing is tagged, published or built."""
        for statuses in (["behind"], ["diverged"], [""]):
            done, asked = self.run_steps(statuses)
            self.assertEqual(done.returncode, 1, statuses)
            self.assertIn("::error::stable has not reached the release commit", done.stdout)
            self.assertEqual(len(asked), WAIT_ROUNDS)

    def test_the_wait_fits_inside_the_jobs_time_limit(self):
        job = _job(TEXT, "tested")
        minutes = int(re.search(r"timeout-minutes: (\d+)", job).group(1))
        sleep = int(re.search(r"\n\s+sleep (\d+)\n", job).group(1))
        self.assertIn(f"seq 1 {WAIT_ROUNDS}", job)
        self.assertLess(WAIT_ROUNDS * sleep, minutes * 60)


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
