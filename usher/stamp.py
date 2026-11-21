"""The running build, written into the two app files that carry it to a phone.

`web/updater.js` and `web/sw.js` hold a placeholder instead of a number. The server swaps in the build when it serves
them. The build is the version plus a fingerprint of everything in `web/`, so a phone gets a new service worker (whose
cache name is the build) and a new build number after every deploy that changes the app, not only after a release, while
`usher/__init__.py` stays the only place the version is written."""

import hashlib
from pathlib import Path

from . import __version__

STATIC_TYPES = {".html", ".css", ".js", ".webmanifest", ".png", ".svg", ".ico", ".json"}  # what the server serves from web/
PLACEHOLDER = "__USHER_BUILD__"
STAMPED = ("updater.js", "sw.js")  # paths below web/, where the placeholder is replaced

_seen = {}  # web folder -> (what its files looked like, the fingerprint read from them)


def build_id(web_dir):
    """The version, a plus sign and eight hex digits that change whenever any file under `web_dir` does, e.g. `1.4.10+3fa9c1e2`.

    Only the files the server serves count (`STATIC_TYPES`), so a stray backup file changes nothing. They are read again
    only when one has a new size or modified time: this runs on every page and file request. A file that disappears while
    this runs is left out, never an error: the next call sees the folder as it is then."""
    root = Path(web_dir).resolve()
    looks = []
    for p in root.rglob("*"):
        if p.suffix not in STATIC_TYPES:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            looks.append((p.relative_to(root).as_posix(), st.st_mtime_ns, st.st_size))
    looks.sort()
    kept = _seen.get(root)
    if not kept or kept[0] != looks:
        digest = hashlib.sha256()
        for name, _, _ in looks:
            try:
                digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
            except OSError:
                continue
        kept = _seen[root] = (looks, digest.hexdigest()[:8])
    return f"{__version__}+{kept[1]}"


def stamp(rel, data, build):
    """`data` (bytes) of the file at `rel` (its path below web/), with the placeholder replaced by `build` where the file carries it."""
    if rel not in STAMPED:
        return data
    if PLACEHOLDER.encode() not in data:
        raise ValueError(f"web/{rel} has no {PLACEHOLDER} to put the build in")  # it would otherwise be served with a stale number
    return data.replace(PLACEHOLDER.encode(), build.encode())


def etag(rel, st, build):
    """Which copy of the file this is: its modified time and size, and for a stamped file the build too, since the same
    file reads differently after a deploy."""
    stamped = f"-{build}" if rel in STAMPED else ""
    return f'"{int(st.st_mtime)}-{st.st_size}{stamped}"'
