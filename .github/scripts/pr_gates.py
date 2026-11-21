"""What a pull request's text must say for what it changes. Tests runs this on every pull request except
Dependabot's; tests/test_pr_gates.py has the cases.

- A `Fixes #<n>` line, and a `Changes how a screen looks: yes` (or `no`) line, so a change to how a screen looks
  is checked on a phone.
- `Reviewer: ran` when the change touches a sensitive path (below) or changes more than LARGE lines: the diff was
  reviewed (CONTRIBUTING.md, Review).
- A change under tests/ when app code under usher/ or web/ changes, or a `Tests: not needed — <reason>` line.
- CHANGELOG.md says only what a person using or installing Usher would notice (CONTRIBUTING.md, Changelog). A change
  to usher/, web/, deploy/ or a Dockerfile adds a line to it, or carries a `Changelog: not needed — <reason>` line;
  a change to only .github/, tests/, .claude/ or documentation leaves it unchanged.

A pull request leaves the version alone: it is written once, in usher/__init__.py, and only the release changes it.
A change to web/sw.js or web/updater.js is sensitive and app code, so it needs `Reviewer: ran` and a test.

Usage: python3 .github/scripts/pr_gates.py BASE HEAD TEXT_FILE (run from the repository root)
"""

import re
import subprocess
import sys

LARGE = 300  # changed lines (added plus deleted) above which the reviewer is asked for
# Sign-in and sessions on the server (docs/architecture.md).
SIGN_IN = ("usher/auth.py", "usher/auth_routes.py", "usher/store.py", "usher/server.py", "usher/cli.py", "web/account.js")
# What keeps data on the device (Cache Storage, localStorage, sessionStorage, IndexedDB). A faulty change stays on
# every phone until a new version replaces it; a rollback can't reach it. The tests check this list against web/*.js.
ON_DEVICE = ("web/data.js", "web/startup.js", "web/sw.js", "web/updater.js", "web/util.js")
# The gate and the security settings are hard to undo too: a change could loosen the checks themselves.
SENSITIVE_DIRS = (".github/workflows/", ".github/scripts/", "deploy/")
SENSITIVE_FILES = (
    ".gitleaks.toml",
    ".github/zizmor.yml",
    ".github/dependabot.yml",
    ".github/accepted-vulnerabilities.toml",
    ".dockerignore",
    "requirements.txt",
    "requirements-dev.txt",
)
SENSITIVE_NAMES = re.compile(r"(^|/)(Dockerfile[^/]*|(docker-)?compose(\.[\w-]+)?\.ya?ml)$")
APP_DIRS = ("usher/", "web/")
CHANGELOG = "CHANGELOG.md"
# What a person using or installing Usher can notice: the app, the container and the server's files.
NOTICEABLE_DIRS = ("usher/", "web/", "deploy/")
NOTICEABLE_NAMES = re.compile(r"(^|/)Dockerfile[^/]*$")
# How the project is run, and its documentation: no line in the changelog.
QUIET_DIRS = (".github/", "tests/", ".claude/", ".githooks/", "tools/", "docs/")
QUIET_FILES = ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CLAUDE.md", "LICENSE")

FIXES = re.compile(r"(?im)^[ \t]*fixes #[0-9]+\b")
SCREEN = re.compile(r"(?im)^[ \t]*changes how a screen looks: (yes|no)\b")
REVIEWER = re.compile(r"(?m)^[ \t]*Reviewer: ran\b")
NO_TESTS = re.compile(r"(?m)^[ \t]*Tests: not needed[ \t]*[—–-]+[ \t]*\S")
NO_CHANGELOG = re.compile(r"(?m)^[ \t]*Changelog: not needed[ \t]*[—–-]+[ \t]*\S")


def is_sensitive(path):
    return path in SIGN_IN or path in ON_DEVICE or path in SENSITIVE_FILES or path.startswith(SENSITIVE_DIRS) or bool(SENSITIVE_NAMES.search(path))


def is_noticeable(path):
    return path.startswith(NOTICEABLE_DIRS) or bool(NOTICEABLE_NAMES.search(path))


