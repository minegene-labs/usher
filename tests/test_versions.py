"""The version is written once, in usher/__init__.py. The two app files that carry the build to a phone hold a placeholder and
the server writes the running build in when it serves them: the version plus a fingerprint of the app's files, so a phone
gets the new files after every deploy, not only after a release, and a pull request never edits the version."""

import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from usher import __version__, stamp
from usher.server import Handler

WEB = Path(__file__).resolve().parent.parent / "web"
PLACEHOLDER = "__USHER_BUILD__"


class PlaceholderTests(unittest.TestCase):
    def test_the_files_carry_the_placeholder_once_and_no_number(self):
        for name in stamp.STAMPED:
            with self.subTest(name=name):
                text = (WEB / name).read_text(encoding="utf-8")
                self.assertEqual(text.count(PLACEHOLDER), 1)
                self.assertNotIn(__version__, text)

    def test_the_build_and_the_cache_name_are_built_from_it(self):
        self.assertRegex((WEB / "updater.js").read_text(encoding="utf-8"), rf'(?m)^export const BUILD = "{PLACEHOLDER}";$')
        self.assertRegex((WEB / "sw.js").read_text(encoding="utf-8"), rf'(?m)^const VERSION = "usher-{PLACEHOLDER}";$')

    def test_no_other_web_file_holds_the_version(self):
        for f in WEB.glob("*.js"):
            if f.name not in stamp.STAMPED:
                with self.subTest(name=f.name):
                    self.assertNotRegex(f.read_text(encoding="utf-8"), r'(BUILD|VERSION) = "[0-9]')


class StampTests(unittest.TestCase):
    def test_only_the_two_files_are_stamped(self):
        self.assertEqual(stamp.stamp("sw.js", b'"usher-__USHER_BUILD__"', "9.8.7+ab12cd34"), b'"usher-9.8.7+ab12cd34"')
        self.assertEqual(stamp.stamp("updater.js", b'"__USHER_BUILD__"', "9.8.7+ab12cd34"), b'"9.8.7+ab12cd34"')
        self.assertEqual(stamp.stamp("app.js", b"__USHER_BUILD__", "9.8.7+ab12cd34"), b"__USHER_BUILD__")
        self.assertEqual(stamp.stamp("sub/sw.js", b"__USHER_BUILD__", "9.8.7+ab12cd34"), b"__USHER_BUILD__")

    def test_a_stamped_file_without_the_placeholder_is_an_error_not_a_stale_number(self):
        with self.assertRaisesRegex(ValueError, "web/sw.js has no __USHER_BUILD__"):
            stamp.stamp("sw.js", b'const VERSION = "usher-1.0.0";', "1.0.0+ab12cd34")

    def test_a_stamped_files_etag_changes_with_the_build_and_the_others_do_not(self):
        st = SimpleNamespace(st_mtime=1700000000.5, st_size=42)
        before = (stamp.etag("sw.js", st, "1.0.0+aaaaaaaa"), stamp.etag("app.js", st, "1.0.0+aaaaaaaa"))
        after = (stamp.etag("sw.js", st, "1.0.0+bbbbbbbb"), stamp.etag("app.js", st, "1.0.0+bbbbbbbb"))
        self.assertNotEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])


