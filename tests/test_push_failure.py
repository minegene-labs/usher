"""A pushed screen that fails to draw must not leave the stack set: the next tap on the account button opens Settings again."""

import json
import shutil
import subprocess
import unittest

from tests.webjs import WEB

SCRIPT = """
import { Shell } from "%(shell)s";
import { Settings } from "%(settings)s";
import { store } from "%(data)s";
class El {
  constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.classList = { toggle() {} }; }
  append(...k) { this.kids.push(...k.flat()); }
  replaceChildren(...k) { this.kids = k; }
  setAttribute(n, v) { this.attrs[n] = v; }
  addEventListener() {}
  querySelector() { return null; }
}
globalThis.Node = El;
globalThis.document = { title: "", createElement: (tag) => new El(tag), createElementNS: (ns, tag) => new El(tag), createTextNode: (text) => ({ text }), querySelectorAll: () => [] };
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
globalThis.matchMedia = () => ({ matches: true });
const entries = [{ state: {} }];
let at = 0;
globalThis.history = {
  get state() { return entries[at].state; },
  pushState(state) { entries.splice(at + 1, entries.length, { state }); at++; },
  back() { at--; },
};
console.error = () => {};
let broken = true;
const toasts = [];
const Parts = Shell(Settings(class {
  querySelectorAll() { return []; }
  statusShape() { return ""; }
  status() { return new El("div"); }
  toast(text) { toasts.push(text); }
}));
// The sections of Settings are not what is under test: one of them fails to draw, then draws.
const App = class extends Parts {
  settingsGroups() { if (broken) throw new Error("a section failed"); return []; }
};
store.me = { name: "Sam" };
const app = new App();
app.main = new El("main");
app.main.scrollTo = () => {};
app.show("/status", false);
const out = {};
app.openSettings();
out.failed = { stack: app.pushed, depth: at, toasts: [...toasts], drawn: app.main.kids.length > 0 };
broken = false;
app.openSettings();
out.retry = { stack: app.pushed, state: history.state.pushed || [] };
console.log(JSON.stringify(out));
"""


class PushFailure(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "needs Node to run the client's code")
    def test_a_failed_draw_resets_the_stack_and_a_later_tap_works(self):
        script = SCRIPT % {"shell": (WEB / "shell.js").as_uri(), "settings": (WEB / "settings.js").as_uri(), "data": (WEB / "data.js").as_uri()}
        run = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual(out["failed"]["stack"], [], "the stack is reset, so the next tap is not ignored")
        self.assertEqual(out["failed"]["depth"], 0, "the history entry of the failed push is taken back")
        self.assertEqual(out["failed"]["toasts"], ["Couldn't open Settings."])
        self.assertTrue(out["failed"]["drawn"], "Status is still drawn")
        self.assertEqual(out["retry"]["stack"], ["settings"])
        self.assertEqual(out["retry"]["state"], ["settings"])


if __name__ == "__main__":
    unittest.main()
