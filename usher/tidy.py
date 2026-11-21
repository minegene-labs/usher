"""Tidy-up: movies and whole show seasons the viewing account has finished watching, biggest first.

Watched means played on the shared viewing account in Jellyfin (media_server.user_id). A season counts only when
every episode of it that's here has been played. Nothing is ever removed by itself: people pick what goes, and
removing unmonitors it (so it isn't downloaded again) and deletes its files through Sonarr or Radarr, which put
them in the recycle bin if the app has one.
"""

import logging

from .clients import ApiError
from .kept import kept_key
from .titles import arr_art

log = logging.getLogger("usher.tidy")


def _ids(item):
    return {k.lower(): str(v).lower() for k, v in (item.get("ProviderIds") or {}).items() if v}


def _under(path, folder):
    path, folder = (path or "").rstrip("/"), (folder or "").rstrip("/")
    return bool(folder) and (path == folder or path.startswith(folder + "/"))


def _later(a, b):
    return max(x for x in (a, b) if x) if (a or b) else None


def candidates(movies, series, jf_movies, jf_series, jf_episodes, busy_movies=(), busy_series=(), kept=()):
    """What could be tidied away, biggest first.

    movies, series: Radarr's and Sonarr's lists. jf_*: Jellyfin items for the viewing account with UserData,
    ProviderIds and Path. A title in two libraries (Movies and Family Movies) is two Jellyfin items for one file;
    it counts as watched when either copy is. busy_*: Radarr/Sonarr ids with something downloading, left out.
    kept: catalogue keys (kept.py) of titles people asked to keep out of the list, every season of a show included.
    """
    out = []

    # Movies --------------------------------------------------------------------------------
    for m in movies:
        if not m.get("hasFile") or m.get("id") in busy_movies or kept_key("movie", m) in kept:
            continue
        tmdb, imdb = str(m.get("tmdbId") or "").lower(), str(m.get("imdbId") or "").lower()
        mine = [
            it
            for it in jf_movies
            if (tmdb and _ids(it).get("tmdb") == tmdb) or (imdb and _ids(it).get("imdb") == imdb) or _under(it.get("Path"), m.get("path"))
        ]
        played = [it for it in mine if (it.get("UserData") or {}).get("Played")]
        if not played:
            continue
        last = None
        for it in played:
            last = _later(last, (it.get("UserData") or {}).get("LastPlayedDate"))
        out.append(
            {
                "kind": "movie",
                "id": m["id"],
                "key": kept_key("movie", m),
                "season": None,
                "title": m.get("title", "Movie"),
                "subtitle": str(m.get("year") or ""),
                "size": m.get("sizeOnDisk") or 0,
                "art": arr_art("movie", m),
                "watched": last,
            }
        )

    # Show seasons ----------------------------------------------------------------------------
    # Which Sonarr show each Jellyfin show is: by TVDb, TMDb or IMDb id, else by folder.
    by_jf = {}
    for s in series:
        want = {"tvdb": str(s.get("tvdbId") or "").lower(), "tmdb": str(s.get("tmdbId") or "").lower(), "imdb": str(s.get("imdbId") or "").lower()}
        for it in jf_series:
            have = _ids(it)
            if any(v and have.get(k) == v for k, v in want.items()) or _under(it.get("Path"), s.get("path")):
                by_jf[it["Id"]] = s["id"]
    # (Sonarr show, season) -> {episode number: (played?, last played)}; a copy in two libraries is one episode.
    eps: dict = {}
    for it in jf_episodes:
        sid = by_jf.get(it.get("SeriesId"))
        if sid is None or it.get("ParentIndexNumber") is None or it.get("IndexNumber") is None:
            continue
        ud = it.get("UserData") or {}
        bucket = eps.setdefault((sid, it["ParentIndexNumber"]), {})
        first = it["IndexNumber"]
        for n in range(first, (it.get("IndexNumberEnd") or first) + 1):  # a double episode is one file
            was = bucket.get(n, (False, None))
            bucket[n] = (was[0] or bool(ud.get("Played")), _later(was[1], ud.get("LastPlayedDate")))
    for s in series:
        if s["id"] in busy_series or kept_key("show", s) in kept:
            continue
        for season in s.get("seasons") or []:
            n = season.get("seasonNumber")
            st = season.get("statistics") or {}
            files, size = st.get("episodeFileCount") or 0, st.get("sizeOnDisk") or 0
            if not files or not size or st.get("nextAiring"):
                continue  # nothing here, or still airing: more episodes are on the way
            seen = eps.get((s["id"], n)) or {}
            # Every episode here must be in Jellyfin and played; fewer in Jellyfin means it hasn't caught up.
            if len(seen) < files or not all(p for p, _ in seen.values()):
                continue
            last = None
            for _, when in seen.values():
                last = _later(last, when)
            label = "Specials" if n == 0 else f"Season {n}"
            out.append(
                {
                    "kind": "season",
                    "id": s["id"],
                    "key": kept_key("show", s),
                    "season": n,
                    "title": s.get("title", "TV show"),
                    "subtitle": f"{label} · {files} episode{'s' if files != 1 else ''}",
                    "size": size,
                    "art": arr_art("show", s),
                    "watched": last,
                }
            )

    out.sort(key=lambda x: -x["size"])
    return out


