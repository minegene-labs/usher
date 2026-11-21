"""The second line of a list row wraps (up to three lines); the title and poster captions stay on one line."""

import re
import unittest
from pathlib import Path

CSS = Path("web/app.css").read_text(encoding="utf-8")


def rule(selector):
    match = re.search(r"^" + re.escape(selector) + r" \{([^}]*)\}", CSS, re.MULTILINE)
    assert match, f"no rule for {selector}"
    return match.group(1)


class RowWrapTests(unittest.TestCase):
    def test_sub_line_wraps_to_at_most_three_lines(self):
        sub = rule(".row .sub")
        self.assertNotIn("nowrap", sub)
        self.assertIn("-webkit-line-clamp: 3", sub)

    def test_title_stays_on_one_line(self):
        self.assertIn("white-space: nowrap", rule(".row .title"))

    def test_row_columns_keep_the_text_shrinkable(self):
        self.assertIn("min-width: 0", rule(".row .text"))
        self.assertIn("grid-template-columns: auto 1fr", rule(".row"))

    def test_poster_captions_are_left_alone(self):
        for selector in re.findall(r"^\.card \.(?:title|sub)\b[^{]*", CSS, re.MULTILINE):
            self.assertNotIn("line-clamp: 3", selector)
