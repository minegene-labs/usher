"""Settings: the operator's choices from one documented file, usher.toml, plus secrets from the environment.

Every choice Usher makes on the operator's behalf is a key in KEYS below, with its default. The file is read from
USHER_CONFIG (default /config/usher.toml) with Python's built-in tomllib, and it is the only place for settings: without
it Usher doesn't start, so an upgrade that lost the file rolls back (the log names the file).
Secrets (API keys, passwords, the TMDb token, Web Push keys) never go in it: they stay in the environment (.env), in
the integrations' own config files (mounted read-only) or, when they are entered in the app, in secrets.json beside it.
"""

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger("usher.config")

DEFAULT_PATH = "/config/usher.toml"
EXAMPLE = "deploy/usher.example.toml"

FAMILY_RATINGS = ["G", "PG", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "TV-PG"]


class ConfigError(Exception):
    """A setting that can't be used. The message names the key and the file it came from."""


@dataclass(frozen=True)
class Key:
    name: str  # "section.key", as written in usher.toml
    kind: type  # str, int or list (a list of strings)
    default: object
    check: object = None  # value -> error text, or "" when fine
    help: str = ""


def _at_least(n):
    return lambda v: "" if v >= n else f"must be {n} or more"


def _offset(v):
    return "" if -14 * 60 <= v <= 14 * 60 else "must be between -840 and 840 (minutes ahead of UTC)"


def _shortcut_link(v):
    return (
        ""
        if not v or re.fullmatch(r"https://www\.icloud\.com/shortcuts/[A-Za-z0-9]+", v)
        else "must be an iCloud Shortcuts link (https://www.icloud.com/shortcuts/…) or empty"
    )


def _bind(v):
    host, _, port = v.rpartition(":")
    return "" if host and port.isdigit() and 0 < int(port) < 65536 else "must look like 127.0.0.1:8484"


def _one_of(*choices):
    return lambda v: "" if v in choices else "must be " + ", ".join(f'"{c}"' for c in choices[:-1]) + f' or "{choices[-1]}"'


def _region(v):
    return "" if re.fullmatch(r"[A-Za-z]{2}", v) else "must be a two-letter country code such as US"


