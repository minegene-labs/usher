"""The Connect your apps step (web/connect.js) that follows the first account: an address and key for each app, Test says
what the server found, Save keeps only the apps that connected, and Skip leaves it for later. A server that can't
describe the apps goes straight to the app."""

import shutil
import unittest

from tests.test_setup_screen import run

APPS = """
const apps = [
  { id: "sonarr", label: "Sonarr", credential: "API key", find: "Open Settings, then General.", optional: false, hasAddress: true, setting: "pvr.tv.url", secret: "SONARR_API_KEY", userSetting: "", url: "http://127.0.0.1:8989", user: "", hasUser: false, keySaved: false, suggestions: ["http://sonarr:8989"] },
  { id: "transmission", label: "Transmission", credential: "password", find: "Its Web interface login.", optional: false, hasAddress: true, setting: "download_client.url", secret: "TR_PASS", userSetting: "download_client.user", url: "http://127.0.0.1:9091/transmission/rpc", user: "viewer", hasUser: true, keySaved: true, suggestions: [] },
  { id: "tmdb", label: "TMDb", credential: "access token", find: "Settings, then API.", optional: true, hasAddress: false, setting: "", secret: "TMDB_TOKEN", userSetting: "", url: "", user: "", hasUser: false, keySaved: false, suggestions: [] },
];
answers["/api/connect"] = [200, { apps }];
const all = (n, test) => { const hits = []; walk(n, (x) => { if (test(x)) hits.push(x); }); return hits; };
const section = (app, id) => all(app, (x) => x.tag === "section")[["sonarr", "transmission", "tmdb"].indexOf(id)];
const button = (node, label) => find(node, (x) => x.tag === "button" && (x.textContent || words(x)) === label);
const result = (node) => find(node, (x) => x.attrs?.role === "status");
const click = (node) => node.on.click({});
"""


def steps(body):
    return APPS + body


