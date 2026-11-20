"""Details behind each Status line, and recent log lines from every app, for the Logs screen."""

import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import __version__, config
from .cache import TTLCache
from .clients import ApiError
from .state.helpers import episode_label

STARTED = time.time()

# Anything that looks like a secret is masked before a log line leaves the server.
SECRET = re.compile(
    r"(?i)(api[_-]?key|apikey|token|password|passwd|secret|authorization|x-api-key|sid)"
    r"([\"']?\s*[:=]\s*[\"']?|\s+)([^\s&\"',;]{4,})"
)


def redact(text: str) -> str:
    return SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}•••", text or "")


class Ring(logging.Handler):
    """Keeps Usher's own last 500 log lines in memory."""

    def __init__(self, size=500):
        super().__init__()
        self.lines = deque(maxlen=size)
        self.lock2 = threading.Lock()

    def emit(self, record):
        with self.lock2:
            self.lines.append(
                {
                    "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                    "level": record.levelname.lower().replace("warning", "warn"),
                    "source": record.name,
                    "message": redact(record.getMessage()),
                }
            )


RING = Ring()
LEVELS = {"all": 0, "info": 1, "warn": 2, "error": 3}
ARR_LEVEL = {"trace": 0, "debug": 0, "info": 1, "warn": 2, "error": 3, "fatal": 3}
JF_LEVEL = {"VRB": 0, "DBG": 0, "INF": 1, "WRN": 2, "ERR": 3, "FTL": 3}
JF_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d[ T][\d:.]+[^\]]*|[\d:]+)\]\s*\[(\w{3})\]\s*(?:\[\d+\]\s*)?([^:]*):\s?(.*)$")