KEYS = {
    k.name: k
    for k in (
        Key("server.origin", str, "", help="The address people open Usher at, e.g. https://usher.example.net"),
        Key(
            "server.bind",
            str,
            "127.0.0.1:8484",
            check=_bind,
            help="Address and port the server listens on; the published image listens on all addresses unless this says otherwise",
        ),
        Key("server.data_dir", str, "/data", help="Where accounts, requests and settings are kept"),
        Key(
            "server.utc_offset_minutes",
            int,
            0,
            check=_offset,
            help="The server's time zone as minutes ahead of UTC, for times shown in Status and messages",
        ),
        Key("server.session_days", int, 90, check=_at_least(1), help="Days a device stays signed in"),
        Key(
            "server.trusted_proxies",
            list,
            ["127.0.0.1", "::1"],
            help="Addresses of the reverse proxy in front of Usher, whose X-Forwarded-For is believed",
        ),
        Key("server.push_contact", str, "", help="Contact sent with notifications to the push services; empty = server.origin"),
        Key(
            "server.shortcut_url",
            str,
            "",
            check=_shortcut_link,
            help="Link that installs the Connect Shortcut, which must be named Usher Connect (Settings → Device); empty = no install row",
        ),
        Key("media_server.url", str, "http://127.0.0.1:8096", help="Jellyfin, as the server reaches it"),
        Key("media_server.public_url", str, "", help="Jellyfin, as people open it in a browser"),
        Key(
            "media_server.user_id",
            str,
            "",
            help="Required: the viewing account whose watch history Usher shows, by ID (usher-user ids lists them)",
        ),
        Key("pvr.tv.url", str, "http://127.0.0.1:8989", help="Sonarr"),
        Key("pvr.tv.quality_profile_id", int, 0, check=_at_least(0), help="Quality profile for new shows, by ID; 0 = look up quality.profile_name"),
        Key("pvr.movies.url", str, "http://127.0.0.1:7878", help="Radarr"),
        Key(
            "pvr.movies.quality_profile_id",
            int,
            0,
            check=_at_least(0),
            help="Quality profile for new movies, by ID; 0 = look up quality.profile_name",
        ),
        Key("indexer_manager.url", str, "http://127.0.0.1:9696", help="Prowlarr"),
        Key("download_client.url", str, "http://127.0.0.1:9091/transmission/rpc", help="Transmission's RPC address"),
        Key("download_client.user", str, "", help="Transmission username (the password stays in .env as TR_PASS)"),
        Key("library.tv_folder", str, "/srv/media/TV Shows", help="Library folder for new shows"),
        Key("library.movie_folder", str, "/srv/media/Movies", help="Library folder for new movies"),
        Key("library.media_path", str, "/srv/media", help="Picks the disk whose free space Status shows"),
        Key("library.recycle_dir", str, "/recycle", help="Sonarr's and Radarr's recycle bin, as mounted"),
        Key("family.tv_folder", str, "", help="Family-friendly library folder for shows; empty = off"),
        Key("family.movie_folder", str, "", help="Family-friendly library folder for movies; empty = off"),
        Key("family.ratings", list, FAMILY_RATINGS, help="Ratings that count as family-friendly"),
        Key("quality.profile_name", str, "HD-1080p", help="Quality profile looked up by name when no ID is set"),
        Key("quality.fallback_contains", str, "1080", help="Without that profile, the first whose name contains this; then simply the first"),
        Key("discover.region", str, "US", check=_region, help="Country for streaming availability"),
        Key("discover.language", str, "en-US", help="Language for titles and descriptions"),
        Key("discover.upcoming_language", str, "en", help="Original language of the shows in All upcoming (ISO 639-1); empty = every language"),
        Key("discover.upcoming_days", int, 60, check=_at_least(1), help="How far ahead All upcoming looks, in days"),
        Key("storage.low_space_gb", int, 30, check=_at_least(0), help="Status warns when the library disk has less free space than this, in GB"),
        Key("retries.first_search_after_minutes", int, 60, check=_at_least(1), help="A request still waiting is searched for again first after this long"),
        Key("retries.early_every_hours", int, 6, check=_at_least(1), help="Then this often"),
        Key("retries.early_for_days", int, 3, check=_at_least(1), help="For this many days, after which it's shown as not found yet"),
        Key("retries.late_every_hours", int, 24, check=_at_least(1), help="Then this often"),
        Key(
            "retries.missing_grace_hours",
            int,
            3,
            check=_at_least(0),
            help="A new episode of a show you have is searched for this long after it aired, giving the feeds first go",
        ),
        Key(
            "retries.missing_max_searches",
            int,
            5,
            check=_at_least(1),
            help="At most this many episode searches are sent at a time, so search sites aren't flooded",
        ),
        Key(
            "downloads.replace",
            str,
            "report",
            check=_one_of("off", "report", "on"),
            help="Replace a stalled download with a healthier copy: on, report (only log what it would do) or off",
        ),
        Key(
            "downloads.stalled_after_hours",
            int,
            6,
            check=_at_least(1),
            help="A download with no progress for this long is stalled and a better copy is looked for",
        ),
        Key("downloads.max_size_factor", int, 2, check=_at_least(1), help="A replacement may be at most this many times the size of the stalled copy"),
        Key(
            "downloads.min_free_margin_gb",
            int,
            20,
            check=_at_least(0),
            help="Free space to keep, in GB, on top of Sonarr's and Radarr's own minimum, after a replacement",
        ),
        Key("downloads.max_parallel", int, 2, check=_at_least(0), help="Replacements running at once, at most"),
        Key("downloads.max_attempts", int, 3, check=_at_least(1), help="Replacements per episode, season or movie"),
        Key("downloads.retry_after_days", int, 3, check=_at_least(1), help="After max_attempts, wait this many days before trying again"),
        Key(
            "downloads.folders",
            list,
            [],
            help="Transmission's download folders (unfinished and finished) as Usher sees them, checked for leftovers"
            " after a copy is removed; empty = ask Transmission only",
        ),
    )
}

