"""Decides whether the Release workflow makes a release now, and which version.

A hand run (`workflow_dispatch`) passes the version typed for it straight through: the maintainer has decided. The daily
scheduled run releases only when everything planned for the next release is done, and says which condition was not
met when it isn't (the run still ends green):

- no open issue is labelled `next-release` or `P1`, apart from those labelled `hold`;
- something is collected under `## [Unreleased]` in CHANGELOG.md;
- the latest commit on `main` is at least 24 hours old, so a count that is briefly zero (one issue closed just before
  the next is filed) can't start a release, which is permanent;
- no pull request merged since the last release carries `needs-server`: a change to the image build, the compose file or
  the update script never reaches a server without a person looking at it first;
- `main` is the commit `stable` holds (the tested one) and no other Release run is in progress.

The version is worked out, never typed: the minor number goes up when the collected changes include anything under
Added, otherwise the patch number does; a major version is never chosen here. It starts from the newer of the app's
version and the latest release, and `prepare_release.py` checks it again.

Reads issues, pull requests and runs from GitHub's REST API (read-only) and the checkout's git history. Writes
`release` (yes or no) and `version` to GITHUB_OUTPUT. The hand-typed version is checked here before it is written there,
so nothing but a version number reaches a later step. tests/test_release_gate.py has the cases.

Usage: python3 .github/scripts/release_gate.py (run from the repository root, with GH_TOKEN, GITHUB_REPOSITORY,
GITHUB_RUN_ID, EVENT_NAME and, for a hand run, TYPED_VERSION in the environment)
"""

import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from merge_report import GitHub  # noqa: E402  (the imports need the paths above)
from prepare_release import UNRELEASED, VERSION, check_version, current_version  # noqa: E402
from usher.update_result import version_key  # noqa: E402

PLANNED_LABELS = ("next-release", "P1")
QUIET_TIME = datetime.timedelta(hours=24)
TAG = re.compile(r"v(\d+\.\d+\.\d+)", re.ASCII)
WORKFLOW = "release.yml"


def labels(issue):
    return {label["name"] for label in issue.get("labels", [])}


def open_planned(issues):
    """Numbers of the open issues (not pull requests) that stand in the way: planned for the next release or P1, and not on hold."""
    return sorted(i["number"] for i in issues if "pull_request" not in i and labels(i) & set(PLANNED_LABELS) and "hold" not in labels(i))


def merged_needing_a_server_step(items, since):
    """Numbers of the pull requests among `items` (issues, as GitHub lists them) merged after `since` (a timestamp, or None for ever)."""
    found = []
    for item in items:
        merged = (item.get("pull_request") or {}).get("merged_at")
        if merged and (since is None or parse_time(merged) > since):
            found.append(item["number"])
    return sorted(found)


def parse_time(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def collected(changelog_text):
    """(whether any line is collected under [Unreleased], whether one of them is under Added)."""
    lines = changelog_text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == UNRELEASED), None)
    if start is None:
        return False, False
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section, kind, any_lines, added = lines[start + 1 : end], "", False, False
    for line in section:
        if line.startswith("### "):
            kind = line[4:].strip()
        elif line.startswith("- "):
            any_lines = True
            added = added or kind == "Added"
    return any_lines, added


def newest(tags):
    """The newest released version among the tags (`v<major>.<minor>.<patch>`), or None."""
    found = [m[1] for tag in tags if (m := TAG.fullmatch(tag))]
    return max(found, key=version_key) if found else None


def next_version(current, tags, added):
    """The next version: from the newer of the app's own and the latest release, the minor number up when something
    was added, otherwise the patch number."""
    released = newest(tags)
    base = max([current, released] if released else [current], key=version_key)
    major, minor, patch = version_key(base)
    version = f"{major}.{minor + 1}.0" if added else f"{major}.{minor}.{patch + 1}"
    check_version(version, current, tags)
    return version


