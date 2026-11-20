"""Settings is a screen pushed over Status, opened from the account button at the top right of Status or, on a wide screen, the
sidebar's footer. Its back button returns to Status, a live update leaves it alone, and text about the phone's own settings says
"the Settings app" so the two aren't confused."""

import json
import re
import shutil
import subprocess
import unittest

from tests.webjs import WEB

HARNESS = """
globalThis.Node = class {};
class El extends Node {
  constructor(tag) { super(); this.tag = tag; this.kids = []; this.attrs = {}; this.className = ""; this.on = {};
    this.style = { setProperty() {} }; this.classList = { toggle() {} }; this.scrollTop = 0; this.isConnected = true; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(n, f) { this.on[n] = f; }
  append(...k) { this.kids.push(...k.flat()); }
  appendChild(k) { this.kids.push(k); return k; }
  replaceChildren(...k) { this.kids = k; }
  querySelector() { return null; }
  scrollTo() {}
}
globalThis.document = { title: "", createElement: (t) => new El(t), createElementNS: (n, t) => new El(t),
  createTextNode: (text) => ({ text }), querySelectorAll: () => [], querySelector: () => null };
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.matchMedia = () => ({ matches: true });
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
const listeners = {};
globalThis.EventSource = class { constructor() { this.readyState = 1; } addEventListener(n, f) { listeners[n] = f; } close() {} };
const entries = [{ state: {} }];
let at = 0;
globalThis.history = {
  get state() { return entries[at].state; },
  pushState(state) { entries.splice(at + 1, entries.length, { state }); at++; },
  back() { at--; },
};
const { Shell } = await import("%(shell)s");
const { Settings } = await import("%(settings)s");
const { Changelog } = await import("%(changelog)s");
const { store } = await import("%(data)s");
const heading = (tag, text) => { const el = new El(tag); el.kids.push({ text }); return el; };
class Base {
  querySelectorAll() { return []; }
  unreadCount() { return 0; }
  downloadsUnseen() { return 0; }
  status() { return new El("div"); }
  notifySection() { return new El("div"); }
  advancedSection() { const d = new El("details"); d.kids.push(heading("h2", "Advanced")); return d; }
  updatesRow() { return new El("div"); }
  loadChangelog() {}
}
const app = new (Shell(Changelog(Settings(Base))))();
app.main = new El("main");
store.online = true;
store.me = { name: "Sam", username: "sam", version: "0.0.0", csrf: "x", devices: [
  { device: "Phone", current: true, lastSeen: 1 }, { device: "Tablet", current: false, lastSeen: 1 }] };
store.snap = { loading: false, status: { ok: true, parts: [{ name: "TV", ok: true, text: "fine" }] } };
const words = (n) => (n.text ?? "") + (n.kids || []).map(words).join(" ");
const walk = (n, f) => { if (!n || typeof n !== "object") return; f(n); (n.kids || []).forEach((k) => walk(k, f)); };
const navbar = () => app.main.kids[0];
const content = () => app.main.kids[1].kids[1];
const back = () => words(navbar()).trim().split(/\\s+/)[0];
const out = {};
%(steps)s
console.log(JSON.stringify(out));
"""


STUBS = """  notifySection() { return new El("div"); }
  advancedSection() { const d = new El("details"); d.kids.push(heading("h2", "Advanced")); return d; }
  updatesRow() { return new El("div"); }
"""
REAL_IMPORTS = """const { Notify } = await import("%(notify)s");
const { Advanced } = await import("%(advanced)s");
const { Updates } = await import("%(updates)s");
const { Connect } = await import("%(connect)s");
"""


def run_app(steps, real=False):
    """Runs the shell with the Settings and Changelog screens in Node; `steps` fills `out`, which comes back.
    With `real`, Settings is drawn with the app's real Notifications, Updates, Advanced and Connect screens, not stubs."""
    names = ("shell", "settings", "changelog", "data", "notify", "advanced", "updates", "connect")
    harness = HARNESS
    if real:
        harness = harness.replace(STUBS, "").replace("Shell(Changelog(Settings(Base)))", "Shell(Changelog(Connect(Settings(Notify(Advanced(Updates(Base)))))))")
        harness = harness.replace("const { store }", REAL_IMPORTS + "const { store }")
    script = harness % {**{name: (WEB / f"{name}.js").as_uri() for name in names}, "steps": steps}
    run = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True, text=True, timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


