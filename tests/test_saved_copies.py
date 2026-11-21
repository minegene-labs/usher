"""The last copy of Library, Upcoming, Discover, Tidy up and the live state (Home and Status) is kept on the device,
shown at once the next time the app opens and refreshed in the background. Only the same account ever sees it, and
signing out deletes it. Fetching ahead happens only while the device is idle, never on a constrained connection."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_list_loader import RIG
from tests.webjs import client_source

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

# Cache Storage, as the page sees it. `opened` stands in for the page being opened again: the copies in memory are gone.
CACHES = """
class FakeCache {
  constructor() { this.m = new Map(); }
  async match(k) { const x = this.m.get(k); return x ? x.clone() : undefined; }
  async put(k, res) { puts.push(k); this.m.set(k, new Response(await res.text())); }
}
const puts = [], boxes = new Map();
globalThis.caches = {
  async open(n) { if (!boxes.has(n)) boxes.set(n, new FakeCache()); return boxes.get(n); },
  async delete(n) { return boxes.delete(n); },
};
const flush = async () => { for (const fn of timers.splice(0)) fn(); for (let i = 0; i < 5; i++) await tick(); };
const opened = () => { lists.drop(...Object.keys(LISTS).filter((n) => n !== "me")); store.snap = null; };
const signedInAs = (account, build = "1.0.0+aaaaaaaa") => lists.set("me", { account, build, csrf: "t" });
"""


def run(script):
    src = (WEB / "data.js").read_text() + RIG + CACHES + script
    done = subprocess.run(["node", "--input-type=module", "-e", src], capture_output=True, text=True, timeout=30)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


@unittest.skipUnless(shutil.which("node"), "needs node")
class LastCopyShowsAtOnce(unittest.TestCase):
    def test_reopened_app_shows_the_last_copy_and_refreshes_it(self):
        got = run("""
signedInAs(7);
await lists.load("library");
lists.save("snap", { home: ["row"] });
await flush();
opened();
await lists.restore();
const shown = lists.show("library");                    // drawn at once from the saved copy...
const snap = store.snap;
await tick(); await tick();
out({ shown: shown?.n, snap, calls: calls["/api/library"], now: store.library.n, redraw: seen.redraw });
""")
        self.assertEqual(got["shown"], 1, "the copy saved last time is drawn straight away")
        self.assertEqual(got["snap"], {"home": ["row"]}, "Home and Status draw from the last live state")
        self.assertEqual(got["calls"], 2, "...and asked for again in the background")
        self.assertEqual(got["now"], 2)
        self.assertEqual(got["redraw"], ["library", "library"])

    def test_writes_are_held_back_and_combined(self):
        got = run("""
signedInAs(7);
for (let i = 0; i < 5; i++) lists.save("snap", { i });
await flush();
opened();
await lists.restore();
out({ puts, snap: store.snap });
""")
        self.assertEqual(got["puts"], ["/saved/snap"], "one write for a burst of live updates")
        self.assertEqual(got["snap"], {"i": 4})

    def test_another_account_never_sees_it_and_it_is_deleted(self):
        got = run("""
signedInAs(7);
await lists.load("library");
await flush();
opened();
signedInAs(8);
await lists.restore();
out({ shown: lists.show("library"), boxes: [...boxes.keys()] });
""")
        self.assertIsNone(got["shown"])
        self.assertEqual(got["boxes"], [], "another account's copies are deleted, artwork included")

    def test_a_copy_saved_before_builds_existed_is_not_shown(self):
        got = run("""
signedInAs(7);
await lists.load("library");
await flush();
const box = await caches.open("usher-data");
const saved = await (await box.match("/saved/library")).json();
delete saved.build;
saved.version = "1.4.10";
await box.put("/saved/library", new Response(JSON.stringify(saved)));
opened();
await lists.restore();
out(lists.show("library"));
""")
        self.assertIsNone(got)

    def test_a_copy_from_another_build_is_not_shown(self):
        got = run("""
