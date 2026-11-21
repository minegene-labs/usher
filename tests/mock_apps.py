"""Fake Sonarr, Radarr, Prowlarr, Transmission and Jellyfin for local testing. Not used on the server.

Run:  python3 -m tests.mock_apps   (ports 9901-9906)
"""

import io
import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

KEY = "a" * 32
JF_KEY = "b" * 32
now = datetime.now(timezone.utc)
iso = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731

SERIES = [
    {
        "id": 1,
        "tvdbId": 100,
        "title": "Slow Horses",
        "sortTitle": "slow horses",
        "year": 2022,
        "network": "Apple TV+",
        "ended": False,
        "added": iso(now - timedelta(days=40)),
        "overview": "A team of British intelligence agents who serve in a dumping ground department of MI5.",
        "statistics": {"seasonCount": 5, "episodeFileCount": 27, "episodeCount": 30, "sizeOnDisk": 41e9},
    },
    {
        "id": 2,
        "tvdbId": 200,
        "title": "Severance",
        "sortTitle": "severance",
        "year": 2022,
        "network": "Apple TV+",
        "ended": False,
        "added": iso(now - timedelta(days=12)),
        "overview": "Mark leads a team of office workers whose memories have been surgically divided.",
        "statistics": {"seasonCount": 2, "episodeFileCount": 14, "episodeCount": 19, "sizeOnDisk": 22e9},
    },
    {
        "id": 3,
        "tvdbId": 300,
        "title": "Bluey",
        "sortTitle": "bluey",
        "year": 2018,
        "network": "ABC",
        "ended": False,
        "added": iso(now - timedelta(days=200)),
        "overview": "A lovable Blue Heeler puppy turns everyday family life into extraordinary adventures.",
        "statistics": {"seasonCount": 3, "episodeFileCount": 151, "episodeCount": 151, "sizeOnDisk": 30e9},
    },
    {
        "id": 4,
        "tvdbId": 400,
        "title": "The Bear",
        "sortTitle": "bear",
        "year": 2022,
        "network": "FX",
        "ended": False,
        "added": iso(now - timedelta(days=5)),
        "overview": "A young chef returns to Chicago to run his family's sandwich shop.",
        "statistics": {"seasonCount": 4, "episodeFileCount": 18, "episodeCount": 38, "sizeOnDisk": 19e9},
    },
]
MOVIES = [
    {
        "id": 11,
        "tmdbId": 11000,
        "title": "Dune: Part Two",
        "sortTitle": "dune part two",
        "year": 2024,
        "monitored": True,
        "hasFile": False,
        "isAvailable": True,
        "added": iso(now - timedelta(days=4)),
        "runtime": 166,
        "sizeOnDisk": 0,
        "overview": "Paul Atreides unites with the Fremen while on a path of revenge.",
    },
    {
        "id": 12,
        "tmdbId": 12000,
        "title": "Paddington in Peru",
        "sortTitle": "paddington in peru",
        "year": 2024,
        "monitored": True,
        "hasFile": True,
        "isAvailable": True,
        "added": iso(now - timedelta(days=2)),
        "runtime": 106,
        "sizeOnDisk": 3.1e9,
        "overview": "Paddington travels to Peru to visit his beloved Aunt Lucy.",
    },
    {
        "id": 13,
        "tmdbId": 13000,
        "title": "The Wild Robot",
        "sortTitle": "wild robot",
        "year": 2024,
        "monitored": True,
        "hasFile": False,
        "isAvailable": True,
        "added": iso(now - timedelta(hours=3)),
        "runtime": 102,
        "sizeOnDisk": 0,
        "overview": "A robot is shipwrecked on an uninhabited island.",
    },
    {
        "id": 14,
        "tmdbId": 14000,
        "title": "Mission: Impossible – The Final Reckoning",
        "sortTitle": "mission impossible final reckoning",
        "year": 2025,
        "monitored": True,
        "hasFile": False,
        "isAvailable": False,
        "added": iso(now - timedelta(days=1)),
        "runtime": 169,
        "sizeOnDisk": 0,
        "overview": "Ethan Hunt and the IMF team continue their mission.",
        "digitalRelease": iso(now + timedelta(days=6)),
    },
    {
        "id": 15,
        "tmdbId": 15000,
        "title": "Inside Out 2",
        "sortTitle": "inside out 2",
        "year": 2024,
        "monitored": False,
        "hasFile": True,
        "isAvailable": True,
        "added": iso(now - timedelta(days=90)),
        "runtime": 96,
        "sizeOnDisk": 2.4e9,
        "overview": "Riley's mind welcomes some new emotions.",
    },
]
for _x in SERIES:
    _x["path"] = f"/srv/media/TV Shows/{_x['title']}"
    _x["images"] = [{"coverType": "poster", "url": f"/MediaCover/{_x['id']}/poster.jpg?lastWrite=1"}]
for _x in MOVIES:
    _x["path"] = f"/srv/media/Movies/{_x['title']} ({_x['year']})"
    _x["images"] = [{"coverType": "poster", "url": f"/MediaCover/{_x['id']}/poster.jpg?lastWrite=1"}]
