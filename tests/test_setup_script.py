"""deploy/setup.sh reads its settings from usher.toml alone; site.env only has to name the repo. It checks for every tool
it needs before it changes anything, installs none of them, and lets Docker Compose wait for Usher to be healthy."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent / "deploy" / "setup.sh"
BASH = shutil.which("bash")
# The tools deploy/README.md lists under Requirements, as setup.sh looks for them
TOOLS = ("git", "curl", "flock", "cmp", "logger", "systemctl", "python3", "docker")


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
        (self.bin / "cmp").unlink()
        self.assertStopsNaming(self.run_setup(), "flock", "cmp")

    def test_missing_docker_is_named_once(self):
        (self.bin / "docker").unlink()
        run = self.run_setup()
        self.assertStopsNaming(run, "docker")
        self.assertNotIn("Compose", run.stdout)

    def test_cmp_and_logger_for_usher_update_are_checked(self):
        (self.bin / "cmp").unlink()
        (self.bin / "logger").unlink()
        self.assertStopsNaming(self.run_setup(), "cmp", "logger")

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
