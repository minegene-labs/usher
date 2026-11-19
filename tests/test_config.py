"""usher.toml: every choice is a documented key with a default, checked at start-up. It is the only
place for settings: the old site.env variables are ignored, with or without the file."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from usher import cli, config
from usher.config import ConfigError

ROOT = Path(__file__).resolve().parent.parent

# What Usher did before usher.toml existed, written out by hand so a changed default is noticed.
OLD_BEHAVIOUR = {
    "server.origin": "",
    "server.bind": "127.0.0.1:8484",
    "server.data_dir": "/data",
    "server.utc_offset_minutes": 0,
    "server.session_days": 90,
    "server.trusted_proxies": ["127.0.0.1", "::1"],
    "server.push_contact": "",
    "server.shortcut_url": "",
    "media_server.url": "http://127.0.0.1:8096",
    "media_server.public_url": "",
    "media_server.user_id": "",
    "pvr.tv.url": "http://127.0.0.1:8989",
    "pvr.tv.quality_profile_id": 0,
    "pvr.movies.url": "http://127.0.0.1:7878",
    "pvr.movies.quality_profile_id": 0,
    "indexer_manager.url": "http://127.0.0.1:9696",
    "download_client.url": "http://127.0.0.1:9091/transmission/rpc",
    "download_client.user": "",
    "library.tv_folder": "/srv/media/TV Shows",
    "library.movie_folder": "/srv/media/Movies",
    "library.media_path": "/srv/media",
    "library.recycle_dir": "/recycle",
    "family.tv_folder": "",
    "family.movie_folder": "",
    "family.ratings": ["G", "PG", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "TV-PG"],
    "quality.profile_name": "HD-1080p",
    "quality.fallback_contains": "1080",
    "discover.region": "US",
    "discover.language": "en-US",
    "discover.upcoming_language": "en",
    "discover.upcoming_days": 60,
    "storage.low_space_gb": 30,
    "retries.first_search_after_minutes": 60,
    "retries.early_every_hours": 6,
    "retries.early_for_days": 3,
    "retries.late_every_hours": 24,
    "retries.missing_grace_hours": 3,
    "retries.missing_max_searches": 5,
    "downloads.replace": "report",
    "downloads.stalled_after_hours": 6,
    "downloads.max_size_factor": 2,
    "downloads.min_free_margin_gb": 20,
    "downloads.max_parallel": 2,
    "downloads.max_attempts": 3,
    "downloads.retry_after_days": 3,
    "downloads.folders": [],
}

# A different, valid value for every key, as usher.toml would hold it.
OTHER = {
    "server.origin": "https://usher.example.org",
    "server.bind": "127.0.0.1:9000",
    "server.data_dir": "/srv/usher-data",
    "server.utc_offset_minutes": -300,
    "server.session_days": 30,
    "server.trusted_proxies": ["::1"],
    "server.push_contact": "mailto:operator@example.org",
    "server.shortcut_url": "https://www.icloud.com/shortcuts/0123456789abcdef",
    "media_server.url": "http://media.example:8096",
    "media_server.public_url": "https://watch.example.org",
    "media_server.user_id": "a" * 32,
    "pvr.tv.url": "http://tv.example:8989",
    "pvr.tv.quality_profile_id": 7,
    "pvr.movies.url": "http://movies.example:7878",
    "pvr.movies.quality_profile_id": 8,
    "indexer_manager.url": "http://indexers.example:9696",
    "download_client.url": "http://dl.example:9091/transmission/rpc",
    "download_client.user": "dl",
    "library.tv_folder": "/media/Shows",
    "library.movie_folder": "/media/Films",
    "library.media_path": "/media",
    "library.recycle_dir": "/bin-of-media",
    "family.tv_folder": "/media/Family Shows",
    "family.movie_folder": "/media/Family Films",
    "family.ratings": ["G", "U"],
    "quality.profile_name": "Ultra-HD",
    "quality.fallback_contains": "2160",
    "discover.region": "NZ",
    "discover.language": "fr-FR",
    "discover.upcoming_language": "",
    "discover.upcoming_days": 30,
    "storage.low_space_gb": 100,
    "retries.first_search_after_minutes": 15,
    "retries.early_every_hours": 2,
    "retries.early_for_days": 5,
    "retries.late_every_hours": 48,
    "retries.missing_grace_hours": 1,
    "retries.missing_max_searches": 2,
    "downloads.replace": "on",
    "downloads.stalled_after_hours": 12,
    "downloads.max_size_factor": 3,
    "downloads.min_free_margin_gb": 50,
    "downloads.max_parallel": 1,
    "downloads.max_attempts": 5,
    "downloads.retry_after_days": 7,
    "downloads.folders": ["/downloads/incomplete", "/downloads/complete"],
}

USER_ID = "b" * 32


def toml_value(value):
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_text(values):
    """{dotted key: value} -> usher.toml text."""
    sections = {}
    for name, value in values.items():
        section, _, leaf = name.rpartition(".")
        sections.setdefault(section, []).append(f"{leaf} = {toml_value(value)}")
    return "\n".join(f"[{s}]\n" + "\n".join(lines) + "\n" for s, lines in sections.items())


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "usher.toml")
        # Each test starts from a clean environment with no usher.toml.
        env = mock.patch.dict(os.environ, {"USHER_CONFIG": self.path}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        config.activate(None)
        self.addCleanup(config.activate, None)
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        Path(self.path).write_text(text, encoding="utf-8")


class DefaultTests(ConfigCase):
    def test_every_key_is_listed_here(self):
        self.assertEqual(set(OLD_BEHAVIOUR), set(config.KEYS))
        self.assertEqual(set(OTHER), set(config.KEYS))

    def test_without_a_file_or_variables_everything_is_as_before(self):
        for name, value in OLD_BEHAVIOUR.items():
            self.assertEqual(config.get(name), value, name)

    def test_an_empty_file_is_the_same_as_none(self):
        self.write("")
        config.activate(config.read())
        for name, value in OLD_BEHAVIOUR.items():
            self.assertEqual(config.get(name), value, name)

    def test_load_without_a_file_stops_and_names_the_file_and_the_example(self):
        with self.assertRaises(ConfigError) as e:
            config.load()
        self.assertIn(self.path, str(e.exception))
        self.assertIn("deploy/usher.example.toml", str(e.exception))

    def test_the_tool_that_looks_up_ids_loads_the_defaults_and_says_the_file_is_missing(self):
        s = config.load(strict=False)
        self.assertEqual((s.bind_host, s.bind_port, str(s.data_dir)), ("127.0.0.1", 8484, "/data"))
        self.assertEqual((s.low_space_gb, s.session_days, s.trusted_proxies), (30, 90, ("127.0.0.1", "::1")))
        (note,) = s.config_notes
        self.assertIn(self.path, note)
        self.assertIn("deploy/usher.example.toml", note)


class OverrideTests(ConfigCase):
    def test_each_key_overrides_its_default(self):
        for name, value in OTHER.items():
            with self.subTest(name):
                self.write(toml_text({name: value, "media_server.user_id": USER_ID} if name != "media_server.user_id" else {name: value}))
                config.activate(config.read())
                self.assertEqual(config.get(name), value)
                untouched = next(k for k in OLD_BEHAVIOUR if k not in (name, "media_server.user_id"))
                self.assertEqual(config.get(untouched), OLD_BEHAVIOUR[untouched])

    def test_all_keys_at_once_reach_the_settings(self):
        self.write(toml_text(OTHER))
        s = config.load()
        self.assertEqual((s.bind_host, s.bind_port, str(s.data_dir)), ("127.0.0.1", 9000, "/srv/usher-data"))
        self.assertEqual(s.public_origin, "https://usher.example.org")
        self.assertEqual((s.sonarr_url, s.radarr_url, s.prowlarr_url), ("http://tv.example:8989", "http://movies.example:7878", "http://indexers.example:9696"))
        self.assertEqual((s.jellyfin_user_id, s.transmission_user), ("a" * 32, "dl"))
        self.assertEqual((s.media_path, s.recycle_dir, s.low_space_gb, s.session_days), ("/media", "/bin-of-media", 100, 30))
        self.assertEqual(s.trusted_proxies, ("::1",))

    def test_the_file_wins_and_old_variables_are_ignored(self):
        self.write(toml_text({"media_server.user_id": USER_ID, "storage.low_space_gb": 5}))
        with mock.patch.dict(os.environ, {"USHER_LOW_SPACE_GB": "70", "USHER_REGION": "DE"}):
            s = config.load()
            self.assertEqual((s.low_space_gb, config.get("discover.region")), (5, "US"))
        self.assertEqual(s.config_notes, ())

    def test_secrets_are_not_settings_in_the_file(self):
        for secret in ("api_key", "password", "token", "vapid"):
            self.assertFalse(any(secret in k for k in config.KEYS), secret)
        self.write(toml_text({"media_server.user_id": USER_ID}) + 'api_key = "x"\n')
        with self.assertRaises(ConfigError) as e:
            config.read()
        self.assertIn("media_server.api_key", str(e.exception))


class InvalidTests(ConfigCase):
    def problem(self, text):
        self.write(text)
        with self.assertRaises(ConfigError) as e:
            config.load()
        return str(e.exception)

    def test_unknown_key_is_named(self):
        self.assertIn("storage.low_space", self.problem("[storage]\nlow_space = 3\n"))

    def test_unknown_section_is_named(self):
        self.assertIn("[storge]", self.problem("[storge]\nlow_space_gb = 3\n"))
        self.assertIn("[pvr.music]", self.problem("[pvr.music]\nurl = \"http://x\"\n"))

    def test_a_section_is_not_a_key(self):
        self.assertIn("pvr", self.problem('pvr = "x"\n'))

    def test_wrong_types_are_named(self):
        for text, key in (
            ('[storage]\nlow_space_gb = "30"\n', "storage.low_space_gb"),
            ("[storage]\nlow_space_gb = true\n", "storage.low_space_gb"),
            ("[discover]\nregion = 1\n", "discover.region"),
            ('[family]\nratings = "G"\n', "family.ratings"),
            ("[family]\nratings = [1]\n", "family.ratings"),
        ):
            with self.subTest(text):
                self.assertIn(key, self.problem(text))

    def test_bad_values_are_named(self):
        for text, key in (
            ('[server]\nbind = "localhost"\n', "server.bind"),
            ("[retries]\nearly_every_hours = 0\n", "retries.early_every_hours"),
            ('[discover]\nregion = "USA"\n', "discover.region"),
            ('[downloads]\nreplace = "yes"\n', "downloads.replace"),
        ):
            with self.subTest(text):
                self.assertIn(key, self.problem(f'[media_server]\nuser_id = "{USER_ID}"\n' + text))

    def test_broken_toml_names_the_file(self):
        self.assertIn(self.path, self.problem("[storage\n"))

    def test_the_viewing_account_id_is_required_in_the_file(self):
        self.assertIn("media_server.user_id", self.problem("[storage]\nlow_space_gb = 3\n"))

    def test_the_server_stops_with_the_key_named(self):
        from usher import server

        self.write("[storage]\nlow_space_gb = -1\n")
        with mock.patch.object(server, "logging"), mock.patch.object(server.log, "error") as error, self.assertRaises(SystemExit) as stop:
            server.main()
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("storage.low_space_gb", str(error.call_args))


class StartUpTests(ConfigCase):
    """/healthz answers HTTP 200 whatever its body says, so a server that starts without its settings would look
    healthy to the update script. Not starting at all is what makes the container unhealthy and rolls the update back."""

    def setUp(self):
        super().setUp()
        self.data = Path(self.tmp.name) / "data"
        self.accounts = 1  # an install that has been in use: setup mode, for one that has none, is in test_setup.py

    def start(self):
        """Run server.main() with the network and the app faked; the exit code (None if it got as far as listening)."""
        from usher import server
        from usher.store import Store

        store = Store(self.data)
        for n in range(self.accounts):
            store.add_user(f"person{n}", "Person", "x")
        self.error = mock.MagicMock()
        with (
            mock.patch.object(server, "Store", return_value=store),
            mock.patch.object(server, "logging"),
            mock.patch.object(server.log, "error", self.error),
            mock.patch.object(server, "Server") as self.listen,
            mock.patch.object(server, "App"),
            mock.patch.object(server, "signal"),
        ):
            self.listen.return_value.serve_forever.side_effect = KeyboardInterrupt
            try:
                server.main()
            except SystemExit as stop:
                return stop.code
            except KeyboardInterrupt:
                return None

    def test_without_usher_toml_the_server_exits_and_never_listens(self):
        self.assertEqual(self.start(), 2)
        self.assertIn(self.path, str(self.error.call_args))
        self.assertIn("deploy/usher.example.toml", str(self.error.call_args))
        self.listen.assert_not_called()

    def test_with_the_viewing_account_empty_the_server_exits_and_never_listens(self):
        self.write('[media_server]\nurl = "http://127.0.0.1:8096"\n')
        self.assertEqual(self.start(), 2)
        self.assertIn("media_server.user_id", str(self.error.call_args))
        self.listen.assert_not_called()

    def test_a_complete_file_starts_and_listens(self):
        self.write(toml_text({"media_server.user_id": USER_ID}))
        self.assertIsNone(self.start())
        self.listen.assert_called_once()
        self.error.assert_not_called()


class SiteEnvTests(ConfigCase):
    """The old site.env variables are no longer read: usher.toml is the only place for settings."""

    SITE_ENV = {
        "USHER_ORIGIN": "https://usher.example.org",
        "JELLYFIN_USER_ID": USER_ID,
        "JELLYFIN_USER": "Viewer",
        "TR_USER": "dl",
        "USHER_REGION": "NZ",
        "USHER_TV_PROFILE_ID": "7",
        "USHER_FAMILY_TV_ROOT": "/media/Family Shows",
        "USHER_KIDS_MOVIE_ROOT": "/media/Family Films",
        "USHER_TZ_OFFSET_MIN": "600",
        "USHER_LOW_SPACE_GB": "lots",
    }

    def test_old_variables_are_ignored_without_a_file(self):
        with mock.patch.dict(os.environ, self.SITE_ENV):
            for name, value in OLD_BEHAVIOUR.items():
                self.assertEqual(config.get(name), value, name)
            s = config.load(strict=False)
        self.assertEqual((s.public_origin, s.jellyfin_user_id, s.transmission_user), ("", "", ""))
        self.assertFalse(hasattr(s, "jellyfin_user"), "no viewing account by name")
        for note in s.config_notes:
            self.assertNotIn("site.env", note)
            self.assertNotIn("USHER_", note)

    def test_nothing_is_logged_about_them(self):
        with mock.patch.dict(os.environ, self.SITE_ENV), mock.patch.object(config.log, "warning") as warn:
            config.load(strict=False)
        logged = " ".join(str(c) for c in warn.call_args_list)
        self.assertNotIn("site.env", logged)
        self.assertNotIn("USHER_", logged)

    def test_the_migration_is_gone(self):
        for name in ("migrate", "env_mapping", "setting", "DEPRECATED_ALIASES"):
            self.assertFalse(hasattr(config, name), name)
        self.assertFalse(any(hasattr(k, "env") for k in config.KEYS.values()))
        for path in ("usher/config.py", "usher/cli.py", "deploy/setup.sh"):
            self.assertNotIn("migrate", (ROOT / path).read_text(encoding="utf-8"), path)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["config", "migrate"]), 2)
        self.assertNotIn("migrate", out.getvalue())


class CheckCommandTests(ConfigCase):
    def check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["config", "check"])
        return code, out.getvalue()

    def test_a_missing_file_is_reported_with_the_example_to_start_from(self):
        with mock.patch.dict(os.environ, {"USHER_ORIGIN": "https://usher.example.org"}):
            code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"No {self.path}", text)
        self.assertIn("deploy/usher.example.toml", text)
        self.assertNotIn("site.env", text)

    def test_a_mistake_is_named(self):
        self.write("[storage]\nlow_space = 3\n")
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn("storage.low_space", text)

    def test_a_good_file_passes(self):
        self.write(f'[media_server]\nuser_id = "{USER_ID}"\n')
        code, text = self.check()
        self.assertEqual(code, 0)
        self.assertIn(f"Settings OK ({self.path})", text)


class ExampleTests(ConfigCase):
    def test_the_example_file_is_valid_and_documents_every_key(self):
        text = (ROOT / "deploy" / "usher.example.toml").read_text()
        values = config.parse(text, "usher.example.toml")
        self.assertEqual(set(values), set(config.KEYS))
        for name, value in values.items():
            if name not in ("server.origin", "media_server.public_url", "media_server.user_id"):
                self.assertEqual(value, config.KEYS[name].default, f"{name}: the example shows the default")

    def test_no_new_dependencies(self):
        reqs = [line for line in (ROOT / "requirements.txt").read_text().splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual([r.split(">")[0].split("=")[0] for r in reqs], ["cryptography"])

    def test_compose_mounts_the_config_folder(self):
        compose = (ROOT / "deploy" / "compose.yaml").read_text()
        self.assertIn(":/config:ro", compose)


class BehaviourTests(ConfigCase):
    """The keys change what the server does, not just what config.get returns."""

    def use(self, values):
        self.write(toml_text(dict(values, **{"media_server.user_id": USER_ID})))
        config.activate(config.read())

    def actions(self):
        from usher.actions import Actions

        app = mock.Mock()
        app.name = "sonarr"
        app.get.return_value = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}, {"id": 9, "name": "Ultra 2160p"}]
        return Actions(app, app, mock.Mock(), mock.Mock()), app

    def test_family_ratings(self):
        a, _ = self.actions()
        self.assertTrue(a.family_title({"certification": "TV-PG"}))
        self.assertFalse(a.family_title({"certification": "U"}))
        self.use({"family.ratings": ["U", "G"]})
        a, _ = self.actions()
        self.assertTrue(a.family_title({"certification": "u"}))
        self.assertFalse(a.family_title({"certification": "TV-PG"}))

    def test_quality_fallback(self):
        a, app = self.actions()
        self.assertEqual(a.profile_choice(app), (4, "name"))
        self.use({"quality.profile_name": "Nope", "quality.fallback_contains": "2160"})
        a, app = self.actions()
        self.assertEqual(a.profile_choice(app), (9, "fallback"))
        self.use({"pvr.tv.quality_profile_id": 1})
        a, app = self.actions()
        self.assertEqual(a.profile_choice(app), (1, "id"))

    def test_upcoming_language_and_days(self):
        from usher.discover import Discover

        d = Discover(mock.Mock(), mock.Mock())
        self.assertEqual((d.upcoming_language, d.upcoming_days), ("en", 60))
        self.use({"discover.upcoming_language": "", "discover.upcoming_days": 14})
        d = Discover(mock.Mock(), mock.Mock())
        self.assertEqual((d.upcoming_language, d.upcoming_days), (None, 14))

    def test_retry_schedule(self):
        from usher.state import helpers as state

        self.assertEqual(
            (state.research_every(False, False), state.research_every(True, False), state.research_every(True, True), state.couldnt_find_after()),
            (3600, 6 * 3600, 86400, 3 * 86400),
        )
        self.use({"retries.first_search_after_minutes": 15, "retries.early_every_hours": 2, "retries.late_every_hours": 48, "retries.early_for_days": 5})
        self.assertEqual(
            (state.research_every(False, False), state.research_every(True, False), state.research_every(True, True), state.couldnt_find_after()),
            (900, 7200, 48 * 3600, 5 * 86400),
        )

    def test_removed_settings_are_ignored(self):
        values = config.parse('[notifications]\nquiet_start = "21:30"\nquiet_end = "06:45"\n[updates]\nnotices = "on"\n')
        self.assertEqual(values, {})


if __name__ == "__main__":
    unittest.main()
