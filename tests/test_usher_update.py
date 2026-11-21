"""deploy/usher-update, run with fake git, docker, curl and logger (no network, no Docker).

It updates the install to the newest release: the highest vX.Y.Z tag of the repository, written to USHER_VERSION in
.env, whose image Docker Compose pulls and waits for until it is healthy. A version that doesn't come up is rolled
back and not tried again until a newer one is out. The daily timer is for the people who turn it on."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SCRIPT = DEPLOY / "usher-update"
NEEDED = ("bash", "flock", "install", "sed", "sort", "grep")

FAKES = {
    # `git ls-remote --tags` answers FAKE_TAGS (one version per word, made into refs), or fails with FAKE_LS_FAILS;
    # every git command is recorded, so a test can see the script ran nothing else
    "git": '#!/bin/sh\necho "git $*" >> "$FAKE_LOG"\n[ -n "${FAKE_LS_FAILS:-}" ] && exit 128\n'
    'for t in $FAKE_TAGS; do printf "0123456789abcdef0123456789abcdef01234567\\trefs/tags/%s\\n" "$t"; done\n',
    # Compose: `pull` fails with FAKE_PULL_FAILS (the image isn't published yet). `up --wait` is the health check:
    # it fails for FAKE_BAD_VERSION, and for the rollback's own `up --force-recreate` with FAKE_ROLLBACK_FAILS.
    # A container that came up is what /healthz then reports, as the file `running`.
    "docker": '#!/bin/sh\necho "docker $*" >> "$FAKE_LOG"\n'
    'case "$*" in "compose version --short") echo "${FAKE_COMPOSE-2.29.1}"; exit 0;; esac\n'
    'case "$*" in *pull*) [ -n "${FAKE_PULL_FAILS:-}" ] && exit 1; exit 0;; esac\n'
    'v=$(sed -n "s/^USHER_VERSION=//p" "$FAKE_HOME/.env")\n'
    '[ "$v" = "${FAKE_BAD_VERSION:-}" ] && exit 1\n'
    'case "$*" in *--force-recreate*) [ -n "${FAKE_ROLLBACK_FAILS:-}" ] && exit 1;; esac\n'
    'echo "$v" > "$FAKE_HOME/running"\nexit 0\n',
    "logger": '#!/bin/sh\necho "logger $*" >> "$FAKE_LOG"\n',
    # a report to Usher is recorded with its body; /healthz answers with the running version, or not at all
    "curl": '#!/bin/sh\ncase "$*" in *update-result*) echo "report $*" >> "$FAKE_LOG"; exit "${FAKE_REPORT_EXIT:-0}";; esac\n'
    'echo "curl $*" >> "$FAKE_LOG"\n[ -n "${FAKE_HEALTHZ_DOWN:-}" ] && exit 7\n'
    'printf \'{"ok": true, "version": "%s"}\' "$(cat "$FAKE_HOME/running")"\n',
}


@unittest.skipUnless(all(shutil.which(c) for c in NEEDED), "needs bash and util-linux")
class UsherUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir, self.state, self.bin, self.log = self.tmp / "opt", self.tmp / "state", self.tmp / "bin", self.tmp / "calls.log"
        self.bin.mkdir()
        self.dir.mkdir()
        for name, body in FAKES.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.dir / "compose.yaml").write_text("services: {}\n")
        (self.dir / "site.env").write_text("TZ=Etc/UTC\nUSHER_REPO=https://example.invalid/usher.git\n")
        self.env_file = self.dir / ".env"
        self.env_file.write_text("TR_PASS=made-up\nUSHER_VERSION=1.0.0\n")
        self.env_file.chmod(0o600)
        (self.dir / "running").write_text("1.0.0\n")

    def run_update(self, *args, journal=True, home=True, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("JOURNAL_STREAM", "INVOCATION_ID", "USHER_HOME")}
        env.update(
            PATH=f"{self.bin}:{env['PATH']}",
            USHER_STATE=str(self.state),
            FAKE_LOG=str(self.log),
            FAKE_HOME=str(self.dir),
            FAKE_TAGS=extra.pop("tags", "v1.0.0 v1.1.0"),
            USHER_DEPLOY_CONF=str(self.tmp / "default-usher"),
            **extra,
        )
        if home:
            env["USHER_HOME"] = str(self.dir)
        if journal:
            env["JOURNAL_STREAM"] = "8:12345"
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def calls(self, kind=""):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [c for c in lines if c.startswith(kind)]

    def version(self):
        return [x for x in self.env_file.read_text().splitlines() if x.startswith("USHER_VERSION=")]

    def running(self):
        return (self.dir / "running").read_text().strip()

    def sent(self, checked):
        return [c.split(" -d ", 1)[1].split(" http://", 1)[0].strip() for c in self.calls("report ") if ('"checked"' in c) is checked]

    def reports(self):
        """What the script told Usher about an update that ended (built or rolled_back)."""
        return self.sent(False)

    def checks(self):
        """The newest release it saw, which it reports on every run that reaches GitHub."""
        return self.sent(True)

    def check(self, newest, held="false", failed="false"):
        return f'{{"result": "checked", "newest": "{newest}", "held": {held}, "failed": {failed}}}'

    # Updating ------------------------------------------------------------
    def test_a_newer_release_is_installed_by_setting_its_version_and_pulling_its_image(self):
        run = self.run_update("--auto")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Usher updated to 1.1.0", run.stdout)
        self.assertEqual(self.version(), ["USHER_VERSION=1.1.0"])
        self.assertIn("TR_PASS=made-up", self.env_file.read_text())  # nothing else in .env is touched
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        steps = [c for c in self.calls("docker") if "version" not in c]
        self.assertEqual(steps, ["docker compose pull -q", "docker compose up -d --wait --wait-timeout 120"])
        self.assertEqual(self.running(), "1.1.0")

    def test_the_newest_release_is_the_highest_number_not_the_last_listed_or_the_highest_text(self):
        tags = "v1.9.0 v1.10.0 v1.2.3 v2.0.0-beta v1.11 latest stable v1.3.0-rc1 1.99.0"
        self.run_update("--auto", tags=tags)
        self.assertEqual(self.version(), ["USHER_VERSION=1.10.0"])

    def test_an_install_that_is_up_to_date_is_left_alone_and_the_timer_stays_quiet(self):
        quiet = self.run_update("--auto", tags="v1.0.0")
        self.assertEqual((quiet.returncode, quiet.stdout, quiet.stderr), (0, "", ""))
        by_hand = self.run_update(tags="v1.0.0")
        self.assertEqual(by_hand.returncode, 0, by_hand.stderr)
        self.assertIn("Already up to date: 1.0.0", by_hand.stdout)
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])
        self.assertFalse(self.calls("docker"))

    def test_a_running_version_newer_than_the_newest_release_is_not_downgraded(self):
        (self.dir / "running").write_text("2.0.0\n")
        run = self.run_update("--auto", tags="v1.0.0 v1.1.0")
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])
        self.assertFalse(self.calls("docker"))

    def test_it_only_reads_the_list_of_releases_and_uses_no_ssh(self):
        """The repository is public: the server asks for its tags and nothing else, with no key."""
        self.run_update("--auto")
        self.assertEqual(self.calls("git "), ["git ls-remote --tags --refs https://example.invalid/usher.git v*"])
        (self.bin / "ssh").write_text('#!/bin/sh\necho "ssh $*" >> "$FAKE_LOG"\nexit 255\n')
        (self.bin / "ssh").chmod(0o755)
        self.assertEqual(self.run_update("--auto", tags="v1.2.0", GIT_SSH_COMMAND="ssh -i /nonexistent").returncode, 0)
        self.assertFalse(self.calls("ssh"))

    def test_an_argument_it_does_not_know_does_not_start_an_update(self):
        """An old timer unit still passes --if-changed; that must not turn into an update by hand."""
        run = self.run_update("--if-changed")
        self.assertEqual(run.returncode, 2)
        self.assertIn("Usage: usher-update [--auto]", run.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])
        self.assertFalse(self.calls("docker") or self.calls("git"))

    def test_a_version_usher_reports_that_is_not_a_plain_number_is_not_used(self):
        (self.dir / "running").write_text("1..2\n")
        run = self.run_update("--auto")
        self.assertEqual(run.returncode, 1)
        self.assertIn("doesn't answer", run.stderr)
        self.assertFalse(self.calls("docker"))

    def test_an_env_file_that_cannot_be_read_is_never_replaced_by_one_holding_only_the_version(self):
        """grep exits 1 when no line is left, which is fine, and 2 when it can't read the file, which must stop."""
        (self.bin / "grep").write_text("#!/bin/sh\nexit 2\n")
        (self.bin / "grep").chmod(0o755)
        run = self.run_update("--auto")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.env_file.read_text(), "TR_PASS=made-up\nUSHER_VERSION=1.0.0\n")
        self.assertFalse([c for c in self.calls("docker") if " pull" in c or " up " in c])
        self.assertEqual(self.env_file.read_text(), "TR_PASS=made-up\nUSHER_VERSION=1.0.0\n")

    def test_an_env_file_with_only_the_version_line_is_updated(self):
        self.env_file.write_text("USHER_VERSION=1.0.0\n")
        self.assertEqual(self.run_update("--auto").returncode, 0)
        self.assertEqual(self.env_file.read_text(), "USHER_VERSION=1.1.0\n")

    def test_a_repository_address_that_is_not_https_is_refused(self):
        (self.dir / "site.env").write_text("USHER_REPO=git@example.invalid:usher.git\n")
        run = self.run_update("--auto")
        self.assertEqual(run.returncode, 1)
        self.assertIn("must be an https:// address", run.stderr)
        self.assertFalse(self.calls("docker") or self.calls("git"))

    # Going back when it doesn't come up ------------------------------------------------------------
    def test_a_version_that_is_not_healthy_in_time_is_rolled_back_and_not_tried_again(self):
        run = self.run_update("--auto", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual(run.returncode, 1)
        self.assertIn("Usher 1.1.0 failed to start; going back to 1.0.0", run.stderr)
        self.assertIn("Usher is back on 1.0.0; it won't try 1.1.0 again", run.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])
        self.assertEqual(self.running(), "1.0.0")
        self.assertEqual((self.state / "failed").read_text().strip(), "1.1.0")
        self.assertIn("docker compose up -d --force-recreate --wait --wait-timeout 120", self.calls("docker"))
        self.log.unlink()
        again = self.run_update("--auto", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, "", ""))
        self.assertFalse(self.calls("docker"))
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])

    def test_a_newer_release_than_the_one_that_failed_is_tried(self):
        self.run_update("--auto", FAKE_BAD_VERSION="1.1.0")
        run = self.run_update("--auto", tags="v1.0.0 v1.1.0 v1.2.0", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.2.0"])
        self.assertFalse((self.state / "failed").exists())

    def test_by_hand_a_failed_version_is_tried_again(self):
        self.run_update("--auto", FAKE_BAD_VERSION="1.1.0")
        run = self.run_update()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.1.0"])

    def test_a_rollback_that_is_not_healthy_either_says_so_and_reports_nothing(self):
        run = self.run_update("--auto", FAKE_BAD_VERSION="1.1.0", FAKE_ROLLBACK_FAILS="1")
        self.assertEqual(run.returncode, 1)
        self.assertIn("Usher 1.0.0 did not come up either", run.stderr)
        self.assertNotIn("is back on", run.stderr)
        self.assertEqual(self.reports() + self.checks(), [])

    def test_an_image_that_is_not_published_yet_changes_nothing_and_is_not_a_failure(self):
        """The Release workflow tags a version a little before its image is pushed."""
        timer = self.run_update("--auto", FAKE_PULL_FAILS="1")
        self.assertEqual(timer.returncode, 0, timer.stderr)
        self.assertIn("can't be downloaded yet", timer.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])
        self.assertFalse([c for c in self.calls("docker") if " up " in c])
        self.assertFalse((self.state / "failed").exists())
        self.assertEqual(self.reports(), [])
        self.assertEqual(self.checks(), [self.check("1.1.0")])
        by_hand = self.run_update(FAKE_PULL_FAILS="1")
        self.assertEqual(by_hand.returncode, 1)
        later = self.run_update("--auto")
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.1.0"])

    def test_a_failed_pull_puts_env_back_exactly_as_it_was_whatever_is_running(self):
        for before in ("TR_PASS=made-up\nUSHER_VERSION=1.0.0\n", "TR_PASS=made-up\n"):
            with self.subTest(before=before):
                self.env_file.write_text(before)
                run = self.run_update(FAKE_PULL_FAILS="1", FAKE_HEALTHZ_DOWN="1")  # by hand, with Usher not answering
                self.assertEqual(run.returncode, 1)
                self.assertIn("nothing was changed", run.stderr)
                self.assertEqual(self.env_file.read_text(), before)

    def test_an_old_compose_holds_the_update_instead_of_rolling_back(self):
        """Without --wait-timeout the update and the rollback would both fail while the old Usher runs fine."""
        timer = self.run_update("--auto", FAKE_COMPOSE="2.17.0")
        self.assertEqual(timer.returncode, 0, timer.stderr)
        self.assertIn("needs Docker Compose 2.20.2 or later (found 2.17.0)", timer.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])
        self.assertEqual(self.calls("docker"), ["docker compose version --short"])
        self.assertFalse((self.state / "failed").exists())
        again = self.run_update("--auto", FAKE_COMPOSE="2.17.0")
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, "", ""))  # logged once
        manual = self.run_update(FAKE_COMPOSE="")
        self.assertEqual(manual.returncode, 1)
        self.assertIn("(found none)", manual.stderr)
        self.assertEqual(self.reports(), [])
        deployed = self.run_update("--auto", FAKE_COMPOSE="v2.20.2")
        self.assertEqual(deployed.returncode, 0, deployed.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.1.0"])
        self.assertFalse((self.state / "held").exists())

    # Output ------------------------------------------------------------
    def test_under_systemd_each_line_reaches_the_journal_once(self):
        run = self.run_update("--auto", journal=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(self.calls("logger"))
        self.assertEqual(run.stdout.count("Usher updated to"), 1)
        self.assertEqual(run.stdout.count("updating"), 1)

    def test_under_systemd_errors_keep_their_priority(self):
        run = self.run_update("--auto", FAKE_COMPOSE="2.17.0")
        self.assertTrue(run.stderr.startswith("<3>Usher update held"), run.stderr)
        self.assertFalse(self.calls("logger"))

    def test_by_hand_lines_go_to_the_terminal_and_logger_once_each(self):
        run = self.run_update(journal=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(self.calls("logger")), 2, self.calls("logger"))  # updating, updated
        self.assertNotIn("<3>", run.stdout + run.stderr)

    # Install folder ------------------------------------------------------------
    def test_install_folder_comes_from_etc_default_usher(self):
        (self.tmp / "default-usher").write_text(f'# written by setup.sh\nUSHER_HOME="{self.dir}"\n')
        run = self.run_update("--auto", home=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.1.0"])

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

    # What Usher is told, and what happens when it can't be reached ------------------------------------------------------------
    def test_a_finished_update_reports_built_with_its_version_and_nothing_else(self):
        self.run_update("--auto")
        self.assertEqual(self.reports(), ['{"result": "built", "version": "1.1.0"}'])
        self.assertIn("127.0.0.1:8484/internal/update-result", self.calls("report ")[0])
        self.assertEqual(self.checks(), [self.check("1.1.0")])  # sent after the update ended, so up to date

    def test_a_rolled_back_update_reports_the_restored_version_and_that_the_new_one_failed(self):
        self.run_update("--auto", FAKE_BAD_VERSION="1.1.0")
        self.run_update("--auto", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual(self.reports(), ['{"result": "rolled_back", "version": "1.0.0"}'])
        self.assertEqual(self.checks(), [self.check("1.1.0", failed="true")] * 2)

    def test_a_quiet_run_still_reports_the_newest_release(self):
        self.run_update("--auto", tags="v1.0.0")
        self.assertEqual((self.checks(), self.reports()), ([self.check("1.0.0")], []))

    def test_a_held_update_is_reported_as_held_on_every_run(self):
        self.run_update("--auto", FAKE_COMPOSE="2.17.0")
        self.run_update("--auto", FAKE_COMPOSE="2.17.0")
        self.assertEqual(self.checks(), [self.check("1.1.0", held="true")] * 2)

    def test_a_report_that_cannot_be_sent_does_not_fail_the_update(self):
        run = self.run_update("--auto", FAKE_REPORT_EXIT="7")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.1.0"])

    def test_github_unreachable_is_quiet_for_the_timer_and_an_error_by_hand(self):
        timer = self.run_update("--auto", FAKE_LS_FAILS="1")
        self.assertEqual((timer.returncode, timer.stdout, timer.stderr), (0, "", ""))
        self.assertEqual(self.run_update(FAKE_LS_FAILS="1").returncode, 1)
        self.assertFalse(self.calls("docker") or self.calls("report"))
        self.state.mkdir(exist_ok=True)
        stamp = self.state / "fetched"
        stamp.touch()
        os.utime(stamp, (0, 0))  # last good look at the releases long ago
        late = self.run_update("--auto", FAKE_LS_FAILS="1")
        self.assertEqual(late.returncode, 0)
        self.assertIn("can't read the releases from GitHub for over a day", late.stderr)

    def test_a_repository_with_no_release_changes_nothing(self):
        timer = self.run_update("--auto", tags="stable nightly")
        self.assertEqual((timer.returncode, timer.stdout), (0, ""))
        self.assertIn("has no release yet", timer.stderr)
        self.assertEqual(self.run_update(tags="").returncode, 1)
        self.assertFalse(self.calls("docker") or self.calls("report"))

    def test_when_usher_does_not_answer_the_timer_changes_nothing_and_says_so(self):
        run = self.run_update("--auto", FAKE_HEALTHZ_DOWN="1")
        self.assertEqual(run.returncode, 1)
        self.assertIn("doesn't answer on 127.0.0.1:8484", run.stderr)
        self.assertEqual(self.version(), ["USHER_VERSION=1.0.0"])
        self.assertFalse(self.calls("docker"))

    def test_by_hand_an_update_goes_ahead_while_usher_is_down_but_has_nothing_to_go_back_to(self):
        run = self.run_update(FAKE_HEALTHZ_DOWN="1", FAKE_BAD_VERSION="1.1.0")
        self.assertEqual(run.returncode, 1)
        self.assertIn("no earlier version to go back to", run.stderr)
        self.assertNotIn("force-recreate", " ".join(self.calls("docker")))


class DailyTimerTests(unittest.TestCase):
    """Automatic updates are optional and run at most once a day."""

    def test_the_timer_runs_once_a_day_and_not_every_few_minutes(self):
        timer = (DEPLOY / "usher-autodeploy.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=daily\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertNotRegex(timer, r"OnUnitActiveSec|OnBootSec|OnUnitInactiveSec|minutes?\b|15")

    def test_the_service_runs_the_script_in_its_automatic_mode(self):
        service = (DEPLOY / "usher-autodeploy.service").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/local/bin/usher-update --auto\n", service)
        self.assertNotIn("--if-changed", service)

    def test_setup_installs_the_units_but_does_not_turn_the_timer_on(self):
        text = (DEPLOY / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/systemd/system/", text)
        self.assertNotRegex(text, r"(?m)^\s*systemctl enable")  # only ever named in what it tells the person
        self.assertIn("Automatic updates are off.", text)


if __name__ == "__main__":
    unittest.main()
