"""Connecting the apps from the browser: Test reads an app's version and says Supported, Not tested yet or Too old; a
wrong key, a missing app and a wrong address each get their own words; a saved key is only ever used against its own
address and none is shown, logged or returned; only an admin reaches any of it."""

import base64
import json
import os
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from tests.mock_apps import JF_KEY, KEY, Base, JfHandler, TmdbHandler, TrHandler, TrHandler40, arr_handler
from tests.test_setup import PASSWORD, Case, WebCase, setup_token_hash
from usher import config, connect, connect_find, discover, saved_settings
from usher.auth import hash_password

TR_USER, TR_PASS = "viewer", "made-up-tr-pass"
OTHER_KEY = "made-up-other-key-0123456789abcdef"


def serve(case, handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    case.addCleanup(srv.server_close)
    case.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_address[1]}"


def arr_with(version, seen=None, app_name="Sonarr"):
    """A Sonarr-like fake that reports `version` and records the key it was sent."""

    class H(Base):
        def do_GET(self):
            if seen is not None:
                seen.append(self.headers.get("X-Api-Key"))
            if self.headers.get("X-Api-Key") != KEY:
                return self.reply({"error": "unauthorised"}, 401)
            if self.path.endswith("/system/status"):
                return self.reply({"appName": app_name, "version": version})
            return self.reply({}, 404)

    return H


def tr_with_login(base):
    """Transmission (the fake `base`) that wants TR_USER and TR_PASS."""

    class H(base):
        def do_POST(self):
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(f"{TR_USER}:{TR_PASS}".encode()).decode():
                return self.reply({}, 401)
            super().do_POST()

    return H


class VerdictTests(Case):
    def test_each_version_gets_its_verdict(self):
        sonarr = connect.APPS["sonarr"]
        cases = {"4.0.9.2244": "supported", "3.0.10": "too_old", "5.0.1.1000": "untested", "v4.1": "supported"}
        for version, expected in cases.items():
            with self.subTest(version=version):
                self.assertEqual(connect.verdict(sonarr, version)["verdict"], expected)

    def test_a_version_nobody_can_read_is_not_taken_for_the_app(self):
        for version in ("", "nightly", None):
            self.assertEqual(connect.verdict(connect.APPS["sonarr"], version)["verdict"], "other_app")

    def test_the_words_name_the_app_the_version_and_what_to_do(self):
        too_old = connect.verdict(connect.APPS["sonarr"], "3.0.10")["message"]
        self.assertIn("Sonarr 3.0.10", too_old)
        self.assertIn("version 4 or newer", too_old)
        self.assertIn("hasn't been tested", connect.verdict(connect.APPS["sonarr"], "5.0.0")["message"])

    def test_every_app_has_a_secret_usher_knows_and_a_setting_that_exists(self):
        for app in connect.APPS.values():
            self.assertIn(app.secret, config.SECRETS)
            if app.url_setting:
                self.assertIn(app.url_setting, config.KEYS)
            if app.user_setting:
                self.assertIn(app.user_setting, config.KEYS)
            self.assertLessEqual(app.oldest, app.newest)


class AddressTests(Case):
    def test_an_address_is_kept_as_a_base_without_a_trailing_slash(self):
        sonarr = connect.APPS["sonarr"]
        self.assertEqual(connect.clean_url(sonarr, " http://sonarr:8989/ "), "http://sonarr:8989")
        self.assertEqual(connect.clean_url(sonarr, "https://example.test/sonarr"), "https://example.test/sonarr")

    def test_transmission_gets_its_usual_path_when_none_is_given(self):
        tr = connect.APPS["transmission"]
        self.assertEqual(connect.clean_url(tr, "http://transmission:9091"), "http://transmission:9091/transmission/rpc")
        self.assertEqual(connect.clean_url(tr, "http://transmission:9091/rpc"), "http://transmission:9091/rpc")

    def test_what_cannot_be_an_app_is_refused(self):
        sonarr = connect.APPS["sonarr"]
        bad_ones = (
            "",
            "sonarr:8989",
            "ftp://sonarr",
            "http://",
            "http://user:pw@sonarr",
            "http://sonarr?x=1",
            "http://sonarr#a",
            "http://sonarr:99999",
            "http://sonarr:0",
            "http://169.254.169.254/latest",
            "http://0.0.0.0:8989",
            "http://2852039166",  # 169.254.169.254 written as one number
            "http://0xa9fea9fe",
            "http://[fe80::1]:8989",
            "http://[::ffff:169.254.169.254]",
            "x" * 300,
        )
        for bad in bad_ones:
            with self.subTest(bad=bad), self.assertRaises(connect.BadAddress):
                connect.clean_url(sonarr, bad)


