"""deploy/setup.sh reads its settings from usher.toml alone; site.env only has to name the repo. It checks for every tool
it needs before it changes anything, installs none of them, and lets Docker Compose wait for Usher to be healthy."""

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent / "deploy" / "setup.sh"
BASH = shutil.which("bash")
# The tools deploy/README.md lists under Requirements, as setup.sh looks for them
TOOLS = ("git", "curl", "flock", "logger", "systemctl", "python3", "docker")


class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = SETUP.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_it_parses(self):
        subprocess.run(["bash", "-n", str(SETUP)], check=True)

    def test_site_env_only_needs_the_repo(self):
        loop = [line for line in self.text.splitlines() if line.startswith("for v in")]
        self.assertEqual(loop, [], "no list of required site.env variables")
        self.assertIn('[ -n "${USHER_REPO:-}" ]', self.text)
        # what it needs is checked only after reading usher.toml
        self.assertLess(self.text.index("tomlget server origin"), self.text.index('[ -n "$ORIGIN" ]'))
        self.assertLess(self.text.index("tomlget download_client user"), self.text.index('[ -n "$TR_USER" ]'))

    def test_settings_come_only_from_usher_toml(self):
        self.assertIn('tomlget server origin', self.text)
        self.assertIn('tomlget media_server user_id', self.text)
        self.assertIn('tomlget download_client user', self.text)
        self.assertIn('usher.example.toml', self.text)
        self.assertIn('usher.cli config check', self.text)
        # no fallback to the old site.env settings, and no migration from them
        for old in ("migrate", "USHER_ORIGIN", "JELLYFIN_USER", "TR_USER:-", "JF_NAME", "this release"):
            self.assertNotIn(old, self.text)

    def test_it_installs_nothing(self):
        self.assertNotIn("apt-get", self.text)

    def test_it_pulls_the_image_of_a_release_and_builds_nothing(self):
        self.assertIn("docker compose pull && docker compose up -d --wait --wait-timeout 120", self.text)
        self.assertNotIn("docker compose build", self.text)
        self.assertNotIn("--branch stable", self.text)
        self.assertIn('--branch "v$VER"', self.text)
        self.assertIn('setkey USHER_VERSION "$VER"', self.text)  # the version to run is written, once

    def test_compose_waits_for_a_healthy_usher(self):
        self.assertIn("docker compose up -d --wait --wait-timeout 120", self.text)
        self.assertNotIn("seq 1 30", self.text)
        self.assertLess(self.text.index("--wait-timeout 120"), self.text.index("curl -fsS http://127.0.0.1:8484/healthz && echo"))

    def test_reverse_proxy_is_left_to_the_operator(self):
        """The setup script no longer edits another app's files; deploy/README.md says how to point the proxy at Usher."""
        self.assertNotIn("Caddyfile", self.text)
        self.assertNotIn('HOST=${USHER_ORIGIN', self.text)


