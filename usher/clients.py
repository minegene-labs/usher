"""Small API clients for Sonarr, Radarr, Prowlarr, Transmission and Jellyfin. Standard library only."""

import base64
import http.client
import itertools
import logging
import re
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .files import write_private

TIMEOUT = 10
MAX_IMAGE = 4_000_000

log = logging.getLogger("usher")


class ApiError(Exception):
    """An integration or TMDb failed. status: the HTTP status it answered with, when it answered at all."""

    def __init__(self, message, status=None, reason=""):
        super().__init__(message)
        self.status = status
        self.reason = reason  # "large" or "slow": Test's capped reader gave up on the reply


def _open(req, timeout=TIMEOUT):
    # The integrations live on localhost; never follow redirects (urllib would send the key on to wherever they point)
    # or honour proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(req, timeout=timeout)


class Arr:
    """Sonarr / Radarr (API v3) and Prowlarr (API v1)."""

    load = staticmethod(json.load)  # Test swaps in a reader with a size limit

    def __init__(self, name: str, base: str, key: str, version: str = "v3"):
        self.name, self.base, self.key, self.version = name, base, key, version

    def get(self, path: str, _timeout=TIMEOUT, **params):
        if not self.key:
            raise ApiError(f"{self.name}: no API key")
        qs = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
        req = urllib.request.Request(f"{self.base}/api/{self.version}/{path}{qs}", headers={"X-Api-Key": self.key, "Accept": "application/json"})
        try:
            with _open(req, timeout=_timeout) as r:
                return self.load(r)
        except urllib.error.HTTPError as e:
            raise ApiError(f"{self.name}: {e}", e.code) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f"{self.name}: {e}") from None

    def send(self, method: str, path: str, body=None, **params):
        """POST, PUT or DELETE. Returns the parsed reply, or raises ApiError with the app's own message."""
        if not self.key:
            raise ApiError(f"{self.name}: no API key")
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}/api/{self.version}/{path}{qs}",
            data=data,
            method=method,
            headers={"X-Api-Key": self.key, "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with _open(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e)
                msg = detail[0].get("errorMessage") if isinstance(detail, list) and detail else (detail.get("message") if isinstance(detail, dict) else "")
            except (ValueError, AttributeError):
                msg = ""
            raise ApiError(f"{self.name}: HTTP {e.code} {msg or ''}".strip()) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f"{self.name}: {e}") from None

    def image(self, path: str):
        """Fetch a cached poster from the app's own MediaCover store. Returns (bytes, content_type)."""
        label = f"{self.name} item {path.split('/')[0]}"
        for url in (f"{self.base}/api/{self.version}/mediacover/{path}", f"{self.base}/MediaCover/{path}"):
            req = urllib.request.Request(url, headers={"X-Api-Key": self.key})
            data, ctype = read_image(lambda req=req: _open(req), label)
            if data:
                return data, ctype
        return None, None


_OLD_KEBAB = {"session-get", "session-set", "free-space"}  # the old protocol answered these in kebab-case


