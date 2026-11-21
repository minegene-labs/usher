"""Setup mode: with no accounts Usher prints a one-time code, which lets one person create the first account (the admin);
after that setup is gone for good and settings need an admin session; secrets are never shown back or logged."""

import json
import logging
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from usher import config, saved_settings, setup
from usher.auth import Auth, hash_password
from usher.diagnostics import RING, Diagnostics
from usher.server import Handler
from usher.setup import MAX_WRONG, Setup
from usher.store import Store

PASSWORD = "a made-up long password"
SECRET = "made-up-sonarr-key-0123456789abcdef"
OTHER_SECRET = "made-up-tr-pass"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.toml = self.dir / "usher.toml"
        env = mock.patch.dict(os.environ, {"USHER_CONFIG": str(self.toml)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        config.activate(None)
        self.addCleanup(config.activate, None)
        self.store = Store(self.dir)
        self.addCleanup(self.store._db.close)


class CodeTests(Case):
    def start(self):
        self.setup = Setup(self.store)
        with self.assertLogs("usher.setup", "WARNING") as logs:
            self.setup.begin()
        return logs.output[0].split("Setup code: ")[1].split()[0] if self.setup.open else None

    def test_a_code_is_printed_to_the_log_when_there_are_no_accounts(self):
        code = self.start()
        self.assertRegex(code, r"^[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")
        self.assertTrue(self.setup.open)

    def test_codes_are_new_each_time(self):
        self.assertNotEqual(setup.new_code(), setup.new_code())

    def test_the_code_is_read_without_caring_for_case_dashes_or_spaces(self):
        code = self.start()
        self.assertEqual(self.setup.check(code.lower().replace("-", " ")), "ok")

    def test_a_wrong_code_is_refused(self):
        self.start()
        self.assertEqual(self.setup.check("AAAA-AAAA-AAAA"), "wrong")
        self.assertEqual(self.setup.check(""), "wrong")

    def test_five_wrong_codes_pause_every_try_for_a_minute_and_keep_the_code(self):
        code = self.start()
        for _ in range(MAX_WRONG - 1):
            self.assertEqual(self.setup.check("AAAA-AAAA-AAAA"), "wrong")
        self.assertEqual(self.setup.check("AAAA-AAAA-AAAA"), "paused")
        self.assertEqual(self.setup.check(code), "paused")  # even the right one waits
        self.assertFalse(self.setup.finish(code, "sam", "Sam", "x"))
        with mock.patch.object(setup.time, "monotonic", return_value=setup.time.monotonic() + setup.PAUSE + 1):
            self.assertEqual(self.setup.check(code), "ok")  # the owner's copied code is still good

    def test_an_account_added_another_way_closes_setup(self):
        self.start()
        self.store.add_user("sam", "Sam", "x")  # usher-user add
        self.assertFalse(self.setup.open)
        self.assertEqual(self.setup.check("AAAA-AAAA-AAAA"), "closed")

    def test_no_code_and_no_setup_when_an_account_exists(self):
        self.store.add_user("sam", "Sam", hash_password(PASSWORD))
        self.setup = Setup(self.store)
        with self.assertNoLogs("usher.setup", "WARNING"):
            self.setup.begin()
        self.assertFalse(self.setup.open)
        self.assertEqual(self.setup.check("AAAA-AAAA-AAAA"), "closed")

    def test_the_first_account_closes_setup_for_good(self):
        code = self.start()
        self.assertTrue(self.setup.finish(code, "sam", "Sam", hash_password(PASSWORD)))
        self.assertFalse(self.setup.open)
        self.assertEqual(self.setup.check(code), "closed")
        self.assertFalse(self.setup.finish(code, "alex", "Alex", hash_password(PASSWORD)))
        self.assertEqual(self.store.user_count(), 1)
        self.assertTrue(self.store.has_admin())
        self.assertTrue(self.store.user_by_name("sam")["admin"])

    def test_even_with_every_account_removed_a_running_server_stays_closed(self):
        code = self.start()
        self.setup.finish(code, "sam", "Sam", hash_password(PASSWORD))
        self.store.remove_user("sam")
        self.assertFalse(self.setup.open)

    def test_two_at_once_make_one_account(self):
        code = self.start()
        results = []
        gate = threading.Barrier(4)

        def attempt(n):
            gate.wait()
            results.append(self.setup.finish(code, f"person{n}", "P", "x"))

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.store.user_count(), 1)

    def test_no_account_when_the_settings_file_cannot_be_made(self):
        code = self.start()
        with mock.patch.object(saved_settings, "ensure_file", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(PermissionError):
                self.setup.finish(code, "sam", "Sam", hash_password(PASSWORD))
        self.assertEqual(self.store.user_count(), 0)
        self.assertTrue(self.setup.open)

    def test_the_first_account_makes_usher_toml_when_there_is_none(self):
        code = self.start()
        self.setup.finish(code, "sam", "Sam", hash_password(PASSWORD))
        self.assertTrue(self.toml.is_file())
        self.assertEqual(stat.S_IMODE(self.toml.stat().st_mode), 0o600)
        self.assertEqual(config.read().values, {})

    def test_an_existing_usher_toml_is_left_as_it_is(self):
        self.toml.write_text('# mine\n[media_server]\nuser_id = "abc"\n')
        code = self.start()
        self.setup.finish(code, "sam", "Sam", hash_password(PASSWORD))
        self.assertEqual(self.toml.read_text(), '# mine\n[media_server]\nuser_id = "abc"\n')


class StartUpModeTests(Case):
    def test_a_fresh_install_with_no_file_starts_in_setup_mode_and_prints_a_code(self):
        from usher import server

        with (
            mock.patch.object(server, "Store", return_value=self.store),
            mock.patch.object(server, "logging"),
            mock.patch.object(server, "Server") as listen,
            mock.patch.object(server, "App") as app,
            mock.patch.object(server, "signal"),
            self.assertLogs("usher.setup", "WARNING") as logs,
        ):
            app.return_value.setup = Setup(self.store)
            listen.return_value.serve_forever.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                server.main()
        self.assertIn("Setup code: ", logs.output[0])
        self.assertTrue(app.return_value.setup.open)
        listen.assert_called_once()

    def test_a_fresh_install_is_setting_up_with_or_without_a_file(self):
        self.assertTrue(setup.is_setting_up(self.store, config.read()))
        self.toml.write_text('[media_server]\nuser_id = "abc"\n')
        self.assertTrue(setup.is_setting_up(self.store, config.read()))

    def test_an_install_made_the_old_way_starts_as_strictly_as_before(self):
        self.store.add_user("sam", "Sam", "x")  # no admin
        self.assertFalse(setup.is_setting_up(self.store, config.read()))  # no file
        self.toml.write_text("[storage]\nlow_space_gb = 3\n")  # no viewing account
        self.assertFalse(setup.is_setting_up(self.store, config.read()))
        with self.assertRaises(config.ConfigError):
            config.load(setting_up=False)

    def test_a_browser_install_waits_for_its_viewing_account_without_failing(self):
        code = Setup(self.store)
        with self.assertLogs("usher.setup", "WARNING"):
            code.begin()
        code.finish(code._code, "sam", "Sam", "x")
        self.assertTrue(setup.is_setting_up(self.store, config.read()))
        config.load(setting_up=True)  # starts
        self.toml.write_text('[media_server]\nuser_id = "abc"\n')
        self.assertFalse(setup.is_setting_up(self.store, config.read()))

    def test_a_browser_install_that_lost_its_file_does_not_start(self):
        self.store.add_first_user("sam", "Sam", "x")
        self.assertFalse(setup.is_setting_up(self.store, config.read()))
        with self.assertRaises(config.ConfigError):
            config.load(setting_up=False)


class WebCase(Case):
    """The real request handler over HTTP, with the integrations left out."""

    ORIGIN = ""  # server.origin: empty, as on a fresh install

    def setUp(self):
        super().setUp()
        self.auth = Auth(self.store, 90)
        self.setup = Setup(self.store)
        with self.assertLogs("usher.setup", "WARNING") as logs:
            self.setup.begin()
        self.code = logs.output[0].split("Setup code: ")[1].split()[0]
        app = SimpleNamespace(
            store=self.store,
            auth=self.auth,
            setup=self.setup,
            s=SimpleNamespace(trusted_proxies=(), public_origin=self.ORIGIN, web_dir=Path(__file__).resolve().parent.parent / "web"),
            origin_host="",
            jf=mock.Mock(app_link=mock.Mock(return_value="")),
            actions=mock.Mock(family_enabled=mock.Mock(return_value=False)),
            diag=Diagnostics(*[mock.Mock() for _ in range(5)]),
        )
        logging.getLogger().addHandler(RING)  # as the server does, so Status → Logs shows what Usher logged
        self.addCleanup(logging.getLogger().removeHandler, RING)
        patch = mock.patch.object(Handler, "app", app)
        patch.start()
        self.addCleanup(patch.stop)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def call(self, path, body=None, cookie="", csrf="", origin=None):
        headers = {"Accept": "application/json", "Origin": self.base if origin is None else origin}
        if cookie:
            headers["Cookie"] = f"__Host-usher={cookie}"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read() or b"{}"), res.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def create(self, **over):
        form = {"code": self.code, "username": "sam", "name": "Sam", "password": PASSWORD, **over}
        return self.call("/api/setup", form)

    def sign_up(self):
        """The first account, set up through the page; (cookie token, csrf token)."""
        status, body, headers = self.create()
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1], body["csrf"]