def unmet(facts, now):
    """The conditions not met, each as a sentence; none means a release may be made. `facts` is what was read."""
    reasons = []
    if facts["planned"]:
        reasons.append("Open issues are planned for the next release (labelled next-release or P1, not on hold): " + issue_list(facts["planned"]) + ".")
    if not facts["has_lines"]:
        reasons.append("Nothing is collected under [Unreleased] in CHANGELOG.md.")
    age = now - facts["last_merge"]
    if age < QUIET_TIME:
        reasons.append(f"The latest commit on main is {int(age.total_seconds() // 3600)} hours old; it has to be at least 24 hours old.")
    if facts["needs_server"]:
        reasons.append("Pull requests merged since the last release carry needs-server: " + issue_list(facts["needs_server"]) + ".")
    if not facts["tested"]:
        reasons.append("main is not at stable yet: the Tests run of the latest merge has not finished, or it failed.")
    if facts["running"]:
        reasons.append("Another Release run is in progress: " + ", ".join(str(n) for n in facts["running"]) + ".")
    return reasons


def issue_list(numbers):
    return ", ".join(f"#{n}" for n in numbers)


def git(*args):
    done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8", check=True)
    return done.stdout.strip()


def tags():
    return git("tag", "--list").split()


def read_facts(api, run_id, existing_tags):
    """What the conditions depend on, read from GitHub and the checkout."""
    planned = []
    for label in PLANNED_LABELS:
        planned += api.pages(f"issues?state=open&labels={label}")
    released = newest(existing_tags)
    since = datetime.datetime.fromtimestamp(int(git("log", "-1", "--format=%ct", f"v{released}^{{commit}}")), datetime.timezone.utc) if released else None
    query = "issues?state=closed&labels=needs-server" + (f"&since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}" if since else "")
    runs = api.get(f"actions/workflows/{WORKFLOW}/runs?per_page=30")["workflow_runs"]
    has_lines, added = collected((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    return {
        "planned": open_planned({i["number"]: i for i in planned}.values()),
        "has_lines": has_lines,
        "added": added,
        "last_merge": datetime.datetime.fromtimestamp(int(git("log", "-1", "--format=%ct", "HEAD")), datetime.timezone.utc),
        "needs_server": merged_needing_a_server_step(api.pages(query), since),
        "tested": git("rev-parse", "HEAD") == git("rev-parse", "origin/stable"),
        "running": sorted(r["id"] for r in runs if r["status"] != "completed" and str(r["id"]) != run_id),
    }


def decide(event, typed, api, run_id, now):
    """(version or None, the lines to log)."""
    if event == "workflow_dispatch":
        if not VERSION.fullmatch(typed):
            raise ValueError(f"Not a version number: {typed[:40]!r}. Use three numbers separated by dots, for example 1.5.0.")
        return typed, [f"Run by hand: releasing {typed}."]
    if event != "schedule":
        raise ValueError(f"Release doesn't start on {event[:40]!r}.")
    existing_tags = tags()
    facts = read_facts(api, run_id, existing_tags)
    reasons = unmet(facts, now)
    if reasons:
        return None, ["No release made:"] + [f"- {reason}" for reason in reasons]
    version = next_version(current_version(ROOT), existing_tags, facts["added"])
    return version, [f"Everything planned is done: releasing {version} ({'something was added' if facts['added'] else 'fixes and changes only'})."]


def main():
    api = GitHub(
        os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"], os.environ.get("GITHUB_API_URL", "https://api.github.com"), agent="usher-release-gate"
    )
    try:
        version, lines = decide(
            os.environ["EVENT_NAME"], os.environ.get("TYPED_VERSION", ""), api, os.environ["GITHUB_RUN_ID"], datetime.datetime.now(datetime.timezone.utc)
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        print(f"::error::Can't decide on a release: {e}")
        return 1
    print("\n".join(lines))
    if version is None:
        print(f"::notice::{lines[0]} {' '.join(line[2:] for line in lines[1:])}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"release={'yes' if version else 'no'}\nversion={version or ''}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
