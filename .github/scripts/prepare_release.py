"""Prepares a release: gives the collected changelog lines their version and sets the app's version to it.

Merged pull requests add their changelog lines under `## [Unreleased]`; nothing is released on a merge. The Release
workflow, run by hand with a version, calls this script on the tested commit. It
- checks the version: three numbers separated by dots (1.5.0), at least the version the app already has and newer than every release
  (tags `v<version>`), so a release can never be overwritten or a version go backwards;
- renames `## [Unreleased]` to `## [<version>]` and leaves a fresh, empty `## [Unreleased]` above it. With no lines
  under `[Unreleased]` the changelog stays as it is: a version with no section says it has no changes to notice;
- sets the version in the three files that must always agree (tests/test_versions.py checks them).

It only edits files in the working tree and reads the repository's tags; the workflow commits the result. The version
reaches it as an argument, never through a shell line built from an expression. tests/test_prepare_release.py has the
cases.

Usage: python3 .github/scripts/prepare_release.py <version> (run from the repository root)
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from usher.update_result import version_key  # noqa: E402  (the import needs the path above)

VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}", re.ASCII)
TAG = re.compile(r"v(\d+(?:\.\d+)*)", re.ASCII)
UNRELEASED = "## [Unreleased]"
# The line each file keeps its version on (the same lines .github/scripts/pr_gates.py treats as a version bump).
VERSION_LINES = {
    "usher/__init__.py": (re.compile(r'(?m)^__version__ = "[0-9.]+"$'), '__version__ = "{}"'),
    "web/sw.js": (re.compile(r'(?m)^const VERSION = "usher-[0-9.]+";$'), 'const VERSION = "usher-{}";'),
    "web/updater.js": (re.compile(r'(?m)^export const BUILD = "[0-9.]+";$'), 'export const BUILD = "{}";'),
}


def check_version(version: str, current: str, tags: list[str]) -> None:
    """Raises ValueError, saying why, unless `version` may be released: a version number at least as new as the app's
    own (`current`) and newer than every release among `tags`."""
    if not VERSION.fullmatch(version):
        raise ValueError(f"Not a version number: {version!r}. Use three numbers separated by dots, for example 1.5.0.")
    if version_key(version) < version_key(current):
        raise ValueError(f"{version} is older than the version the app already has ({current}).")
    released = [found[1] for tag in tags if (found := TAG.fullmatch(tag))]
    if released:
        newest = max(released, key=version_key)
        if version_key(version) <= version_key(newest):
            raise ValueError(f"{version} is not newer than the latest release ({newest}).")


def rename_unreleased(text: str, version: str) -> str:
    """The changelog with the lines under `## [Unreleased]` moved under `## [<version>]` and an empty `[Unreleased]`
    left above. A changelog with no lines there (or no such heading) comes back as it was."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == UNRELEASED), None)
    if start is None:
        return text
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    if not any(line.startswith("- ") for line in lines[start + 1 : end]):
        return text
    lines[start : start + 1] = [UNRELEASED, "", f"## [{version}]"]
    return "\n".join(lines)


def set_version(path: str, text: str, version: str) -> str:
    """`text` of the file at `path` with its one version line set to `version`."""
    pattern, template = VERSION_LINES[path]
    out, count = pattern.subn(template.format(version), text)
    if count != 1:
        raise ValueError(f"{path} has {count} version lines; expected exactly one.")
    return out


def current_version(root: Path) -> str:
    found = re.search(r'(?m)^__version__ = "([0-9.]+)"$', (root / "usher/__init__.py").read_text(encoding="utf-8"))
    if not found:
        raise ValueError("No __version__ found in usher/__init__.py.")
    return found[1]


def tags(root: Path) -> list[str]:
    done = subprocess.run(["git", "-C", str(root), "tag", "--list"], capture_output=True, text=True, encoding="utf-8", check=True)
    return done.stdout.split()


def prepare(root: Path, version: str, existing_tags: list[str]) -> bool:
    """Edits the files under `root`; whether the changelog had lines to release."""
    check_version(version, current_version(root), existing_tags)
    changelog = root / "CHANGELOG.md"
    before = changelog.read_text(encoding="utf-8")
    after = rename_unreleased(before, version)
    new = {path: set_version(path, (root / path).read_text(encoding="utf-8"), version) for path in VERSION_LINES}
    changelog.write_text(after, encoding="utf-8")
    for path, text in new.items():
        (root / path).write_text(text, encoding="utf-8")
    return after != before


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: prepare_release.py <version>", file=sys.stderr)
        return 2
    try:
        had_lines = prepare(ROOT, argv[1], tags(ROOT))
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"::error::Can't prepare the release of {argv[1][:40]!r}: {e}")
        return 1
    if not had_lines:
        print("::notice::Nothing is listed under [Unreleased], so the release notes say it has no changes to notice.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
