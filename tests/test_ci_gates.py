"""The quality gates in CI: style, a coverage floor, pinned tools, and the weekly slow checks."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class PullRequestGateTests(unittest.TestCase):
    def setUp(self):
        self.tests_yml = _read(".github/workflows/tests.yml")

    def test_lint_and_format_run_in_the_tests_job(self):
        self.assertIn("ruff check .", self.tests_yml)
        self.assertIn("ruff format --check .", self.tests_yml)

    def test_suite_runs_under_coverage_and_reports_the_floor(self):
        self.assertIn("coverage run -m unittest discover -s tests -t .", self.tests_yml)
        self.assertIn("coverage report", self.tests_yml)
        self.assertRegex(_read("pyproject.toml"), r"(?m)^fail_under = \d+$")

    def test_tools_are_pinned_exactly(self):
        pins = [line for line in _read("requirements-dev.txt").splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(sorted(p.split("==")[0] for p in pins), ["actionlint-py", "coverage", "ruff", "shellcheck-py", "zizmor"])
        self.assertTrue(all(re.fullmatch(r"[\w-]+==\d+(\.\d+)+", p) for p in pins), pins)

    def test_dev_tools_stay_out_of_the_image(self):
        self.assertNotIn("requirements-dev", _read("Dockerfile"))

    def test_no_extra_job_on_pull_requests(self):
        jobs = re.search(r"(?m)^jobs:\n((?:[ #].*\n|\n)+)", self.tests_yml).group(1)
        self.assertEqual(re.findall(r"(?m)^  ([\w-]+):$", jobs), ["test", "promote"])


class WeeklyChecksTests(unittest.TestCase):
    def setUp(self):
        self.text = _read(".github/workflows/weekly-checks.yml")

    def test_runs_weekly_or_by_hand_never_on_pull_requests(self):
        on = re.search(r"(?m)^on:\n((?:[ #].*\n)+)", self.text).group(1)
        self.assertIn("schedule:", on)
        self.assertIn("workflow_dispatch:", on)
        self.assertNotIn("pull_request", on)
        self.assertNotIn("push:", on)

    def test_one_job_tests_two_pythons_and_checks_the_image(self):
        jobs = re.search(r"(?m)^jobs:\n((?:[ #].*\n|\n)+)", self.text).group(1)
        self.assertEqual(len(re.findall(r"(?m)^  ([\w-]+):$", jobs)), 1)
        self.assertEqual(self.text.count("python3 -m unittest discover -s tests -t ."), 2)
        self.assertIn("docker build", self.text)
        self.assertIn("/healthz", self.text)
        self.assertIn("python3 -m tests.mock_apps", self.text)

    def test_image_check_uses_made_up_settings_only(self):
        self.assertNotIn("secrets.", self.text)


if __name__ == "__main__":
    unittest.main()
