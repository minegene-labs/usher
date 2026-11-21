"""The published image and the one generic compose file. Release, run by hand, builds the image for amd64 and arm64, pushes it with
the version and latest as tags and attests its build provenance; compose.yaml at the repository root runs that image
with nothing from the machine it runs on. The install folder's deploy/compose.yaml runs the same image at the version in USHER_VERSION.
The workflow and the files are read, not run: no registry is called."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_deploy_paths import ROOT
from tests.test_merge_report import _job, _permissions, _step_script

WORKFLOWS = ROOT / ".github" / "workflows"
COMPOSE = ROOT / "compose.yaml"
DOCKERFILE = ROOT / "Dockerfile"


def _release():
    return (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def _promote():
    return (WORKFLOWS / "promote.yml").read_text(encoding="utf-8")


def _service(text):
    return re.search(r"(?m)^  usher:\n((?:    .*\n|\n)+)", text).group(1)


class ReleaseImageTests(unittest.TestCase):
    def setUp(self):
        self.job = _job(_release(), "image")

    def test_builds_both_platforms_and_tags_the_version_and_latest(self):
        self.assertIn("--platform linux/amd64,linux/arm64", self.job)
        self.assertIn('imagetools create --tag "$IMAGE:$VERSION" --tag "$IMAGE:latest" "$IMAGE@$DIGEST"', self.job)
        self.assertIn("push-by-digest=true", self.job)
        self.assertIn("platforms: arm64", self.job)
        self.assertRegex(self.job, r"docker/setup-qemu-action@[0-9a-f]{40} # v\d")

    def test_image_name_comes_from_the_repository_in_lower_case(self):
        self.assertIn('name=ghcr.io/${GITHUB_REPOSITORY,,}', self.job)
        self.assertNotRegex(self.job, r"ghcr\.io/[a-z0-9-]+/[a-z0-9-]+\b")

    def test_provenance_is_attested_for_the_pushed_digest_and_stored_with_the_image(self):
        self.assertRegex(self.job, r"actions/attest-build-provenance@[0-9a-f]{40} # v\d")
        self.assertIn("subject-digest: ${{ steps.build.outputs.digest }}", self.job)
        self.assertIn("subject-name: ${{ steps.image.outputs.name }}", self.job)
        self.assertIn("push-to-registry: true", self.job)
        self.assertLess(self.job.index("buildx build"), self.job.index("attest-build-provenance"))
        # the tags come after the attestation, so a version tag means the image is complete and a re-run isn't skipped early
        self.assertLess(self.job.index("attest-build-provenance"), self.job.index("imagetools create"))
        self.assertNotIn("--tag", self.job.split("imagetools create")[0].split("buildx build")[1])
        self.assertIn('"containerimage.digest"', self.job)  # the digest comes from the build itself

    def test_a_version_that_has_its_image_builds_nothing(self):
        self.assertIn('imagetools inspect "$IMAGE:$VERSION"', self.job)
        for step in ("Set up emulation", "Build and publish", "Attest", "Tag the image"):
            found = re.search(rf"(?m)^      - name: {step}.*\n((?:        .*\n)+)", self.job)
            self.assertIn("steps.exists.outputs.publish == 'yes'", found.group(1), step)

    def test_only_not_found_means_build_anything_else_stops_the_job(self):
        check = re.search(r"(?m)^      - name: Check whether this version.*\n((?:        .*\n)+)", self.job).group(1)
        self.assertRegex(check, r'\*"not found"\*\) echo "publish=yes"')
        self.assertRegex(check, r"\*\) echo \"::error::Could not tell[^\n]*exit 1")

    def test_runs_only_after_a_published_release_with_only_its_own_permissions(self):
        self.assertRegex(self.job, r"(?m)^    needs: \[prepare, publish\]$")
        self.assertIn("needs.publish.result == 'success'", self.job)
        self.assertIn("ref: ${{ needs.prepare.outputs.sha }}", self.job)
        self.assertEqual(_permissions(self.job), {"contents": "read", "packages": "write", "id-token": "write", "attestations": "write"})
        self.assertIn("persist-credentials: false", self.job)
        self.assertNotIn("secrets.", self.job)
        self.assertNotIn("contents: write", self.job)

    def test_only_the_sign_in_step_handles_the_token(self):
        self.assertEqual(self.job.count("github.token"), 1)
        sign_in = re.search(r"(?m)^      - name: Sign in to the registry\n((?:        .*\n)+)", self.job).group(1)
        self.assertIn("TOKEN: ${{ github.token }}", sign_in)
        self.assertIn('--password-stdin', sign_in)  # never on a command line
        self.assertNotIn("GH_TOKEN", self.job)

    def test_the_other_jobs_cannot_publish_a_package(self):
        jobs = [(_release(), "prepare"), (_release(), "publish"), (_promote(), "promote"), (_promote(), "report")]
        for text, name in jobs:
            permissions = _permissions(_job(text, name))
            for scope in ("packages", "id-token", "attestations"):
                self.assertNotIn(scope, permissions, name)

    def test_a_merge_publishes_no_image(self):
        """The image is made by the Release workflow, run by hand; nothing a merge starts builds or tags one."""
        self.assertNotIn("buildx", _promote())
        self.assertNotIn("packages", _promote())

    def test_the_image_says_where_it_came_from(self):
        for label in ("source", "revision", "version", "licenses"):
            self.assertIn(f"org.opencontainers.image.{label}=", self.job)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class CheckStepTests(unittest.TestCase):
    """The check step's shell, run with a stand-in for docker that answers as the registry would."""

    def run_step(self, answer, code):
        script = _step_script(_release(), "Check whether this version already has its image")
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "bin").mkdir()
            stub = folder / "bin" / "docker"
            stub.write_text(f"#!/bin/sh\necho '{answer}'\nexit {code}\n", encoding="utf-8")
            stub.chmod(0o755)
            out = folder / "output"
            env = {
                "PATH": f"{folder / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                "GITHUB_OUTPUT": str(out),
                "IMAGE": "ghcr.io/example/usher",
                "VERSION": "2.0.0",
            }
            done = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, timeout=30)
            return done, out.read_text(encoding="utf-8") if out.exists() else ""

    def test_a_published_version_is_left_alone(self):
        done, output = self.run_step("Name: ghcr.io/example/usher:2.0.0", 0)
        self.assertEqual((done.returncode, output), (0, "publish=no\n"))

    def test_a_version_the_registry_doesnt_have_is_built(self):
        done, output = self.run_step("ghcr.io/example/usher:2.0.0: not found", 1)
        self.assertEqual((done.returncode, output), (0, "publish=yes\n"))

    def test_any_other_failure_stops_the_job_instead_of_building(self):
        done, output = self.run_step("error: denied", 1)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(output, "")
        self.assertIn("Could not tell whether ghcr.io/example/usher:2.0.0 exists", done.stdout)