@unittest.skipUnless(BASH, "needs bash")
class ToolCheckTests(unittest.TestCase):
    """setup.sh run with a PATH that holds only fakes of the tools it needs (and id, so it runs as root)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin, self.log, self.home = self.tmp / "bin", self.tmp / "calls.log", self.tmp / "usher"
        self.bin.mkdir()
        self.fake("id", "echo 0")
        for tool in TOOLS:
            self.fake(tool, "exit 0")
        self.fake("python3", 'case "$*" in *version_info*) exit "${FAKE_PYTHON_OLD:-0}";; esac')
        self.fake("docker", 'case "$*" in "compose version --short") echo "${FAKE_COMPOSE-2.29.1}";; esac')

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{BASH}\necho "{name} $*" >> "{self.log}"\n{body}\n')
        path.chmod(0o755)

    def run_setup(self, **extra):
        env = {"PATH": str(self.bin), "USHER_HOME": str(self.home), **extra}
        return subprocess.run([BASH, str(SETUP)], env=env, capture_output=True, text=True, timeout=30)

    def assertStopsNaming(self, run, *names):
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        (line,) = [x for x in run.stdout.splitlines() if x.startswith("FAIL")]
        for name in names:
            self.assertIn(name, line)
        self.assertFalse(self.home.exists(), "created the install folder")
        checks = [c for c in self.log.read_text().splitlines() if not c.startswith("id ")]
        self.assertTrue(all(c.startswith(("python3 -c", "docker compose version")) for c in checks), checks)

    def test_with_every_tool_it_goes_on_to_read_site_env(self):
        run = self.run_setup()
        self.assertEqual(run.returncode, 1)
        self.assertIn("site.env is missing", run.stdout)
        self.assertFalse(self.home.exists())

    def test_no_ssh_tools_are_needed(self):
        """Only the tools above are on the path, and none of them is ssh, ssh-keygen or ssh-keyscan."""
        self.assertFalse({"ssh", "ssh-keygen", "ssh-keyscan"} & {p.name for p in self.bin.iterdir()})
        self.assertIn("site.env is missing", self.run_setup().stdout)

    def test_a_repo_address_that_is_not_https_is_refused(self):
        self.home.mkdir()
        (self.home / "site.env").write_text("USHER_REPO=git@github.com:example/usher.git\n")
        run = self.run_setup()
        self.assertEqual(run.returncode, 1)
        self.assertIn("must be an https:// address", run.stdout)

    def test_an_unreadable_repo_stops_setup_with_a_hint(self):
        self.home.mkdir()
        (self.home / "site.env").write_text("USHER_REPO=https://example.invalid/usher.git\n")
        self.fake("git", "exit 128")
        run = self.run_setup()
        self.assertEqual(run.returncode, 1)
        self.assertIn("can't read https://example.invalid/usher.git", run.stdout)
        self.assertNotIn("ssh", [c.split()[0] for c in self.log.read_text().splitlines()])

    def test_missing_git_is_named_and_nothing_is_created(self):
        (self.bin / "git").unlink()
        self.assertStopsNaming(self.run_setup(), "git")

    def test_every_missing_tool_is_named_in_one_line(self):
        (self.bin / "flock").unlink()
        (self.bin / "systemctl").unlink()
        self.assertStopsNaming(self.run_setup(), "flock", "systemctl")

    def test_missing_docker_is_named_once(self):
        (self.bin / "docker").unlink()
        run = self.run_setup()
        self.assertStopsNaming(run, "docker")
        self.assertNotIn("Compose", run.stdout)

    def test_flock_and_logger_for_usher_update_are_checked(self):
        (self.bin / "flock").unlink()
        (self.bin / "logger").unlink()
        self.assertStopsNaming(self.run_setup(), "flock", "logger")

    def test_a_container_that_does_not_become_healthy_stops_setup_with_a_hint(self):
        text = SETUP.read_text()
        self.assertIn("isn't healthy. Look at: sudo docker logs --tail 50 usher", text)

    def test_old_python_is_named(self):
        self.assertStopsNaming(self.run_setup(FAKE_PYTHON_OLD="1"), "python3 3.11 or later")

    def test_old_or_missing_compose_is_named(self):
        for version in ("2.17.0", "v2.20.1", "2.9.0", "2.20", ""):
            with self.subTest(version=version):
                self.assertStopsNaming(self.run_setup(FAKE_COMPOSE=version), "Docker Compose 2.20.2 or later")

    def test_newer_compose_versions_pass(self):
        for version in ("2.20.2", "v2.29.1-desktop.1", "2.100.0", "5.6.1"):
            with self.subTest(version=version):
                self.assertIn("site.env is missing", self.run_setup(FAKE_COMPOSE=version).stdout)


def setkey_source():
    """The setkey function exactly as deploy/setup.sh defines it, so the tests below run the real code."""
    found = re.search(r"(?ms)^setkey\(\) \{.*?^\}", SETUP.read_text(encoding="utf-8"))
    assert found, "no setkey() in deploy/setup.sh"
    return found.group(0)


@unittest.skipUnless(BASH, "needs bash")
class SetkeyTests(unittest.TestCase):
    """setkey in deploy/setup.sh sets one key in .env without ever emptying it: the kept lines are read into a
    unique temp file first, and .env is replaced only by one atomic rename once the read succeeded. A .env that
    cannot be read is left exactly as it was, and setkey stops with a message (issue #152)."""

    SECRETS = "TR_PASS=made-up\nJELLYFIN_API_KEY=api-key\nTMDB_TOKEN=token\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = self.tmp / "usher"
        self.dir.mkdir()
        self.env_file = self.dir / ".env"

    def write_env(self, content=SECRETS, mode=0o600):
        self.env_file.write_text(content)
        self.env_file.chmod(mode)

    def run_setkey(self, key, value, path=""):
        """Run setup.sh's setkey in a bash of its own, with DIR at the test's install folder."""
        script = f'set -euo pipefail\nDIR={shlex.quote(str(self.dir))}\n{setkey_source()}\nsetkey "$KEY" "$VALUE"\n'
        env = dict(os.environ, KEY=key, VALUE=value)
        if path:
            env["PATH"] = path + os.pathsep + env["PATH"]
        return subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True, timeout=30)

    def leftovers(self):
        """Files the run left in the install folder besides .env itself (temp files, say)."""
        return sorted(p.name for p in self.dir.iterdir() if p.name != ".env")

    def test_the_source_reads_before_it_replaces(self):
        func = setkey_source()
        self.assertNotIn("|| true", func)  # a read error is never swallowed
        self.assertIn("[ $? -eq 1 ]", func)  # grep's exit 1 (no line left) is the only tolerated failure
        self.assertIn("mktemp", func)  # a unique temp file, so concurrent runs can't clobber each other
        self.assertIn("mv -f", func)  # .env is replaced by one atomic rename, never truncated in place
        self.assertNotIn(".env.new", SETUP.read_text(encoding="utf-8"))

    def test_a_read_error_from_grep_stops_setkey_and_leaves_env_unchanged(self):
        """grep exits 2 when it can't read the file (permission denied, an I/O error); only exit 1 is fine."""
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "grep").write_text("#!/bin/sh\nexit 2\n")
        (bin_dir / "grep").chmod(0o755)
        self.write_env()
        run = self.run_setkey("NEW_KEY", "value", path=str(bin_dir))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("FAIL", run.stdout)
        self.assertIn(".env", run.stdout)
        self.assertEqual(self.env_file.read_text(), self.SECRETS)
        self.assertEqual(self.leftovers(), [], "the temp file was left behind")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read a 000 file")
    def test_an_env_file_with_no_read_permission_stops_setkey_and_leaves_it_unchanged(self):
        self.write_env(mode=0o000)
        run = self.run_setkey("NEW_KEY", "value")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("FAIL", run.stdout)
        self.assertIn(".env", run.stdout)
        self.env_file.chmod(0o600)
        self.assertEqual(self.env_file.read_text(), self.SECRETS)
        self.assertEqual(self.leftovers(), [])

    def test_a_missing_env_file_stops_setkey_without_creating_one(self):
        run = self.run_setkey("NEW_KEY", "value")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("FAIL", run.stdout)
        self.assertFalse(self.env_file.exists())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_an_empty_env_file_gets_the_key_written(self):
        """grep's exit 1 (no line is left) is the normal empty case: the one key is written."""
        self.write_env("")
        run = self.run_setkey("TR_PASS", "secret")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.env_file.read_text(), "TR_PASS=secret\n")
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        self.assertEqual(self.leftovers(), [])

    def test_the_normal_update_replaces_only_the_one_key_and_keeps_the_rest(self):
        self.write_env("TR_PASS=made-up\nTR_PASSX=keep-me\nJELLYFIN_API_KEY=api-key\n")
        run = self.run_setkey("JELLYFIN_API_KEY", "new-key")
        self.assertEqual(run.returncode, 0, run.stderr)
        # TR_PASSX only starts like TR_PASS, so the anchored grep must keep it
        self.assertEqual(self.env_file.read_text(), "TR_PASS=made-up\nTR_PASSX=keep-me\nJELLYFIN_API_KEY=new-key\n")
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        run = self.run_setkey("NEW_KEY", "a value")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.env_file.read_text().count("JELLYFIN_API_KEY="), 1)
        self.assertTrue(self.env_file.read_text().endswith("NEW_KEY=a value\n"))
        self.assertEqual(self.leftovers(), [])

    def test_concurrent_setkey_writes_leave_a_complete_env_file(self):
        """Two setkey runs at once each get their own temp file, and a reader never sees .env missing or empty."""
        self.write_env("A=0\n")
        flag = self.tmp / "reader-flag"
        script = (
            "set -euo pipefail\n"
            f"DIR={shlex.quote(str(self.dir))}\n"
            f"FLAG={shlex.quote(str(flag))}\n"
            f"{setkey_source()}\n"
            '( while :; do [ -f "$DIR/.env" ] && [ -s "$DIR/.env" ] || echo incomplete >> "$FLAG"; done ) &\n'
            "reader=$!\n"
            'for i in $(seq 1 15); do setkey A "a$i" & p1=$!; setkey B "b$i" & p2=$!; wait "$p1" "$p2"; done\n'
            'kill "$reader" 2>/dev/null || true; wait "$reader" 2>/dev/null || true\n'
            "exit 0\n"
        )
        run = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(flag.exists(), "a reader saw .env missing or empty while it was being written")
        lines = self.env_file.read_text().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(re.fullmatch(r"[AB]=[ab]\d+", line) for line in lines), lines)
        self.assertEqual(sorted(line.split("=")[0] for line in lines), ["A", "B"])
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)


