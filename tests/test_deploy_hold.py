"""deploy/usher-update, run against a throwaway git repo with fake docker, curl and logger (no network).

On a shallow checkout the timer must hold any change to Dockerfile or deploy/compose.yaml since
what is running. Under systemd each line goes to the journal once. The install folder comes
from USHER_HOME in /etc/default/usher. Docker Compose's --wait is the health check: a container that isn't
healthy in time is rolled back, and /healthz isn't polled. An unreachable GitHub behaves as before."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "usher-update"
NEEDED = ("bash", "git", "flock", "cmp", "install")

VERSION = 'v=$(sed -n \'s/^__version__ = "\\(.*\\)"$/\\1/p\' "$FAKE_HOME/src/usher/__init__.py")\n'
FAKES = {
    # Every call is recorded; logger lines go to a file so tests can count them.
    # `up --wait` is the health check: it fails for FAKE_BAD_VERSION, which never comes up, and for the
    # rollback's own `up --force-recreate --wait` when FAKE_ROLLBACK_FAILS is set
    "docker": '#!/bin/sh\necho "docker $*" >> "$FAKE_LOG"\n'
    'case "$*" in "compose version --short") echo "${FAKE_COMPOSE-2.29.1}"; exit 0;; esac\n'
    'case "$*" in *--wait*) ;; *) exit 0;; esac\n' + VERSION + '[ "$v" = "${FAKE_BAD_VERSION:-}" ] && exit 1\n'
    'case "$*" in *--force-recreate*) [ -n "${FAKE_ROLLBACK_FAILS:-}" ] && exit 1;; esac\nexit 0\n',
    "logger": '#!/bin/sh\necho "logger $*" >> "$FAKE_LOG"\n',
    # a report to Usher is recorded with its body and answered, so tests can read what the script said;
    # /healthz answers with the checked-out version, or not at all with FAKE_HEALTHZ_DOWN
    "curl": '#!/bin/sh\ncase "$*" in *update-result*) echo "report $*" >> "$FAKE_LOG"; exit "${FAKE_REPORT_EXIT:-0}";; esac\n'
    'echo "curl $*" >> "$FAKE_LOG"\n' + VERSION + '[ -n "${FAKE_HEALTHZ_DOWN:-}" ] && exit 7\n'
    'printf \'{"version": "%s"}\' "$v"\n',
    "sleep": '#!/bin/sh\necho "sleep $*" >> "$FAKE_LOG"\n',  # recorded, without the waiting
}


@unittest.skipUnless(all(shutil.which(c) for c in NEEDED), "needs bash, git and util-linux")
class DeployHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin, self.dir, self.state = self.tmp / "origin", self.tmp / "opt", self.tmp / "state"
        self.bin, self.log = self.tmp / "bin", self.tmp / "calls.log"
        self.bin.mkdir()
        for name, body in FAKES.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.git(self.tmp, "init", "-q", "-b", "stable", str(self.origin))
        self.commit("A", {"Dockerfile": "FROM python\n", "deploy/compose.yaml": "services: {}\n", "usher/__init__.py": '__version__ = "1.0.0"\n'})
        self.dir.mkdir()
        self.git(self.tmp, "clone", "-q", "--depth", "1", "-b", "stable", self.origin.as_uri(), str(self.dir / "src"))
        shutil.copy(self.dir / "src" / "deploy" / "compose.yaml", self.dir / "compose.yaml")
        self.running = self.head()

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, msg, files):
        for name, text in files.items():
            path = self.origin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-qm", msg)
        return self.git(self.origin, "rev-parse", "HEAD")

    def head(self):
        return self.git(self.dir / "src", "rev-parse", "HEAD")

    def run_update(self, *args, journal=True, home=True, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("JOURNAL_STREAM", "INVOCATION_ID", "USHER_HOME")}
        env.update(
            PATH=f"{self.bin}:{env['PATH']}",
            USHER_STATE=str(self.state),
            FAKE_LOG=str(self.log),
            FAKE_HOME=str(self.dir),
            USHER_DEPLOY_CONF=str(self.tmp / "default-usher"),
            **extra,
        )
        if home:
            env["USHER_HOME"] = str(self.dir)
        if journal:
            env["JOURNAL_STREAM"] = "8:12345"
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assertHeld(self, run):
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Usher update held", run.stderr)
        self.assertEqual(self.head(), self.running, "a held change was deployed")
        self.assertFalse([c for c in self.calls() if c.startswith("docker")])

    # Holding changes to the image files ------------------------------------------------------------
    def test_compose_change_on_a_shallow_checkout_is_held(self):
        self.commit("B", {"deploy/compose.yaml": "services: {usher: {}}\n"})
        self.assertHeld(self.run_update("--if-changed"))

    def test_two_commits_at_once_where_only_the_first_changes_compose_are_held(self):
        self.commit("B", {"deploy/compose.yaml": "services: {usher: {}}\n"})
        self.commit("C", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.assertHeld(self.run_update("--if-changed"))

    def test_installed_compose_that_differs_from_the_repo_is_held(self):
        (self.dir / "compose.yaml").write_text("services: {edited: {}}\n")
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.assertHeld(self.run_update("--if-changed"))

    def test_dockerfile_change_since_the_running_image_is_held(self):
        """The checkout moved (setup.sh re-run) but the image was built from the old Dockerfile."""
        self.state.mkdir()
        (self.state / "Dockerfile.deployed").write_text("FROM older\n")
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.assertHeld(self.run_update("--if-changed"))

    def test_hold_is_logged_once_and_a_manual_run_deploys_it(self):
        new = self.commit("B", {"deploy/compose.yaml": "services: {usher: {}}\n"})
        self.assertHeld(self.run_update("--if-changed"))
        again = self.run_update("--if-changed")
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, "", ""))
        manual = self.run_update()
        self.assertEqual(manual.returncode, 0, manual.stderr)
        self.assertEqual(self.head(), new)
        self.assertEqual((self.dir / "compose.yaml").read_text(), "services: {usher: {}}\n")
        self.assertFalse((self.state / "held").exists())

    def test_app_only_change_deploys_by_itself_and_records_the_dockerfile(self):
        new = self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Usher updated to 1.1.0", run.stdout)
        self.assertEqual(self.head(), new)
        self.assertEqual((self.state / "Dockerfile.deployed").read_text(), "FROM python\n")

    # Journal output ------------------------------------------------------------
    def test_under_systemd_each_line_reaches_the_journal_once(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed", journal=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("logger")])
        self.assertEqual(run.stdout.count("Usher updated to"), 1)
        self.assertEqual(run.stdout.count("deploying"), 1)
        self.assertNotIn('"version"', run.stdout)  # no /healthz JSON in the journal

    def test_under_systemd_errors_keep_their_priority(self):
        self.commit("B", {"deploy/compose.yaml": "services: {usher: {}}\n"})
        run = self.run_update("--if-changed", journal=True)
        self.assertTrue(run.stderr.startswith("<3>Usher update held"), run.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("logger")])

    def test_by_hand_lines_go_to_the_terminal_and_logger_once_each(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update(journal=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        loggers = [c for c in self.calls() if c.startswith("logger")]
        self.assertEqual(len(loggers), 2, loggers)  # deploying, updated
        self.assertNotIn("<3>", run.stdout + run.stderr)

    # Install folder ------------------------------------------------------------
    def test_install_folder_comes_from_etc_default_usher(self):
        (self.tmp / "default-usher").write_text(f'# written by setup.sh\nUSHER_HOME="{self.dir}"\n')
        new = self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed", home=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.head(), new)

    def test_a_folder_without_usher_is_refused(self):
        (self.tmp / "default-usher").write_text(f"USHER_HOME={self.tmp / 'nowhere'}\n")
        run = self.run_update(home=False)
        self.assertEqual(run.returncode, 1)
        self.assertIn("No Usher install in", run.stderr)
        self.assertFalse(self.calls())

    def test_a_relative_install_folder_is_refused(self):
        (self.tmp / "default-usher").write_text("USHER_HOME=usher\n")
        run = self.run_update(home=False)
        self.assertEqual(run.returncode, 1)
        self.assertIn("must be a full path", run.stderr)

    def test_failed_health_check_rolls_back_and_is_not_retried(self):
        new = self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual(run.returncode, 1)
        self.assertIn("rolling back", run.stderr)
        self.assertIn("rolled back to 1.0.0", run.stderr)
        self.assertEqual(self.head(), self.running)
        self.assertEqual((self.state / "failed").read_text().strip(), new)
        self.assertIn("docker image tag usher:prev usher:local", self.calls())
        self.assertIn("docker compose up -d --force-recreate --wait --wait-timeout 120", self.calls())
        again = self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, "", ""))
        self.assertEqual(self.head(), self.running)

    # Health comes from Docker Compose ------------------------------------------------------------
    def polls(self):
        return [c for c in self.calls() if c.startswith(("curl", "sleep"))]

    def test_compose_waits_for_health_instead_of_polling_healthz(self):
        """A healthy container is the new version: /healthz isn't asked, even when it doesn't answer."""
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed", FAKE_HEALTHZ_DOWN="1")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Usher updated to 1.1.0", run.stdout)
        self.assertIn("docker compose up -d --wait --wait-timeout 120", self.calls())
        self.assertEqual(self.polls(), [])

    def test_a_container_that_is_not_healthy_in_time_is_rolled_back_without_polling(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0", FAKE_HEALTHZ_DOWN="1")
        self.assertEqual(run.returncode, 1)
        self.assertIn("rolled back to 1.0.0", run.stderr)
        self.assertEqual(self.polls(), [])

    def test_a_rollback_that_is_not_healthy_either_says_so_and_reports_nothing(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0", FAKE_ROLLBACK_FAILS="1")
        self.assertEqual(run.returncode, 1)
        self.assertIn("did not come up either", run.stderr)
        self.assertNotIn("rolled back to", run.stderr)
        self.assertEqual(self.head(), self.running)
        self.assertEqual(self.reports(), [])

    def test_an_old_compose_holds_the_update_instead_of_rolling_back(self):
        """Without --wait-timeout the deploy and the rollback would both fail while the old Usher runs fine."""
        new = self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        timer = self.run_update("--if-changed", FAKE_COMPOSE="2.17.0")
        self.assertEqual(timer.returncode, 0, timer.stderr)
        self.assertIn("needs Docker Compose 2.20.2 or later (found 2.17.0)", timer.stderr)
        self.assertEqual(self.head(), self.running)
        self.assertFalse((self.state / "failed").exists())
        self.assertEqual([c for c in self.calls() if c.startswith("docker")], ["docker compose version --short"])
        again = self.run_update("--if-changed", FAKE_COMPOSE="2.17.0")
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, "", ""))
        manual = self.run_update(FAKE_COMPOSE="")
        self.assertEqual(manual.returncode, 1)
        self.assertIn("(found none)", manual.stderr)
        self.assertEqual(self.reports(), [])
        deployed = self.run_update("--if-changed", FAKE_COMPOSE="v2.20.2")
        self.assertEqual(deployed.returncode, 0, deployed.stderr)
        self.assertEqual(self.head(), new)
        self.assertFalse((self.state / "held").exists())

    def reports(self):
        """What the script told Usher about an update that ended (built or rolled_back)."""
        return [c for c in self.calls() if c.startswith("report ") and '"result": "checked"' not in c]

    def checks(self):
        """The newest version it saw on stable, which it reports on every run that reaches GitHub."""
        return [c.split(" -d ", 1)[1].split(" http://", 1)[0].strip() for c in self.calls() if '"result": "checked"' in c]

    def check(self, newest, held="false", failed="false"):
        return f'{{"result": "checked", "newest": "{newest}", "held": {held}, "failed": {failed}}}'

    def test_a_finished_update_reports_built_with_its_version_and_commit(self):
        new = self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed")
        self.assertEqual(run.returncode, 0, run.stderr)
        (call,) = self.reports()
        self.assertIn("127.0.0.1:8484/internal/update-result", call)
        self.assertIn(f'{{"result": "built", "version": "1.1.0", "commit": "{new}"}}', call)

    def test_a_rolled_back_update_reports_the_restored_version(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0")
        (call,) = self.reports()
        self.assertIn(f'{{"result": "rolled_back", "version": "1.0.0", "commit": "{self.running}"}}', call)

    def test_nothing_is_reported_when_there_was_no_update(self):
        self.run_update("--if-changed")
        self.assertEqual(self.reports(), [])

    def test_a_report_that_cannot_be_sent_does_not_fail_the_update(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        run = self.run_update("--if-changed", FAKE_REPORT_EXIT="7")
        self.assertEqual(run.returncode, 0, run.stderr)

    # What Settings → About → Updates is told ------------------------------------------------------------
    def test_a_quiet_run_still_reports_the_newest_version(self):
        run = self.run_update("--if-changed")
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        self.assertEqual(self.checks(), [self.check("1.0.0")])
        self.assertEqual(self.reports(), [])

    def test_a_newer_version_is_reported_and_then_the_finished_update(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.run_update("--if-changed")
        self.assertEqual(self.checks(), [self.check("1.1.0")])  # sent after the update ended, so up to date
        self.assertEqual(len(self.reports()), 1)

    def test_a_held_change_is_reported_as_held_on_every_run(self):
        self.commit("B", {"deploy/compose.yaml": "services: {usher: {}}\n", "usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.assertHeld(self.run_update("--if-changed"))
        again = self.run_update("--if-changed")  # the second run logs nothing, but the app is told again
        self.assertEqual((again.stdout, again.stderr), ("", ""))
        self.assertEqual(self.checks(), [self.check("1.1.0", held="true")] * 2)

    def test_an_old_compose_is_reported_as_held(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.run_update("--if-changed", FAKE_COMPOSE="2.17.0")
        self.assertEqual(self.checks(), [self.check("1.1.0", held="true")])

    def test_a_rollback_is_reported_and_the_version_that_failed_stays_reported_as_failed(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0")
        self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual(self.checks(), [self.check("1.1.0", failed="true")] * 2)

    def test_a_rollback_that_did_not_come_up_reports_no_check(self):
        self.commit("B", {"usher/__init__.py": '__version__ = "1.1.0"\n'})
        self.run_update("--if-changed", FAKE_BAD_VERSION="1.1.0", FAKE_ROLLBACK_FAILS="1")
        self.assertEqual(self.checks(), [])

    def test_a_check_that_cannot_be_sent_does_not_fail_the_run(self):
        run = self.run_update("--if-changed", FAKE_REPORT_EXIT="7")
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        self.assertEqual(len(self.checks()), 1)

    def test_a_commit_whose_version_cannot_be_read_does_not_stop_the_script(self):
        """No version means no check is sent; the hold (or an update) still happens as before."""
        for name, files in (("no version line", {"usher/__init__.py": "# none\n"}), ("no file", {"usher/other.py": "x = 1\n"})):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                self.commit(name, {"deploy/compose.yaml": f"services: {{{name[:2]}: {{}}}}\n", **files})
                if name == "no file":
                    self.git(self.origin, "rm", "-q", "usher/__init__.py")
                    self.git(self.origin, "commit", "-qm", "drop the version file")
                self.assertHeld(self.run_update("--if-changed"))
                self.assertEqual(self.checks(), [])

    def test_a_failed_fetch_reports_nothing(self):
        self.git(self.dir / "src", "remote", "set-url", "origin", (self.tmp / "gone").as_uri())
        self.run_update("--if-changed")
        self.run_update()
        self.assertEqual(self.checks() + self.reports(), [])

    def test_github_unreachable_is_quiet_for_the_timer_and_an_error_by_hand(self):
        self.git(self.dir / "src", "remote", "set-url", "origin", (self.tmp / "gone").as_uri())
        timer = self.run_update("--if-changed")
        self.assertEqual((timer.returncode, timer.stdout), (0, ""))
        self.assertNotIn("Usher updates", timer.stderr)
        self.assertEqual(self.run_update().returncode, 1)
        self.assertFalse([c for c in self.calls() if c.startswith("docker")])
        self.state.mkdir(exist_ok=True)
        stamp = self.state / "fetched"
        stamp.touch()
        os.utime(stamp, (0, 0))  # last good fetch long ago
        late = self.run_update("--if-changed")
        self.assertEqual(late.returncode, 0)
        self.assertIn("can't fetch stable from GitHub for over a day", late.stderr)
        self.assertIn("network or branch", late.stderr)

    def test_the_fetch_uses_no_ssh_and_no_key_files(self):
        """The repository is public: an update from an address with no ssh tools on the path needs no key."""
        self.commit("B", {"usher/__init__.py": '__version__ = "1.0.1"\n'})
        (self.bin / "ssh").write_text('#!/bin/sh\necho "ssh $*" >> "$FAKE_LOG"\nexit 255\n')
        (self.bin / "ssh").chmod(0o755)
        run = self.run_update("--if-changed", GIT_SSH_COMMAND="ssh -i /nonexistent", **{"GIT_SSH": "/nonexistent"})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotEqual(self.head(), self.running, "the update was not fetched")
        self.assertFalse([c for c in self.calls() if c.startswith("ssh")])


if __name__ == "__main__":
    unittest.main()