def _snake(name: str) -> str:
    """hashString or download-dir, as Transmission 4.1+ spells them: hash_string, download_dir."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).replace("-", "_").lower()


def _old_name(name: str, kebab: bool) -> str:
    """hash_string back to hashString (or download_dir to download-dir), the names the rest of Usher reads."""
    head, *rest = name.split("_")
    return "-".join([head, *rest]) if kebab else head + "".join(w[:1].upper() + w[1:] for w in rest)


def _old_keys(value, kebab: bool):
    if isinstance(value, dict):
        return {_old_name(k, kebab): _old_keys(v, kebab) for k, v in value.items()}
    if isinstance(value, list):
        return [_old_keys(v, kebab) for v in value]
    return value


class Transmission:
    """Speaks JSON-RPC 2.0 to Transmission 4.1+ and the old protocol to older ones.

    Callers always use the old method and field names; the new spelling stays inside this class. The 409 session
    handshake says which protocol to use: from 4.1 it carries X-Transmission-Rpc-Version."""

    load = staticmethod(json.load)

    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self._sid = ""
        self._jsonrpc = False
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _body(self, method: str, arguments: dict, jsonrpc: bool) -> bytes:
        if not jsonrpc:
            return json.dumps({"method": method, "arguments": arguments}).encode()
        params = {_snake(k): ([_snake(f) for f in v] if k == "fields" else v) for k, v in arguments.items()}
        return json.dumps({"jsonrpc": "2.0", "method": _snake(method), "params": params, "id": next(self._ids)}).encode()

    def _answer(self, method: str, data: dict, jsonrpc: bool) -> dict:
        if not jsonrpc:
            if data.get("result") != "success":
                raise ApiError(f"transmission: {method}: {data.get('result')}")
            return data.get("arguments", {})
        if "error" in data:
            err = data.get("error") or {}
            detail = (err.get("data") or {}).get("error_string") if isinstance(err.get("data"), dict) else ""
            raise ApiError(f"transmission: {method}: {err.get('message', 'error')}" + (f" ({detail})" if detail else ""))
        return _old_keys(data.get("result") or {}, method in _OLD_KEBAB)

    def call(self, method: str, **arguments):
        for _ in range(2):  # second try after the 409 session handshake
            with self._lock:
                sid, jsonrpc = self._sid, self._jsonrpc
            req = urllib.request.Request(
                self.url,
                data=self._body(method, arguments, jsonrpc),
                headers={"Authorization": self._auth, "X-Transmission-Session-Id": sid, "Content-Type": "application/json"},
            )
            try:
                with _open(req) as r:
                    data = self.load(r)
                return self._answer(method, data, jsonrpc)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    self._handshake(e.headers)
                    continue
                raise ApiError(f"transmission: {method}: HTTP {e.code}", e.code) from None
            except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
                raise ApiError(f"transmission: {method}: {e}") from None
        raise ApiError("transmission: session handshake failed")

    def _handshake(self, headers):
        """A new session id, and the protocol: JSON-RPC 2.0 from RPC version 6.0.0 (Transmission 4.1) on."""
        version = headers.get("X-Transmission-Rpc-Version", "") or ""
        major = version.split(".")[0]
        with self._lock:
            self._sid = headers.get("X-Transmission-Session-Id", "")
            self._jsonrpc = major.isdigit() and int(major) >= 6

    def torrents(self):
        fields = ["hashString", "percentDone", "eta", "rateDownload", "status", "error", "errorString", "leftUntilDone", "sizeWhenDone"]
        return self.call("torrent-get", fields=fields).get("torrents", [])


def jellyfin_device_id(data_dir) -> str:
    """Usher's own device ID for Jellyfin, made once and kept in the data folder, so Jellyfin lists one device for
    the server however often it restarts."""
    path = Path(data_dir) / "jellyfin-device-id"
    try:
        kept = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        kept = ""
    if re.fullmatch(r"[0-9a-f]{32}", kept):
        return kept
    made = secrets.token_hex(16)
    write_private(path, made + "\n")
    return made


class Jellyfin:
    load = staticmethod(json.load)

    def __init__(self, base: str, key: str, user_id: str = "", device_id: str = ""):
        self.base, self.key = base, key
        # The server passes the kept ID; one-off uses (tests) get one of their own rather than share a made-up one.
        self.device_id = device_id or secrets.token_hex(16)
        self._uid = (user_id or "").replace("-", "").lower() or None
        self._sid = None

    def _req(self, path: str, **params):
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        return urllib.request.Request(f"{self.base}{path}{qs}", headers={"Authorization": self._authorization(), "Accept": "application/json"})

    def _authorization(self):
        """The header Jellyfin's developer guidance asks for, values URL-encoded; never the api_key query."""
        q = lambda v: urllib.parse.quote(str(v), safe="")  # noqa: E731
        return f'MediaBrowser Token="{q(self.key)}", Client="Usher", Device="{q("Usher server")}", DeviceId="{q(self.device_id)}", Version="{q(__version__)}"'

    def get(self, path: str, **params):
        if not self.key:
            raise ApiError("jellyfin: no API key")
        try:
            with _open(self._req(path, **params)) as r:
                return self.load(r)
        except urllib.error.HTTPError as e:
            raise ApiError(f"jellyfin: {e}", e.code) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f"jellyfin: {e}") from None

    def post(self, path: str, body):
        """POST to the media server; it answers 204 with nothing to read."""
        if not self.key:
            raise ApiError("jellyfin: no API key")
        req = self._req(path)
        req.data, req.method = json.dumps(body).encode(), "POST"
        req.add_header("Content-Type", "application/json")
        try:
            with _open(req) as r:
                r.read()
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"jellyfin: {e}") from None

    def media_updated(self, paths, how="Deleted"):
        """Tell the media server these folders changed ("Deleted" or "Modified"), so it drops or rescans them now
        rather than at its next library scan."""
        paths = [p for p in dict.fromkeys(paths) if p]
        if paths:
            self.post("/Library/Media/Updated", {"Updates": [{"Path": p, "UpdateType": how} for p in paths]})

    def existing(self, ids):
        """Which of these item IDs the media server still has."""
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return set()
        items = self.get("/Items", userId=self.user_id(), Ids=",".join(ids), EnableImages="false", Fields="Path").get("Items", [])
        return {it.get("Id") for it in items}

    def get_text(self, path: str, **params) -> str:
        try:
            with _open(self._req(path, **params)) as r:
                return r.read(8_000_000).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(f"jellyfin: {e}") from None

    def user_id(self):
        """The viewing account's ID. media_server.user_id is used as is, so renaming the account changes nothing."""
        if self._uid is None:
            raise ApiError("jellyfin: set media_server.user_id to the viewing account's ID")
        return self._uid

    def server_id(self):
        if self._sid is None:
            self._sid = self.get("/System/Info/Public")["Id"]
        return self._sid

    def app_link(self):
        """What the Jellyfin apps need to open a title: swiftfin://<server id>/<user id>/item/<item id>."""
        try:
            return {"server": self.server_id(), "user": self.user_id()}
        except (ApiError, KeyError, TypeError):
            return None

    def unplayed(self, limit=24):
        """Most recently added movies and episodes not yet played on the shared viewing account."""
        return self.get(
            "/Items",
            userId=self.user_id(),
            IncludeItemTypes="Movie,Episode",
            Recursive="true",
            Filters="IsUnplayed",
            SortBy="DateCreated",
            SortOrder="Descending",
            Limit=str(limit),
            Fields="DateCreated,Overview,ProductionYear,ProviderIds,Path",
        ).get("Items", [])

    def watched(self, limit=100):
        """What the shared viewing account watched most recently, as (kind, tmdb id, title), newest first, one per
        movie or show. Episodes count towards their show."""
        uid = self.user_id()
        played = self.get(
            "/Items",
            userId=uid,
            IncludeItemTypes="Movie,Episode",
            Recursive="true",
            Filters="IsPlayed",
            SortBy="DatePlayed",
            SortOrder="Descending",
            Limit=str(limit),
            Fields="ProviderIds",
        ).get("Items", [])
        resume = self.get(
            "/Items",
            userId=uid,
            IncludeItemTypes="Movie,Episode",
            Recursive="true",
            Filters="IsResumable",
            SortBy="DatePlayed",
            SortOrder="Descending",
            Limit="20",
            Fields="ProviderIds",
        ).get("Items", [])
        series_ids = [it.get("SeriesId") for it in resume + played if it.get("Type") == "Episode" and it.get("SeriesId")]
        series = {}
        if series_ids:
            for it in self.get("/Items", userId=uid, Ids=",".join(dict.fromkeys(series_ids)), Fields="ProviderIds").get("Items", []):
                series[it["Id"]] = it
        out, seen = [], set()
        for it in resume + played:
            if it.get("Type") == "Episode":
                it, kind = series.get(it.get("SeriesId")) or {}, "show"
            else:
                kind = "movie"
            tmdb = (it.get("ProviderIds") or {}).get("Tmdb")
            if tmdb and str(tmdb).isdigit() and (kind, int(tmdb)) not in seen:
                seen.add((kind, int(tmdb)))
                out.append((kind, int(tmdb), it.get("Name", "")))
        return out

    def library_tmdb(self):
        """TMDb ids of every movie and show Jellyfin has, as {(kind, id)}."""
        items = self.get("/Items", userId=self.user_id(), IncludeItemTypes="Movie,Series", Recursive="true", Fields="ProviderIds", Limit="10000").get(
            "Items", []
        )
        out = set()
        for it in items:
            tmdb = (it.get("ProviderIds") or {}).get("Tmdb")
            if tmdb and str(tmdb).isdigit():
                out.add(("movie" if it.get("Type") == "Movie" else "show", int(tmdb)))
        return out

    # Jellyfin moved its per-user endpoints in 10.9; try the current path, then the older one.
    def _either(self, new_path, old_path, **params):
        try:
            return self.get(new_path, userId=self.user_id(), **params)
        except ApiError as e:
            if "404" not in str(e) and "Not Found" not in str(e):
                raise
            return self.get(old_path.format(uid=self.user_id()), **params)

    FIELDS = "Overview,ProductionYear,DateCreated,DateLastMediaAdded,PrimaryImageAspectRatio,SeriesPrimaryImage,ProviderIds,Path"

    def views(self):
        return self._either("/UserViews", "/Users/{uid}/Views").get("Items", [])

    def resume(self, limit=12):
        return self._either("/UserItems/Resume", "/Users/{uid}/Items/Resume", Limit=str(limit), MediaTypes="Video", Fields=self.FIELDS).get("Items", [])

    def next_up(self, limit=12):
        return self.get("/Shows/NextUp", userId=self.user_id(), Limit=str(limit), Fields=self.FIELDS, EnableResumable="false").get("Items", [])

    def latest(self, parent_id, limit=16):
        items = self._either("/Items/Latest", "/Users/{uid}/Items/Latest", ParentId=parent_id, Limit=str(limit), Fields=self.FIELDS, GroupItems="true")
        return items if isinstance(items, list) else items.get("Items", [])

    def image(self, item_id: str, kind: str = "Primary", width=None):
        width = str(width or (320 if kind == "Primary" else 640))  # the media server resizes it to this width
        return read_image(lambda: _open(self._req(f"/Items/{item_id}/Images/{kind}", fillWidth=width, quality="85")), f"media server item {item_id}")

    def find(self, kind: str, title: str, ids: dict, path: str = ""):
        """The Jellyfin item id for a movie or show, or None if Jellyfin doesn't have it yet.

        ids: provider ids known to Sonarr/Radarr, e.g. {"Tvdb": 465690, "Tmdb": 295778, "Imdb": "tt…"}.
        Matches on any of them, or on the folder (both apps see the same paths). Jellyfin's search doesn't
        match titles with a year in brackets ("Scrubs (2026)"), so the year is dropped from the search term,
        and if the search still finds nothing the whole library of that type is checked.
        """
        want = {k: str(v).lower() for k, v in ids.items() if v not in (None, "", 0)}
        folder = path.rstrip("/")

        def match(items):
            for it in items:
                have = {k: str(v).lower() for k, v in (it.get("ProviderIds") or {}).items()}
                if any(have.get(k) == v for k, v in want.items()):
                    return it.get("Id")
                p = (it.get("Path") or "").rstrip("/")
                if folder and (p == folder or p.startswith(folder + "/")):  # a movie's Path is its file
                    return it.get("Id")
            return None

        common = dict(userId=self.user_id(), Recursive="true", Fields="ProviderIds,Path", IncludeItemTypes="Movie" if kind == "movie" else "Series")
        term = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()[:80] or title[:80]
        found = match(self.get("/Items", searchTerm=term, Limit="25", **common).get("Items", []))
        if found:
            return found
        return match(self.get("/Items", Limit="5000", **common).get("Items", []))

    def healthy(self) -> bool:
        try:
            with _open(urllib.request.Request(f"{self.base}/health"), timeout=5) as r:
                return r.read(50).strip() == b"Healthy"
        except (urllib.error.URLError, OSError):
            return False