# Settings that no longer do anything. A usher.toml that still sets one starts as before, with the value ignored.
RETIRED = {"notifications.quiet_start", "notifications.quiet_end", "updates.notices"}

SECTIONS = {".".join(k.split(".")[:i]) for k in set(KEYS) | RETIRED for i in range(1, k.count(".") + 1)}


# reading ---------------------------------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _type_name(kind):
    return {str: "text", int: "a whole number", list: "a list of text"}[kind]


def _checked(key, value, where):
    """value, already of the right Python type, after the key's own check."""
    problem = key.check(value) if key.check else ""
    if problem:
        raise ConfigError(f"{key.name} {problem} ({where})")
    return value


def _flatten(table, prefix, where):
    out = {}
    for k, v in table.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            if name not in SECTIONS:
                raise ConfigError(f"[{name}] is not a section Usher knows ({where})")
            out.update(_flatten(v, name + ".", where))
        elif name in RETIRED:
            continue
        elif name not in KEYS:
            raise ConfigError(f"{name} is not a setting Usher knows ({where})")
        else:
            out[name] = v
    return out


def parse(text: str, where: str = "usher.toml") -> dict:
    """usher.toml's text -> {key name: value}, only the keys it sets. Raises ConfigError naming the key at fault."""
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{where} isn't valid TOML: {e}") from None
    values = _flatten(table, "", where)
    for name, v in values.items():
        key = KEYS[name]
        ok = (
            (isinstance(v, int) and not isinstance(v, bool))
            if key.kind is int
            else (isinstance(v, list) and all(isinstance(x, str) for x in v))
            if key.kind is list
            else isinstance(v, str)
        )
        if not ok:
            raise ConfigError(f"{name} must be {_type_name(key.kind)} ({where})")
        values[name] = _checked(key, [x.strip() for x in v] if key.kind is list else v, where)
    return values


@dataclass
class Source:
    path: str
    values: dict | None  # what usher.toml sets, or None when there's no file (every key keeps its default)


_active: Source | None = None


def config_path() -> str:
    return _env("USHER_CONFIG") or DEFAULT_PATH


def read(path: str | None = None) -> Source:
    path = path or config_path()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Source(path, None)
    except OSError as e:
        raise ConfigError(f"Can't read {path}: {e.strerror}") from None
    return Source(path, parse(text, path))


def activate(source: Source | None):
    """Use these values from now on (None: read the file again when next asked)."""
    global _active
    _active = source


def _default(key: Key):
    """A key's default. Only server.bind has a second one: USHER_BIND, which the container image sets so that the
    published port reaches Usher inside it. usher.toml still wins, and a run outside a container keeps the key's own."""
    override = _env("USHER_BIND") if key.name == "server.bind" else ""
    if not override:
        return key.default
    return _checked(key, override, "the USHER_BIND environment variable")


def get(name: str):
    """The value of a setting, e.g. get("storage.low_space_gb"); its default when usher.toml doesn't set it."""
    global _active
    key = KEYS[name]
    if _active is None:
        _active = read()
    values = _active.values or {}
    value = values[name] if name in values else _default(key)
    return list(value) if key.kind is list else value


# secrets ---------------------------------------------------------------------------------------------------
# The environment names of every secret Usher uses. One entered in the app is kept in secrets.json beside usher.toml
# (see saved_settings.py); the environment wins, so an operator's own value is never silently replaced.
SECRETS = ("JELLYFIN_API_KEY", "SONARR_API_KEY", "RADARR_API_KEY", "PROWLARR_API_KEY", "TR_PASS", "TMDB_TOKEN")
SECRETS_FILE = "secrets.json"


def secrets_path() -> Path:
    return Path(config_path()).with_name(SECRETS_FILE)


def read_secrets() -> dict:
    """The secrets entered in the app: {environment name: value}; empty when there is no file."""
    path = secrets_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:  # the message names the file, never its content
        raise ConfigError(f"Can't read {path}: {type(e).__name__}") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ConfigError(f"{path} must hold one text value for each secret")
    return data


def secret(name: str) -> str:
    """A secret's value: from the environment, else the one entered in the app, else empty."""
    return _env(name) or read_secrets().get(name, "").strip()


