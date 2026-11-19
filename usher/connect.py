"""Connecting the apps from the browser: what each app needs, which addresses it may be at, and the Test that reads
its version and says whether Usher supports it.

Only an admin reaches this (connect_routes.py). A key typed into the page is used for the one request and never
kept, logged or returned; a key already saved is used only against the address it was saved for, so the page can't
be pointed at another host to make Usher hand a saved key over.
"""

import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config, discover
from .clients import ApiError, Arr, Jellyfin, Transmission, _NoRedirect

log = logging.getLogger("usher.connect")

MAX_URL = 200


@dataclass(frozen=True)
class App:
    label: str
    url_setting: str  # the usher.toml key holding its address; empty for TMDb, which has one fixed address
    secret: str  # the secret holding its key (Transmission: its password)
    credential: str  # what the person calls it
    find: str  # where to find it, in a sentence
    port: int = 0
    path: str = ""
    oldest: int = 0  # the oldest major version Usher supports
    newest: int = 0  # the newest major version Usher has been tested with
    user_setting: str = ""
    optional: bool = False


APPS = {
    "jellyfin": App(
        "Jellyfin",
        "media_server.url",
        "JELLYFIN_API_KEY",
        "API key",
        "In Jellyfin, open Dashboard, then API Keys, and add a key called Usher.",
        port=8096,
        oldest=10,
        newest=12,
    ),
    "sonarr": App(
        "Sonarr",
        "pvr.tv.url",
        "SONARR_API_KEY",
        "API key",
        "In Sonarr, open Settings, then General, and copy the API Key under Security.",
        port=8989,
        oldest=4,
        newest=4,
    ),
    "radarr": App(
        "Radarr",
        "pvr.movies.url",
        "RADARR_API_KEY",
        "API key",
        "In Radarr, open Settings, then General, and copy the API Key under Security.",
        port=7878,
        oldest=5,
        newest=5,
    ),
    "prowlarr": App(
        "Prowlarr",
        "indexer_manager.url",
        "PROWLARR_API_KEY",
        "API key",
        "In Prowlarr, open Settings, then General, and copy the API Key under Security.",
        port=9696,
        oldest=1,
        newest=1,
    ),
    "transmission": App(
        "Transmission",
        "download_client.url",
        "TR_PASS",
        "password",
        "The username and password you set for Transmission's remote access (its Web interface).",
        port=9091,
        path="/transmission/rpc",
        oldest=3,
        newest=4,
        user_setting="download_client.user",
    ),
    "tmdb": App(
        "TMDb",
        "",
        "TMDB_TOKEN",
        "access token",
        "On themoviedb.org, open Settings, then API, and copy the API Read Access Token. Without it Discover stays hidden.",
        optional=True,
    ),
}
ORDER = tuple(APPS)


class BadAddress(ValueError):
    """The address can't be one of the apps; the message says why, in words for the person who typed it."""