signedInAs(7, "1.0.0+aaaaaaaa");
await lists.load("library");
await flush();
opened();
signedInAs(7, "1.0.0+bbbbbbbb");
await lists.restore();
out(lists.show("library"));
""")
        self.assertIsNone(got, "an older build's copy may not have the fields this one draws, even when no release came between")

    def test_nothing_is_restored_before_the_server_confirms_the_account(self):
        got = run("""
signedInAs(7);
await lists.load("library");
await flush();
opened();
lists.drop("me");
await lists.restore();
out(store.library);
""")
        self.assertIsNone(got)

    def test_sign_out_deletes_everything_and_a_late_write_does_not_bring_it_back(self):
        got = run("""
signedInAs(7);
await lists.load("library");
await flush();
lists.save("snap", { home: [] });                         // waiting to be written when the person signs out
await lists.clear();
await flush();
out({ boxes: [...boxes.keys()], library: store.library });
""")
        self.assertEqual(got["boxes"], [])
        self.assertIsNone(got["library"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class DetailsAhead(unittest.TestCase):
    def test_details_are_fetched_once_two_at_a_time_and_dropped_when_a_title_changes(self):
        got = run("""
let open = 0, most = 0;
const waiting = [];
answer = (url) => { open++; most = Math.max(most, open); return new Promise((r) => waiting.push(() => { open--; r({ status: 200, body: { url } }); })); };
for (const id of [1, 2, 3, 4]) details.ahead(`/api/discover/item?kind=movie&id=${id}`);
while (waiting.length) { waiting.shift()(); await tick(); await tick(); }
answer = (url) => ({ status: 200, body: { url } });
const again = await details.load("/api/discover/item?kind=movie&id=1");
const before = Object.keys(calls).length;
lists.changed({ titles: [] });                            // a title changed: "in your library" may be out of date
await details.load("/api/discover/item?kind=movie&id=1");
out({ most, calls: before, again: again.url, after: calls["/api/discover/item?kind=movie&id=1"] });
""")
        self.assertEqual(got["most"], 2, "at most two details are asked for ahead at once")
        self.assertEqual(got["calls"], 4, "each once; the sheet then opens from the copy")
        self.assertEqual(got["after"], 2)


class FetchingAheadIsCareful(unittest.TestCase):
    js = client_source()

    def test_only_when_idle_and_never_on_a_constrained_connection(self):
        ahead = (WEB / "prefetch.js").read_text()
        self.assertIn("saveData", ahead)
        self.assertIn("requestIdleCallback", ahead)
        self.assertIn("whenIdle(run)", ahead, "a card seen on screen waits for the device to be idle too")
        for module in ("posters.js", "shell.js"):
            self.assertIn("whenIdle", (WEB / module).read_text(), module)
        self.assertIn("whenSeen(card", (WEB / "discover.js").read_text())

    def test_artwork_is_asked_for_at_the_size_shown(self):
        posters = (WEB / "posters.js").read_text()
        self.assertIn("w=", posters)
        self.assertIn("devicePixelRatio", posters)

    def test_every_way_of_being_signed_out_deletes_the_copies(self):
        account = (WEB / "account.js").read_text()
        sign_in = account[account.index("  signIn() {") :]
        self.assertIn("lists.clear()", sign_in[: sign_in.index("\n  }\n")], "signIn() is where every signed-out path ends")

    def test_saved_copies_wait_for_the_server_to_confirm_the_account(self):
        shell = (WEB / "shell.js").read_text()
        boot = shell[shell.index("  async boot() {") :]
        self.assertLess(boot.index('lists.load("me"'), boot.index("lists.restore("))

    def test_sign_out_also_clears_the_browsers_own_cache(self):
        routes = (ROOT / "usher" / "auth_routes.py").read_text()
        logout = routes[routes.index("def logout(") : routes.index("def sign_out_others(")]
        self.assertIn("\"Clear-Site-Data\": '\"cache\"'", logout)

    def test_me_names_the_account_by_id(self):
        self.assertRegex((ROOT / "usher" / "auth_routes.py").read_text(), r'"account": s\["user_id"\]')


if __name__ == "__main__":
    unittest.main()
