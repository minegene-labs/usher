"""Usher's web server: serves the app, handles sign-in, and exposes a fixed set of read-only actions."""

import email.utils
import hmac
import json
import logging
import mimetypes
import re
import signal
import sys
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__, artwork, config
from .stamp import STATIC_TYPES, build_id, etag as file_etag, stamp
from .auth import COOKIE, Auth
from .actions import Actions, Problem
from .auth_routes import AuthRoutes, describe_device
from .connect_routes import ConnectRoutes
from .diagnostics import RING, Diagnostics
from .discover import Discover
from .notify import Notifier
from .notify_routes import NotifyRoutes
from .opinion_routes import OpinionRoutes
from .replace import Replacer
from .setup import Setup, is_setting_up
from .setup_routes import SetupRoutes
from .push import endpoint_ok
from .clients import ApiError, Arr, Jellyfin, Transmission, jellyfin_device_id, cut_off_note
from .state import State
from .store import Store
from .update_result import UpdateResult, UpdateRoutes
from .wiring import Wiring

log = logging.getLogger("usher")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "manifest-src 'self'; worker-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), bluetooth=(), serial=(), hid=(), midi=(), display-capture=(), interest-cohort=()"
    ),
}
ART = re.compile(r"^/api/art/(sonarr|radarr)/(\d{1,9})$|^/api/art/jellyfin/([0-9a-f]{32})(?:/(Primary|Thumb|Backdrop))?$")
REMOTE_ART = re.compile(r"^/api/art/remote/([0-9a-f]{24})$")
MAX_BODY = 8 * 1024


class App:
    def __init__(self, settings, store=None):
        self.s = settings
        self.store = store or Store(settings.data_dir)
        self.setup = Setup(self.store)
        self.auth = Auth(self.store, settings.session_days)
        self.sonarr = Arr("sonarr", settings.sonarr_url, settings.sonarr_key)
        self.radarr = Arr("radarr", settings.radarr_url, settings.radarr_key)
        self.prowlarr = Arr("prowlarr", settings.prowlarr_url, settings.prowlarr_key, "v1")
        self.tr = Transmission(settings.transmission_url, settings.transmission_user, settings.transmission_pass)
        self.jf = Jellyfin(settings.jellyfin_url, settings.jellyfin_key, settings.jellyfin_user_id, jellyfin_device_id(settings.data_dir))
        self.state = State(settings, self.sonarr, self.radarr, self.prowlarr, self.tr, self.jf, self.store)
        self.actions = Actions(self.sonarr, self.radarr, self.store, self.state)
        self.notifier = Notifier(self.store, settings, config.get("server.push_contact") or settings.public_origin)
        self.state.notifier = self.notifier
        self.updates = UpdateResult(settings.data_dir)
        self.state.replacer = Replacer(self.sonarr, self.radarr, self.tr, settings.data_dir)
        self.diag = Diagnostics(self.sonarr, self.radarr, self.prowlarr, self.tr, self.jf)
        self.state.titles.clears(self.diag._cache)
        self.discover = Discover(self.actions, self.state, settings.data_dir)
        self.wiring = Wiring(self.sonarr, self.radarr, self.prowlarr, self.tr, self.jf, self.actions)
        self.origin_host = urlsplit(settings.public_origin).netloc