# start-up --------------------------------------------------------------------------------------------------
def missing_file(source: Source) -> str:
    """What to say when there is no usher.toml: nothing is set up for this install."""
    return f"No {source.path}: Usher can't start without it. Copy {EXAMPLE} there and fill it in."


def check(source: Source, strict: bool = True, setting_up: bool = False) -> list:
    """Validate everything for start-up and return warnings for the log and /healthz. Raises ConfigError when the
    server shouldn't start. While an install is still being set up in the browser there may be no usher.toml and no
    viewing account yet, and that is no problem."""
    if setting_up:
        return []
    if source.values is None:
        if strict:
            raise ConfigError(missing_file(source))
        return [missing_file(source)]
    if strict and not source.values.get("media_server.user_id"):
        raise ConfigError(
            f"media_server.user_id is required ({source.path}): the ID of the media server account whose watch history Usher shows. usher-user ids lists them."
        )
    return []


# settings the rest of the server uses ------------------------------------------------------------------------
def _api_key_from_xml(path: str) -> str:
    """Read <ApiKey> from a Sonarr/Radarr/Prowlarr config.xml. The key is never copied elsewhere."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"<ApiKey>\s*([A-Za-z0-9]{20,64})\s*</ApiKey>", text)
    return m.group(1) if m else ""


@dataclass(frozen=True)
class Settings:
    bind_host: str
    bind_port: int
    data_dir: Path
    web_dir: Path
    public_origin: str  # server.origin
    sonarr_url: str
    radarr_url: str
    prowlarr_url: str
    transmission_url: str
    jellyfin_url: str
    sonarr_key: str
    radarr_key: str
    prowlarr_key: str
    transmission_user: str
    transmission_pass: str
    jellyfin_key: str
    jellyfin_user_id: str  # media_server.user_id
    media_path: str
    recycle_dir: str
    family_tv_folder: str
    family_movie_folder: str
    low_space_gb: int
    session_days: int
    trusted_proxies: tuple
    config_notes: tuple = ()  # warnings from reading the settings, shown in the log and /healthz


def load(path: str | None = None, strict: bool = True, setting_up: bool = False) -> Settings:
    """Read and check the settings, make them the active ones, and log any warnings. Raises ConfigError."""
    source = read(path)
    activate(source)
    notes = check(source, strict, setting_up)
    for n in notes:
        log.warning("%s", n)
    host, _, port = get("server.bind").rpartition(":")
    keys = _env("USHER_KEYS_DIR", "/keys")
    return Settings(
        bind_host=host,
        bind_port=int(port),
        data_dir=Path(get("server.data_dir")),
        web_dir=Path(_env("USHER_WEB", str(Path(__file__).resolve().parent.parent / "web"))),
        public_origin=get("server.origin").rstrip("/"),
        sonarr_url=get("pvr.tv.url").rstrip("/"),
        radarr_url=get("pvr.movies.url").rstrip("/"),
        prowlarr_url=get("indexer_manager.url").rstrip("/"),
        transmission_url=get("download_client.url"),
        jellyfin_url=get("media_server.url").rstrip("/"),
        sonarr_key=secret("SONARR_API_KEY") or _api_key_from_xml(f"{keys}/sonarr.xml"),
        radarr_key=secret("RADARR_API_KEY") or _api_key_from_xml(f"{keys}/radarr.xml"),
        prowlarr_key=secret("PROWLARR_API_KEY") or _api_key_from_xml(f"{keys}/prowlarr.xml"),
        transmission_user=get("download_client.user"),
        transmission_pass=secret("TR_PASS"),
        jellyfin_key=secret("JELLYFIN_API_KEY"),
        jellyfin_user_id=get("media_server.user_id"),
        media_path=get("library.media_path"),
        recycle_dir=get("library.recycle_dir"),
        family_tv_folder=get("family.tv_folder"),
        family_movie_folder=get("family.movie_folder"),
        low_space_gb=get("storage.low_space_gb"),
        session_days=get("server.session_days"),
        trusted_proxies=tuple(get("server.trusted_proxies")),
        config_notes=tuple(notes),
    )
