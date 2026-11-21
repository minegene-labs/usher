"""Preparing a release: the lines collected under `## [Unreleased]` get the version the maintainer gives, and the app's
version is set to it. The script is .github/scripts/prepare_release.py; it only edits files, so these cases run it on a
made-up copy of the three version files and a changelog. Made-up values only, and GitHub is never called."""

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from usher.changelog import parse

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "prepare_release.py"

spec = importlib.util.spec_from_file_location("prepare_release", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

LOG = """# Changelog

Intro.

## [Unreleased]

### Added
- A **Shelf** row on Home.

### Fixed
- Posters no longer flicker.
  A second line.

## [1.4.0]

### Fixed
- Older change.
"""
FILES = {
    "usher/__init__.py": '"""Usher."""\n\n__version__ = "1.4.2"\n',
    "web/sw.js": '// service worker\nconst VERSION = "usher-__USHER_VERSION__";\n',  # the server writes the version in: left alone
    "web/updater.js": '// updates\nexport const BUILD = "__USHER_VERSION__";\n',
}


def make_tree(folder, changelog=LOG):
    root = Path(folder)
    for path, text in {**FILES, "CHANGELOG.md": changelog}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    return root


class RenameTests(unittest.TestCase):
    def test_the_lines_move_under_the_version_and_a_fresh_unreleased_is_left(self):
        out = script.rename_unreleased(LOG, "1.5.0")
        self.assertIn("## [Unreleased]\n\n## [1.5.0]\n\n### Added\n- A **Shelf** row on Home.", out)
        self.assertEqual(out.count("## [Unreleased]"), 1)
        got = parse(out)
        self.assertEqual([v["version"] for v in got], ["1.5.0", "1.4.0"])
        self.assertEqual([c["text"] for c in got[0]["changes"]], ["A Shelf row on Home.", "Posters no longer flicker. A second line."])
        self.assertEqual([c["kind"] for c in got[0]["changes"]], ["Added", "Fixed"])
        self.assertEqual(out.split("## [1.4.0]", 1)[1], LOG.split("## [1.4.0]", 1)[1])  # older versions are untouched

    def test_two_merges_before_a_release_land_in_one_version(self):
        """The issue's own check: lines added by two pull requests are both in the release's notes."""
        merged = LOG.replace("### Fixed\n- Posters", "### Fixed\n- Second pull request's line.\n- Posters")
        out = script.rename_unreleased(merged, "1.5.0")
        self.assertEqual(len(parse(out)[0]["changes"]), 3)

    def test_no_lines_under_unreleased_leaves_the_changelog_alone(self):
        empty = LOG.replace(LOG[LOG.index("### Added") : LOG.index("## [1.4.0]")], "")
        self.assertEqual(script.rename_unreleased(empty, "1.5.0"), empty)

    def test_no_unreleased_heading_leaves_the_changelog_alone(self):
        without = LOG.replace("## [Unreleased]\n\n", "")
        self.assertEqual(script.rename_unreleased(without, "1.5.0"), without)

    def test_unreleased_at_the_end_of_the_file(self):
        only = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- One.\n"
        self.assertEqual(script.rename_unreleased(only, "2.0.0"), "# Changelog\n\n## [Unreleased]\n\n## [2.0.0]\n\n### Fixed\n- One.\n")


class VersionTests(unittest.TestCase):
    def check(self, version, current="1.4.2", tags=("v1.4.0", "v1.3.0")):
        script.check_version(version, current, list(tags))

    def test_a_newer_version_is_accepted(self):
        for version in ("1.4.2", "1.4.3", "1.5.0", "2.0.0", "10.0.0"):
            with self.subTest(version=version):
                self.check(version)

    def test_only_digits_and_dots_are_accepted(self):
        for bad in (
            "",
            "v1.5.0",
            "1.5.0; echo x",
            "$(id)",
            "1.5.0\n",
            "1..5",
            "1.5.",
            "Unreleased",
            "٢.0.0",
            "../1.5",
            "1.5",
            "2",
            "1.5.0.0",
            "1.05.0",
            "01.5.0",
        ):
            with self.subTest(version=bad):
                with self.assertRaises(ValueError):
                    self.check(bad)

    def test_a_version_older_than_the_apps_is_refused(self):
        with self.assertRaisesRegex(ValueError, r"older than the version the app already has \(1\.4\.2\)"):
            self.check("1.4.1")
        with self.assertRaises(ValueError):
            self.check("1.3.9")

    def test_a_version_that_is_already_released_or_older_than_a_release_is_refused(self):
        with self.assertRaisesRegex(ValueError, r"not newer than the latest release \(1\.4\.2\)"):
            self.check("1.4.2", tags=("v1.4.2", "v1.0.0"))
        with self.assertRaisesRegex(ValueError, r"latest release \(1\.10\.0\)"):
            self.check("1.9.0", current="1.0.0", tags=("v1.10.0", "v1.9.0", "v1.2.0"))  # numbers, not text, are compared

    def test_tags_that_are_not_releases_are_ignored(self):
        self.check("1.4.3", tags=("v1.4.0", "stable", "nightly-9.9.9", "v2.0.0-beta", "release-v8"))

    def test_the_version_line_is_set_and_nothing_else(self):
        out = script.set_version(FILES["usher/__init__.py"], "1.5.0")
        self.assertEqual(out, '"""Usher."""\n\n__version__ = "1.5.0"\n')

    def test_a_file_without_exactly_one_version_line_is_an_error(self):
        with self.assertRaises(ValueError):
            script.set_version("no version here\n", "1.5.0")
        with self.assertRaises(ValueError):
            script.set_version(FILES["usher/__init__.py"] * 2, "1.5.0")


class PrepareTests(unittest.TestCase):
    def test_edits_the_changelog_and_the_version_and_leaves_the_web_files_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_tree(folder)
            self.assertTrue(script.prepare(root, "1.5.0", ["v1.4.0"]))
            self.assertEqual(parse((root / "CHANGELOG.md").read_text(encoding="utf-8"))[0]["version"], "1.5.0")
            self.assertIn('__version__ = "1.5.0"', (root / "usher/__init__.py").read_text(encoding="utf-8"))
            for path in ("web/sw.js", "web/updater.js"):  # the server writes the version into these when it serves them
                self.assertEqual((root / path).read_text(encoding="utf-8"), FILES[path], path)

    def test_a_refused_version_changes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_tree(folder)
            with self.assertRaises(ValueError):
                script.prepare(root, "1.4.0", ["v1.4.0"])
            for path, text in {**FILES, "CHANGELOG.md": LOG}.items():
                self.assertEqual((root / path).read_text(encoding="utf-8"), text, path)

    def test_a_file_that_cant_be_edited_changes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_tree(folder)
            (root / "usher/__init__.py").write_text("no version here\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                script.prepare(root, "1.5.0", [])
            self.assertEqual((root / "CHANGELOG.md").read_text(encoding="utf-8"), LOG)
            self.assertEqual((root / "usher/__init__.py").read_text(encoding="utf-8"), "no version here\n")

    def test_with_nothing_unreleased_the_version_still_moves_and_it_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_tree(folder, "# Changelog\n\n## [Unreleased]\n\n## [1.4.0]\n\n### Fixed\n- Older.\n")
            self.assertFalse(script.prepare(root, "1.5.0", []))
            self.assertIn('__version__ = "1.5.0"', (root / "usher/__init__.py").read_text(encoding="utf-8"))
            self.assertNotIn("[1.5.0]", (root / "CHANGELOG.md").read_text(encoding="utf-8"))


class CommandLineTests(unittest.TestCase):
    """The script as the workflow runs it, in a made-up git repository, so the tags it reads are real."""

    def run_in(self, folder, *args):
        scripts = Path(folder) / ".github" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "prepare_release.py").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        (Path(folder) / "usher" / "update_result.py").write_text("def version_key(v):\n    return tuple(int(p) for p in v.split('.'))\n", encoding="utf-8")
        return subprocess.run([sys.executable, "-I", str(scripts / "prepare_release.py"), *args], capture_output=True, text=True, timeout=30, cwd=folder)

    def repo(self, folder, tags=()):
        root = make_tree(folder)
        (root / "usher" / "__init__.py").write_text(FILES["usher/__init__.py"], encoding="utf-8")
        git = ["git", "-c", "user.name=Example", "-c", "user.email=example@example.invalid", "-C", folder]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "chore: start"], check=True)
        for tag in tags:
            subprocess.run([*git, "tag", tag], check=True)

    def test_a_good_version_edits_the_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self.repo(folder, ["v1.4.0"])
            done = self.run_in(folder, "1.5.0")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn('__version__ = "1.5.0"', (Path(folder) / "usher/__init__.py").read_text(encoding="utf-8"))
            self.assertIn("## [Unreleased]\n\n## [1.5.0]", (Path(folder) / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_a_version_that_exists_is_refused_with_a_message_and_no_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            self.repo(folder, ["v1.5.0"])
            done = self.run_in(folder, "1.5.0")
            self.assertEqual(done.returncode, 1)
            self.assertRegex(done.stdout, r"^::error::Can't prepare the release.*not newer than the latest release \(1\.5\.0\)")
            self.assertIn('"1.4.2"', (Path(folder) / "usher/__init__.py").read_text(encoding="utf-8"))

    def test_a_hostile_version_is_shown_as_text_not_as_a_workflow_command(self):
        with tempfile.TemporaryDirectory() as folder:
            self.repo(folder)
            done = self.run_in(folder, "1.5.0\n::set-output name=x::y")
            self.assertEqual(done.returncode, 1)
            self.assertEqual(len(done.stdout.strip().splitlines()), 1)

    def test_nothing_unreleased_is_said_but_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            self.repo(folder)
            (Path(folder) / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
            done = self.run_in(folder, "1.5.0")
            self.assertEqual(done.returncode, 0)
            self.assertIn("::notice::Nothing is listed under [Unreleased]", done.stdout)

    def test_without_a_version_it_says_how_to_run_it(self):
        with tempfile.TemporaryDirectory() as folder:
            self.repo(folder)
            done = self.run_in(folder)
            self.assertEqual(done.returncode, 2)
            self.assertIn("Usage", done.stderr)


class RepositoryTests(unittest.TestCase):
    def test_the_repositorys_own_files_take_a_release(self):
        """The real version lines and changelog are shaped as the script expects, so a run in the workflow can't fail on that."""
        text = (ROOT / script.VERSION_FILE).read_text(encoding="utf-8")
        self.assertEqual(script.set_version(text, "99.0.0").count("99.0.0"), 1)
        self.assertRegex((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.compile(r"(?m)^## \[\d"))


if __name__ == "__main__":
    unittest.main()