@unittest.skipUnless(BASH, "needs bash")
class SecretsSectionTests(unittest.TestCase):
    """setup.sh run with fake tools all the way to its Secrets step: a .env that cannot be read makes it stop
    with a message and leave .env as it was, instead of replacing it with a file holding only the new key."""

    SECRETS = "TR_PASS=made-up\nJELLYFIN_API_KEY=api-key\n"
    # tomlget asks python3 - FILE SECTION KEY; the fake answers from the key alone
    PYTHON = (
        'case "$*" in *version_info*) exit 0;; esac\n'
        '[ "$1" = "-" ] || exit 0\n'
        "cat > /dev/null\n"
        'case "$4" in\n'
        "  origin) echo https://usher.example.invalid;;\n"
        "  user_id) echo 11111111222233334444555555555555;;\n"
        "  user) echo tr-user;;\n"
        "esac\n"
    )
    GIT = (
        'case "$*" in\n'
        '  *"ls-remote --tags"*) printf \'0123456789abcdef0123456789abcdef01234567\\trefs/tags/v1.2.3\\n\'; exit 0;;\n'
        '  *clone*) for last; do :; done; mkdir -p "$last"; exit 0;;\n'
        "esac\n"
        "exit 0\n"
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin, self.home, self.log = self.tmp / "bin", self.tmp / "usher", self.tmp / "calls.log"
        self.bin.mkdir()
        self.home.mkdir()
        self.fake("id", "echo 0")
        for tool in ("curl", "flock", "logger", "systemctl"):
            self.fake(tool, "exit 0")
        self.fake("docker", 'case "$*" in "compose version --short") echo 2.29.1;; esac')
        self.fake("python3", self.PYTHON)
        self.fake("git", self.GIT)
        self.fake("grep", "exit 2")  # the read of .env fails, as with permission denied or an I/O error
        (self.home / "site.env").write_text("USHER_REPO=https://example.invalid/usher.git\n")
        (self.home / "config").mkdir()
        (self.home / "config" / "usher.toml").write_text('[server]\norigin = "https://usher.example.invalid"\n')
        self.env_file = self.home / ".env"
        self.env_file.write_text(self.SECRETS)
        self.env_file.chmod(0o600)

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(f'#!{BASH}\necho "{name} $*" >> {self.log}\n{body}\n')
        path.chmod(0o755)

    def run_setup(self, answers):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", USHER_HOME=str(self.home))
        return subprocess.run([BASH, str(SETUP)], env=env, input=answers, capture_output=True, text=True, timeout=30)

    def test_an_env_file_that_cannot_be_read_stops_setup_with_env_unchanged(self):
        run = self.run_setup("tr-pass\n")
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        (line,) = [x for x in run.stdout.splitlines() if x.startswith("FAIL")]
        self.assertIn(".env", line)
        self.assertIn("left unchanged", line)
        self.assertEqual(self.env_file.read_text(), self.SECRETS)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".env.")], [], "temp files left behind")