def normalise(app: App, raw) -> str:
    """The address as Usher will save and use it, or BadAddress. Transmission gets its usual path when none is given."""
    text = str(raw or "").strip()
    if not text or len(text) > MAX_URL:
        raise BadAddress("Enter the address, such as http://sonarr:8989")
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise BadAddress("That address isn't valid.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BadAddress("An address starts with http:// or https://")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise BadAddress("Leave the username, password and anything after a ? or # out of the address.")
    if port == 0:
        raise BadAddress("That address isn't valid.")
    path = parts.path.rstrip("/")
    if app.path and not path:
        path = app.path
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _refuse_special(host: str, port):
    """Refuse an address that is, or resolves to, a link-local, unspecified or multicast one: link-local holds cloud
    metadata services. The resolver is asked, so 2852039166 and 0xa9fea9fe, which it reads as 169.254.169.254, are
    caught as well as a name that points there. A name that doesn't resolve is left for Test to report as unreachable."""
    try:
        found = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
    except OSError:
        return
    for family, _, _, _, sockaddr in found:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise BadAddress("That address can't be an app.")


def clean_url(app: App, raw) -> str:
    """normalise(), and the address must not lead to a special one. A name is resolved here and again when Test
    connects, so this is a guard against mistakes, not against a name that changes its answer in between."""
    url = normalise(app, raw)
    parts = urllib.parse.urlsplit(url)
    _refuse_special(parts.hostname, parts.port)
    return url


def _shown(url: str) -> str:
    """The saved address for the page: no username, password or query, which an operator's own file may hold."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else (parts.hostname or "")
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return ""


def saved_url(app: App) -> str:
    if not app.url_setting:
        return ""
    return (config.read().values or {}).get(app.url_setting) or config.KEYS[app.url_setting].default


def _saved_user(app: App) -> str:
    return (config.read().values or {}).get(app.user_setting, "") if app.user_setting else ""


def key_is_saved(app: App) -> bool:
    return bool(config.secret(app.secret))


def _major(version: str):
    found = re.match(r"v?(\d+(?:\.\d+)*)", str(version).strip())
    return (int(found[1].split(".")[0]), found[1]) if found else (None, "")


def verdict(app: App, version: str) -> dict:
    """Supported, Not tested yet or Too old, from the version's major number."""
    major, shown = _major(version)
    name = app.label
    if major is None:
        return {"verdict": "other_app", "version": "", "message": f"That address answered, but not like {name}. Check the address."}
    if major < app.oldest:
        return {
            "verdict": "too_old",
            "version": shown,
            "message": f"{name} {shown} is older than Usher supports. Update {name} to version {app.oldest} or newer, then test again.",
        }
    if major > app.newest:
        return {"verdict": "untested", "version": shown, "message": f"Connected. {name} {shown} works, but Usher hasn't been tested with this version yet."}
    return {"verdict": "supported", "version": shown, "message": f"Connected. {name} {shown} is supported."}


def _read_version(app_id: str, app: App, url: str, key: str, user: str):
    """(who answered, its version), from the call Usher itself makes. Raises ApiError. `who` is "" for Transmission,
    whose own call is specific to it."""
    if app_id in ("sonarr", "radarr", "prowlarr"):
        data = Arr(app.label, url, key, "v1" if app_id == "prowlarr" else "v3").get("system/status")
        who, version = (data.get("appName"), data.get("version")) if isinstance(data, dict) else ("", "")
    elif app_id == "jellyfin":
        data = Jellyfin(url, key).get("/System/Info")
        who, version = (str(data.get("ProductName", "")).split()[:1] or [""])[0], data.get("Version") if isinstance(data, dict) else ""
    elif app_id == "transmission":
        data = Transmission(url, user, key).call("session-get", fields=["version"])
        who, version = app.label, data.get("version") if isinstance(data, dict) else ""
    else:
        raise ValueError(app_id)
    return str(who or ""), str(version or "")


def _read_tmdb(key: str):
    """TMDb takes the long access token in a header, and the older 32-character key only as a query, as Discover does."""
    headers, query = {"Accept": "application/json"}, ""
    if len(key) == 32:
        query = "?" + urllib.parse.urlencode({"api_key": key})
    else:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{discover.API}/authentication{query}", headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=10):
            return
    except urllib.error.HTTPError as e:
        raise ApiError(f"tmdb: HTTP {e.code}", e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(f"tmdb: {e}") from None


def _problem(app: App, e: ApiError) -> dict:
    name = app.label
    if e.status in (401, 403):
        return {"verdict": "bad_key", "version": "", "message": f"{name} didn't accept that {app.credential}. Check it and try again."}
    if e.status and 300 <= e.status < 400:
        return {"verdict": "unreachable", "version": "", "message": f"That address sends you somewhere else. Enter the address {name} finally answers at."}
    if e.status == 404:
        return {"verdict": "other_app", "version": "", "message": f"That address answered, but not like {name}. Check the address and its port."}
    if e.status:
        return {"verdict": "unreachable", "version": "", "message": f"{name} answered with an error (HTTP {e.status}). Try again in a moment."}
    return {"verdict": "unreachable", "version": "", "message": f"Usher couldn't reach {name} at that address. Check it, and that {name} is running."}


def _is_saved_address(app: App, base: str) -> bool:
    try:
        return base == normalise(app, saved_url(app))
    except BadAddress:
        return False  # a saved address Usher wouldn't take as typed (it holds a password, say): no saved key goes to it


def test(app_id: str, url, key, user="") -> dict:
    """{"app", "url", "verdict", "version", "message"}. The verdict is supported, untested (works, with a note),
    too_old, bad_key, no_key, other_app or unreachable. Raises BadAddress for an address that can't be an app."""
    app = APPS[app_id]
    base = clean_url(app, url) if app.url_setting else ""
    key = str(key or "").strip()
    if not key and (not app.url_setting or _is_saved_address(app, base)):
        key = config.secret(app.secret)  # only for the address it was saved with
    cause = ""
    if not key:
        out = {"verdict": "no_key", "version": "", "message": f"Paste the {app.credential} first."}
    else:
        try:
            if app_id == "tmdb":
                _read_tmdb(key)
                out = {"verdict": "supported", "version": "", "message": "Connected. TMDb accepted the token."}
            else:
                who, version = _read_version(app_id, app, base, key, str(user or "").strip() or _saved_user(app))
                out = verdict(app, version) if who.lower() == app.label.lower() else verdict(app, "")  # another app: not this one
        except ApiError as e:
            out, cause = _problem(app, e), f" ({e})"  # what the app or the network said: no key or address in it
    log.info("connect test: %s %s%s", app_id, out["verdict"], cause)
    return {"app": app_id, "url": base, **out}


def describe() -> list:
    """What the page shows for each app. Never a key: only whether one is saved."""
    return [
        {
            "id": app_id,
            "label": app.label,
            "credential": app.credential,
            "find": app.find,
            "optional": app.optional,
            "hasAddress": bool(app.url_setting),
            "setting": app.url_setting,
            "secret": app.secret,
            "userSetting": app.user_setting,
            "url": _shown(saved_url(app)),
            "user": _saved_user(app),
            "hasUser": bool(app.user_setting),
            "keySaved": key_is_saved(app),
        }
        for app_id, app in APPS.items()
    ]
