"""The report Promote posts after each merge, once `stable` has moved, so nobody has to check a merge by hand.

Reads the merged pull request, its squash commit, `stable` and the open issues from GitHub's REST API and prints a
report: a verdict line (`OK` or `PROBLEM`), then one line per check. The promote workflow posts it as a comment on the
pull request and in the job summary; a PROBLEM also opens or updates one issue and turns the run red.
tests/test_merge_report.py has the cases. A merge makes no release (the Release workflow is run by hand), so no check
looks at tags or images.

- `promote` finished: when it failed, `stable` may not have moved.
- The version in usher/__init__.py differs from the one before the merge (it is the build number that makes a device
  drop its saved copy of the app). A merge that changes only documentation, `.claude/` or `.github/` files needs no
  new version, and Dependabot's updates keep it.
- `stable` is at the merge commit, or past it when a later merge was promoted since (information).
- A merge that is superseded, with `stable` past it (a later merge was promoted while its Tests run waited, so
  promote's fast-forward push was refused), is reported as information: its promote job isn't counted against it, so
  no problem issue is opened. Every other check still counts.
- Every issue the description fixes (`Fixes #<n>`) is closed.
- Which changed files need a person's eye (workflows, Dockerfile, deploy/, .claude/, web/sw.js, web/data.js): shown,
  never a problem by itself. A change to web/sw.js that only moves its version line isn't shown.
- Every open issue a maintainer has had time to triage has a priority label (P1, P2 or P3). Issues the repository
  opens by itself (labelled upstream, upstream-beta, upstream-release or vulnerabilities) and issues opened in the
  last 24 hours are left out.

Dependabot's updates keep the version and fix no issue, so those lines say so instead.

Usage: python3 .github/scripts/merge_report.py SHA (GH_TOKEN and GITHUB_REPOSITORY in the environment; PROMOTE, the
result of Promote's promote job, defaults to success)
"""

import base64
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

PROBLEM_TITLE = "Merge problem: PR #"
DEPENDABOT = "dependabot[bot]"
PRIORITIES = {"P1", "P2", "P3"}
PROBLEM_LABELS = {"bug", "P1"}
# Issues the repository's own workflows open, which nobody has triaged yet.
UNTRIAGED_LABELS = {"upstream", "upstream-beta", "upstream-release", "vulnerabilities"}
TRIAGE_TIME = datetime.timedelta(hours=24)
FLAGGED_DIRS = (".github/workflows/", "deploy/", ".claude/")
FLAGGED_FILES = ("web/sw.js", "web/data.js")
DOCKERFILE = re.compile(r"(^|/)Dockerfile[^/]*$")
# What a new version is for: the app, its container (the Dockerfile also copies requirements.txt and CHANGELOG.md, which
# the app shows) and the tests that cover them. A phone keeps its saved copy of the app until the version changes.
VERSIONED_DIRS = ("usher/", "web/", "deploy/", "tests/")
VERSIONED_FILES = ("requirements.txt", "CHANGELOG.md")
SW_VERSION_LINE = re.compile(r'const VERSION = "usher-[0-9.]+";')
VERSION = re.compile(r'(?m)^__version__ = "([0-9]+(?:\.[0-9]+)*)"$')
# The same words Auto-merge closes issues by.
FIXES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):? +#([0-9]+)\b")
SHOWN_FILES = 20


def shown(path):
    """A path safe to print in Markdown and on a log line."""
    return re.sub(r"[^\w./ -]", "?", path)


def issue_list(numbers):
    return ", ".join(f"#{n}" for n in numbers)


# Each check returns (status, line): "ok" and "info" lines leave the verdict OK, a "problem" line makes it PROBLEM.


def superseded(merge):
    """Whether a later merge was promoted while this one's Tests run waited: `stable` is past the merge commit, which
    refuses promote's fast-forward push."""
    return merge["stable"] == "ahead"


def check_promote(merge):
    result = merge["promote"]
    if result == "success":
        return "ok", "The promote job finished."
    if superseded(merge):
        return "info", f"The promote job didn't finish ({shown(result)}), but `stable` is already past the merge commit: a later merge was promoted."
    return "problem", f"The promote job didn't finish ({shown(result)}): `stable` may not have moved."