def _when(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


class Diagnostics:
    def __init__(self, sonarr, radarr, prowlarr, tr, jf):
        self.sonarr, self.radarr, self.prowlarr, self.tr, self.jf = sonarr, radarr, prowlarr, tr, jf
        self.pool = ThreadPoolExecutor(max_workers=5)
        self._cache = TTLCache(15, 1)  # the details, under one key

    # the details behind each Status line -----------------------------------------
    def details(self):
        cached = self._cache.get("details")
        if cached:
            return cached
        jobs = {
            name: self.pool.submit(fn)
            for name, fn in (
                ("Search", self._search),
                ("TV", lambda: self._arr(self.sonarr)),
                ("Movies", lambda: self._arr(self.radarr)),
                ("Downloads", self._downloads),
                ("Watching", self._watching),
            )
        }
        out = {}
        for name, job in jobs.items():
            try:
                out[name] = job.result(timeout=20)
            except Exception as e:  # one app being down mustn't hide the others
                out[name] = {"facts": [["Problem", redact(str(e))[:200]]], "items": []}
        out["Usher"] = {"facts": [["Version", __version__], ["Running for", _ago(STARTED)]], "items": []}
        self._cache.set("details", out)
        return out

    def _search(self):
        indexers = self.prowlarr.get("indexer")
        status = {s.get("indexerId"): s for s in self.prowlarr.get("indexerstatus")}
        items = []
        for ix in sorted(indexers, key=lambda i: i.get("name", "").lower()):
            st = status.get(ix.get("id"))
            if not ix.get("enable"):
                state, ok = "Turned off", None
            elif st and _when(st.get("disabledTill")) and _when(st["disabledTill"]) > datetime.now(timezone.utc):
                since = _when(st.get("initialFailure") or st.get("mostRecentFailure"))
                state = f"Not responding{' since ' + _short(since) if since else ''}. Next try {_short(_when(st['disabledTill']))}."
                ok = False
            else:
                state, ok = "Working", True
            items.append({"name": ix.get("name", "?"), "text": state, "ok": ok})
        version = self.prowlarr.get("system/status").get("version", "?")
        health = [redact(h.get("message", "")) for h in self.prowlarr.get("health")]
        return {
            "facts": [["Prowlarr version", version], ["Search sites", f"{sum(1 for i in items if i['ok'])} of {len(items)} working"]],
            "items": items,
            "messages": health,
        }

    def _arr(self, app):
        st = app.get("system/status")
        queue = app.get("queue", pageSize=1).get("totalRecords", 0)
        health = app.get("health")
        roots = app.get("rootfolder")
        facts = [[f"{st.get('appName', app.name.title())} version", st.get("version", "?")], ["In the download queue", str(queue)]]
        for r in roots:
            facts.append([r.get("path", "?"), f"{(r.get('freeSpace') or 0) / 1e9:.0f} GB free" + ("" if r.get("accessible", True) else " (can't reach it)")])
        return {"facts": facts, "items": [], "messages": [redact(f"{h.get('message', '')}") for h in health]}

    def _downloads(self):
        stats = self.tr.call("session-stats")
        sess = self.tr.call("session-get", fields=["version", "download-dir"])
        mb = lambda b: f"{(b or 0) / 1e6:.1f} MB/s"  # noqa: E731
        return {
            "facts": [
                ["Transmission", sess.get("version", "?")],
                ["Downloading at", mb(stats.get("downloadSpeed"))],
                ["Sharing at", mb(stats.get("uploadSpeed"))],
                ["Active", str(stats.get("activeTorrentCount", 0))],
                ["Paused or finished", str(stats.get("pausedTorrentCount", 0))],
            ],
            "items": [],
        }

    def _watching(self):
        info = self.jf.get("/System/Info")
        sessions = self.jf.get("/Sessions", activeWithinSeconds="600")
        items = []
        for s in sessions:
            np = s.get("NowPlayingItem")
            if not np:
                continue
            what = np.get("Name", "")
            if np.get("SeriesName"):
                what = " ".join(p for p in (np["SeriesName"], episode_label(np.get("ParentIndexNumber"), np.get("IndexNumber"))) if p)
            ps = s.get("PlayState") or {}
            pos, total = ps.get("PositionTicks") or 0, np.get("RunTimeTicks") or 0
            pct = f", {int(pos / total * 100)}%" if total else ""
            how = "transcoding" if (s.get("TranscodingInfo") or {}).get("IsVideoDirect") is False else "direct play"
            items.append(
                {
                    "name": f"{s.get('DeviceName', '?')} ({s.get('Client', '')})",
                    "text": f"{'Paused: ' if ps.get('IsPaused') else ''}{what}{pct} · {how}",
                    "ok": True,
                }
            )
        return {"facts": [["Jellyfin version", info.get("Version", "?")], ["Watching now", str(len(items))]], "items": items}

    # logs ----------------------------------------------------------------------------
    def logs(self, source: str, level: str):
        floor = LEVELS.get(level, 0)
        if source == "usher":
            lines = list(RING.lines)[::-1]
        elif source in ("sonarr", "radarr", "prowlarr"):
            app = {"sonarr": self.sonarr, "radarr": self.radarr, "prowlarr": self.prowlarr}[source]
            recs = app.get("log", page=1, pageSize=200, sortKey="time", sortDirection="descending").get("records", [])
            lines = [
                {
                    "time": r.get("time"),
                    "level": r.get("level", "info").lower(),
                    "source": r.get("logger", ""),
                    "message": redact((r.get("message") or "") + (f" — {r['exception']}" if r.get("exception") else "")),
                }
                for r in recs
            ]
        elif source == "jellyfin":
            lines = self._jellyfin_log()
        else:
            raise ApiError("unknown log")
        keep = []
        for ln in lines:
            lvl = ln["level"]
            rank = ARR_LEVEL.get(lvl, LEVELS.get(lvl, 1))
            if rank >= floor:
                ln["level"] = "error" if rank >= 3 else "warn" if rank == 2 else "info"
                ln["message"] = ln["message"][:2000]
                keep.append(ln)
        return {"source": source, "lines": keep[:200]}

    def _jellyfin_log(self):
        files = sorted(self.jf.get("/System/Logs"), key=lambda f: f.get("DateModified", ""), reverse=True)
        files = [f for f in files if f.get("Name", "").startswith("log_")] or files
        if not files:
            return []
        text = self.jf.get_text("/System/Logs/Log", name=files[0]["Name"])
        out, last = [], None
        for raw in text.splitlines()[-1500:]:
            m = JF_LINE.match(raw)
            if m:
                last = {
                    "time": m.group(1),
                    "level": {"VRB": "debug", "DBG": "debug", "INF": "info", "WRN": "warn", "ERR": "error", "FTL": "error"}.get(m.group(2), "info"),
                    "source": m.group(3).strip()[:60],
                    "message": redact(m.group(4)),
                }
                out.append(last)
            elif last is not None and raw.strip():  # stack traces continue the previous line
                last["message"] += "\n" + redact(raw)[:300]
        return out[::-1]


def _short(dt):
    local = timezone(timedelta(minutes=config.get("server.utc_offset_minutes")))  # the server's local offset
    return dt.astimezone(local).strftime("%-d %b %H:%M") if dt else ""


def _ago(ts):
    secs = int(time.time() - ts)
    if secs < 3600:
        return f"{secs // 60} min"
    if secs < 86400:
        return f"{secs // 3600} hr {secs % 3600 // 60} min"
    return f"{secs // 86400} days"
