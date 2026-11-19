"""The setup page (web/setup.js): a server with no account yet swaps the sign-in page for it; every other server leaves
the sign-in page alone. A wrong code shows its message instead of looking like a sign-out, and a good one signs the new
admin in."""

import json
import shutil
import subprocess
import unittest

from tests.webjs import WEB

HARNESS = """
globalThis.Node = class {};
class El extends Node {
  constructor(tag) { super(); this.tag = tag; this.kids = []; this.attrs = {}; this.className = ""; this.on = {}; this.style = { setProperty() {} }; this.value = ""; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(n, f) { this.on[n] = f; }
  append(...k) { this.kids.push(...k.flat()); }
  replaceChildren(...k) { this.kids = k; }
  focus() {}
  select() {}
}
globalThis.document = { createElement: (t) => new El(t), createTextNode: (text) => ({ text }) };
globalThis.history = { replaceState() {} };
globalThis.AbortSignal = { timeout: () => undefined };
const calls = [];
let answers = {};
globalThis.fetch = async (path, init = {}) => {
  calls.push({ path, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null });
  const [status, body] = answers[path] || [404, { error: "Not found" }];
  return { status, ok: status < 400, json: async () => body };
};
const { Setup } = await import("%(setup)s");
const { Account } = await import("%(account)s");
const { lists, store } = await import("%(data)s");
const words = (n) => (n.text ?? "") + (n.kids || []).map(words).join(" ");
const walk = (n, f) => { if (!n || typeof n !== "object") return; f(n); (n.kids || []).forEach(k => walk(k, f)); };
const find = (n, test) => { let hit = null; walk(n, (x) => { if (!hit && test(x)) hit = x; }); return hit; };
const input = (app, name) => find(app, (x) => x.tag === "input" && x.attrs?.name === name);
class Base extends El { constructor() { super("usher-app"); this.started = 0; } start() { this.started++; } }
const make = () => new (Setup(Account(Base)))();
const out = {};
%(steps)s
console.log(JSON.stringify(out));
"""


def run(steps):
    code = HARNESS % {"setup": (WEB / "setup.js").as_uri(), "account": (WEB / "account.js").as_uri(), "data": (WEB / "data.js").as_uri(), "steps": steps}
    done = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@unittest.skipUnless(shutil.which("node"), "needs Node to run the client's code")
class SetupScreenTests(unittest.TestCase):
    def test_a_server_with_an_account_keeps_the_sign_in_page(self):
        out = run("""
const app = make();
app.signIn();
await new Promise((r) => setTimeout(r, 10));
out.heading = find(app, (x) => x.tag === "h1").kids[0].text;
out.asked = calls.map((c) => c.path);
out.fields = ["username", "password", "code"].map((n) => !!input(app, n));
""")
        self.assertEqual(out["heading"], "Usher")
        self.assertEqual(out["asked"], ["/api/setup"])
        self.assertEqual(out["fields"], [True, True, False])

    def test_a_server_with_no_account_shows_the_setup_page(self):
        out = run("""
answers["/api/setup"] = [200, { open: true }];
const app = make();
app.signIn();
await new Promise((r) => setTimeout(r, 10));
out.heading = find(app, (x) => x.tag === "h1").kids[0].text;
out.fields = ["code", "name", "username", "password"].map((n) => input(app, n)?.attrs.autocomplete ?? null);
out.password = input(app, "password").attrs.type;
""")
        self.assertEqual(out["heading"], "Set up Usher")
        self.assertEqual(out["fields"], ["off", "name", "username", "new-password"])
        self.assertEqual(out["password"], "password")

    def test_a_wrong_code_shows_its_message_and_is_not_a_sign_out(self):
        out = run("""
answers["/api/setup"] = [401, { error: "That setup code isn't right. It is in Usher's log." }];
const app = make();
let signedOut = 0;
app.signIn = () => { signedOut++; };
app.setup();
input(app, "code").value = "AAAA-AAAA-AAAA"; input(app, "username").value = "sam"; input(app, "password").value = "a made-up long password";
await find(app, (x) => x.tag === "form").on.submit({ preventDefault() {} });
out.error = find(app, (x) => x.attrs?.role === "alert").textContent;
out.signedOut = signedOut;
out.started = app.started;
out.button = find(app, (x) => x.tag === "button").disabled;
""")
        self.assertIn("setup code isn't right", out["error"])
        self.assertEqual((out["signedOut"], out["started"], out["button"]), (0, 0, False))

    def test_a_good_code_makes_the_account_and_opens_the_app(self):
        out = run("""
answers["/api/setup"] = [200, { username: "sam", name: "Sam", admin: true, csrf: "c" }];
const app = make();
app.setup();
input(app, "code").value = " aaaa-bbbb-cccc "; input(app, "name").value = "Sam"; input(app, "username").value = "sam"; input(app, "password").value = "a made-up long password";
await find(app, (x) => x.tag === "form").on.submit({ preventDefault() {} });
out.post = calls.find((c) => c.method === "POST");
out.started = app.started;
""")
        self.assertEqual(out["post"]["path"], "/api/setup")
        self.assertEqual(out["post"]["body"], {"code": "aaaa-bbbb-cccc", "name": "Sam", "username": "sam", "password": "a made-up long password"})
        self.assertEqual(out["started"], 1)

    def test_an_empty_form_asks_for_every_field_without_calling_the_server(self):
        out = run("""
const app = make();
app.setup();
await find(app, (x) => x.tag === "form").on.submit({ preventDefault() {} });
out.error = find(app, (x) => x.attrs?.role === "alert").textContent;
out.calls = calls.length;
""")
        self.assertIn("Fill in every field", out["error"])
        self.assertEqual(out["calls"], 0)


if __name__ == "__main__":
    unittest.main()