class BuildIdTests(unittest.TestCase):
    """The build is the version and a fingerprint of everything the server sends as the app."""

    def setUp(self):
        self.web = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.web, ignore_errors=True)
        (self.web / "icons").mkdir()
        (self.web / "app.js").write_text("export const a = 1;\n")
        (self.web / "app.css").write_text("body { margin: 0; }\n")
        (self.web / "icons" / "icon.png").write_bytes(b"\x89PNG one")

    def test_it_is_the_version_then_eight_hex_digits(self):
        self.assertRegex(stamp.build_id(self.web), rf"^{re.escape(__version__)}\+[0-9a-f]{{8}}$")

    def test_the_same_files_give_the_same_build(self):
        self.assertEqual(stamp.build_id(self.web), stamp.build_id(self.web))

    def test_changing_a_script_a_style_or_a_picture_changes_it(self):
        seen = {stamp.build_id(self.web)}
        for name, extra in (("app.js", b"// more\n"), ("app.css", b"/* more */\n"), ("icons/icon.png", b"two")):
            with self.subTest(name=name):
                with (self.web / name).open("ab") as f:  # longer, so the change shows even on a coarse clock
                    f.write(extra)
                seen.add(stamp.build_id(self.web))
        self.assertEqual(len(seen), 4, "each change gave a build not seen before")

    def test_the_same_files_always_give_the_same_build_and_other_files_another(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        (other / "app.js").write_text("export const b = 2;\n")
        self.assertEqual(stamp.build_id(self.web), stamp.build_id(self.web))
        self.assertNotEqual(stamp.build_id(self.web), stamp.build_id(other))

    def test_an_unchanged_folder_is_not_read_again(self):
        stamp.build_id(self.web)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read again")):
            stamp.build_id(self.web)

    def test_files_the_server_does_not_serve_change_nothing(self):
        before = stamp.build_id(self.web)
        (self.web / "app.js~").write_text("a backup")
        (self.web / ".DS_Store").write_bytes(b"x")
        self.assertEqual(stamp.build_id(self.web), before)

    def test_a_file_that_vanishes_while_it_runs_is_left_out_not_an_error(self):
        real = Path.read_bytes

        def vanishing(path):
            if path.name == "app.css":
                raise FileNotFoundError(path)
            return real(path)

        with mock.patch.object(Path, "read_bytes", vanishing):
            self.assertRegex(stamp.build_id(self.web), r"\+[0-9a-f]{8}$")

    def test_adding_or_renaming_a_file_changes_it(self):
        before = stamp.build_id(self.web)
        (self.web / "extra.js").write_text("export {};\n")
        added = stamp.build_id(self.web)
        (self.web / "extra.js").rename(self.web / "other.js")
        self.assertNotEqual(added, before)
        self.assertNotEqual(stamp.build_id(self.web), added)

    def test_a_new_version_changes_it(self):
        before = stamp.build_id(self.web)
        with mock.patch("usher.stamp.__version__", "9.8.7"):
            after = stamp.build_id(self.web)
        self.assertTrue(after.startswith("9.8.7+"))
        self.assertNotEqual(after, before)


class ServedTests(unittest.TestCase):
    """The real request handler over HTTP, serving a copy of the real web/ folder."""

    def setUp(self):
        self.web = Path(tempfile.mkdtemp()) / "web"
        self.addCleanup(shutil.rmtree, self.web.parent, ignore_errors=True)
        shutil.copytree(WEB, self.web)
        app = SimpleNamespace(origin_host="", s=SimpleNamespace(web_dir=self.web))
        patch = mock.patch.object(Handler, "app", app)
        patch.start()
        self.addCleanup(patch.stop)
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http.daemon_threads = True
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.base = f"http://127.0.0.1:{http.server_address[1]}"

    def get(self, path, etag=None):
        req = urllib.request.Request(self.base + path, headers={"If-None-Match": etag} if etag else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.read().decode("utf-8"), res.headers
        except urllib.error.HTTPError as e:
            return e.code, "", e.headers

    def served_build(self):
        _, sw, _ = self.get("/sw.js")
        _, updater, _ = self.get("/updater.js")
        cache = re.search(r'(?m)^const VERSION = "usher-(.+)";$', sw).group(1)
        build = re.search(r'(?m)^export const BUILD = "(.+)";$', updater).group(1)
        self.assertEqual(cache, build, "the cache name and the build number are the same build")
        return build

    def test_the_service_worker_and_the_updater_carry_the_running_build(self):
        build = self.served_build()
        self.assertEqual(build, stamp.build_id(self.web))
        self.assertTrue(build.startswith(__version__ + "+"))
        self.assertNotIn(PLACEHOLDER, self.get("/sw.js")[1] + self.get("/updater.js")[1])

    def test_a_head_request_gets_the_stamped_length_and_no_body(self):
        _, sw, _ = self.get("/sw.js")
        req = urllib.request.Request(self.base + "/sw.js", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as res:
            self.assertEqual(int(res.headers["Content-Length"]), len(sw.encode("utf-8")))
            self.assertEqual(res.read(), b"")

    def test_a_new_version_changes_what_is_served_and_its_etag(self):
        _, old_sw, old_headers = self.get("/sw.js")
        with mock.patch("usher.stamp.__version__", "9.8.7"):
            status, sw, headers = self.get("/sw.js")
            self.assertEqual(status, 200)
            self.assertRegex(sw, r'const VERSION = "usher-9\.8\.7\+[0-9a-f]{8}";')
            self.assertNotEqual(headers["ETag"], old_headers["ETag"])
            # A phone holding the old copy is sent the new one, not told it is current.
            self.assertEqual(self.get("/sw.js", etag=old_headers["ETag"])[0], 200)
            self.assertEqual(self.get("/sw.js", etag=headers["ETag"])[0], 304)
        self.assertNotEqual(old_sw, sw)

    def test_a_deploy_that_changes_a_web_file_without_a_release_gives_a_new_service_worker_and_build(self):
        """The version is the same, as between two releases; one script differs. The worker's cache name and the build the
        page reports must differ, or a phone keeps answering the old scripts and styles from its saved copy."""
        _, old_sw, old_headers = self.get("/sw.js")
        before = self.served_build()
        script = self.web / "home.js"
        script.write_text(script.read_text(encoding="utf-8") + "\n// a change that is not a release\n", encoding="utf-8")
        after = self.served_build()
        self.assertNotEqual(after, before)
        self.assertEqual(after.split("+")[0], before.split("+")[0], "the version did not move")
        status, sw, headers = self.get("/sw.js", etag=old_headers["ETag"])
        self.assertEqual(status, 200, "a phone holding the old worker is sent the new one")
        self.assertNotEqual(sw, old_sw)
        self.assertNotEqual(headers["ETag"], old_headers["ETag"])

    def test_a_style_change_alone_gives_a_new_build(self):
        before = self.served_build()
        sheet = self.web / "app.css"
        sheet.write_text(sheet.read_text(encoding="utf-8") + "\n/* a change */\n", encoding="utf-8")
        self.assertNotEqual(self.served_build(), before)

    def test_who_am_i_reports_the_build_the_files_are_served_with(self):
        """The app compares this with the build in its own copy of updater.js: both must come from the same place, or a
        phone on the current files would reload forever (or never, on old ones)."""
        app = SimpleNamespace(
            store=mock.Mock(sessions_for=mock.Mock(return_value=[]), baseline=mock.Mock(return_value=0), opened=mock.Mock(return_value={})),
            s=SimpleNamespace(web_dir=self.web),
            jf=mock.Mock(app_link=mock.Mock(return_value="")),
            actions=mock.Mock(family_enabled=mock.Mock(return_value=False)),
        )
        session = {"user_id": 1, "admin": 0, "username": "sam", "display": "Sam", "csrf": "t", "id_hash": "x"}
        who = Handler._me_body(SimpleNamespace(app=app), session)
        self.assertEqual(who["build"], self.served_build())
        script = self.web / "home.js"
        script.write_text(script.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
        self.assertEqual(Handler._me_body(SimpleNamespace(app=app), session)["build"], self.served_build())
        self.assertNotEqual(who["build"], self.served_build())

    def test_other_files_are_served_as_they_are(self):
        _, page, _ = self.get("/app.js")
        self.assertEqual(page, (WEB / "app.js").read_text(encoding="utf-8"))