def is_quiet(path):
    return path.startswith(QUIET_DIRS) or path in QUIET_FILES


def shown(path):
    """A path safe to print on one log line: nothing in it can start a workflow command."""
    return re.sub(r"[^\w./ -]", "?", path)


def problems(changed, text, changelog_added=None):
    """Messages for each line the text is missing. `changed` maps each changed path to its changed lines;
    `changelog_added` says whether CHANGELOG.md gained a line (by default: whether it changed at all)."""
    text = re.sub(r"<!--.*?-->", "", text.replace("\r", ""), flags=re.S)  # the template's instructions don't count
    found = []
    if not FIXES.search(text):
        found.append("No `Fixes #<n>` line: name the issue this pull request fixes.")
    if not SCREEN.search(text):
        found.append("No `Changes how a screen looks: yes` or `Changes how a screen looks: no` line: a change to how a screen looks is checked on a phone.")
    real = sorted(changed)
    sensitive = [p for p in real if is_sensitive(p)]
    total = sum(changed.values())
    if (sensitive or total > LARGE) and not REVIEWER.search(text):
        why = f"it touches {', '.join(shown(p) for p in sensitive)}" if sensitive else f"it changes {total} lines"
        found.append(f"No `Reviewer: ran` line, and {why}: have the diff reviewed (CONTRIBUTING.md, Review), then add the line.")
    app = [p for p in real if p.startswith(APP_DIRS)]
    if app and not any(p.startswith("tests/") for p in changed) and not NO_TESTS.search(text):
        found.append(
            f"It changes {shown(app[0])}"
            + (f" and {len(app) - 1} more app file(s)" if len(app) > 1 else "")
            + " but nothing under tests/: add a test, or a `Tests: not needed — <reason>` line."
        )
    if changelog_added is None:
        changelog_added = CHANGELOG in changed
    noticeable = [p for p in real if is_noticeable(p)]
    others = [p for p in real if p != CHANGELOG]
    if noticeable and not changelog_added and not NO_CHANGELOG.search(text):
        found.append(
            f"It changes {shown(noticeable[0])}"
            + (f" and {len(noticeable) - 1} more file(s) a person using Usher could notice" if len(noticeable) > 1 else "")
            + f" but adds no line to {CHANGELOG}: add one in plain words, or a `Changelog: not needed — <reason>` line"
            " for a change nobody would notice (CONTRIBUTING.md, Changelog)."
        )
    if CHANGELOG in changed and not noticeable and others and all(is_quiet(p) for p in others):
        found.append(
            f"It changes only files that describe how the project is run or its documentation ({shown(others[0])}"
            + (f" and {len(others) - 1} more" if len(others) > 1 else "")
            + f"), so {CHANGELOG} stays unchanged: it lists only what a person using or installing Usher would notice (CONTRIBUTING.md, Changelog)."
        )
    return found


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout


def changed_lines(base, head):
    """{path: added + deleted lines} between the merge base and head; a rename counts for both of its paths."""
    fields = git("diff", "--numstat", "-z", "-M", f"{base}...{head}").split("\0")
    changed, i = {}, 0
    while i < len(fields):
        if not fields[i]:
            i += 1
            continue
        added, deleted, path = fields[i].split("\t", 2)
        paths = [path] if path else fields[i + 1 : i + 3]  # -z writes a rename as an empty path, then old, then new
        i += 1 if path else 3
        lines = sum(int(n) for n in (added, deleted) if n != "-")  # "-" for a binary file
        for p in paths:
            changed[p] = changed.get(p, 0) + lines
    return changed


def changelog_gained_a_line(base, head):
    """Whether CHANGELOG.md has at least one added line between the merge base and head."""
    out = git("diff", "--numstat", f"{base}...{head}", "--", CHANGELOG).split()
    return bool(out) and out[0].isdigit() and int(out[0]) > 0


def main(argv):
    base, head, text_file = argv
    changed = changed_lines(base, head)
    with open(text_file, encoding="utf-8") as f:
        found = problems(changed, f.read(), changelog_gained_a_line(base, head))
    for message in found:
        print(f"::error::{message}")
    if not found:
        print("The pull request text has the lines this change needs.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