def changes_the_app(files):
    """Whether any changed file, at its new or its old path (a rename), is part of the app, its container or its tests."""
    paths = [p for c in files for p in (c["filename"], c.get("previous_filename") or "")]
    return any(p.startswith(VERSIONED_DIRS) or p in VERSIONED_FILES or DOCKERFILE.search(p) for p in paths)


def check_version(merge):
    """The version is a build number too: a device keeps its saved copy of the app until it changes. A release, made by
    hand, gives the collected changes a version of its own."""
    version, before = merge["version"], merge["previous_version"]
    if not version:
        return "problem", "No version found in usher/__init__.py at the merge commit."
    if version != before:
        return "ok", f"The version changed to {version}."
    if merge["dependabot"]:
        return "info", f"The version stays {version}: Dependabot's updates keep it."
    if not changes_the_app(merge["files"]):
        return "ok", f"The version stays {version}: this merge changes no app code, so no new version is needed."
    return "problem", f"The version is still {version}: it wasn't bumped, so devices may keep their saved copy of the app."


def check_stable(merge):
    status = merge["stable"]
    if status == "identical":
        return "ok", "`stable` is at the merge commit."
    if status == "ahead":
        return "info", "`stable` is past the merge commit: a later merge was promoted since."
    return "problem", f"`stable` is not at the merge commit ({shown(status)})."


def fixed_issues(body):
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)  # the template's instructions don't count
    return sorted({int(n) for n in FIXES.findall(body)})


def check_issues(merge):
    states = merge["issues"]
    if merge["dependabot"] and not states:
        return "info", "Fixed issue: none expected on Dependabot's updates."
    if not states:
        return "problem", "The description names no issue with a `Fixes #<n>` line."
    wrong = []
    for label in ("open", "missing", "pull request"):
        numbers = [n for n, state in sorted(states.items()) if state == label]
        if numbers:
            wrong.append(f"{label}: {issue_list(numbers)}")
    if wrong:
        return "problem", f"Not every issue it fixes is closed ({'; '.join(wrong)})."
    numbers = sorted(states)
    return "ok", f"{'The issue it fixes is' if len(numbers) == 1 else 'The issues it fixes are'} closed: {issue_list(numbers)}."


def is_version_bump(patch):
    """Whether every line a web/sw.js patch adds or removes is its version line."""
    if not patch:
        return False  # GitHub leaves the patch out of very large diffs: show the file
    edits = [line[1:] for line in patch.splitlines() if line[:1] in "+-"]
    return bool(edits) and all(SW_VERSION_LINE.fullmatch(line) for line in edits)


def is_flagged(change):
    path = change["filename"]
    if path == "web/sw.js" and is_version_bump(change.get("patch")):
        return False
    return path in FLAGGED_FILES or path.startswith(FLAGGED_DIRS) or bool(DOCKERFILE.search(path))


def check_files(merge):
    flagged = sorted({c["filename"] for c in merge["files"] if is_flagged(c)})
    if not flagged:
        return "info", "Files flagged: none."
    listed = ", ".join(f"`{shown(p)}`" for p in flagged[:SHOWN_FILES])
    more = f" and {len(flagged) - SHOWN_FILES} more" if len(flagged) > SHOWN_FILES else ""
    return "info", f"Files flagged (workflows, Dockerfile, deploy/, .claude/, web/sw.js or web/data.js): {listed}{more}."


def check_priorities(merge):
    missing = merge["unlabelled"]
    if missing:
        return "problem", f"Open issues without a priority label (P1, P2 or P3), opened over a day ago: {issue_list(missing)}."
    return "ok", "Every open issue has a priority label (P1, P2 or P3), leaving out the repository's own and the past day's."


def labels_of(issue):
    return {label["name"] for label in issue.get("labels") or []}


def needs_priority(issue, now):
    """Whether a maintainer should have given the open issue a priority label by now."""
    labels = labels_of(issue)
    if labels & PRIORITIES or labels & UNTRIAGED_LABELS:
        return False
    created = datetime.datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
    return now - created >= TRIAGE_TIME


def is_problem_issue(issue):
    """The one issue a PROBLEM opens: its title and both its labels, so no other issue is updated or closed."""
    return str(issue.get("title", "")).startswith(PROBLEM_TITLE) and PROBLEM_LABELS <= labels_of(issue)


CHECKS = (check_promote, check_version, check_stable, check_issues, check_files, check_priorities)


