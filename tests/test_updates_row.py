"""Settings → About → Updates: a read-only row that says in words whether the server is keeping up, in each of
the five situations, and reads what the server computed (web/updates.js)."""

import json
import re
import shutil
import subprocess
import unittest

from tests.webjs import WEB

SCRIPT = """
globalThis.Node = class {};
class El extends Node {
  constructor(tag) { super(); this.tag = tag; this.kids = []; this.attrs = {}; this.className = ""; this.style = { setProperty() {} }; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  append(...k) { this.kids.push(...k); }
  replaceChildren(...k) { this.kids = k; }
}
globalThis.document = { createElement: (t) => new El(t), createElementNS: (n, t) => new El(t), createTextNode: (t) => ({ text: t }) };
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.matchMedia = () => ({ matches: false });
const { Updates, updatesText } = await import("%(updates)s");
const { store } = await import("%(data)s");
const now = 1_000_000_000;
const at = (minutes) => now / 1000 - minutes * 60;
const views = {
  up_to_date: { state: "up_to_date", check: { newest: "1.0.0", at: at(4) } },
  just_now: { state: "up_to_date", check: { newest: "1.0.0", at: at(0) } },
  one_minute: { state: "up_to_date", check: { newest: "1.0.0", at: at(1) } },
  waiting: { state: "waiting", check: { newest: "1.1.0", at: at(2) } },
  held: { state: "held", check: { newest: "1.1.0", at: at(2) } },
  rolled_back: { state: "rolled_back", check: { newest: "1.1.0", at: at(2) } },
  unknown: { state: "unknown", check: { newest: "1.0.0", at: at(300) } },
  none: { state: "unknown", check: null },
  newer: { state: "unknown", check: null, release: { enabled: true, failed: false, newest: "1.1.0", at: at(30), available: true, notes: "https://example.test/notes", command: "example update" } },
  same: { state: "unknown", check: null, release: { enabled: true, failed: false, newest: "1.0.0", at: at(30), available: false, notes: "https://example.test/notes", command: "example update" } },
  unreachable: { state: "unknown", check: null, release: { enabled: true, failed: true, command: "example update" } },
  off: { state: "unknown", check: null, release: { enabled: false, failed: false, command: "example update" } },
  script_wins: { state: "up_to_date", check: { newest: "1.0.0", at: at(4) }, release: { enabled: true, failed: false, newest: "1.1.0", at: at(30), available: true, notes: "n", command: "c" } },
};
const text = Object.fromEntries(Object.entries(views).map(([k, v]) => [k, updatesText(v, now * 1)]));
const words = (n) => (n.text ?? "") + (n.kids || []).map(words).join("");
const draw = (view, error) => {
  const App = class extends Updates(class {}) {};
  const app = new App();
  const row = new El("div");
  app.fillUpdates(row, view, error);
  return { words: words(row), dot: row.kids[0].className, hidden: row.kids[0].attrs["aria-hidden"] };
};
const link = (n) => (n.tag === "a" ? [n.attrs.href] : []).concat((n.kids || []).flatMap(link));
const rows = { newer: draw(views.newer), newer_links: (() => { const r = new El("div"); new (class extends Updates(class {}) {})().fillUpdates(r, views.newer); return link(r); })(), waiting: draw(views.waiting), loading: draw(null), failed: draw(null, "Can't reach home") };
console.log(JSON.stringify({ text, rows }));
"""