def kept_titles(movies, series, kept):
    """The kept titles still on the server, by name. A kept title that Radarr or Sonarr no longer has, or that has no
    files left, isn't listed (and isn't an error): it would have nothing to put back."""
    out = []
    for m in movies:
        key = kept_key("movie", m)
        if key in kept and m.get("hasFile"):
            out.append(
                {
                    "key": key,
                    "kind": "movie",
                    "title": m.get("title", "Movie"),
                    "subtitle": str(m.get("year") or ""),
                    "size": m.get("sizeOnDisk") or 0,
                    "art": arr_art("movie", m),
                }
            )
    for s in series:
        key = kept_key("show", s)
        files = (s.get("statistics") or {}).get("episodeFileCount") or 0
        if key in kept and files:
            out.append(
                {
                    "key": key,
                    "kind": "show",
                    "title": s.get("title", "TV show"),
                    "subtitle": "TV show",
                    "size": (s.get("statistics") or {}).get("sizeOnDisk") or 0,
                    "art": arr_art("show", s),
                }
            )
    out.sort(key=lambda x: x["title"].lower())
    return out


def jellyfin_items(jf):
    """The viewing account's movies, shows and episodes with what's been played, for candidates()."""
    common = dict(userId=jf.user_id(), Recursive="true", Fields="ProviderIds,Path", EnableImages="false", Limit="50000")
    movies = jf.get("/Items", IncludeItemTypes="Movie", **common).get("Items", [])
    shows = jf.get("/Items", IncludeItemTypes="Series", **common).get("Items", [])
    episodes = jf.get("/Items", IncludeItemTypes="Episode", **common).get("Items", [])
    return movies, shows, episodes


def remove(sonarr, radarr, kind, item_id, season=None):
    """Unmonitor one movie or one season and delete its files through Radarr or Sonarr (into the recycle bin).
    Returns the title removed."""
    if kind == "movie":
        m = radarr.get(f"movie/{item_id}")
        m["monitored"] = False
        radarr.send("PUT", f"movie/{item_id}", m)
        files = radarr.get("moviefile", movieId=item_id)
        for f in files:
            radarr.send("DELETE", f"moviefile/{f['id']}")
        log.info("tidied movie %s (%d file(s))", m.get("title"), len(files))
        return m.get("title", "")
    s = sonarr.get(f"series/{item_id}")
    match = next((x for x in s.get("seasons") or [] if x.get("seasonNumber") == season), None)
    if match is None:
        raise ApiError("sonarr: that season isn't there")
    match["monitored"] = False
    sonarr.send("PUT", f"series/{item_id}", s)
    episodes = [e for e in sonarr.get("episode", seriesId=item_id) if e.get("seasonNumber") == season]
    ids = [e["id"] for e in episodes if e.get("monitored")]
    if ids:  # Sonarr usually follows the season's switch; set the episodes too so none is fetched again
        sonarr.send("PUT", "episode/monitor", {"episodeIds": ids, "monitored": False})
    files = [f for f in sonarr.get("episodefile", seriesId=item_id) if f.get("seasonNumber") == season]
    if files:
        try:
            sonarr.send("DELETE", "episodefile/bulk", {"episodeFileIds": [f["id"] for f in files]})
        except ApiError:
            for f in files:
                sonarr.send("DELETE", f"episodefile/{f['id']}")
    log.info("tidied %s season %s (%d file(s))", s.get("title"), season, len(files))
    return f"{s.get('title', 'TV show')} · {'Specials' if season == 0 else f'Season {season}'}"
