"""Small pure helpers the state modules share: time, labels, recycle bin usage and plain-language health."""

import os
from datetime import datetime, timedelta, timezone

from .. import config


def couldnt_find_after():
    return config.get("retries.early_for_days") * 86400


def research_every(searched: bool, stuck: bool) -> int:
    """Seconds until a waiting request is searched for again."""
    if not searched:
        return config.get("retries.first_search_after_minutes") * 60
    return config.get("retries.late_every_hours" if stuck else "retries.early_every_hours") * 3600


READY_WINDOW = 30 * 86400
STALLED_AFTER = 3 * 3600  # a download with no progress for this long is shown as stalled


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def local_time(ts):
    """A Unix time as the server's local clock time, e.g. 21:30, for messages people read."""
    off = timezone(timedelta(minutes=config.get("server.utc_offset_minutes")))
    return datetime.fromtimestamp(ts, off).strftime("%H:%M")


def episode_label(season, number):
    """S1E2, or "" when either number is missing: a number nobody gave is never shown as 0 (a real season 0 is)."""
    if season is None or number is None:
        return ""
    return f"S{int(season)}E{int(number)}"


def episode_subtitle(season, number, name=None):
    """An episode's row subtitle: its label and name, whichever of them there is."""
    return " · ".join(p for p in (episode_label(season, number), name) if p)


def hms_seconds(s):
    """Sonarr/Radarr timeleft '01:02:03' or '1.02:03:04' -> seconds."""
    if not s:
        return None
    try:
        days = 0
        if "." in s.split(":")[0]:
            d, s = s.split(".", 1)
            days = int(d)
        h, m, sec = (int(float(x)) for x in s.split(":"))
        return days * 86400 + h * 3600 + m * 60 + sec
    except ValueError:
        return None


def recycle_usage(path):
    """What the recycle bin holds: removed items, and the space emptying it would actually free.
    A file still being shared by the downloader is a second name for the same data (a hard link), so it frees nothing."""
    if not os.path.isdir(path):
        return None
    items, freeable = 0, 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return None
    for top in entries:
        items += 1
        for root, _dirs, files in os.walk(top.path) if top.is_dir(follow_symlinks=False) else [(path, [], [top.name])]:
            for name in files:
                try:
                    st = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                if st.st_nlink == 1:
                    freeable += st.st_size
    return {"items": items, "bytes": freeable}


def sites_not_responding(indexers, statuses, now=None):
    """Names of the search sites that are turned on but paused after failing, from Prowlarr's own per-site state.
    Its health warnings linger until something next succeeds, so only this says whether a site is down now."""
    now = now or datetime.now(timezone.utc)
    paused = set()
    for st in statuses:
        try:
            if datetime.fromisoformat(str(st.get("disabledTill", "")).replace("Z", "+00:00")) > now:
                paused.add(st.get("indexerId"))
        except ValueError:
            continue
    return sorted((i.get("name") or "?" for i in indexers if i.get("enable") and i.get("id") in paused), key=str.lower)


def plain_health(app, item):
    """Turn an arr health check into a sentence for people who don't know what an indexer is."""
    src, msg = item.get("source", ""), item.get("message", "")
    if "Proxy" in src:
        return "The helper that gets past site blocks had a problem."
    if "Indexer" in src and ("unavailable" in msg or "failures" in msg):
        return "Some search sites aren't responding. Others still work, and it usually fixes itself."
    if "Indexer" in src:
        return "There's a problem with the search sites."
    if "DownloadClient" in src:
        return "It can't reach the downloader."
    if "RootFolder" in src:
        return "A library folder is missing."
    if "DiskSpace" in src or "space" in msg.lower():
        return "Storage is running low."
    if "Update" in src:
        return f"{app} has an update waiting. It installs itself on Sunday."
    return msg[:160] or "Needs attention."