SERIES[1]["seasons"] = [{"seasonNumber": 1, "monitored": True}, {"seasonNumber": 2, "monitored": True}]
EPISODES = [{"id": 2000 + n, "seriesId": 2, "seasonNumber": 1, "episodeNumber": n, "hasFile": True, "monitored": True} for n in range(1, 10)]
JF_UPDATES = []  # what Usher told the fake media server had changed (POST /Library/Media/Updated)
HASH_A, HASH_B, HASH_C = "a1" * 20, "b2" * 20, "c3" * 20
start = time.time()


def torrents():
    t = time.time() - start
    pa = min(0.999, 0.42 + t / 600)
    return [
        {
            "hashString": HASH_A,
            "percentDone": pa,
            "eta": int((1 - pa) * 900),
            "rateDownload": 2_500_000,
            "status": 4,
            "error": 0,
            "errorString": "",
            "leftUntilDone": 1,
            "sizeWhenDone": 2,
        },
        {
            "hashString": HASH_B,
            "percentDone": 0.0,
            "eta": -1,
            "rateDownload": 0,
            "status": 3,
            "error": 0,
            "errorString": "",
            "leftUntilDone": 1,
            "sizeWhenDone": 2,
        },
        {
            "hashString": HASH_C,
            "percentDone": 0.31,
            "eta": -1,
            "rateDownload": 0,
            "status": 0,
            "error": 3,
            "errorString": "No data found",
            "leftUntilDone": 1,
            "sizeWhenDone": 2,
        },
    ]


QUEUE_TV = [
    {
        "id": 101,
        "seriesId": 2,
        "episodeId": 2005,
        "downloadId": HASH_A.upper(),
        "status": "downloading",
        "trackedDownloadStatus": "ok",
        "trackedDownloadState": "downloading",
        "size": 2e9,
        "sizeleft": 1.1e9,
        "timeleft": "00:12:30",
        "series": SERIES[1],
        "episode": {"seasonNumber": 2, "episodeNumber": 5, "title": "Trojan's Horse"},
    },
] + [
    {
        "id": 110 + n,
        "seriesId": 4,
        "episodeId": 4100 + n,
        "downloadId": HASH_B.upper(),
        "status": "queued",
        "trackedDownloadStatus": "ok",
        "trackedDownloadState": "downloading",
        "size": 9e9,
        "sizeleft": 9e9,
        "timeleft": None,
        "series": SERIES[3],
        "episode": {"seasonNumber": 3, "episodeNumber": n, "title": ""},
    }
    for n in range(1, 11)
]
QUEUE_MOVIES = [
    {
        "id": 201,
        "movieId": 13,
        "downloadId": HASH_C.upper(),
        "status": "warning",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "downloading",
        "size": 3e9,
        "sizeleft": 2e9,
        "timeleft": None,
        "movie": MOVIES[2],
    },
]
MISSING = [
    {"id": 1029, "seriesId": 1, "seasonNumber": 5, "episodeNumber": 6, "airDateUtc": iso(now - timedelta(days=5)), "series": SERIES[0]},
    {"id": 2006, "seriesId": 2, "seasonNumber": 2, "episodeNumber": 6, "airDateUtc": iso(now - timedelta(hours=20)), "series": SERIES[1]},
]
CAL_TV = [
    {
        "seriesId": 1,
        "seasonNumber": 5,
        "episodeNumber": 7,
        "title": "Hello Goodbye",
        "hasFile": False,
        "airDateUtc": iso((now + timedelta(days=1)).replace(hour=1, minute=0)),
        "series": SERIES[0],
    },
    {
        "seriesId": 2,
        "seasonNumber": 2,
        "episodeNumber": 7,
        "title": "Chikhai Bardo",
        "hasFile": False,
        "airDateUtc": iso((now + timedelta(days=3)).replace(hour=1, minute=0)),
        "series": SERIES[1],
    },
    {
        "seriesId": 4,
        "seasonNumber": 4,
        "episodeNumber": 1,
        "title": "Groundhog",
        "hasFile": False,
        "airDateUtc": iso((now + timedelta(days=9)).replace(hour=14, minute=0)),
        "series": SERIES[3],
    },
]
JF_ITEMS = [
    {
        "Id": "1" * 32,
        "Type": "Episode",
        "Name": "Hello Goodbye",
        "SeriesName": "Slow Horses",
        "SeriesId": "a" * 32,
        "ParentIndexNumber": 5,
        "IndexNumber": 5,
        "DateCreated": iso(now - timedelta(hours=6)),
    },
    {
        "Id": "2" * 32,
        "Type": "Episode",
        "Name": "Return",
        "SeriesName": "Slow Horses",
        "SeriesId": "a" * 32,
        "ParentIndexNumber": 5,
        "IndexNumber": 4,
        "DateCreated": iso(now - timedelta(days=2)),
    },
    {
        "Id": "3" * 32,
        "Type": "Movie",
        "Name": "Paddington in Peru",
        "ProductionYear": 2024,
        "DateCreated": iso(now - timedelta(days=1)),
        "ProviderIds": {"Tmdb": "12000"},
        "Path": "/srv/media/Movies/Paddington in Peru (2024)/Paddington in Peru.mkv",
    },
    {
        "Id": "4" * 32,
        "Type": "Episode",
        "Name": "Hide and Seek",
        "SeriesName": "Severance",
        "SeriesId": "c" * 32,
        "ParentIndexNumber": 2,
        "IndexNumber": 4,
        "DateCreated": iso(now - timedelta(days=3)),
        "Path": "/srv/media/TV Shows/Severance/Season 2/Severance S02E04.mkv",
    },
    {"Id": "5" * 32, "Type": "Movie", "Name": "Old Movie", "ProductionYear": 1999, "DateCreated": iso(now - timedelta(days=200))},
]
JF_EXTRA = [{"Id": "a" * 32, "Type": "Series"}, {"Id": "c" * 32, "Type": "Series"}]
COLOURS = {
    1: (40, 60, 90),
    2: (20, 90, 110),
    3: (80, 140, 200),
    4: (140, 70, 40),
    11: (180, 130, 60),
    12: (150, 40, 50),
    13: (60, 120, 70),
    14: (30, 30, 30),
    15: (120, 60, 160),
}