class SetupPageTests(WebCase):
    def test_the_setup_page_is_open_with_no_account(self):
        self.assertEqual(self.call("/api/setup")[:2], (200, {"open": True}))

    def test_without_the_code_setup_is_refused(self):
        for code in ("", "AAAA-AAAA-AAAA"):
            status, body, _ = self.create(code=code)
            self.assertEqual(status, 401, code)
        status, _, _ = self.call("/api/setup", {"username": "sam", "password": PASSWORD})
        self.assertEqual(status, 401)
        self.assertEqual(self.store.user_count(), 0)

    def test_only_the_exact_local_names_pass_the_host_check(self):
        for host, ok in (("127.0.0.1:8484", True), ("localhost", True), ("localhost.evil.example", False), ("127.0.0.1.evil.example:80", False)):
            req = urllib.request.Request(self.base + "/api/setup", headers={"Host": host})
            try:
                status = urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as e:
                status = e.code
            self.assertEqual(status == 200, ok, host)

    def test_a_request_from_another_site_is_refused(self):
        status, _, _ = self.call("/api/setup", {"code": self.code, "username": "sam", "password": PASSWORD}, origin="https://elsewhere.example")
        self.assertEqual(status, 403)
        status, _, _ = self.call("/api/setup", {"code": self.code, "username": "sam", "password": PASSWORD}, origin="")
        self.assertEqual(status, 403)
        self.assertEqual(self.store.user_count(), 0)

    def test_a_weak_password_or_bad_username_is_refused_and_the_code_still_works(self):
        self.assertEqual(self.create(password="short")[0], 400)
        self.assertEqual(self.create(username="x")[0], 400)
        self.assertEqual(self.create(username="has space")[0], 400)
        self.assertEqual(self.store.user_count(), 0)
        self.assertEqual(self.create()[0], 200)

    def test_the_first_account_is_the_admin_and_is_signed_in(self):
        token, csrf = self.sign_up()
        status, me, _ = self.call("/api/me", cookie=token)
        self.assertEqual((status, me["username"], me["name"], me["admin"]), (200, "sam", "Sam", True))
        self.assertRegex(me["build"], r"^\d+\.\d+\.\d+\+[0-9a-f]{8}$")  # what the app compares with its own build
        self.assertTrue(self.auth.session(token))

    def test_the_password_is_never_in_the_answer_or_the_log(self):
        with self.assertLogs(level="DEBUG") as logs:
            _, body, headers = self.create()
        self.assertNotIn(PASSWORD, json.dumps(body) + str(headers) + "\n".join(logs.output))
        self.assertNotIn(PASSWORD, self.store.user_by_name("sam")["pw"])  # only its hash is kept

    def test_the_account_is_made_even_when_the_sign_in_limit_is_reached(self):
        for _ in range(30):
            self.auth._ip_fail("127.0.0.1")
        status, body, headers = self.create()
        self.assertEqual(status, 200)
        self.assertIn("Set-Cookie", headers)

    def test_after_the_first_account_the_setup_page_is_gone_for_good(self):
        self.sign_up()
        self.assertEqual(self.call("/api/setup")[0], 404)
        self.assertEqual(self.create(username="alex")[0], 404)  # even with the right code
        self.assertEqual(self.create(code="AAAA-AAAA-AAAA", username="alex")[0], 404)
        self.assertEqual(self.store.user_count(), 1)

    def test_too_many_wrong_codes_pause_setup_without_changing_the_code(self):
        for _ in range(MAX_WRONG - 1):
            self.create(code="AAAA-AAAA-AAAA")
        status, body, _ = self.create(code="AAAA-AAAA-AAAA")
        self.assertEqual(status, 429)
        self.assertIn("minute", body["error"])
        self.assertEqual(self.create()[0], 429)
        self.assertEqual(self.store.user_count(), 0)
        with mock.patch.object(setup.time, "monotonic", return_value=setup.time.monotonic() + setup.PAUSE + 1):
            self.assertEqual(self.create()[0], 200)

    def test_with_no_server_origin_set_yet_the_address_in_use_counts(self):
        # signing in works from the page's own address; it didn't before, so nobody could sign in until origin was set
        self.sign_up()
        status, _, _ = self.call("/api/login", {"username": "sam", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("/api/login", {"username": "sam", "password": PASSWORD}, origin="https://elsewhere.example")[0], 403)


class ConfiguredOriginTests(WebCase):
    ORIGIN = "https://usher.example.net"

    def test_with_an_origin_set_only_that_origin_is_believed(self):
        self.assertEqual(self.create(code=self.code)[0], 403)  # the page's own address is no longer enough
        status, _, _ = self.call("/api/setup", {"code": self.code, "username": "sam", "password": PASSWORD}, origin=self.ORIGIN)
        self.assertEqual(status, 200)


class SettingsTests(WebCase):
    def setUp(self):
        super().setUp()
        self.token, self.csrf = self.sign_up()
        self.store.add_user("alex", "Alex", hash_password(PASSWORD))
        alex, _ = self.auth.sign_in("alex", PASSWORD, "10.0.0.5", "iPhone")
        self.alex = alex
        self.alex_csrf = self.store.session(setup_token_hash(alex))["csrf"]

    def save(self, body, token=None, csrf=None):
        return self.call("/api/settings", body, cookie=token or self.token, csrf=csrf or self.csrf)

    def test_settings_need_a_session(self):
        self.assertEqual(self.call("/api/settings")[0], 401)
        self.assertEqual(self.call("/api/settings", {"settings": {}}, csrf=self.csrf)[0], 401)

    def test_settings_need_an_admin(self):
        self.assertEqual(self.call("/api/settings", cookie=self.alex)[0], 403)
        status, _, _ = self.call("/api/settings", {"settings": {"discover.region": "NZ"}}, cookie=self.alex, csrf=self.alex_csrf)
        self.assertEqual(status, 403)
        self.assertEqual(config.read().values, {})

    def test_a_change_needs_the_csrf_token(self):
        self.assertEqual(self.call("/api/settings", {"settings": {}}, cookie=self.token, csrf="wrong")[0], 403)

    def test_an_admin_reads_and_saves_settings(self):
        status, body, _ = self.save({"settings": {"discover.region": "NZ", "storage.low_space_gb": 12, "family.ratings": ["G", "PG"]}})
        self.assertEqual((status, body["ok"], body["restartNeeded"]), (200, True, True))
        self.assertEqual(config.read().values, {"discover.region": "NZ", "storage.low_space_gb": 12, "family.ratings": ["G", "PG"]})
        status, body, _ = self.call("/api/settings", cookie=self.token)
        self.assertEqual(body["settings"]["discover.region"]["value"], "NZ")
        self.assertTrue(body["settings"]["discover.region"]["saved"])
        self.assertFalse(body["settings"]["discover.language"]["saved"])
        self.assertEqual(stat.S_IMODE(self.toml.stat().st_mode), 0o600)

    def test_a_setting_can_be_cleared_back_to_its_default(self):
        self.save({"settings": {"discover.region": "NZ"}})
        self.save({"settings": {"discover.region": None}})
        self.assertEqual(config.read().values, {})

    def test_a_setting_that_would_stop_usher_starting_is_refused_and_nothing_is_written(self):
        before = self.toml.read_text()
        for bad in ({"discover.region": "Nowhere"}, {"storage.low_space_gb": "lots"}, {"no.such.key": 1}, {"storage.low_space_gb": True}):
            status, body, _ = self.save({"settings": bad})
            self.assertEqual(status, 400, bad)
            self.assertTrue(body["error"])
        self.assertEqual(self.toml.read_text(), before)

    def test_where_usher_listens_and_keeps_its_data_cannot_be_changed_from_the_app(self):
        for key, value in (("server.bind", "0.0.0.0:9000"), ("server.data_dir", "/elsewhere")):
            self.assertEqual(self.save({"settings": {key: value}})[0], 400)
        body = self.call("/api/settings", cookie=self.token)[1]
        self.assertNotIn("server.bind", body["settings"])
        self.assertEqual(body["locked"], ["server.bind", "server.data_dir"])

    def test_a_setting_name_cannot_write_other_keys_into_the_file(self):
        evil = 'server]\nbind = "0.0.0.0:1"\ndata_dir = "/elsewhere"\n[storage.low_space_gb'
        before = self.toml.read_text()
        self.assertEqual(self.save({"settings": {evil: 5}})[0], 400)
        self.assertEqual(self.toml.read_text(), before)
        self.assertEqual(config.read().values, {})

    def test_a_character_that_cannot_be_saved_is_refused(self):
        self.assertEqual(self.save({"settings": {"library.tv_folder": "\ud800"}})[0], 400)
        self.assertEqual(self.save({"secrets": {"SONARR_API_KEY": "\ud800"}})[0], 400)

    def test_the_operators_own_file_is_kept_once_before_usher_rewrites_it(self):
        own = '# my notes\n[media_server]\nuser_id = "abc"  # the viewer\n'
        self.toml.write_text(own)
        self.save({"settings": {"discover.region": "NZ"}})
        self.save({"settings": {"discover.region": "AU"}})
        backup = self.dir / saved_settings.BACKUP
        self.assertEqual(backup.read_text(), own)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(config.read().values, {"media_server.user_id": "abc", "discover.region": "AU"})

    def test_two_saves_at_once_keep_both(self):
        names = ["discover.region", "discover.language", "storage.low_space_gb", "downloads.max_parallel"]
        values = ["NZ", "en-NZ", 7, 1]
        threads = [threading.Thread(target=self.save, args=({"settings": {n: v}},)) for n, v in zip(names, values)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(config.read().values, dict(zip(names, values)))

    def test_text_that_needs_escaping_survives(self):
        self.save({"settings": {"library.tv_folder": '/srv/My "Shows"\\ü'}})
        self.assertEqual(config.read().values["library.tv_folder"], '/srv/My "Shows"\\ü')

    def test_a_secret_is_kept_beside_usher_toml_for_usher_alone(self):
        status, body, _ = self.save({"secrets": {"SONARR_API_KEY": SECRET, "TR_PASS": OTHER_SECRET}})
        self.assertEqual(status, 200)
        path = self.dir / "secrets.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {"SONARR_API_KEY": SECRET, "TR_PASS": OTHER_SECRET})
        self.assertNotIn(SECRET, self.toml.read_text())
        self.assertEqual(config.secret("SONARR_API_KEY"), SECRET)
        self.assertEqual(config.load(setting_up=True).sonarr_key, SECRET)

    def test_a_secret_is_never_shown_back_in_full(self):
        answers = [self.save({"secrets": {"SONARR_API_KEY": SECRET, "TR_PASS": OTHER_SECRET}})[1], self.call("/api/settings", cookie=self.token)[1]]
        for body in answers:
            text = json.dumps(body)
            self.assertNotIn(SECRET, text)
            self.assertNotIn(OTHER_SECRET, text)
            self.assertEqual(body["secrets"]["SONARR_API_KEY"], {"set": True, "hint": "…" + SECRET[-4:], "fromEnvironment": False})
            self.assertEqual(body["secrets"]["TR_PASS"]["hint"], "")  # too short to give any of it away
            self.assertEqual(body["secrets"]["RADARR_API_KEY"], {"set": False, "hint": "", "fromEnvironment": False})

    def test_a_password_inside_an_address_is_not_shown_back(self):
        self.save({"settings": {"media_server.url": "http://viewer:hunter2hunter2@127.0.0.1:8096"}})
        text = json.dumps(self.call("/api/settings", cookie=self.token)[1])
        self.assertNotIn("hunter2hunter2", text)

    def test_secrets_never_reach_the_log(self):
        with self.assertLogs(level="DEBUG") as logs:
            self.save({"secrets": {"SONARR_API_KEY": SECRET}})
            self.save({"secrets": {"SONARR_API_KEY": SECRET + "\n"}})  # refused
            self.save({"secrets": {"NOT_A_SECRET": SECRET}})  # refused
            self.call("/api/settings", cookie=self.token)
            shown = self.call("/api/logs", cookie=self.token)[1]
        text = "\n".join(logs.output) + json.dumps(shown)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("SONARR_API_KEY", text)  # not even a secret's name
        self.assertIn("1 secret(s) changed", text)

    def test_the_environment_wins_and_says_so(self):
        self.save({"secrets": {"SONARR_API_KEY": SECRET}})
        with mock.patch.dict(os.environ, {"SONARR_API_KEY": "made-up-env-key-9999999999"}):
            self.assertEqual(config.load(setting_up=True).sonarr_key, "made-up-env-key-9999999999")
            body = self.call("/api/settings", cookie=self.token)[1]
        self.assertTrue(body["secrets"]["SONARR_API_KEY"]["fromEnvironment"])

    def test_an_empty_secret_clears_it(self):
        self.save({"secrets": {"SONARR_API_KEY": SECRET}})
        self.save({"secrets": {"SONARR_API_KEY": ""}})
        self.assertEqual(config.secret("SONARR_API_KEY"), "")
        self.assertEqual(json.loads((self.dir / "secrets.json").read_text()), {})

    def test_bad_secrets_are_refused(self):
        for bad in ({"NOT_A_SECRET": "x"}, {"SONARR_API_KEY": "two\nlines"}, {"SONARR_API_KEY": "x" * 300}, {"SONARR_API_KEY": 5}):
            self.assertEqual(self.save({"secrets": bad})[0], 400, bad)
        self.assertFalse((self.dir / "secrets.json").exists())

    def test_a_settings_save_keeps_the_secrets_and_the_other_way_round(self):
        self.save({"secrets": {"SONARR_API_KEY": SECRET}})
        self.save({"settings": {"discover.region": "NZ"}})
        self.save({"secrets": {"TR_PASS": OTHER_SECRET}})
        self.assertEqual(config.read().values, {"discover.region": "NZ"})
        self.assertEqual(json.loads((self.dir / "secrets.json").read_text()), {"SONARR_API_KEY": SECRET, "TR_PASS": OTHER_SECRET})

    def test_the_tmdb_token_entered_in_the_app_is_the_one_discover_uses(self):
        self.save({"secrets": {"TMDB_TOKEN": SECRET}})
        self.assertEqual(config.secret("TMDB_TOKEN"), SECRET)


class ExistingServerTests(Case):
    """A server that already has accounts and its own usher.toml keeps working untouched."""

    def test_nothing_changes_for_it(self):
        self.toml.write_text('# my own notes\n[media_server]\nuser_id = "abc"\n')
        self.store.add_user("sam", "Sam", "x")
        before = self.toml.read_text()
        self.assertFalse(setup.is_setting_up(self.store, config.read()))
        config.load(setting_up=False)
        app_setup = Setup(self.store)
        app_setup.begin()
        self.assertFalse(app_setup.open)
        self.assertEqual(self.toml.read_text(), before)
        self.assertFalse((self.dir / "secrets.json").exists())

    def test_the_settings_file_and_environment_are_read_as_before(self):
        self.toml.write_text('[media_server]\nuser_id = "abc"\n')
        with mock.patch.dict(os.environ, {"JELLYFIN_API_KEY": "made-up-env-key", "TR_PASS": "made-up-env-pass"}):
            s = config.load()
        self.assertEqual((s.jellyfin_key, s.transmission_pass, s.sonarr_key), ("made-up-env-key", "made-up-env-pass", ""))

    def test_its_accounts_are_not_admins_so_the_settings_routes_stay_closed(self):
        self.store.add_user("sam", "Sam", "x")
        self.assertFalse(self.store.user_by_name("sam")["admin"])
        self.assertFalse(self.store.has_admin())

    def test_an_older_database_gets_the_admin_column_and_keeps_its_accounts(self):
        self.store._db.close()
        (self.dir / "usher.db").unlink()
        import sqlite3

        db = sqlite3.connect(self.dir / "usher.db")
        db.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display TEXT NOT NULL,"
            " pw TEXT NOT NULL, created INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0);"
            "INSERT INTO users (username, display, pw, created) VALUES ('sam', 'Sam', 'x', 1);"
        )
        db.close()
        store = Store(self.dir)
        self.addCleanup(store._db.close)
        self.assertEqual(store.user_count(), 1)
        self.assertFalse(store.user_by_name("sam")["admin"])


def setup_token_hash(token):
    from usher.auth import token_hash

    return token_hash(token)


if __name__ == "__main__":
    unittest.main()
