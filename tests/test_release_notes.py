"""A release's notes are its section of CHANGELOG.md, read with the parser Settings → About → Changelog uses. The
builder is .github/scripts/release_notes.py; the workflow gives the token only to the step that creates the
release. Made-up values only, and GitHub is never called."""

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from usher.changelog import parse

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "release_notes.py"
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

LOG = """# Changelog

## [Unreleased]

### Added
- Not out yet.

## [2.0.0]

### Added
- A **Shelf** row on Home. It has `two` lines
  that wrap onto a second line.
- Details are [explained here](docs/x.md).

### Fixed
- Posters no longer flicker.

## [1.9.0]

## [1.8.0]

### Changed
- Older change.
"""


class NotesTests(unittest.TestCase):
    def test_a_version_with_added_and_fixed_entries(self):
        self.assertEqual(
            script.notes("2.0.0", LOG),
            "### Added\n\n"
            "- A Shelf row on Home. It has two lines that wrap onto a second line.\n"
            "- Details are explained here.\n\n"
            "### Fixed\n\n"
            "- Posters no longer flicker.\n",
        )

    def test_the_words_are_the_apps_own(self):
        """Same parser as the Changelog screen, so the page can't differ from the app."""
        shown = [c["text"] for v in parse(LOG) if v["version"] == "2.0.0" for c in v["changes"]]
        body = script.notes("2.0.0", LOG)
        self.assertEqual(re.findall(r"(?m)^- (.*)$", body), shown)

    def test_a_version_with_an_empty_section_has_no_changes_to_notice(self):
        self.assertEqual(script.notes("1.9.0", LOG), script.NO_CHANGES + "\n")

    def test_a_version_without_a_heading_has_no_changes_to_notice(self):
        for version in ("3.0.0", "2.0", "1"):
            with self.subTest(version=version):
                self.assertEqual(script.notes(version, LOG), script.NO_CHANGES + "\n")

    def test_the_unreleased_section_is_never_a_release(self):
        self.assertEqual(script.notes("0", LOG), script.NO_CHANGES + "\n")
        self.assertNotIn("Not out yet", script.notes("2.0.0", LOG))

    def test_only_a_version_number_is_accepted(self):
        for bad in ("", "2.0.0; echo x", "$(id)", "v2.0.0", "2.0.0\n", "../2.0.0", "Unreleased"):
            with self.subTest(version=bad):
                with self.assertRaises(ValueError):
                    script.notes(bad, LOG)

    def test_the_repositorys_own_changelog_reads(self):
        """A version that really has entries: plain words, no Markdown marks, one line per entry."""
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        entries = [c["text"] for v in parse(text) if v["version"] == "1.0.0" for c in v["changes"]]
        self.assertTrue(entries)
        body = script.notes("1.0.0", text)
        self.assertEqual(re.findall(r"(?m)^- (.*)$", body), entries)
        self.assertTrue(body.startswith("### Added\n\n- Usher is a Home Screen web app"))
        self.assertNotRegex(body, r"[`*]|\]\(")

    def test_digits_other_than_ascii_are_not_a_version(self):
        with self.assertRaises(ValueError):
            script.notes("\u0662.0.0", LOG)


class CommandLineTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, timeout=30, cwd=ROOT)

    def test_it_writes_the_file_for_a_version_with_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "notes.md"
            done = self.run_script("1.0.0", str(target))
            self.assertEqual(done.returncode, 0, done.stderr)
            written = target.read_text(encoding="utf-8")
            self.assertTrue(written.startswith("### Added\n\n- Usher is a Home Screen web app"))
            self.assertEqual(written, script.notes("1.0.0", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")))

    def test_a_bad_version_writes_nothing_and_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "notes.md"
            done = self.run_script("1.0; rm -rf x", str(target))
            self.assertEqual(done.returncode, 1)
            self.assertFalse(target.exists())

    def test_without_arguments_it_says_how_to_run_it(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("Usage", done.stderr)


def _step(name):
    found = re.search(rf"(?m)^      - name: {re.escape(name)}\n((?:        .*\n|\n)+)", RELEASE)
    assert found, f"no step {name}"
    return found.group(1)


class WorkflowTests(unittest.TestCase):
    def test_the_notes_are_built_without_the_token(self):
        build = _step("Build the release notes from CHANGELOG.md")
        self.assertIn('python3 .github/scripts/release_notes.py "$v" "$RUNNER_TEMP/release-notes.md"', build)
        self.assertNotIn("GH_TOKEN", build)
        self.assertNotIn("github.token", build)
        job_settings = RELEASE.split("  report:", 1)[0].split("    steps:", 1)[0]
        self.assertNotRegex(job_settings, r"GH_TOKEN|github\.token")  # not on the whole job either

    def test_only_the_create_step_has_the_token(self):
        publish = _step("Publish a release for a new version")
        self.assertIn("GH_TOKEN: ${{ github.token }}", publish)
        self.assertIn('--notes-file "$RUNNER_TEMP/release-notes.md"', publish)
        promote = RELEASE.split("  image:", 1)[0]  # the promote job alone: the image job has its own token step
        self.assertEqual(promote.count("github.token"), 1)
        self.assertNotIn("GH_TOKEN", _step("Move stable to the tested commit (fast-forward only)"))

    def test_the_version_reaches_the_shell_as_a_variable(self):
        publish = _step("Publish a release for a new version")
        self.assertIn("VERSION: ${{ steps.notes.outputs.version }}", publish)
        run = publish.split("        run: |\n", 1)[1]
        self.assertNotIn("${{", run)


if __name__ == "__main__":
    unittest.main()