# Search results carry posters hosted by these sites. Usher fetches them itself, so devices never contact them.
POSTER_HOSTS = {"image.tmdb.org", "artworks.thetvdb.com", "thetvdb.com", "www.thetvdb.com", "assets.fanart.tv"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def remote_image(url: str, label: str = ""):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname not in POSTER_HOSTS:
        return None, None
    req = urllib.request.Request(url, headers={"User-Agent": "Usher", "Accept": "image/*"})
    opener = urllib.request.build_opener(_NoRedirect())  # never follow a redirect off the allow-list
    return read_image(lambda: opener.open(req, timeout=10), label or f"picture from {u.hostname}", ctype_default="")


# ---- pictures: the one reader every poster fetch goes through -------------------------------------------
_cut_lock = threading.Lock()
CUT_OFF: dict = {}  # label -> times a cut-off picture came back from it


def image_whole(data: bytes) -> bool:
    """True when a picture ends the way its format says it must: never pass on a cut-off one as good.
    JPEG ends with FF D9 (trailing zero bytes allowed), PNG with its IEND chunk, GIF with 3B, and WebP's RIFF
    header gives its length. Other formats get only the length check in read_image."""
    if not data:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return data.rstrip(b"\x00").endswith(b"\xff\xd9")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data.rstrip(b"\x00").endswith(b"IEND\xaeB`\x82")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return len(data) >= int.from_bytes(data[4:8], "little") + 8
    if data[:4] in (b"GIF8",):
        return data.rstrip(b"\x00").endswith(b"\x3b")
    return True


def read_image(open_response, label: str, ctype_default: str = "image/jpeg"):
    """Fetch one picture: (bytes, content_type), or (None, None) when there is none, it isn't a picture, or it
    came back cut off twice (short of its Content-Length, or not ending as its format must). A cut-off picture
    is never returned, so it can't be passed on and kept on a device."""
    for attempt in (1, 2):
        try:
            with open_response() as r:
                ctype = r.headers.get("Content-Type", ctype_default) or ""
                if not ctype.startswith("image/"):
                    return None, None
                length = r.headers.get("Content-Length")
                try:
                    data = r.read(MAX_IMAGE)
                except http.client.IncompleteRead:
                    data = b""  # the connection closed before the whole picture arrived
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            return None, None  # no picture (or no answer): the caller's fallback takes over
        length = length.strip() if isinstance(length, str) else ""
        if image_whole(data) and (not length.isdigit() or int(length) == len(data)):
            return data, ctype
        if attempt == 2:
            _cut_off(label)
    return None, None


def _cut_off(label):
    with _cut_lock:
        first = label not in CUT_OFF
        if first and len(CUT_OFF) >= 2000:
            CUT_OFF.pop(next(iter(CUT_OFF)))
        CUT_OFF[label] = CUT_OFF.get(label, 0) + 1
    if first:
        log.warning("picture cut off (twice in a row) for %s; showing the stand-in instead", label)


def cut_off_note():
    """For /healthz notes: only once it repeats, since one bad fetch can be the network."""
    with _cut_lock:
        n, items = sum(CUT_OFF.values()), len(CUT_OFF)
    return f"{n} cut-off pictures from {items} items since start (see the log)" if n >= 2 else None
