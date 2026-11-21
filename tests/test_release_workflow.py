"""The Release workflow is run by hand with a version: it gives the changes collected under `## [Unreleased]` that
version, pushes that commit to main as the release GitHub App (the protect-main ruleset has no bypass for GitHub Actions
itself), waits for Tests to pass on it, tags it, publishes the release with its notes and an announcement, and publishes
the image. Nothing a merge starts does any of this (tests/test_workflows.py). The workflow's text is checked, and the
shell of its guards and its push is run with stand-ins for gh and sleep and a made-up repository: no GitHub is called."""

import base64
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
        self.assertEqual(_permissions(_job(TEXT, "prepare")), {"contents": "read"})  # the push is made with the app's token
        self.assertEqual(_permissions(_job(TEXT, "tested")), {"contents": "read"})
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
        self.assertNotIn("github.token", _job(TEXT, "prepare"))  # it pushes with the app's token
        self.assertEqual(_job(TEXT, "tested").count("github.token"), 1)
        self.assertEqual(_job(TEXT, "publish").count("github.token"), 1)
        self.assertEqual(_job(TEXT, "image").count("github.token"), 1)  # the registry sign-in


class AppKeyTests(unittest.TestCase):
    """The release app's key is the one secret: one environment, one job, one step, and nothing the repository's own
    code runs sees it."""

    def setUp(self):
        self.job = _job(TEXT, "prepare")

    def test_the_key_is_read_by_one_step_of_the_prepare_job_only(self):
        self.assertEqual(re.findall(r"secrets\.[A-Z_]+", TEXT), ["secrets.RELEASE_APP_KEY"])
        self.assertIn("APP_KEY: ${{ secrets.RELEASE_APP_KEY }}", _step(self.job, "Get a short-lived token for the release app"))
        self.assertNotIn("secrets.", TEXT.split("  tested:", 1)[1])

    def test_the_key_lives_in_the_release_environment_that_main_alone_can_use(self):
        """The environment's branch rule (a repository setting, CONTRIBUTING.md) keeps a branch's copy of this file from
        reading the key; the first step refuses any ref but main too."""
        self.assertRegex(self.job, r"(?m)^    environment: release( .*)?$")
        self.assertEqual(TEXT.count("environment:"), 1)
        self.assertIn('[ "$REF" = refs/heads/main ]', _step_script(TEXT, "Check that this runs on main"))

    def test_nothing_from_the_repository_runs_after_the_key_is_read(self):
        """The version script, the tests and the guards run first; afterwards only the token script and git run."""
        key_step = self.job.index("Get a short-lived token")
        for earlier in ("prepare_release.py", "unittest", "Check that main is the tested commit", "actions/checkout"):
            self.assertLess(self.job.index(earlier), key_step, earlier)
        after = self.job[key_step:]
        self.assertNotIn("python3 -m", after)
        self.assertNotIn("prepare_release.py", after)
        self.assertEqual(re.findall(r"python3 (\S+)", after), [".github/scripts/app_token.py"])

    def test_the_checkout_keeps_no_credentials_and_the_push_uses_the_apps_token(self):
        checkout = self.job.split("- name: Check that main", 1)[0]
        self.assertIn("persist-credentials: false", checkout)
        push = _step(self.job, "Commit and push to main")
        self.assertIn("TOKEN: ${{ steps.token.outputs.token }}", push)
        self.assertLess(push.index("::add-mask::$header"), push.index("git push origin"))
        self.assertNotIn("${{", _step_script(TEXT, "Commit and push to main"))

    def test_the_token_is_revoked_when_the_job_ends_also_after_a_failed_push(self):
        step = _step(self.job, "Revoke the token")
        self.assertIn("gh api --method DELETE /installation/token", step)
        self.assertIn("GH_TOKEN: ${{ steps.token.outputs.token }}", step)
        self.assertIn("always()", step)
        self.assertIn("continue-on-error: true", step)  # the token expires by itself; a failure here must not fail the release
        self.assertEqual(self.job.rindex("      - name: "), self.job.index("      - name: Revoke the token"))  # the last step

    def test_the_token_script_gets_only_what_it_needs(self):
        step = _step(self.job, "Get a short-lived token for the release app")
        self.assertEqual(re.findall(r"(?m)^          ([A-Z_]+):", step), ["APP_ID", "APP_KEY", "GH_REPO"])
        self.assertIn("APP_ID: ${{ vars.RELEASE_APP_ID }}", step)
        self.assertIn("run: python3 .github/scripts/app_token.py", step)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.job = _job(TEXT, "prepare")

    def test_the_guards_come_before_anything_is_changed(self):
        self.assertLess(self.job.index("Check that this runs on main"), self.job.index("actions/checkout"))
        self.assertIn("fetch-depth: 0", self.job)  # the tags and stable
        self.assertLess(self.job.index("Check that main is the tested commit"), self.job.index("prepare_release.py"))

    def test_the_version_is_prepared_checked_committed_and_pushed_in_that_order(self):
        order = [
            "prepare_release.py",
            "unittest tests.test_versions tests.test_release_notes",
            "app_token.py",
            "git commit",
            "push origin HEAD:refs/heads/main",
        ]
        positions = [self.job.index(word) for word in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('python3 .github/scripts/prepare_release.py "$VERSION"', self.job)

    def test_it_commits_only_the_files_the_script_edits_with_no_force(self):
        step = _step(self.job, "Commit and push to main")
        self.assertIn("git add CHANGELOG.md usher/__init__.py web/sw.js web/updater.js", step)
        self.assertIn('git commit -m "chore(release): version $VERSION"', step)
        self.assertNotRegex(step, r"--force|\s-f\s|--no-verify|git add (-A|\.)")
        self.assertIn('echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"', step)
        self.assertIn('echo "version=$VERSION" >> "$GITHUB_OUTPUT"', step)

    def test_it_does_not_start_tests_itself(self):
        """A failure of that call would fail the job that carries the commit's outputs; the tested job does it."""
        self.assertNotIn("gh workflow run", self.job)


class TestedTests(unittest.TestCase):
    """Nothing is tagged, published or built until Tests have passed on the release commit."""

    def setUp(self):
        self.job = _job(TEXT, "tested")

    def test_the_apps_push_starts_tests_by_itself(self):
        """A push made with an app's token starts the workflows that listen for it (one made with the workflow's own
        token starts none), so nothing dispatches Tests here: that would run them twice."""
        tests = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertRegex(tests, r"(?m)^  push:\n    branches: \[main\]$")
        self.assertNotIn("gh workflow run", TEXT)
        self.assertNotIn("actions: write", TEXT)
        self.assertIn("Wait until stable holds the release commit", self.job)

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


@unittest.skipUnless(shutil.which("bash") and shutil.which("git") and shutil.which("base64"), "needs bash, git and base64")
class PushStepTests(unittest.TestCase):
    """The push step in a made-up repository whose remote is a local folder: the release commit lands on main, a main
    that moved on refuses it, and the token never appears in what the step prints."""

    TOKEN = "ghs_madeUpTokenForTheTest0123456789"

    def git(self, folder, *args):
        who = {"GIT_AUTHOR_NAME": "Example", "GIT_AUTHOR_EMAIL": "example@example.invalid", "GIT_COMMITTER_NAME": "Example"}
        who |= {"GIT_COMMITTER_EMAIL": "example@example.invalid", "PATH": os.defpath, "HOME": str(folder), "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, text=True, env=who).stdout

    def setup_repo(self, folder):
        remote, work = Path(folder) / "remote.git", Path(folder) / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
        for name in ("CHANGELOG.md", "usher/__init__.py", "web/sw.js", "web/updater.js"):
            (work / name).parent.mkdir(exist_ok=True)
            (work / name).write_text("first\n", encoding="utf-8")
        self.git(work, "add", ".")
        self.git(work, "commit", "-q", "-m", "first")
        self.git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
        for name in ("CHANGELOG.md", "usher/__init__.py", "web/sw.js", "web/updater.js"):
            (work / name).write_text("released\n", encoding="utf-8")
        return remote, work

    def push(self, folder, work):
        script = _step_script(TEXT, "Commit and push to main")
        output = Path(folder) / "output"
        # A git that writes down how it is called, and what is in its environment, before running the real one.
        log = Path(folder) / "git-calls"
        real = shutil.which("git")
        path = _stubs(
            folder,
            git=f'printf \'%s|%s|%s|%s\\n\' "$*" "$GIT_CONFIG_COUNT" "$GIT_CONFIG_KEY_0" "$GIT_CONFIG_VALUE_0" >> "{log}"\nexec "{real}" "$@"\n',
        )
        env = {
            "VERSION": "9.9.9",
            "TOKEN": self.TOKEN,
            "GITHUB_OUTPUT": str(output),
            "PATH": f"{path}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "HOME": str(folder),
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        return _bash(script, folder, env, cwd=work), output

    def test_the_release_commit_lands_on_main_and_its_sha_is_the_output(self):
        with tempfile.TemporaryDirectory() as folder:
            remote, work = self.setup_repo(folder)
            done, output = self.push(folder, work)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            sha = self.git(work, "rev-parse", "HEAD").strip()
            self.assertEqual(self.git(remote, "rev-parse", "main").strip(), sha)
            self.assertEqual(self.git(remote, "log", "-1", "--format=%s", "main").strip(), "chore(release): version 9.9.9")
            self.assertEqual(output.read_text(encoding="utf-8"), f"sha={sha}\nversion=9.9.9\n")

    def test_a_main_that_moved_on_refuses_it_and_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as folder:
            remote, work = self.setup_repo(folder)
            other = Path(folder) / "other"
            subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True)
            self.git(other, "commit", "-q", "--allow-empty", "-m", "merged meanwhile")
            self.git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
            done, output = self.push(folder, work)
            self.assertEqual(done.returncode, 1)
            self.assertIn("::error::The push to main did not go through", done.stdout)
            self.assertIn("run Release again", done.stdout)
            self.assertFalse(output.exists())  # nothing downstream gets a commit that isn't on main

    def test_git_pushes_with_the_token_as_basic_auth_passed_in_its_environment(self):
        """Git is given the credential the way GitHub's own checkout does (the extraheader of github.com), through
        environment variables so it is on no command line, and the exact header is masked before it is used."""
        header = base64.b64encode(f"x-access-token:{self.TOKEN}".encode()).decode()
        with tempfile.TemporaryDirectory() as folder:
            _, work = self.setup_repo(folder)
            done, _ = self.push(folder, work)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertNotIn(self.TOKEN, done.stdout + done.stderr)
            self.assertNotIn(header, done.stdout.replace(f"::add-mask::{header}", "") + done.stderr)
            self.assertIn(f"::add-mask::{header}", done.stdout)
            calls = [line.split("|") for line in (Path(folder) / "git-calls").read_text(encoding="utf-8").splitlines()]
            pushes = [call for call in calls if call[0].startswith("push ")]
            self.assertEqual(pushes, [["push origin HEAD:refs/heads/main", "1", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {header}"]])
            for call in calls:
                self.assertNotIn(header, call[0])  # never in what git is asked on its command line
                self.assertNotIn(self.TOKEN, call[0])
            before = calls[: calls.index(pushes[0])]
            self.assertEqual([call[1] for call in before], [""] * len(before))  # the commit and the rest run without it


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
