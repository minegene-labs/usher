"""The Dockerfile's base image names a Python minor version and an image digest, so two builds of the same
commit match and Dependabot has a version to update. The tests run on that same minor version."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FROM = re.compile(r"(?m)^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+\S+)?[ \t]*$")
TRAILING_COMMENT = re.compile(r"\s#")
PINNED = re.compile(r"python:(\d+\.\d+)-slim@sha256:[0-9a-f]{64}")


def base_images(dockerfile):
    return [m.group(1) for m in FROM.finditer(dockerfile)]


def base_minor(dockerfile):
    match = PINNED.fullmatch(base_images(dockerfile)[0])
    if not match:
        raise AssertionError("the base image has no minor version and digest")
    return match.group(1)


def trailing_comments(dockerfile):
    """Line numbers of instruction lines that end with a comment, which Docker reads as arguments."""
    return [
        number for number, line in enumerate(dockerfile.splitlines(), 1) if line.strip() and not line.lstrip().startswith("#") and TRAILING_COMMENT.search(line)
    ]


class BaseImageTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_every_base_image_has_a_minor_version_and_a_digest(self):
        images = base_images(self.dockerfile)
        self.assertTrue(images)
        for image in images:
            with self.subTest(image=image):
                self.assertRegex(image, PINNED.pattern)

    def test_the_check_catches_a_moving_tag_and_a_missing_digest(self):
        digest = "a" * 64
        self.assertIsNone(PINNED.fullmatch("python:3-slim"))
        self.assertIsNone(PINNED.fullmatch("python:3.13-slim"))
        self.assertIsNone(PINNED.fullmatch(f"python:3-slim@sha256:{digest}"))
        self.assertIsNone(PINNED.fullmatch("python:3.13-slim@sha256:abc"))
        self.assertIsNotNone(PINNED.fullmatch(f"python:3.13-slim@sha256:{digest}"))
        self.assertEqual(base_images("FROM python:3-slim\nRUN true\n"), ["python:3-slim"])

    def test_the_version_is_in_a_comment_above_from(self):
        lines = self.dockerfile.splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
        self.assertTrue(lines[index - 1].startswith("#"))
        self.assertIn(f"Python {base_minor(self.dockerfile)}", lines[index - 1])

    def test_no_instruction_ends_with_a_comment(self):
        self.assertEqual(trailing_comments(self.dockerfile), [])

    def test_the_check_catches_a_comment_after_an_instruction(self):
        self.assertEqual(trailing_comments("FROM python:3.13-slim # Python 3.13\n"), [1])
        self.assertEqual(trailing_comments("# note\nRUN true # why\n"), [2])
        self.assertEqual(trailing_comments("# only a comment\nFROM a\n"), [])

    def test_the_tests_run_on_the_images_minor_version(self):
        minor = base_minor(self.dockerfile)
        for name in ("tests.yml", "weekly-checks.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertIn(f'python-version: "{minor}"', text)


if __name__ == "__main__":
    unittest.main()