def report(merge):
    """(verdict, text): the verdict line first, then one line per check."""
    results = [check(merge) for check in CHECKS]
    problems = sum(1 for status, _ in results if status == "problem")
    where = f"the merge of #{merge['number']} ({merge['sha'][:12]})"
    if problems:
        verdict, head = "PROBLEM", f"PROBLEM: {where} needs attention: {problems} check(s) failed."
    else:
        verdict, head = "OK", f"OK: {where} passed every check."
    lines = [head, ""] + [f"- **{status}**: {line}" for status, line in results]
    return verdict, "\n".join(lines) + "\n"


def version_at(api, ref):
    """The version in usher/__init__.py at `ref`, or None."""
    init = api.get(f"contents/usher/__init__.py?ref={ref}")
    found = VERSION.search(base64.b64decode(init["content"]).decode("utf-8")) if init else None
    return found.group(1) if found else None


def collect(api, sha, promote="success", now=None):
    """Everything the checks read about the merge whose squash commit is `sha`; None when no pull request was
    merged as that commit (a push straight to main). `promote` is the result of Promote's promote job."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pulls = api.get(f"commits/{sha}/pulls") or []
    pull = next((p for p in pulls if p.get("merge_commit_sha") == sha and p.get("merged_at")), None)
    if pull is None:
        return None
    number = int(pull["number"])
    body = pull.get("body") or ""
    version, previous_version = version_at(api, sha), None
    commit = api.get(f"commits/{sha}")
    parents = (commit or {}).get("parents") or []
    if parents:
        previous_version = version_at(api, parents[0]["sha"])
    compare = api.get(f"compare/{sha}...stable")
    issues = {}
    for n in fixed_issues(body):
        issue = api.get(f"issues/{n}")
        issues[n] = "missing" if issue is None else "pull request" if "pull_request" in issue else issue["state"]
    open_issues = [i for i in api.pages("issues?state=open") if "pull_request" not in i]
    return {
        "number": number,
        "sha": sha,
        "promote": promote,
        "dependabot": (pull.get("user") or {}).get("login") == DEPENDABOT,
        "body": body,
        "version": version,
        "previous_version": previous_version,
        "stable": compare["status"] if compare else "missing",
        "issues": issues,
        "files": [
            {"filename": f["filename"], "previous_filename": f.get("previous_filename"), "patch": f.get("patch")} for f in api.pages(f"pulls/{number}/files")
        ],
        "unlabelled": sorted(i["number"] for i in open_issues if needs_priority(i, now)),
        "problem_issue": next((i["number"] for i in open_issues if is_problem_issue(i)), None),
    }


class GitHub:
    """GitHub's REST API for one repository, read-only."""

    def __init__(self, repo, token, base="https://api.github.com"):
        self.root = f"{base.rstrip('/')}/repos/{repo}/"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "usher-merge-report",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _fetch(self, url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=self.headers), timeout=30) as response:
            return json.load(response), response.headers.get("Link", "")

    def get(self, path):
        """The answer for `path`, or None when GitHub has no such thing (404)."""
        try:
            return self._fetch(self.root + path)[0]
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise RuntimeError(f"GitHub answered {error.code} for {path}") from None  # the message, never the headers

    def pages(self, path):
        """Every item of a list, following GitHub's `next` links."""
        url, items = self.root + path + ("&" if "?" in path else "?") + "per_page=100", []
        while url:
            page, links = self._fetch(url)
            items += page
            found = re.search(r'<([^>]+)>;\s*rel="next"', links)
            url = found.group(1) if found else None
        return items


def main(argv):
    if len(argv) != 1 or not re.fullmatch(r"[0-9a-f]{40}", argv[0]):
        raise SystemExit(__doc__)
    sha = argv[0]
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"], os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    merge = collect(api, sha, os.environ.get("PROMOTE") or "success")
    if merge is None:
        verdict, text, outputs = "", f"No pull request was merged as {sha[:12]}, so there is no merge to report.\n", {}
    else:
        verdict, text = report(merge)
        outputs = {"number": merge["number"], "verdict": verdict, "open_issue": merge["problem_issue"] or ""}
    sys.stdout.write(text)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.writelines(f"{key}={value}\n" for key, value in outputs.items())


if __name__ == "__main__":
    main(sys.argv[1:])