def poster_png(seed: int, label: str) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (250, 375), COLOURS.get(seed, (70, 70, 90)))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 270, 250, 375], fill=(0, 0, 0))
    d.text((14, 290), label[:22], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


# ---- a simulated request: added -> searching -> downloading -> importing -> in library -> in Jellyfin ----------
# Compressed timeline in seconds since "Get it".
T_FOUND, T_DONE, T_IMPORTED, T_IN_JELLYFIN = 8, 40, 48, 54
SIM: dict = {}  # (kind, arr_id) -> {"t": added time, "hash": ..., "eps": n}
LOOKUP_TV = [
    {
        "tvdbId": 500,
        "tmdbId": 500,
        "title": "Shrinking",
        "year": 2023,
        "network": "Apple TV+",
        "status": "continuing",
        "ended": False,
        "overview": "A grieving therapist starts breaking the rules and telling clients exactly what he thinks.",
        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
        "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/original/shrinking.jpg"}],
        "seriesType": "standard",
        "titleSlug": "shrinking",
    },
    dict(SERIES[0], images=[]),
]
LOOKUP_MOVIES = [
    {
        "tmdbId": 286217,
        "title": "The Martian",
        "year": 2015,
        "runtime": 144,
        "studio": "20th Century Fox",
        "ratings": {"imdb": {"value": 8.0, "votes": 950000}, "rottenTomatoes": {"value": 91}, "metacritic": {"value": 80}},
        "overview": "An astronaut is stranded on Mars and must find a way to survive.",
        "isAvailable": True,
        "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/original/martian.jpg"}],
    },
    dict(MOVIES[1], images=[]),
]
COLOURS.update({5: (200, 120, 40), 16: (170, 60, 30)})


def sim_phase(key):
    t = time.time() - SIM[key]["t"]
    if t < T_FOUND:
        return "searching", 0
    if t < T_DONE:
        return "downloading", (t - T_FOUND) / (T_DONE - T_FOUND)
    if t < T_IMPORTED:
        return "importing", 1
    return ("imported" if t < T_IN_JELLYFIN else "jellyfin"), 1


def sim_torrents():
    out = []
    for key, v in SIM.items():
        phase, prog = sim_phase(key)
        if phase in ("downloading", "importing"):
            out.append(
                {
                    "hashString": v["hash"],
                    "percentDone": min(prog, 1.0),
                    "eta": int((1 - prog) * 600),
                    "rateDownload": 3_000_000,
                    "status": 4 if prog < 1 else 6,
                    "error": 0,
                    "errorString": "",
                    "leftUntilDone": 1,
                    "sizeWhenDone": 2,
                }
            )
    return out


def sim_queue(kind):
    recs = []
    for (k, arr_id), v in SIM.items():
        if k != kind:
            continue
        phase, prog = sim_phase((k, arr_id))
        if phase not in ("downloading", "importing"):
            continue
        state = "importing" if phase == "importing" else "downloading"
        base = {
            "downloadId": v["hash"].upper(),
            "status": "downloading",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": state,
            "size": 4e9,
            "sizeleft": 4e9 * (1 - prog),
            "timeleft": "00:05:00",
        }
        if kind == "movie":
            recs.append(dict(base, id=900 + arr_id, movieId=arr_id, movie=next(m for m in MOVIES if m["id"] == arr_id)))
        else:
            series = next(x for x in SERIES if x["id"] == arr_id)
            for n in range(1, v["eps"] + 1):
                recs.append(
                    dict(
                        base,
                        id=9000 + arr_id * 10 + n,
                        seriesId=arr_id,
                        episodeId=50000 + n,
                        series=series,
                        episode={"seasonNumber": 2, "episodeNumber": n, "title": ""},
                    )
                )
    return recs