class TestAgainstFakes(Case):
    def setUp(self):
        super().setUp()
        saved_settings.ensure_file()

    def test_a_working_app_is_supported_and_its_version_is_shown(self):
        url = serve(self, arr_handler("sonarr"))
        out = connect.test("sonarr", url, KEY)
        self.assertEqual((out["verdict"], out["version"], out["url"]), ("supported", "4.0.99", url))
        self.assertIn("Sonarr 4.0.99 is supported", out["message"])

    def test_every_app_is_read_with_the_call_usher_makes(self):
        radarr = connect.test("radarr", serve(self, arr_handler("radarr")), KEY)
        self.assertEqual((radarr["version"], radarr["verdict"]), ("4.0.99", "too_old"))  # the fake is Radarr 4; Usher wants 5
        prowlarr = connect.test("prowlarr", serve(self, arr_handler("prowlarr")), KEY)
        self.assertEqual((prowlarr["version"], prowlarr["verdict"]), ("4.0.99", "untested"))  # newer than Prowlarr 1
        jellyfin = connect.test("jellyfin", serve(self, JfHandler), JF_KEY)
        self.assertEqual((jellyfin["version"], jellyfin["verdict"]), ("12.4.0", "supported"))

    def test_transmission_new_and_old_are_both_read(self):
        for base, version in ((TrHandler, "4.1.0"), (TrHandler40, "4.0.6")):
            with self.subTest(version=version):
                out = connect.test("transmission", serve(self, tr_with_login(base)), TR_PASS, TR_USER)
                self.assertEqual((out["verdict"], out["version"]), ("supported", version))

    def test_a_wrong_key_is_said_so_and_the_key_is_not_in_the_answer_or_the_log(self):
        url = serve(self, arr_handler("sonarr"))
        with self.assertLogs("usher.connect", "INFO") as logs:
            out = connect.test("sonarr", url, OTHER_KEY)
        self.assertEqual(out["verdict"], "bad_key")
        self.assertIn("didn't accept that API key", out["message"])
        self.assertNotIn(OTHER_KEY, json.dumps(out))
        self.assertNotIn(OTHER_KEY, "\n".join(logs.output))
        self.assertNotIn(url, "\n".join(logs.output))

    def test_a_wrong_transmission_password_is_a_wrong_password(self):
        out = connect.test("transmission", serve(self, tr_with_login(TrHandler)), "not it", TR_USER)
        self.assertEqual(out["verdict"], "bad_key")
        self.assertIn("password", out["message"])

    def test_an_app_that_is_not_there_is_unreachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # nothing listens on it once the socket is closed
        out = connect.test("sonarr", f"http://127.0.0.1:{port}", KEY)
        self.assertEqual(out["verdict"], "unreachable")
        self.assertIn("couldn't reach Sonarr", out["message"])

    def test_an_address_that_does_not_know_the_call_is_not_the_app(self):
        handler = type("Elsewhere", (Base,), {"do_GET": lambda self: self.reply({}, 404)})
        out = connect.test("sonarr", serve(self, handler), KEY)
        self.assertEqual(out["verdict"], "other_app")
        self.assertIn("not like Sonarr", out["message"])

    def test_a_too_old_and_a_newer_version_are_told_apart(self):
        self.assertEqual(connect.test("sonarr", serve(self, arr_with("3.0.10")), KEY)["verdict"], "too_old")
        out = connect.test("sonarr", serve(self, arr_with("5.0.1")), KEY)
        self.assertEqual(out["verdict"], "untested")
        self.assertIn("works", out["message"])

    def test_tmdb_is_checked_with_its_token(self):
        with mock.patch.object(discover, "API", serve(self, TmdbHandler)):
            self.assertEqual(connect.test("tmdb", "", "tmdb-test-token")["verdict"], "supported")
            self.assertEqual(connect.test("tmdb", "", "wrong")["verdict"], "bad_key")

    def test_the_wrong_app_at_the_address_is_not_taken_for_the_right_one(self):
        sonarr = serve(self, arr_with("4.0.9", app_name="Sonarr"))
        for app_id in ("radarr", "prowlarr"):
            with self.subTest(app=app_id):
                out = connect.test(app_id, sonarr, KEY)
                self.assertEqual(out["verdict"], "other_app", out)

    def test_a_page_that_is_not_json_is_not_reported_as_the_app(self):
        handler = type("Page", (Base,), {"do_GET": lambda self: self.reply(b"<html></html>", ctype="text/html")})
        self.assertNotIn(connect.test("sonarr", serve(self, handler), KEY)["verdict"], ("supported", "untested", "too_old"))

    def test_a_reply_larger_than_the_cap_is_stopped_and_said_so(self):
        sent = []

        class Huge(Base):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    for _ in range(400):  # 400 MB if read to the end
                        self.wfile.write(b" " * 1_000_000)
                        sent.append(1)
                except OSError:
                    pass

        for app_id in ("sonarr", "jellyfin"):
            with self.subTest(app=app_id):
                sent.clear()
                out = connect.test(app_id, serve(self, Huge), KEY)
                self.assertEqual(out["verdict"], "other_app", out)
                self.assertIn("far more than", out["message"])
                self.assertLess(len(sent), 100)  # not read to the end (400); socket buffers hold some

    def test_a_transmission_reply_larger_than_the_cap_is_stopped(self):
        handler = type("Huge", (Base,), {"do_POST": lambda self: self.reply(b"[" + b" " * (connect.MAX_REPLY + 10) + b"]")})
        out = connect.test("transmission", serve(self, handler), TR_PASS, TR_USER)
        self.assertEqual(out["verdict"], "other_app", out)

    def test_a_reply_that_never_ends_is_stopped_at_the_time_limit(self):
        class Endless(Base):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    while True:  # a byte at a time: under the size cap, never done
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    pass

        with mock.patch.object(connect, "REPLY_SECONDS", 0.3):
            out = connect.test("sonarr", serve(self, Endless), KEY)
        self.assertEqual(out["verdict"], "unreachable", out)
        self.assertIn("too slow", out["message"])

    def test_a_reply_just_under_the_cap_still_tests_fine(self):
        padding = "x" * (connect.MAX_REPLY - 200)
        handler = type("Big", (Base,), {"do_GET": lambda self: self.reply({"appName": "Sonarr", "version": "4.0.1", "pad": padding})})
        self.assertEqual(connect.test("sonarr", serve(self, handler), KEY)["verdict"], "supported")

    def test_a_redirect_is_not_followed_and_the_key_does_not_go_with_it(self):
        seen = []
        target = serve(self, arr_with("4.0.9", seen))

        class Redirect(Base):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target + "/api/v3/system/status")
                self.send_header("Content-Length", "0")
                self.end_headers()

        out = connect.test("sonarr", serve(self, Redirect), KEY)
        self.assertEqual(out["verdict"], "unreachable")
        self.assertIn("somewhere else", out["message"])
        self.assertEqual(seen, [], "the key must not follow a redirect")

    def test_the_older_tmdb_key_is_checked_as_discover_uses_it(self):
        seen = []

        class Tmdb(Base):
            def do_GET(self):
                seen.append((self.headers.get("Authorization"), self.path))
                self.reply({"success": True} if "api_key=" + "k" * 32 in self.path else {}, 200 if "api_key=" + "k" * 32 in self.path else 401)

        with mock.patch.object(discover, "API", serve(self, Tmdb)):
            self.assertEqual(connect.test("tmdb", "", "k" * 32)["verdict"], "supported")
        self.assertEqual(seen[0][0], None)

    def test_the_cause_of_a_failure_is_logged_without_the_address(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertLogs("usher.connect", "INFO") as logs:
            connect.test("sonarr", f"http://127.0.0.1:{port}", KEY)
        self.assertIn("unreachable", logs.output[0])
        self.assertNotIn(str(port), logs.output[0])

    def test_no_key_asks_for_one(self):
        self.assertEqual(connect.test("sonarr", serve(self, arr_handler("sonarr")), "  ")["verdict"], "no_key")


class SavedKeyTests(Case):
    def setUp(self):
        super().setUp()
        self.seen = []
        self.home = serve(self, arr_with("4.0.9", self.seen))
        self.elsewhere = serve(self, arr_with("4.0.9", self.seen))
        saved_settings.ensure_file()
        saved_settings.save({"pvr.tv.url": self.home}, {"SONARR_API_KEY": KEY})

    def test_a_blank_key_uses_the_saved_one_for_the_saved_address(self):
        self.assertEqual(connect.test("sonarr", self.home + "/", "")["verdict"], "supported")
        self.assertEqual(self.seen, [KEY])

    def test_a_blank_key_is_never_sent_to_another_address(self):
        out = connect.test("sonarr", self.elsewhere, "")
        self.assertEqual(out["verdict"], "no_key")
        self.assertEqual(self.seen, [], "the saved key must not leave for an address it wasn't saved with")

    def test_a_key_from_the_environment_follows_the_same_rule(self):
        saved_settings.save({}, {"SONARR_API_KEY": ""})
        with mock.patch.dict(os.environ, {"SONARR_API_KEY": KEY}):
            self.assertEqual(connect.test("sonarr", self.home, "")["verdict"], "supported")
            self.assertEqual(connect.test("sonarr", self.elsewhere, "")["verdict"], "no_key")


class SavedAddressWithCredentialsTests(Case):
    def test_an_address_with_a_password_gets_no_saved_key_and_is_shown_without_it(self):
        saved_settings.ensure_file()
        seen = []
        home = serve(self, arr_with("4.0.9", seen))
        with_login = home.replace("http://", "http://user:pw@")
        saved_settings.save({"pvr.tv.url": with_login}, {"SONARR_API_KEY": KEY})
        self.assertEqual(connect.test("sonarr", home, "")["verdict"], "no_key")
        self.assertEqual(seen, [])
        shown = next(a for a in connect.describe() if a["id"] == "sonarr")["url"]
        self.assertEqual(shown, home)
        self.assertNotIn("pw", shown)


class FindingTests(Case):
    def test_only_the_usual_places_are_tried(self):
        with mock.patch.object(connect_find, "_gateway", return_value="172.18.0.1"):
            self.assertEqual(
                connect_find.candidates("sonarr"),
                ["http://127.0.0.1:8989", "http://172.18.0.1:8989", "http://host.docker.internal:8989", "http://sonarr:8989"],
            )
            self.assertEqual(connect_find.candidates("transmission")[0], "http://127.0.0.1:9091/transmission/rpc")
            self.assertEqual(connect_find.candidates("tmdb"), [])

    def test_suggestions_are_the_places_where_something_listens(self):
        up = ("http://sonarr:8989", "http://127.0.0.1:7878")
        with mock.patch.object(connect_find, "_gateway", return_value=None), mock.patch.object(connect_find, "_listening", lambda url: url in up):
            found = connect_find.suggestions()
        self.assertEqual(found["sonarr"], ["http://sonarr:8989"])
        self.assertEqual(found["radarr"], ["http://127.0.0.1:7878"])
        self.assertEqual((found["jellyfin"], found["tmdb"]), ([], []))

    def test_a_slow_lookup_does_not_hold_the_page_up(self):
        def listening(url):
            if "sonarr" in url:
                threading.Event().wait(5)
            return url == "http://127.0.0.1:7878"

        with (
            mock.patch.object(connect_find, "_gateway", return_value=None),
            mock.patch.object(connect_find, "_listening", listening),
            mock.patch.object(connect_find, "BUDGET", 0.3),
        ):
            started = time.monotonic()
            found = connect_find.suggestions()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((found["radarr"], found["sonarr"]), (["http://127.0.0.1:7878"], []))

    def test_listening_is_a_plain_connection_that_sends_nothing(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            self.assertTrue(connect_find._listening(f"http://127.0.0.1:{port}"))
        self.assertFalse(connect_find._listening(f"http://127.0.0.1:{port}"))

    def test_the_gateway_is_read_from_the_routing_table(self):
        table = "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t0111A8C0\t0003\neth0\t0011A8C0\t00000000\t0001\n"
        with mock.patch("pathlib.Path.read_text", return_value=table):
            self.assertEqual(connect_find._gateway(), "192.168.17.1")
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            self.assertIsNone(connect_find._gateway())


class ConnectRouteTests(WebCase):
    def setUp(self):
        super().setUp()
        self.token, self.csrf = self.sign_up()
        self.store.add_user("alex", "Alex", hash_password(PASSWORD))
        alex, _ = self.auth.sign_in("alex", PASSWORD, "10.0.0.5", "iPhone")
        self.alex = alex
        self.alex_csrf = self.store.session(setup_token_hash(alex))["csrf"]
        patch = mock.patch.object(connect_find, "suggestions", return_value={"sonarr": ["http://sonarr:8989"]})
        patch.start()
        self.addCleanup(patch.stop)

    def post_test(self, body, token=None, csrf=None):
        return self.call("/api/connect/test", body, cookie=token or self.token, csrf=csrf or self.csrf)

    def test_both_routes_need_a_session_and_an_admin(self):
        self.assertEqual(self.call("/api/connect")[0], 401)
        self.assertEqual(self.call("/api/connect", cookie=self.alex)[0], 403)
        self.assertEqual(self.call("/api/connect/test", {"app": "sonarr"}, csrf=self.csrf)[0], 401)
        self.assertEqual(self.post_test({"app": "sonarr", "url": "http://x"}, token=self.alex, csrf=self.alex_csrf)[0], 403)

    def test_a_test_from_another_origin_is_refused(self):
        status, _, _ = self.call("/api/connect/test", {"app": "sonarr"}, cookie=self.token, csrf=self.csrf, origin="http://other.test")
        self.assertEqual(status, 403)

    def test_a_test_needs_the_csrf_token(self):
        self.assertEqual(self.post_test({"app": "sonarr"}, csrf="wrong")[0], 403)

    def test_the_page_gets_each_app_with_what_was_found_and_no_key(self):
        saved_settings.save({}, {"SONARR_API_KEY": KEY})
        status, body, _ = self.call("/api/connect", cookie=self.token)
        self.assertEqual(status, 200)
        apps = {a["id"]: a for a in body["apps"]}
        self.assertEqual(list(apps), ["jellyfin", "sonarr", "radarr", "prowlarr", "transmission", "tmdb"])
        self.assertEqual(apps["sonarr"]["suggestions"], ["http://sonarr:8989"])
        self.assertTrue(apps["sonarr"]["keySaved"])
        self.assertFalse(apps["radarr"]["keySaved"])
        self.assertTrue(apps["tmdb"]["optional"])
        self.assertNotIn(KEY, json.dumps(body))

    def test_a_test_answers_with_the_verdict_and_never_the_key(self):
        url = serve(self, arr_handler("sonarr"))
        status, body, _ = self.post_test({"app": "sonarr", "url": url, "key": KEY})
        self.assertEqual((status, body["verdict"]), (200, "supported"))
        self.assertNotIn(KEY, json.dumps(body))
        status, body, _ = self.post_test({"app": "sonarr", "url": url, "key": OTHER_KEY})
        self.assertEqual((status, body["verdict"]), (200, "bad_key"))
        self.assertNotIn(OTHER_KEY, json.dumps(body))

    def test_a_test_saves_nothing(self):
        self.post_test({"app": "sonarr", "url": serve(self, arr_handler("sonarr")), "key": KEY})
        self.assertEqual(config.read().values, {})
        self.assertEqual(config.read_secrets(), {})

    def test_what_makes_no_sense_is_refused(self):
        bodies = ({}, {"app": "plex"}, {"app": ["sonarr"]}, {"app": "sonarr", "url": "ftp://x"}, {"app": "sonarr", "url": "http://169.254.169.254"})
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.post_test(body)[0], 400)

    def test_a_key_entered_through_the_page_is_kept_and_then_only_reported_as_saved(self):
        url = serve(self, arr_handler("sonarr"))
        status, _, _ = self.call("/api/settings", {"settings": {"pvr.tv.url": url}, "secrets": {"SONARR_API_KEY": KEY}}, cookie=self.token, csrf=self.csrf)
        self.assertEqual(status, 200)
        _, body, _ = self.call("/api/connect", cookie=self.token)
        sonarr = next(a for a in body["apps"] if a["id"] == "sonarr")
        self.assertEqual((sonarr["url"], sonarr["keySaved"]), (url, True))
        self.assertNotIn(KEY, json.dumps(body))
        self.assertEqual(self.post_test({"app": "sonarr", "url": url})[1]["verdict"], "supported")


if __name__ == "__main__":
    unittest.main()