class UpdatesRow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (WEB / "updates.js").read_text(encoding="utf-8")
        cls.out = None
        if shutil.which("node"):
            script = SCRIPT % {name: (WEB / f"{name}.js").as_uri() for name in ("updates", "data")}
            run = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, timeout=30)
            if run.returncode:
                raise AssertionError(run.stderr)
            cls.out = json.loads(run.stdout)

    def need_node(self):
        if self.out is None:
            self.skipTest("needs Node to run the client's code")

    def test_the_module_is_assembled_and_cached_offline(self):
        self.assertIn('from "./updates.js"', (WEB / "app.js").read_text(encoding="utf-8"))
        self.assertIn('"/updates.js"', (WEB / "sw.js").read_text(encoding="utf-8"))

    def test_it_sits_in_about_and_is_read_only(self):
        about = re.search(r"(?s)  aboutSection\(\) \{.*?\n  \}", (WEB / "settings.js").read_text(encoding="utf-8")).group(0)
        self.assertIn("this.updatesRow()", about)
        self.assertNotIn("onclick", re.search(r"(?s)  fillUpdates\(.*?\n  \}", self.source).group(0))
        self.assertIn('api("/api/updates")', self.source)

    def test_each_state_is_said_in_words(self):
        self.need_node()
        text = self.out["text"]
        self.assertEqual(text["up_to_date"], {"label": "Up to date", "detail": "Checked 4 minutes ago", "dot": ""})
        self.assertEqual(text["just_now"]["detail"], "Checked just now")
        self.assertEqual(text["one_minute"]["detail"], "Checked 1 minute ago")
        self.assertEqual(text["waiting"]["label"], "Update waiting")
        self.assertIn("is out and isn't installed yet", text["waiting"]["detail"])
        self.assertIn("1.1.0", text["waiting"]["detail"])
        self.assertEqual(text["held"]["label"], "Held")
        self.assertIn("needs a step on the server first", text["held"]["detail"])
        self.assertEqual(text["rolled_back"]["label"], "Last update rolled back")
        self.assertIn("didn't work, so the previous version is still running", text["rolled_back"]["detail"])
        self.assertEqual(text["unknown"]["label"], "Can't tell")
        self.assertEqual(text["none"]["label"], "Can't tell")
        self.assertIn("over two days", text["none"]["detail"])

    def test_a_newer_release_is_offered_with_its_notes_and_the_command(self):
        self.need_node()
        text = self.out["text"]
        self.assertEqual((text["newer"]["label"], text["newer"]["detail"]), ("Update available", "Version 1.1.0 is out"))
        self.assertEqual(text["same"]["label"], "Up to date")
        self.assertEqual(text["unreachable"]["label"], "Can't check right now")
        self.assertEqual(text["off"]["label"], "Not checking")
        self.assertEqual(text["script_wins"]["label"], "Up to date")  # an update script that reports in is believed first
        row = self.out["rows"]["newer"]["words"]
        self.assertIn("What's new in 1.1.0", row)
        self.assertIn("example update", row)
        self.assertEqual(self.out["rows"]["newer_links"], ["https://example.test/notes"])

    def test_wording_follows_the_interface_rules(self):
        self.need_node()
        for state, text in self.out["text"].items():
            line = f"{text['label']} {text['detail']}"
            self.assertNotRegex(line.lower(), r"please|sonarr|docker|compose|github", state)
            self.assertFalse(text["detail"].endswith("."), state)  # list rows carry no full stop, as the rest of Settings
            self.assertEqual(text["label"][0], text["label"][0].upper(), state)
            self.assertNotRegex(text["label"][1:], r"\b[A-Z][a-z]+\b", f"sentence case: {state}")

    def test_a_problem_is_never_only_a_colour(self):
        self.need_node()
        dots = {state: text["dot"] for state, text in self.out["text"].items()}
        self.assertEqual((dots["held"], dots["rolled_back"]), (" warn", " warn"))
        for state in ("held", "rolled_back"):
            self.assertTrue(self.out["text"][state]["label"])  # the label says it, the dot only adds to it

    def test_the_row_reads_the_state_with_a_hidden_dot(self):
        self.need_node()
        row = self.out["rows"]["waiting"]
        self.assertEqual(row["words"], "UpdatesUpdate waiting · Version 1.1.0 is out and isn't installed yet")
        self.assertEqual((row["dot"], row["hidden"]), ("dot off", "true"))

    def test_while_loading_and_when_the_server_cant_be_reached(self):
        self.need_node()
        self.assertEqual(self.out["rows"]["loading"]["words"], "UpdatesChecking…")
        self.assertEqual(self.out["rows"]["failed"]["words"], "UpdatesCan't reach home")


if __name__ == "__main__":
    unittest.main()