LIVE_UPDATE = """
app.show("/status", false);
app.listen();
let renders = 0;
const render = app.render.bind(app);
app.render = (full) => { renders++; render(full); };
const send = (ok) => listeners.state({ data: JSON.stringify({ changes: 1, status: { ok, parts: [{ name: "TV", ok, text: ok ? "fine" : "down" }] } }) });
send(false);
app.listLoaded("tidy");
out.onStatus = renders;
app.openSettings();
const page = app.main.kids[1];
renders = 0;
send(true);
app.listLoaded("tidy");
out.onSettings = renders;
out.samePage = app.main.kids[1] === page;
out.stack = app.pushed;
"""

NAVIGATION = """
app.show("/status", false);
let account = null;
walk(navbar(), (n) => { if (n.tag === "button" && n.attrs["aria-label"] === "Signed in as Sam, opens Settings") account = n; });
out.account = account && { text: words(account).trim(), hidden: [] };
walk(account, (n) => { if (n.className === "avatar") out.account.hidden.push(n.attrs["aria-hidden"]); });
account.on.click();
out.stack = app.pushed;
out.title = document.title;
out.back = back();
out.heads = [];
walk(content(), (n) => { if (n.tag === "h2") out.heads.push(words(n)); });
const last = content().kids.at(-1);
let signOut = null;
walk(last, (n) => { if (n.tag === "button") signOut = n; });
out.lastGroup = { text: words(last).trim(), red: signOut.className.split(" ").includes("danger") };
let accountOnSettings = false;
walk(navbar(), (n) => { if (n.attrs?.["aria-label"]?.endsWith("opens Settings")) accountOnSettings = true; });
out.accountOnSettings = accountOnSettings;
app.popScreen();
app.show("/status", false);   // what the browser's back does
out.backToStatus = { stack: app.pushed, title: document.title };
"""


NO_LETTERS = """
store.me = { ...store.me, name: "--" };
app.show("/status", false);
let account = null;
walk(navbar(), (n) => { if (n.tag === "button" && n.attrs["aria-label"]?.endsWith("opens Settings")) account = n; });
let glyph = false;
walk(account, (n) => { if (n.tag === "svg") glyph = true; });
out.fallback = { text: words(account).trim(), glyph, label: account.attrs["aria-label"] };
"""

SIDEBAR = """
app.replaceChildren = (...kids) => { out.shell = kids[0]; };
app.show("/library", false);
app.layout();
let who = null;
walk(out.shell, (n) => { if (n.className === "who") who = n; });
out.who = who && { tag: who.tag, label: who.attrs["aria-label"], text: words(who).trim() };
who.on.click();
out.current = app.current;
out.stack = app.pushed;
out.title = document.title;
out.back = back();
who.on.click();
out.stackAfterSecondTap = app.pushed;
app.popScreen();
app.show("/status", false);   // what the browser's back does
out.backToStatus = { current: app.current, stack: app.pushed, title: document.title };
"""

REAL_SECTIONS = """
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { userAgent: "iPhone", platform: "iPhone", maxTouchPoints: 5,
  standalone: true, serviceWorker: { ready: new Promise(() => {}) } } });
globalThis.PushManager = class {};
globalThis.Notification = { permission: "default" };
globalThis.fetch = () => Promise.reject(new Error("offline"));
store.me.connectShortcut = "https://example.test/shortcut";
const openSettings = () => {
  app.show("/status", false);
  let account = null;
  walk(navbar(), (n) => { if (n.attrs?.["aria-label"]?.endsWith("opens Settings")) account = n; });
  account.on.click();
  return app.pushed.slice();
};
out.first = openSettings();
app.popScreen();
app.notifyLast = { data: { prefs: { ready: true, problem: false, space: true, server: false }, devices: [{ endpoint: "e" }] }, here: true };
out.second = openSettings();
out.heads = [];
walk(content(), (n) => { if (n.tag === "h2" || n.tag === "h3") out.heads.push(words(n)); });
let shortcut = false;
walk(content(), (n) => { if (words(n).trim() === "Add the Connect shortcut Connects Tailscale, then reopens Usher") shortcut = true; });
out.shortcut = shortcut;
"""


