"""No host paths in the deploy files. The install folder comes from USHER_HOME (/opt/usher only as the
documented default), and compose.yaml takes every folder and file from this machine from variables in .env."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
MOUNTS = ("USHER_RECYCLE_BIN", "USHER_SONARR_XML", "USHER_RADARR_XML", "USHER_PROWLARR_XML")
# deploy/usher.example.toml is left out: its library folders are config.py's documented defaults
CHECKED = ("compose.yaml", "setup.sh", "usher-update", "usher-autodeploy.service", "usher-autodeploy.timer", "site.env.example", "README.md")


def volume_sources(text):
    return [re.match(r"\s*- (.+?):/[a-z]", line).group(1) for line in text.split("volumes:", 1)[1].splitlines() if re.match(r"\s*- ", line)]


class DeployPathTests(unittest.TestCase):
    def test_no_host_paths_beyond_the_documented_default(self):
        for name in CHECKED:
            for n, line in enumerate((DEPLOY / name).read_text(encoding="utf-8").splitlines(), 1):
                where = f"deploy/{name}:{n}: {line}"
                self.assertNotRegex(line, r"/srv/|/opt/(?!usher)", where)
                # deploy/README.md's commands use the default folder, and it says so
                if "/opt/usher" in line and name != "README.md":
                    self.assertTrue("${USHER_HOME:-/opt/usher}" in line or "by default" in line, where)
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotRegex(dockerfile, r"/opt/|/srv/")

    def test_compose_mounts_only_its_own_folder_and_variables(self):
        sources = volume_sources(COMPOSE.read_text(encoding="utf-8"))
        self.assertEqual(sources[:2], ["./data", "./config"])
        self.assertEqual([re.match(r"\$\{(\w+):\?", s).group(1) for s in sources[2:]], list(MOUNTS))
        self.assertNotIn("docker.sock", COMPOSE.read_text(encoding="utf-8"))

    def test_compose_health_check_is_the_dockerfiles_with_a_quick_start(self):
        """compose.yaml's healthcheck replaces the image's, so it repeats it, and checks every 2 seconds while starting:
        `docker compose up --wait` (in setup.sh and usher-update) then returns within seconds of a healthy start."""
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        found = re.search(r"^HEALTHCHECK (.*?) \\\n\s*CMD (\[.*\])$", dockerfile, re.M)
        image = dict(re.findall(r"--([a-z-]+)=(\S+)", found.group(1)))
        block = COMPOSE.read_text(encoding="utf-8").split("    healthcheck:\n", 1)[1]
        compose = dict(re.findall(r"^      (\w+): (.+)$", re.match(r"(?:      .*\n)*", block).group(0), re.M))
        self.assertEqual(json.loads(compose.pop("test")), ["CMD", *json.loads(found.group(2))])
        self.assertEqual(compose.pop("start_interval"), "2s")
        self.assertEqual(compose, {k.replace("-", "_"): v for k, v in image.items()})

    def test_mount_variables_are_not_settings_the_container_reads(self):
        """.env is also the container's environment, so a host path there must not look like one of its settings."""
        code = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "usher").rglob("*.py"))
        for var in MOUNTS:
            self.assertNotIn(f'"{var}"', code)

    def test_setup_asks_for_each_mount_and_records_the_install_folder(self):
        text = (DEPLOY / "setup.sh").read_text(encoding="utf-8")
        for var in MOUNTS:
            self.assertIn(f"askpath {var} ", text)
        self.assertIn("DIR=${USHER_HOME:-/opt/usher}", text)
        self.assertIn("CONF=/etc/default/usher", text)
        self.assertIn('USHER_HOME=%s', text)
        self.assertNotRegex(text, r"Caddyfile|/opt/transmission|/opt/arr")

    def test_usher_update_reads_the_same_file_setup_writes(self):
        text = (DEPLOY / "usher-update").read_text(encoding="utf-8")
        self.assertIn("CONF=${USHER_DEPLOY_CONF:-/etc/default/usher}", text)
        self.assertIn("DIR=${USHER_HOME:-/opt/usher}", text)
        self.assertNotIn('. "$CONF"', text, "read, not sourced: it runs as root")

    def test_readme_explains_install_updating_security_and_upgrading(self):
        text = (DEPLOY / "README.md").read_text(encoding="utf-8")
        for heading in ("## Requirements", "## Install", "## First run", "## Updating", "## Security model", "## Upgrading"):
            self.assertIn(heading, text)
        for var in (*MOUNTS, "USHER_HOME"):
            self.assertIn(f"`{var}`", text)

    @unittest.skipUnless(shutil.which("shellcheck"), "needs shellcheck")
    def test_scripts_pass_shellcheck(self):
        run = subprocess.run(["shellcheck", str(DEPLOY / "setup.sh"), str(DEPLOY / "usher-update")], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout)

    @unittest.skipUnless(shutil.which("docker"), "needs docker compose")
    def test_compose_names_a_missing_mount_and_uses_the_ones_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shutil.copy(COMPOSE, home / "compose.yaml")
            (home / "site.env").touch()
            (home / ".env").write_text("TR_PASS=x\n")
            missing = subprocess.run(["docker", "compose", "config"], cwd=home, capture_output=True, text=True)
            if "is not a docker command" in missing.stderr or "unknown command" in missing.stderr:
                self.skipTest("needs the Compose plugin")
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("USHER_RECYCLE_BIN", missing.stderr)
            (home / ".env").write_text("".join(f"{v}=/made/up/{v.lower()}\n" for v in MOUNTS) + "USHER_VERSION=1.2.3\n")
            ok = subprocess.run(["docker", "compose", "config"], cwd=home, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            for var in MOUNTS:
                self.assertIn(f"source: /made/up/{var.lower()}", ok.stdout)
            self.assertIn("start_interval: 2s", ok.stdout)
            self.assertIn("image: ghcr.io/minegene-labs/usher:1.2.3", ok.stdout)


if __name__ == "__main__":
    unittest.main()