class DockerfileDataFolderTests(unittest.TestCase):
    def test_data_folder_belongs_to_the_user_the_image_runs_as(self):
        """A named volume copies the owner of the folder it covers, so Usher can write to a new one."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        user = re.search(r"(?m)^USER (\S+)$", text).group(1)
        self.assertEqual(user, "1000:1001")
        self.assertRegex(text, rf"(?m)^RUN mkdir /data && chown {user} /data$")
        self.assertLess(text.index("chown"), text.index("USER 1000"))  # root makes it, then the image drops to the user


class ListensInsideTheContainerTests(unittest.TestCase):
    """The image listens on all addresses so the published port reaches it; who can reach that port is the compose
    file's decision, and a compose file that shares the host's network must not inherit the wide default."""

    def test_the_image_listens_on_all_addresses_by_default(self):
        self.assertRegex(DOCKERFILE.read_text(encoding="utf-8"), r"(?m)^    USHER_BIND=0\.0\.0\.0:8484$")

    def test_the_published_compose_file_leaves_the_default_alone_and_publishes_on_loopback_only(self):
        service = _service(COMPOSE.read_text(encoding="utf-8"))
        self.assertNotIn("USHER_BIND", service)
        self.assertEqual(re.findall(r'(?m)^      - "([^"]*:\d+:\d+)"$', service), ["127.0.0.1:8484:8484"])

    def test_the_host_networked_install_keeps_loopback(self):
        mine = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("    network_mode: host\n", mine)
        self.assertIn("      - USHER_BIND=127.0.0.1:8484\n", mine)

    def test_the_weekly_check_on_the_host_network_keeps_loopback(self):
        text = (ROOT / ".github" / "workflows" / "weekly-checks.yml").read_text(encoding="utf-8")
        run = re.search(r"docker run [^\n]*--network host(?:[^\n]*\\\n)*[^\n]*", text).group(0)
        self.assertIn("-e USHER_BIND=127.0.0.1:8484", run)


class GenericComposeTests(unittest.TestCase):
    def setUp(self):
        self.text = COMPOSE.read_text(encoding="utf-8")
        self.service = _service(self.text)

    def test_runs_the_published_image_not_a_build(self):
        self.assertRegex(self.service, r"(?m)^    image: ghcr\.io/[\w.-]+/usher:\$\{USHER_VERSION:-latest\}$")
        self.assertNotRegex(self.text, r"(?m)^\s+build:")
        self.assertNotRegex(self.text, r"(?m)^\s*(include|extends):")  # one self-contained file, no remote parts

    def test_port_is_reachable_from_this_machine_only(self):
        self.assertIn('      - "127.0.0.1:8484:8484"', self.service)
        self.assertNotIn("network_mode", self.service)
        self.assertEqual(re.findall(r'(?m)^      - "([^"]*:\d+:\d+)"$', self.service), ["127.0.0.1:8484:8484"])

    def test_keeps_the_hardening_of_the_maintainers_file(self):
        mine = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        for line in (
            '    user: "1000:1001"',
            "    read_only: true",
            "    tmpfs: [/tmp]",
            "    cap_drop: [ALL]",
            '    security_opt: ["no-new-privileges:true"]',
            "    mem_limit: 256m",
            "    pids_limit: 128",
        ):
            self.assertIn(line + "\n", self.service, line)
            self.assertIn(line + "\n", mine, line)  # the two files don't drift apart

    def test_mounts_one_named_volume_and_nothing_from_the_host(self):
        self.assertEqual(re.findall(r"(?m)^      - (\S+):/\S+$", self.service), ["usher-data"])
        self.assertRegex(self.text, r"(?m)^volumes:\n  usher-data:\n")
        self.assertNotIn("docker.sock", self.text)
        self.assertNotIn("env_file", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s+- [./~$]\S*:/")  # no folder or file of this machine
        self.assertNotRegex(self.text, r"/srv/|/opt/|/home/|/etc/|/var/")

    def test_settings_file_is_in_the_data_volume(self):
        self.assertIn("      - USHER_CONFIG=/data/usher.toml", self.service)

    def test_health_check_is_the_dockerfiles_with_a_quick_start(self):
        found = re.search(r"(?m)^HEALTHCHECK (.*?) \\\n\s*CMD (\[.*\])$", DOCKERFILE.read_text(encoding="utf-8"))
        image = dict(re.findall(r"--([a-z-]+)=(\S+)", found.group(1)))
        block = self.service.split("    healthcheck:\n", 1)[1]
        health = dict(re.findall(r"^      (\w+): (.+)$", re.match(r"(?:      .*\n)*", block).group(0), re.M))
        self.assertEqual(json.loads(health.pop("test")), ["CMD", *json.loads(found.group(2))])
        self.assertEqual(health.pop("start_interval"), "2s")
        self.assertEqual(health, {k.replace("-", "_"): v for k, v in image.items()})

    def test_reaches_apps_on_this_machine_by_a_name_not_by_host_networking(self):
        self.assertIn('      - "host.docker.internal:host-gateway"', self.service)

    def test_the_install_folders_file_runs_the_same_image_at_the_version_in_env(self):
        mine = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        image = re.search(r"(?m)^    image: (\S+)$", self.service).group(1)
        self.assertIn(f"    image: {image}\n", mine)  # the same name, and USHER_VERSION picks the version
        self.assertNotRegex(mine, r"(?m)^\s+build:")
        self.assertNotIn("usher:local", mine)

    @unittest.skipUnless(shutil.which("docker"), "needs docker compose")
    def test_compose_accepts_the_file_as_it_is_with_no_other_file_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(COMPOSE, Path(tmp) / "compose.yaml")
            run = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=tmp, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            service = json.loads(run.stdout)["services"]["usher"]
            self.assertTrue(service["image"].endswith("/usher:latest"))
            self.assertEqual([(p["host_ip"], p["target"]) for p in service["ports"]], [("127.0.0.1", 8484)])
            self.assertEqual([v["type"] for v in service["volumes"]], ["volume"])


if __name__ == "__main__":
    unittest.main()