@unittest.skipUnless(shutil.which("node"), "needs Node to run the client's code")
class SettingsScreen(unittest.TestCase):
    def test_a_live_update_leaves_an_open_settings_screen_alone(self):
        out = run_app(LIVE_UPDATE)
        self.assertGreaterEqual(out["onStatus"], 1, "Status itself still redraws when its facts change")
        self.assertEqual(out["stack"], ["settings"])
        self.assertEqual(out["onSettings"], 0, "a live update must not redraw an open Settings screen")
        self.assertTrue(out["samePage"])

    def test_the_account_button_opens_settings_with_its_groups_in_order_and_back_returns_to_status(self):
        out = run_app(NAVIGATION)
        self.assertEqual(out["account"], {"text": "S", "hidden": ["true"]}, "Status has a button at the top right showing the initials, named for VoiceOver")
        self.assertEqual(out["stack"], ["settings"])
        self.assertEqual(out["title"], "Settings · Usher")
        self.assertEqual(out["back"], "Status")
        self.assertEqual(out["heads"], ["Account", "Device", "Devices", "About", "Advanced"])
        self.assertEqual(out["lastGroup"], {"text": "Sign out", "red": True})
        self.assertFalse(out["accountOnSettings"], "the account button is only on Status")
        self.assertEqual(out["backToStatus"], {"stack": [], "title": "Status · Usher"})

    def test_settings_opens_with_the_real_sections_the_first_time_and_again_with_notifications_known(self):
        out = run_app(REAL_SECTIONS, real=True)
        self.assertEqual(out["first"], ["settings"], "a throw while drawing would leave Status showing")
        self.assertEqual(out["second"], ["settings"])
        self.assertEqual(out["heads"][:3], ["Account", "Device", "Notifications"])
        self.assertTrue(out["shortcut"], "the Connect shortcut row, offered on iPhone when the server has a link")


@unittest.skipUnless(shutil.which("node"), "needs Node to run the client's code")
class AccountButtonFallback(unittest.TestCase):
    def test_a_name_with_no_letters_shows_a_person_glyph_and_keeps_its_label(self):
        out = run_app(NO_LETTERS)
        self.assertEqual(out["fallback"], {"text": "", "glyph": True, "label": "Signed in as --, opens Settings"})


class SettingsWaysIn(unittest.TestCase):
    def test_the_account_button_is_in_the_status_navigation_bar_with_no_gear_left(self):
        shell = (WEB / "shell.js").read_text(encoding="utf-8")
        self.assertIn('settings: "Settings"', shell)
        self.assertIn('class: "nav-action"', shell)
        self.assertIn("this.openSettings()", shell)
        self.assertNotIn('icon("gear")', shell)
        self.assertNotIn("gear:", (WEB / "dom.js").read_text(encoding="utf-8"))
        self.assertIn("person:", (WEB / "dom.js").read_text(encoding="utf-8"), "the glyph shown when a name has no letters")

    def test_the_back_button_style_is_shared(self):
        app_css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn(".navbar .nav-back", app_css)
        self.assertIn(".navbar .nav-action", app_css)
        self.assertNotIn(".nav-back", (WEB / "changelog.css").read_text(encoding="utf-8"))


class ScreensKeepTheirMethodsApart(unittest.TestCase):
    def test_no_two_screens_define_a_method_of_the_same_name(self):
        """The screens are mixed into one class, so a later one silently replaces an earlier one's method of the same name."""
        app = (WEB / "app.js").read_text(encoding="utf-8")
        files = {m.group(1): m.group(2) for m in re.finditer(r'import \{ (\w+) \} from "\./(\w+)\.js"', app)}
        listed = re.search(r"const screens = \[(.*?)\]", app).group(1).replace(" ", "").split(",")
        seen = {}
        for name in listed:
            source = (WEB / f"{files[name]}.js").read_text(encoding="utf-8")
            for method in re.findall(r"^  (?:async )?(\w+)\(.*\) \{$", source, re.M):
                self.assertNotIn(method, seen, f"{method}() is defined in both {seen.get(method)} and {name}")
                seen[method] = name


class PhoneSettingsAreTheSettingsApp(unittest.TestCase):
    def test_notification_help_names_the_settings_app(self):
        notify = (WEB / "notify.js").read_text(encoding="utf-8")
        self.assertNotIn("in Settings →", notify)
        self.assertGreaterEqual(notify.count("the Settings app"), 2)


if __name__ == "__main__":
    unittest.main()

    def test_the_sidebar_footer_is_a_button_that_opens_settings_over_status(self):
        out = run_app(SIDEBAR)
        self.assertEqual(out["who"]["tag"], "button", "reachable by keyboard and VoiceOver")
        self.assertEqual(out["who"]["text"], "S Signed in as Sam", "the initials, then the line")
        self.assertIn("Settings", out["who"]["label"])
        self.assertEqual(out["current"], "/status", "from another screen, Status comes first")
        self.assertEqual(out["stack"], ["settings"])
        self.assertEqual(out["title"], "Settings · Usher")
        self.assertEqual(out["back"], "Status")
        self.assertEqual(out["stackAfterSecondTap"], ["settings"], "tapping it again does not stack Settings twice")
        self.assertEqual(out["backToStatus"], {"current": "/status", "stack": [], "title": "Status · Usher"})