@unittest.skipUnless(shutil.which("node"), "needs Node to run the client's code")
class ConnectScreenTests(unittest.TestCase):
    def test_the_step_follows_the_new_account_and_waits_for_a_choice(self):
        out = run(
            steps("""
answers["/api/setup"] = [200, { username: "sam", name: "Sam", admin: true, csrf: "c" }];
const app = make();
app.setup();
input(app, "code").value = "aaaa-bbbb-cccc"; input(app, "username").value = "sam"; input(app, "password").value = "a made-up long password";
await find(app, (x) => x.tag === "form").on.submit({ preventDefault() {} });
out.heading = find(app, (x) => x.tag === "h1").kids[0].text;
out.apps = all(app, (x) => x.tag === "h2").map((x) => words(x).replace(/\\s+/g, " ").trim());
out.started = app.started;
""")
        )
        self.assertEqual(out["heading"], "Connect your apps")
        self.assertEqual(out["apps"], ["Sonarr", "Transmission", "TMDb (optional)"])
        self.assertEqual(out["started"], 0)

    def test_what_was_found_is_filled_in_and_a_saved_key_is_never_shown(self):
        out = run(
            steps("""
const app = make();
await app.connectApps();
const sonarr = section(app, "sonarr"); const tr = section(app, "transmission");
out.url = input(sonarr, "sonarr-url").attrs.value;
out.options = all(sonarr, (x) => x.tag === "option").map((x) => x.attrs.value);
out.keyType = input(sonarr, "sonarr-key").attrs.type;
out.placeholder = input(tr, "transmission-key").attrs.placeholder;
out.keyValue = input(tr, "transmission-key").attrs.value ?? null;
out.user = input(tr, "transmission-user").attrs.value;
out.tmdbAddress = !!input(section(app, "tmdb"), "tmdb-url");
""")
        )
        self.assertEqual(out["url"], "http://sonarr:8989")
        self.assertEqual(out["options"], ["http://sonarr:8989"])
        self.assertEqual(out["keyType"], "password")
        self.assertIn("saved", out["placeholder"])
        self.assertIsNone(out["keyValue"])
        self.assertEqual(out["user"], "viewer")
        self.assertFalse(out["tmdbAddress"])

    def test_test_sends_the_address_and_key_and_shows_the_answer(self):
        out = run(
            steps("""
answers["/api/connect/test"] = [200, { app: "sonarr", url: "http://sonarr:8989", verdict: "supported", version: "4.0.9", message: "Connected. Sonarr 4.0.9 is supported." }];
const app = make();
await app.connectApps();
const sonarr = section(app, "sonarr");
input(sonarr, "sonarr-key").value = "made-up-key";
await click(button(sonarr, "Test"));
out.post = calls.find((c) => c.path === "/api/connect/test");
out.message = result(sonarr).textContent;
out.class = result(sonarr).className;
""")
        )
        self.assertEqual(out["post"]["body"], {"app": "sonarr", "url": "", "key": "made-up-key", "user": ""})
        self.assertEqual(out["message"], "Connected. Sonarr 4.0.9 is supported.")
        self.assertEqual(out["class"], "connect-result supported")

    def test_a_problem_shows_its_words_and_a_refused_address_shows_the_servers(self):
        out = run(
            steps("""
const app = make();
await app.connectApps();
const sonarr = section(app, "sonarr");
answers["/api/connect/test"] = [200, { verdict: "bad_key", message: "Sonarr didn't accept that API key. Check it and try again." }];
await click(button(sonarr, "Test"));
out.bad = [result(sonarr).textContent, result(sonarr).className];
answers["/api/connect/test"] = [400, { error: "An address starts with http:// or https://" }];
await click(button(sonarr, "Test"));
out.refused = [result(sonarr).textContent, result(sonarr).className];
""")
        )
        self.assertEqual(out["bad"], ["Sonarr didn't accept that API key. Check it and try again.", "connect-result problem"])
        self.assertEqual(out["refused"], ["An address starts with http:// or https://", "connect-result problem"])

    def test_not_tested_yet_is_a_connection_with_a_note(self):
        out = run(
            steps("""
answers["/api/connect/test"] = [200, { verdict: "untested", message: "Connected. Sonarr 5.0.1 works, but Usher hasn't been tested with this version yet." }];
const app = make();
await app.connectApps();
const sonarr = section(app, "sonarr");
await click(button(sonarr, "Test"));
out.class = result(sonarr).className;
""")
        )
        self.assertEqual(out["class"], "connect-result untested")

    def test_save_keeps_only_the_apps_that_connected_and_then_continues(self):
        out = run(
            steps("""
answers["/api/connect/test"] = [200, { app: "sonarr", url: "http://sonarr:8989", verdict: "supported", version: "4.0.9", message: "ok" }];
answers["/api/settings"] = [200, { ok: true }];
const app = make();
await app.connectApps();
const sonarr = section(app, "sonarr");
input(sonarr, "sonarr-key").value = " made-up-key ";
await click(button(sonarr, "Test"));
await click(button(app, "Save"));
out.post = calls.find((c) => c.path === "/api/settings").body;
out.note = find(app, (x) => x.className === "connect-note").textContent;
out.started = app.started;
await click(button(app, "Continue"));
out.startedAfter = app.started;
""")
        )
        self.assertEqual(out["post"], {"settings": {"pvr.tv.url": "http://sonarr:8989"}, "secrets": {"SONARR_API_KEY": "made-up-key"}})
        self.assertIn("Saved", out["note"])
        self.assertEqual((out["started"], out["startedAfter"]), (0, 1))

    def test_a_blank_key_keeps_the_saved_one_and_a_failed_test_saves_nothing(self):
        out = run(
            steps("""
answers["/api/connect/test"] = [200, { app: "transmission", url: "http://127.0.0.1:9091/transmission/rpc", verdict: "supported", message: "ok" }];
answers["/api/settings"] = [200, { ok: true }];
const app = make();
await app.connectApps();
await click(button(section(app, "transmission"), "Test"));
await click(button(app, "Save"));
out.post = calls.find((c) => c.path === "/api/settings").body;
""")
        )
        self.assertEqual(out["post"]["secrets"], {})
        self.assertEqual(out["post"]["settings"], {"download_client.url": "http://127.0.0.1:9091/transmission/rpc", "download_client.user": ""})

    def test_save_without_a_test_says_so_and_sends_nothing(self):
        out = run(
            steps("""
const app = make();
await app.connectApps();
await click(button(app, "Save"));
out.note = find(app, (x) => x.className === "connect-note").textContent;
out.posted = calls.some((c) => c.path === "/api/settings");
""")
        )
        self.assertIn("Test an app first", out["note"])
        self.assertFalse(out["posted"])

    def test_skip_opens_the_app_without_saving(self):
        out = run(
            steps("""
const app = make();
await app.connectApps();
await click(button(app, "Skip for now"));
out.started = app.started;
out.posted = calls.some((c) => c.method === "POST");
""")
        )
        self.assertEqual(out["started"], 1)
        self.assertFalse(out["posted"])

    def test_an_edit_after_a_test_means_it_has_to_be_tested_again(self):
        out = run(
            steps("""
answers["/api/connect/test"] = [200, { app: "sonarr", url: "http://sonarr:8989", verdict: "supported", version: "4.0.9", message: "ok" }];
answers["/api/settings"] = [200, { ok: true }];
const app = make();
await app.connectApps();
const sonarr = section(app, "sonarr");
await click(button(sonarr, "Test"));
input(sonarr, "sonarr-key").on.input({});
out.message = result(sonarr).textContent;
await click(button(app, "Save"));
out.posted = calls.some((c) => c.path === "/api/settings");
""")
        )
        self.assertEqual(out["message"], "")
        self.assertFalse(out["posted"])

    def test_a_screen_that_cannot_be_drawn_still_opens_the_app(self):
        out = run(
            """
answers["/api/connect"] = [200, { apps: [{ id: "sonarr" }] }];
const app = make();
await app.connectApps();
out.started = app.started;
"""
        )
        self.assertEqual(out["started"], 1)

    def test_a_server_that_cannot_describe_the_apps_opens_the_app(self):
        out = run(
            """
answers["/api/connect"] = [500, { error: "no" }];
const app = make();
await app.connectApps();
out.started = app.started;
"""
        )
        self.assertEqual(out["started"], 1)


if __name__ == "__main__":
    unittest.main()
