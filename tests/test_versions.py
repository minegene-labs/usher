"""The version is written once, in usher/__init__.py. The two app files that carry it to a phone hold a placeholder and the
server writes the running version in when it serves them, so a phone still sees a new version after an update and a pull
request never edits the version."""

import re
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
PLACEHOLDER = "__USHER_VERSION__"


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
        with mock.patch("usher.stamp.__version__", "9.8.7"):
            self.assertEqual(stamp.stamp("sw.js", b'"usher-__USHER_VERSION__"'), b'"usher-9.8.7"')
            self.assertEqual(stamp.stamp("updater.js", b'"__USHER_VERSION__"'), b'"9.8.7"')
            self.assertEqual(stamp.stamp("app.js", b"__USHER_VERSION__"), b"__USHER_VERSION__")
            self.assertEqual(stamp.stamp("sub/sw.js", b"__USHER_VERSION__"), b"__USHER_VERSION__")

    def test_a_stamped_file_without_the_placeholder_is_an_error_not_a_stale_number(self):
        with self.assertRaisesRegex(ValueError, "web/sw.js has no __USHER_VERSION__"):
            stamp.stamp("sw.js", b'const VERSION = "usher-1.0.0";')

    def test_a_stamped_files_etag_changes_with_the_version_and_the_others_do_not(self):
        st = SimpleNamespace(st_mtime=1700000000.5, st_size=42)
        with mock.patch("usher.stamp.__version__", "1.0.0"):
            before = (stamp.etag("sw.js", st), stamp.etag("app.js", st))
        with mock.patch("usher.stamp.__version__", "1.0.1"):
            after = (stamp.etag("sw.js", st), stamp.etag("app.js", st))
        self.assertNotEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])


class ServedTests(unittest.TestCase):
    """The real request handler over HTTP, serving the real web/ folder."""

    def setUp(self):
        app = SimpleNamespace(origin_host="", s=SimpleNamespace(web_dir=WEB))
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

    def test_the_service_worker_and_the_updater_carry_the_running_version(self):
        status, sw, _ = self.get("/sw.js")
        self.assertEqual(status, 200)
        self.assertRegex(sw, rf'(?m)^const VERSION = "usher-{re.escape(__version__)}";$')
        status, updater, _ = self.get("/updater.js")
        self.assertEqual(status, 200)
        self.assertRegex(updater, rf'(?m)^export const BUILD = "{re.escape(__version__)}";$')
        self.assertNotIn(PLACEHOLDER, sw + updater)

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
            self.assertIn('const VERSION = "usher-9.8.7";', sw)
            self.assertNotEqual(headers["ETag"], old_headers["ETag"])
            # A phone holding the old copy is sent the new one, not told it is current.
            self.assertEqual(self.get("/sw.js", etag=old_headers["ETag"])[0], 200)
            self.assertEqual(self.get("/sw.js", etag=headers["ETag"])[0], 304)
        self.assertNotEqual(old_sw, sw)

    def test_other_files_are_served_as_they_are(self):
        _, page, _ = self.get("/app.js")
        self.assertEqual(page, (WEB / "app.js").read_text(encoding="utf-8"))