def sim_update():
    """Flip library state as simulated downloads finish."""
    for (k, arr_id), v in SIM.items():
        phase, _ = sim_phase((k, arr_id))
        done = phase in ("imported", "jellyfin")
        if k == "movie":
            m = next(m for m in MOVIES if m["id"] == arr_id)
            m["hasFile"], m["sizeOnDisk"] = done, (8.1e9 if done else 0)
        else:
            st = next(x for x in SERIES if x["id"] == arr_id)["statistics"]
            st["episodeFileCount"] = v["eps"] if done else 0


def sim_jellyfin(term):
    out = []
    for (k, arr_id), v in SIM.items():
        if sim_phase((k, arr_id))[0] != "jellyfin":
            continue
        src = MOVIES if k == "movie" else SERIES
        item = next(x for x in src if x["id"] == arr_id)
        if term.lower() in item["title"].lower():
            out.append(
                {
                    "Id": f"{arr_id:032x}",
                    "Name": item["title"],
                    "Type": "Movie" if k == "movie" else "Series",
                    "ProviderIds": {"Tmdb": str(item.get("tmdbId")), "Tvdb": str(item.get("tvdbId"))},
                }
            )
    return out


class Base(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, data, code=200, ctype="application/json"):
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def arr_handler(app):
    class H(Base):
        def do_GET(self):
            if self.headers.get("X-Api-Key") != KEY:
                return self.reply({"error": "unauthorised"}, 401)
            u = urlsplit(self.path)
            p = u.path
            sim_update()
            qs = parse_qs(u.query)
            if p.endswith("/lookup"):
                term = qs.get("term", [""])[0].lower()
                pool = LOOKUP_TV if app == "sonarr" else LOOKUP_MOVIES
                if term.startswith(("tvdb:", "tmdb:")):
                    ext = int(term.split(":")[1])
                    return self.reply([x for x in pool if x.get("tvdbId", x.get("tmdbId")) == ext])
                lib = SERIES if app == "sonarr" else MOVIES
                res = []
                for x in pool:
                    if term in x["title"].lower():
                        known = next((y for y in lib if y.get("tvdbId", y.get("tmdbId")) == x.get("tvdbId", x.get("tmdbId"))), None)
                        res.append(
                            dict(
                                x,
                                **(
                                    {
                                        "id": known["id"],
                                        "hasFile": known.get("hasFile"),
                                        "monitored": known.get("monitored", True),
                                        "statistics": known.get("statistics"),
                                    }
                                    if known
                                    else {}
                                ),
                            )
                        )
                return self.reply(res)
            if p.endswith("/system/status"):
                return self.reply({"appName": app.title(), "version": "4.0.99 (mock)"})
            if p.endswith("/log"):
                return self.reply(
                    {
                        "records": [
                            {
                                "time": iso(now - timedelta(minutes=3)),
                                "level": "warn",
                                "logger": "IndexerStatusCheck",
                                "message": "Indexers unavailable due to failures: YTS",
                            },
                            {
                                "time": iso(now - timedelta(minutes=9)),
                                "level": "info",
                                "logger": "DownloadDecisionMaker",
                                "message": "Processing 52 releases from https://example/api?apikey=SECRETSECRET123",
                            },
                            {
                                "time": iso(now - timedelta(hours=2)),
                                "level": "error",
                                "logger": "ImportApprovedEpisodes",
                                "message": "Not enough free space",
                                "exception": "System.IO.IOException: No space left",
                            },
                        ]
                    }
                )
            if app == "prowlarr" and p.endswith("/indexer"):
                return self.reply(
                    [{"id": 1, "name": "EZTV", "enable": True}, {"id": 2, "name": "YTS", "enable": True}, {"id": 3, "name": "1337x", "enable": True}]
                )
            if app == "prowlarr" and p.endswith("/indexerstatus"):
                return self.reply(
                    [
                        {
                            "indexerId": 2,
                            "initialFailure": iso(now - timedelta(hours=20)),
                            "mostRecentFailure": iso(now - timedelta(minutes=20)),
                            "disabledTill": iso(now + timedelta(hours=1)),
                        }
                    ]
                )
            if p.endswith("/qualityprofile"):
                return self.reply([{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}])
            if p.endswith("/downloadclient"):
                return self.reply([{"id": 1, "name": "Transmission", "implementation": "Transmission", "enable": True}])
            if app == "prowlarr" and p.endswith("/applications"):
                return self.reply(
                    [
                        {"id": 1, "name": "Sonarr", "implementation": "Sonarr", "syncLevel": "fullSync"},
                        {"id": 2, "name": "Radarr", "implementation": "Radarr", "syncLevel": "fullSync"},
                    ]
                )
            if p.endswith("/rootfolder"):
                return self.reply([{"path": "/srv/media/TV Shows" if app == "sonarr" else "/srv/media/Movies"}])
            if app == "radarr" and p.startswith("/api/v3/movie/") and p.split("/")[-1].isdigit():
                return self.reply(next(m for m in MOVIES if m["id"] == int(p.split("/")[-1])))
            if app == "sonarr" and p.startswith("/api/v3/series/") and p.split("/")[-1].isdigit():
                return self.reply(next(m for m in SERIES if m["id"] == int(p.split("/")[-1])))
            if app == "sonarr":
                routes = {
                    "/api/v3/queue": {"totalRecords": len(QUEUE_TV + sim_queue("show")), "records": QUEUE_TV + sim_queue("show")},
                    "/api/v3/wanted/missing": {"records": MISSING},
                    "/api/v3/series": SERIES,
                    "/api/v3/config/mediamanagement": {"recycleBin": "/recycle", "recycleBinCleanupDays": 7},
                    "/api/v3/calendar": CAL_TV,
                    "/api/v3/health": [{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers unavailable due to failures: YTS"}],
                    "/api/v3/diskspace": [{"path": "/", "freeSpace": 28.4e9, "totalSpace": 243e9}, {"path": "/boot", "freeSpace": 1.5e9, "totalSpace": 2e9}],
                }
            elif app == "radarr":
                routes = {
                    "/api/v3/queue": {"totalRecords": len(QUEUE_MOVIES + sim_queue("movie")), "records": QUEUE_MOVIES + sim_queue("movie")},
                    "/api/v3/movie": MOVIES,
                    "/api/v3/config/mediamanagement": {"recycleBin": "/recycle", "recycleBinCleanupDays": 7},
                    "/api/v3/calendar": [m for m in MOVIES if m.get("digitalRelease")],
                    "/api/v3/health": [{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers unavailable due to failures: YTS"}],
                }
            else:
                routes = {"/api/v1/health": [{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers unavailable due to failures: YTS"}]}
            if p in routes:
                return self.reply(routes[p])
            if app == "sonarr" and p in ("/api/v3/episode", "/api/v3/episodefile"):
                sid = int(qs.get("seriesId", ["0"])[0])
                eps = [e for e in EPISODES if e["seriesId"] == sid]
                if p.endswith("episode"):
                    return self.reply(eps)
                return self.reply([{"id": e["id"] + 50000, "seasonNumber": e["seasonNumber"]} for e in eps if e["hasFile"]])
            if app == "radarr" and p == "/api/v3/moviefile":
                mid = int(qs.get("movieId", ["0"])[0])
                return self.reply([{"id": mid + 70000}] if any(m["id"] == mid and m["hasFile"] for m in MOVIES) else [])
            if p == "/api/v3/queue" and qs.get("pageSize") == ["1"]:
                return self.reply({"totalRecords": 3, "records": []})
            if "/mediacover/" in p:
                sid = int(p.split("/")[4])
                title = next((x["title"] for x in SERIES + MOVIES if x["id"] == sid), "?")
                return self.reply(poster_png(sid, title), ctype="image/jpeg")
            return self.reply({"error": "not found"}, 404)

        def body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}

        def do_POST(self):
            data, p = self.body(), urlsplit(self.path).path
            if p == "/api/v3/series" and any(x.get("tvdbId") == data["tvdbId"] for x in SERIES):
                return self.reply([{"errorMessage": "This series has already been added"}], 400)
            if p == "/api/v3/movie" and any(x.get("tmdbId") == data["tmdbId"] for x in MOVIES):
                return self.reply([{"errorMessage": "This movie has already been added"}], 400)
            if p == "/api/v3/series":
                assert data["qualityProfileId"] == 4 and data["rootFolderPath"] == "/srv/media/TV Shows", data
                new = {
                    "id": max(x["id"] for x in SERIES) + 1,
                    "tvdbId": data["tvdbId"],
                    "title": data["title"],
                    "sortTitle": data["title"].lower(),
                    "year": data.get("year"),
                    "network": data.get("network"),
                    "ended": False,
                    "added": iso(datetime.now(timezone.utc)),
                    "overview": data.get("overview", ""),
                    "statistics": {
                        "seasonCount": 2,
                        "episodeFileCount": 0,
                        "episodeCount": 0 if data["addOptions"]["monitor"] == "future" else 3,
                        "sizeOnDisk": 0,
                    },
                }
                SERIES.append(new)
                if data["addOptions"]["searchForMissingEpisodes"]:
                    SIM[("show", new["id"])] = {"t": time.time(), "hash": f"{new['id']:040x}", "eps": 3}
                return self.reply(new, 201)
            if p == "/api/v3/movie":
                assert data["qualityProfileId"] == 4 and data["addOptions"]["searchForMovie"], data
                new = {
                    "id": max(x["id"] for x in MOVIES) + 1,
                    "tmdbId": data["tmdbId"],
                    "title": data["title"],
                    "sortTitle": data["title"].lower(),
                    "year": data.get("year"),
                    "monitored": True,
                    "hasFile": False,
                    "isAvailable": True,
                    "added": iso(datetime.now(timezone.utc)),
                    "runtime": data.get("runtime"),
                    "sizeOnDisk": 0,
                    "overview": data.get("overview", ""),
                }
                MOVIES.append(new)
                SIM[("movie", new["id"])] = {"t": time.time(), "hash": f"{new['id']:040x}", "eps": 1}
                return self.reply(new, 201)
            if p == "/api/v3/command":
                for mid in data.get("movieIds", []):
                    SIM[("movie", mid)] = {"t": time.time(), "hash": f"{mid:040x}", "eps": 1}
                return self.reply({"id": 1, "name": data["name"]}, 201)
            return self.reply({"error": "not found"}, 404)

        def do_PUT(self):
            data = self.body()
            for m in MOVIES:
                if m["id"] == data.get("id"):
                    m.update(monitored=data.get("monitored"))
            return self.reply(data, 202)

        def do_DELETE(self):
            p = urlsplit(self.path).path
            parts = p.split("/")
            if parts[3] == "queue":
                qid = int(parts[4])
                for lst in (QUEUE_TV, QUEUE_MOVIES):
                    lst[:] = [q for q in lst if q["id"] != qid]
                return self.reply({}, 200)
            if parts[3] in ("moviefile", "episodefile"):
                return self.reply({}, 200)
            if parts[3] in ("movie", "series"):
                lst = MOVIES if parts[3] == "movie" else SERIES
                lst[:] = [x for x in lst if x["id"] != int(parts[4])]
                return self.reply({}, 200)
            return self.reply({"error": "not found"}, 404)

    return H


def _snake(value):
    """The fake's answers spelled the way Transmission 4.1+ spells them (snake_case keys)."""
    if isinstance(value, dict):
        return {re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", k).replace("-", "_").lower(): _snake(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_snake(v) for v in value]
    return value


class TrHandler(Base):
    """Transmission 4.1+ by default: offers JSON-RPC 2.0 in the handshake and still accepts the old protocol, as
    4.1 does. TrHandler40 is 4.0, which only knows the old one."""

    RPC_VERSION = "6.0.0"
    VERSION = "4.1.0 (mock)"
    seen = []  # (method, style) of every request, for tests

    def do_POST(self):
        if self.headers.get("X-Transmission-Session-Id") != "sid123":
            self.send_response(409)
            self.send_header("X-Transmission-Session-Id", "sid123")
            if self.RPC_VERSION:
                self.send_header("X-Transmission-Rpc-Version", self.RPC_VERSION)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        jsonrpc = req.get("jsonrpc") == "2.0"
        if jsonrpc and not self.RPC_VERSION:
            return self.reply({"result": "method name not recognized", "arguments": {}})
        method = (req.get("method") or "").replace("_", "-")
        type(self).seen.append((method, "jsonrpc" if jsonrpc else "old"))
        if jsonrpc and req.get("method") != method.replace("-", "_"):
            return self.reply({"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found"}, "id": req.get("id")})
        answer = self.answer(method, req.get("params" if jsonrpc else "arguments") or {})
        if answer is None:
            if jsonrpc:
                return self.reply(
                    {"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found", "data": {"error_string": method}}, "id": req.get("id")}
                )
            return self.reply({"result": "method name not recognized", "arguments": {}})
        if jsonrpc:
            return self.reply({"jsonrpc": "2.0", "result": _snake(answer), "id": req.get("id")})
        return self.reply({"result": "success", "arguments": answer})

    def answer(self, method, args):
        if method == "session-stats":
            return {"downloadSpeed": 2_400_000, "uploadSpeed": 310_000, "activeTorrentCount": 2, "pausedTorrentCount": 5, "torrentCount": 7}
        if method == "session-get":
            return {"version": self.VERSION, "download-dir": "/data/torrents"}
        if method == "free-space":
            return {"path": args.get("path", ""), "size-bytes": 500_000_000_000}
        if method == "torrent-get":
            return {"torrents": torrents() + sim_torrents()}
        return None


class TrHandler40(TrHandler):
    RPC_VERSION = None
    VERSION = "4.0.6 (mock)"


class JfHandler(Base):
    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/health":
            return self.reply(b"Healthy", ctype="text/plain")
        if f'Token="{JF_KEY}"' not in self.headers.get("Authorization", ""):
            return self.reply({"error": "unauthorised"}, 401)
        if u.path in ("/Users/" + "8" * 32, "/Users/" + "9" * 32):
            return self.reply({"Id": u.path[7:], "Policy": {"IsDisabled": False}})
        if u.path == "/Users":
            return self.reply([{"Name": "jellyfin", "Id": "9" * 32}, {"Name": "Viewer", "Id": "8" * 32}])
        if u.path == "/Items":
            q = parse_qs(u.query)
            assert q["userId"] == ["8" * 32]
            if "searchTerm" in q:
                return self.reply({"Items": sim_jellyfin(q["searchTerm"][0])})
            if "Ids" in q:
                ids = set(q["Ids"][0].split(","))
                return self.reply({"Items": [it for it in JF_ITEMS + JF_EXTRA if it["Id"] in ids]})
            return self.reply({"Items": JF_ITEMS})
        if u.path == "/UserViews":
            return self.reply(
                {
                    "Items": [
                        {"Id": "f1" * 16, "Name": "Movies", "CollectionType": "movies"},
                        {"Id": "f2" * 16, "Name": "TV Shows", "CollectionType": "tvshows"},
                        {"Id": "f3" * 16, "Name": "Minecraft", "CollectionType": "homevideos"},
                    ]
                }
            )
        if u.path == "/UserItems/Resume":
            return self.reply(
                {
                    "Items": [
                        {
                            "Id": "4" * 32,
                            "Type": "Episode",
                            "Name": "Hide and Seek",
                            "SeriesName": "Severance",
                            "SeriesId": "c" * 32,
                            "ParentIndexNumber": 2,
                            "IndexNumber": 4,
                            "Path": "/srv/media/TV Shows/Severance/Season 2/Severance S02E04.mkv",
                            "UserData": {"PlayedPercentage": 38},
                            "Overview": "Mark finds a door.",
                        },
                        {
                            "Id": "3" * 32,
                            "Type": "Movie",
                            "Name": "Paddington in Peru",
                            "ProductionYear": 2024,
                            "ProviderIds": {"Tmdb": "12000"},
                            "ImageTags": {"Primary": "p1"},
                            "Path": "/srv/media/Movies/Paddington in Peru (2024)/Paddington in Peru.mkv",
                            "UserData": {"PlayedPercentage": 40},
                        },
                    ]
                }
            )
        if u.path == "/Shows/NextUp":
            return self.reply(
                {
                    "Items": [
                        {
                            "Id": "2" * 32,
                            "Type": "Episode",
                            "Name": "Return",
                            "SeriesName": "Slow Horses",
                            "SeriesId": "a" * 32,
                            "ParentIndexNumber": 5,
                            "IndexNumber": 4,
                            "UserData": {},
                        }
                    ]
                }
            )
        if u.path == "/Items/Latest":
            parent = parse_qs(u.query)["ParentId"][0]
            fresh = iso(datetime.now(timezone.utc) - timedelta(minutes=1))
            if parent == "f1" * 16:
                return self.reply(
                    [
                        {"Id": "3" * 32, "Type": "Movie", "Name": "Paddington in Peru", "ProductionYear": 2024, "DateCreated": fresh, "UserData": {}},
                        {
                            "Id": "6" * 32,
                            "Type": "Movie",
                            "Name": "Inside Out 2",
                            "ProductionYear": 2024,
                            "DateCreated": iso(now - timedelta(days=40)),
                            "UserData": {"Played": True},
                        },
                    ]
                )
            if parent == "f2" * 16:
                return self.reply(
                    [
                        {
                            "Id": "a" * 32,
                            "Type": "Series",
                            "Name": "Slow Horses",
                            "ProductionYear": 2022,
                            "ProviderIds": {"Tvdb": "100"},
                            "Path": "/srv/media/TV Shows/Slow Horses",
                            "DateLastMediaAdded": fresh,
                            "UserData": {"UnplayedItemCount": 2},
                        },
                        {
                            "Id": "c" * 32,
                            "Type": "Series",
                            "Name": "Severance",
                            "ProductionYear": 2022,
                            "ProviderIds": {"Tvdb": "200"},
                            "Path": "/srv/media/TV Shows/Severance",
                            "DateLastMediaAdded": iso(now - timedelta(days=3)),
                            "UserData": {"UnplayedItemCount": 1},
                        },
                    ]
                )
            return self.reply([])
        if u.path == "/System/Info/Public":
            return self.reply({"Id": "e" * 32, "ServerName": "jellyfin", "Version": "12.4.0"})
        if u.path == "/System/Info":
            return self.reply({"Version": "12.4.0 (mock)", "ServerName": "jellyfin", "ProductName": "Jellyfin Server"})
        if u.path == "/Sessions":
            return self.reply(
                [
                    {
                        "DeviceName": "Living room TV",
                        "Client": "Jellyfin",
                        "NowPlayingItem": {"Name": "Hide and Seek", "SeriesName": "Severance", "ParentIndexNumber": 2, "IndexNumber": 4, "RunTimeTicks": 1000},
                        "PlayState": {"PositionTicks": 380, "IsPaused": False},
                    }
                ]
            )
        if u.path == "/System/Logs":
            return self.reply([{"Name": "log_20261104.log", "DateModified": iso(now)}])
        if u.path == "/System/Logs/Log":
            text = (
                "[2026-11-04 12:00:01.123 Z] [INF] [12] Jellyfin.Server: Startup complete\n"
                "[2026-11-04 12:05:44.001 Z] [WRN] [33] MediaBrowser.Providers: Image fetch slow\n"
                "[2026-11-04 12:06:10.500 Z] [ERR] [7] Emby.Server.Implementations: Error processing request\n"
                "   at System.Threading.Tasks.Task.Wait()\n"
            )
            return self.reply(text.encode(), ctype="text/plain")
        if "/Images/" in u.path:
            iid = u.path.split("/")[2]
            seed = {"a" * 32: 1, "c" * 32: 2, "3" * 32: 12, "4" * 32: 2, "2" * 32: 1, "6" * 32: 15}.get(iid, 0)
            return self.reply(poster_png(seed, {1: "Slow Horses", 2: "Severance", 12: "Paddington"}.get(seed, "?")), ctype="image/jpeg")
        return self.reply({"error": "not found"}, 404)

    def do_POST(self):
        if f'Token="{JF_KEY}"' not in self.headers.get("Authorization", ""):
            return self.reply({"error": "unauthorised"}, 401)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if urlsplit(self.path).path == "/Library/Media/Updated":
            JF_UPDATES.extend(body.get("Updates", []))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        return self.reply({"error": "not found"}, 404)


class TmdbHandler(Base):
    """Just enough of TMDb for Discover."""

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer tmdb-test-token":
            return self.reply({"status_message": "Invalid API key"}, 401)
        u = urlsplit(self.path)
        if u.path == "/authentication":
            return self.reply({"success": True})
        q = parse_qs(u.query)

        def tv(i, name, y):
            return {
                "id": i,
                "name": name,
                "first_air_date": f"{y}-01-01",
                "poster_path": f"/p{i}.jpg",
                "overview": f"{name}: a show.",
                "vote_average": 7.9,
                "vote_count": 800,
            }

        def mv(i, title, y):
            return {
                "id": i,
                "title": title,
                "release_date": f"{y}-05-01",
                "poster_path": f"/m{i}.jpg",
                "overview": f"{title}: a film.",
                "vote_average": 7.1,
                "vote_count": 900,
            }

        if u.path == "/3/trending/all/week":
            return self.reply(
                {
                    "results": [
                        dict(tv(500, "Shrinking", 2023), media_type="tv"),
                        dict(mv(286217, "The Martian", 2015), media_type="movie"),
                        {"id": 1, "media_type": "person", "name": "Someone"},
                    ]
                }
            )
        if u.path == "/3/movie/popular":
            return self.reply({"results": [mv(12000, "Paddington in Peru", 2024), mv(286217, "The Martian", 2015)]})
        if u.path == "/3/tv/popular":
            return self.reply({"results": [tv(500, "Shrinking", 2023), tv(777, "No Listings Show", 2025)]})
        if u.path == "/3/watch/providers/tv":
            return self.reply(
                {
                    "results": [
                        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.jpg", "display_priorities": {q["watch_region"][0]: 1}},
                        {"provider_id": 385, "provider_name": "Binge", "logo_path": "/b.jpg", "display_priorities": {q["watch_region"][0]: 3}},
                    ]
                }
            )
        if u.path == "/3/discover/tv" and ("first_air_date.gte" in q or "air_date.gte" in q):
            if q.get("page") != ["1"]:
                return self.reply({"results": []})
            soon = q.get("first_air_date.gte", q.get("air_date.gte"))[0]
            if "first_air_date.gte" in q:  # premieres: one soon, one later
                return self.reply(
                    {
                        "results": [
                            dict(tv(901, "The Lighthouse Keepers", 2026), first_air_date=soon, popularity=90),
                            dict(tv(902, "Harbour Lights", 2026), first_air_date=soon, popularity=40),
                        ]
                    }
                )
            return self.reply({"results": [dict(tv(500, "Shrinking", 2023), popularity=70), dict(tv(903, "Slow Burn", 2022), popularity=30)]})
        if u.path in ("/3/discover/tv", "/3/discover/movie"):
            if u.path.endswith("tv"):
                return self.reply({"results": [tv(500, "Shrinking", 2023)] if q["with_watch_providers"] == ["8"] else []})
            return self.reply({"results": [mv(286217, "The Martian", 2015)]})
        if u.path.startswith("/3/tv/") or u.path.startswith("/3/movie/"):
            kind, iid = u.path.split("/")[2], int(u.path.split("/")[3])
            region = "US"
            base = (
                tv(
                    iid,
                    {500: "Shrinking", 777: "No Listings Show", 901: "The Lighthouse Keepers", 902: "Harbour Lights", 903: "Slow Burn"}.get(iid, "Show"),
                    2023,
                )
                if kind == "tv"
                else mv(iid, {286217: "The Martian", 12000: "Paddington in Peru"}.get(iid, "Film"), 2015)
            )
            base["external_ids"] = {"tvdb_id": 500 if iid == 500 else None}
            base["number_of_seasons"] = 2
            base["networks"] = [{"name": "Apple TV+"}]
            base["runtime"] = 144
            if iid in (901, 902):
                base["first_air_date"] = (date.today() + timedelta(days=3 if iid == 901 else 12)).isoformat()
                base["vote_average"], base["vote_count"] = 0, 0
                base["genres"] = [{"name": "Drama"}, {"name": "Mystery"}]
            if iid in (500, 903):  # 500: new season soon; 903: mid-season, so not listed
                base["next_episode_to_air"] = {
                    "season_number": 3 if iid == 500 else 2,
                    "episode_number": 1 if iid == 500 else 4,
                    "air_date": (date.today() + timedelta(days=20)).isoformat(),
                }
            base["watch/providers"] = {
                "results": {
                    region: {"flatrate": [{"provider_name": "Netflix", "logo_path": "/n.jpg"}]}
                    if iid in (500, 286217)
                    else {"rent": [{"provider_name": "Store"}]}
                }
            }
            return self.reply(base)
        return self.reply({"status_message": "not found"}, 404)


def main():
    servers = [
        (9901, arr_handler("sonarr")),
        (9902, arr_handler("radarr")),
        (9903, arr_handler("prowlarr")),
        (9904, TrHandler),
        (9905, JfHandler),
        (9906, TmdbHandler),
    ]
    for port, handler in servers:
        srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("mock apps on 9901-9906", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