class Handler(AuthRoutes, SetupRoutes, ConnectRoutes, NotifyRoutes, OpinionRoutes, UpdateRoutes, BaseHTTPRequestHandler):
    server_version = "Usher"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App = None  # set at start-up

    # plumbing ---------------------------------------------------------------
    def log_message(self, fmt, *args):
        pass  # access logging is Caddy's job; Usher logs only what matters

    def client_ip(self) -> str:
        peer = self.client_address[0]
        if peer in self.app.s.trusted_proxies:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[-1].strip()[:64]
        return peer

    def send(self, status, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if ctype:
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/j")) else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if "Cache-Control" not in (headers or {}):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def err(self, status, message):
        self.send(status, {"error": message})

    def token(self) -> str:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        return jar[COOKIE].value if COOKIE in jar else ""

    def session(self):
        return self.app.auth.session(self.token())

    def host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        return host == self.app.origin_host or host.rpartition(":")[0] in ("127.0.0.1", "localhost") or host in ("127.0.0.1", "localhost")

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        if self.app.s.public_origin:
            return origin == self.app.s.public_origin
        host = self.headers.get("Host", "")  # no server.origin yet, as while setting up: the page's own address
        return bool(host) and urlsplit(origin).netloc == host

    def body_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n > MAX_BODY:
            self.close_connection = True  # don't read it; drop the connection afterwards instead
            return None
        if n <= 0:
            return None
        try:
            data = json.loads(self.rfile.read(n))
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    # routing ----------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.host_ok():
            return self.err(HTTPStatus.MISDIRECTED_REQUEST, "Wrong address")
        path = urlsplit(self.path).path
        if path == "/healthz":
            w = self.app.wiring.result
            body = {"ok": w["status"] != "degraded", "version": __version__, "status": w["status"]}
            if self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("X-Forwarded-For"):
                cut = cut_off_note()  # cut-off pictures, once they repeat
                body.update(
                    problems=w["problems"],
                    notes=w["notes"] + ([cut] if cut else []),  # details only here
                    config=list(getattr(self.app.s, "config_notes", ())),
                    attention=self.app.state.attention(),
                )
            return self.send(200, body)
        if not path.startswith("/api/"):
            return self.static(path)
        if path == "/api/setup":
            return self.setup_open()
        s = self.session()
        if not s:
            return self.err(HTTPStatus.UNAUTHORIZED, "Sign in to continue")
        try:
            if path == "/api/me":
                return self.me(s)
            if path == "/api/settings":
                return self.settings_get(s)
            if path == "/api/connect":
                return self.connect_get(s)
            if path == "/api/state":
                return self.send(200, self.app.state.snapshot)
            if path == "/api/events":
                return self.events(s)
            if path == "/api/library":
                return self.send(200, self.app.state.library())
            if path == "/api/tidy":
                return self.send(200, self.app.state.tidy())
            if path == "/api/tidy/kept":
                return self.send(200, self.app.state.kept_titles())
            if path == "/api/upcoming":
                return self.send(200, self.app.state.upcoming())
            if path == "/api/upcoming/all":
                return self.send(200, self.app.discover.upcoming_shows())
            if path == "/api/push/key":
                return self.send(200, {"key": self.app.notifier.vapid.public})
            if path == "/api/notify":
                return self.send(200, self.notify_body(s))
            if path in ("/api/updates", "/api/changelog"):
                return self.update_get(path)
            if path == "/api/status/details":
                return self.send(200, self.app.diag.details())
            if path == "/api/logs":
                qs = parse_qs(urlsplit(self.path).query)
                return self.send(200, self.app.diag.logs(qs.get("source", ["usher"])[0], qs.get("level", ["all"])[0]))
            if path == "/api/discover":
                return self.send(200, self.app.discover.rows(s["user_id"]))
            if path == "/api/opinions":
                return self.list_opinions(s, urlsplit(self.path).query)
            if path == "/api/discover/item":
                qs = parse_qs(urlsplit(self.path).query)
                kind = qs.get("kind", [""])[0]
                if kind not in ("movie", "show"):
                    return self.err(HTTPStatus.BAD_REQUEST, "Unknown kind of item.")
                return self.send(200, self.mark_opinions(s, [self.app.discover.item(kind, int(qs.get("id", ["0"])[0]))])[0])
            if path == "/api/show/seasons":
                qs = parse_qs(urlsplit(self.path).query)
                return self.send(200, self.app.actions.seasons(int(qs.get("id", ["0"])[0])))
            if path == "/api/search":
                qs = parse_qs(urlsplit(self.path).query)
                # part=library|show|movie: one source at a time, so the app shows each as it answers.
                # c: this app tab's own id, so a lookup it no longer needs is skipped rather than run.
                part = qs.get("part", [None])[0]
                tab = qs.get("c", [""])[0][:40]
                client = f"{s['user_id']}:{tab}" if tab else None
                res = self.app.actions.search(qs.get("q", [""])[0], part=part, client=client)
                if qs.get("extras", ["1"])[0] != "0":  # extras=0: the app shows results now, badges after
                    self.app.discover.annotate(res["results"])
                self.mark_opinions(s, res["results"])
                return self.send(200, res)
            if path == "/api/search/extras":
                # Streaming badges, TMDb scores and missing posters for results already shown: ids=show:123,movie:456
                items = []
                for part in parse_qs(urlsplit(self.path).query).get("ids", [""])[0].split(",")[:40]:
                    kind, _, num = part.partition(":")
                    if kind in ("movie", "show") and num.isdigit():
                        items.append({"kind": kind, "tmdb": int(num), "ratings": {}})
                self.app.discover.annotate(items)
                return self.send(200, {"items": items})
            m = ART.match(path)
            if m:
                return self.art(*m.groups())
            m = REMOTE_ART.match(path)
            if m:
                return self.remote_art(m.group(1))
        except ValueError:
            return self.err(HTTPStatus.BAD_REQUEST, "That request didn't make sense.")
        except ApiError as e:
            log.warning("request %s failed: %s", path, e)
            return self.err(HTTPStatus.BAD_GATEWAY, "The server couldn't reach one of its apps. Try again shortly.")
        return self.err(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        if not self.host_ok():
            return self.err(HTTPStatus.MISDIRECTED_REQUEST, "Wrong address")
        path = urlsplit(self.path).path
        if path == "/internal/update-result":  # the update script on this server: no sign-in or origin
            return self.update_report(self.body_json() or {})
        if path == "/api/setup":  # the setup code stands in for a sign-in; setup_create checks the origin itself
            return self.setup_create(self.body_json() or {})
        if not self.same_origin():
            log.warning("POST %s refused: origin %r", self.path, self.headers.get("Origin"))
            return self.err(HTTPStatus.FORBIDDEN, "Request refused")
        data = self.body_json() or {}  # always read the body, so a kept-alive connection stays in step
        if path == "/api/login":
            return self.login(data)
        s = self.session()
        if not s:
            return self.err(HTTPStatus.UNAUTHORIZED, "Sign in to continue")
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), s["csrf"]):
            return self.err(HTTPStatus.FORBIDDEN, "Request refused")
        if path == "/api/logout":
            return self.logout(s)
        if path == "/api/sessions/sign-out-others":
            return self.sign_out_others(s)
        if path == "/api/settings":
            return self.settings_save(s, data)
        if path == "/api/connect/test":
            return self.connect_test(s, data)
        if path == "/api/refresh":
            self.app.state.refresh_now()
            return self.send(200, {"ok": True})
        try:
            if path == "/api/get":
                return self.send(200, self.app.actions.get(str(data.get("kind")), int(data.get("ext", 0)), str(data.get("choice", "")), s))
            if path == "/api/show/season":
                return self.send(200, self.app.actions.get_season(int(data.get("id", 0)), int(data.get("season", 0)), s))
            if path == "/api/cancel":
                keys = data.get("keys") if isinstance(data.get("keys"), list) else [data.get("key", "")]
                keys = [str(k)[:80] for k in keys[:50] if k]
                if not keys:
                    return self.err(HTTPStatus.BAD_REQUEST, "Nothing to stop.")
                return self.send(200, self.app.actions.cancel_many(keys, s))
            if path == "/api/retry":
                return self.send(200, self.app.actions.retry(str(data.get("key", ""))[:80], s))
            if path == "/api/push/subscribe":
                sub = data.get("subscription") or {}
                keys = sub.get("keys") or {}
                if not endpoint_ok(str(sub.get("endpoint", ""))) or not keys.get("p256dh") or not keys.get("auth"):
                    return self.err(HTTPStatus.BAD_REQUEST, "That notification address isn't one Usher can use.")
                self.app.store.add_sub(
                    s["user_id"], sub["endpoint"], str(keys["p256dh"])[:200], str(keys["auth"])[:100], describe_device(self.headers.get("User-Agent", ""))
                )
                self.save_prefs(s, data)
                log.info("%s turned on notifications on a %s", s["username"], describe_device(self.headers.get("User-Agent", "")))
                return self.send(200, self.notify_body(s))
            if path == "/api/push/unsubscribe":
                self.app.store.drop_sub(str(data.get("endpoint", "")), s["user_id"])
                return self.send(200, self.notify_body(s))
            if path == "/api/push/test":
                return self.send(200, {"sent": self.app.notifier.test(s)})
            if path == "/api/notify":
                self.save_prefs(s, data)
                return self.send(200, self.notify_body(s))
            if path == "/api/requests/stop":
                return self.send(200, self.app.actions.stop_looking(int(data.get("rid", 0)), s))
            if path == "/api/requests/clear":
                ok = self.app.store.hide_request(int(data.get("rid", 0)))
                self.app.state.refresh_now()
                return self.send(200 if ok else 404, {"ok": ok})
            if path == "/api/opinion":
                return self.set_opinion(s, data)
            if path == "/api/opened/all":
                return self.send(200, {"ok": True, "baseline": self.app.store.mark_all_seen(s["user_id"])})
            if path == "/api/opened":
                jf = str(data.get("jf", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", jf):
                    return self.err(HTTPStatus.BAD_REQUEST, "Unknown item.")
                self.app.store.mark_opened(s["user_id"], jf)
                return self.send(200, {"ok": True, "at": int(time.time())})
            if path == "/api/recycle/empty":
                return self.send(200, self.app.actions.empty_recycle(s, self.app.s.recycle_dir))
            if path.startswith("/api/tidy/"):
                return self.send(200, self.app.actions.tidy_call(path[len("/api/tidy/") :], data, s))
            if path == "/api/remove":
                kind = data.get("kind")
                if kind not in ("movie", "show"):
                    return self.err(HTTPStatus.BAD_REQUEST, "Unknown kind of item.")
                return self.send(200, self.app.actions.remove(kind, int(data.get("id", 0)), s))
        except Problem as e:
            return self.err(HTTPStatus.CONFLICT, str(e))
        except (ValueError, TypeError):
            return self.err(HTTPStatus.BAD_REQUEST, "That request didn't make sense.")
        except ApiError as e:
            log.warning("action %s failed: %s", path, e)
            return self.err(HTTPStatus.BAD_GATEWAY, "The server couldn't finish that just now. Try again shortly.")
        return self.err(HTTPStatus.NOT_FOUND, "Not found")

    # handlers ---------------------------------------------------------------
    def events(self, s):
        """Server-Sent Events: the latest state straight away, then every change, with a heartbeat.
        A change to a title (titles.py) goes out first as a `changed` event, so the device drops its own copies of
        that title (Library, Upcoming, seasons, an open details sheet, Search) before the new state arrives.
        since: the last change the device saw, so one that reconnects catches up on what it missed."""
        self.send_response(200)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        version = -1
        titles = self.app.state.titles
        since = parse_qs(urlsplit(self.path).query).get("since", [""])[0]
        seq = int(since) if since.isdigit() else titles.seq
        if seq > titles.seq:
            seq = -1  # the server restarted since: the device refetches everything
        started = time.monotonic()
        try:
            while time.monotonic() - started < 3600:
                snap, notices = self.app.state.wait_events(version, seq, 20)
                if not self.app.store.session(s["id_hash"]):
                    self.wfile.write(b"event: signed-out\ndata: {}\n\n")
                    break
                for n in notices:
                    seq = max(seq, n["seq"])
                    self.wfile.write(b"event: changed\ndata: " + json.dumps(n, separators=(",", ":")).encode() + b"\n\n")
                if snap.get("version") != version:
                    version = snap.get("version")
                    self.wfile.write(b"event: state\ndata: " + json.dumps(snap, separators=(",", ":")).encode() + b"\n\n")
                else:
                    self.wfile.write(b": still here\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass

    def art(self, app_name, num_id, jf_id, jf_kind=None):
        status, data, ctype, keep = artwork.picture(self.app, app_name, num_id, jf_id, jf_kind, urlsplit(self.path).query)
        return self.send(status, data, ctype=ctype, headers={"Cache-Control": keep})

    def remote_art(self, key):
        status, data, ctype, keep = artwork.remote_picture(self.app, key, urlsplit(self.path).query)
        return self.send(status, data, ctype=ctype, headers={"Cache-Control": keep})

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        root = self.app.s.web_dir.resolve()
        try:
            f = (root / path.lstrip("/")).resolve()
        except (OSError, ValueError):
            return self.err(404, "Not found")
        if root not in f.parents or f.suffix not in STATIC_TYPES or not f.is_file():
            # Unknown paths load the app, which handles its own screens.
            f = root / "index.html"
        st = f.stat()
        rel = f.relative_to(root).as_posix()
        build = build_id(root)
        etag = file_etag(rel, st, build)
        headers = {"ETag": etag, "Cache-Control": "no-cache", "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True)}
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for k, v in {**SECURITY_HEADERS, **headers}.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        ctype = "application/manifest+json" if f.suffix == ".webmanifest" else (mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        return self.send(200, stamp(rel, f.read_bytes(), build), ctype=ctype, headers=headers)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return  # a device went to sleep or changed network mid-request
        log.exception("request from %s failed", client_address[0])


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().addHandler(RING)
    try:
        source = config.read()
        config.activate(source)
        store = Store(Path(config.get("server.data_dir")))
        settings = config.load(setting_up=is_setting_up(store, source))
    except config.ConfigError as e:
        log.error("Usher can't start: %s", e)
        sys.exit(2)
    app = App(settings, store)
    app.setup.begin()
    missing = [
        n
        for n, v in (
            ("server.origin", settings.public_origin),
            ("media_server.user_id", settings.jellyfin_user_id),
            ("Sonarr key", settings.sonarr_key),
            ("Radarr key", settings.radarr_key),
            ("Prowlarr key", settings.prowlarr_key),
            ("Transmission password", settings.transmission_pass),
            ("Jellyfin key", settings.jellyfin_key),
        )
        if not v
    ]
    if missing:
        log.warning("missing settings: %s", ", ".join(missing))
    Handler.app = app
    app.state.start()
    app.wiring.start()
    app.discover.start()
    httpd = Server((settings.bind_host, settings.bind_port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: (app.state.stop(), app.wiring.stop(), sys.exit(0)))
    log.info("Usher %s listening on %s:%d", __version__, settings.bind_host, settings.bind_port)
    try:
        httpd.serve_forever()
    finally:
        app.state.stop()


if __name__ == "__main__":
    main()
