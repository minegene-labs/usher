"""The running build, written into the two app files that carry it to a phone.

`web/updater.js` and `web/sw.js` hold a placeholder instead of a number. The server swaps in the build when it serves
them. The build is the version plus a fingerprint of everything in `web/`, so a phone gets a new service worker (whose
cache name is the build) and a new build number after every deploy that changes the app, not only after a release, while
`usher/__init__.py` stays the only place the version is written."""

import hashlib
from pathlib import Path

from . import __version__

PLACEHOLDER = "__USHER_BUILD__"
STAMPED = ("updater.js", "sw.js")  # paths below web/, where the placeholder is replaced

_seen = {}  # web folder -> (what its files looked like, the fingerprint read from them)


def build_id(web_dir):
    """The version, a plus sign and eight hex digits that change whenever any file under `web_dir` does, e.g. `1.4.10+3fa9c1e2`.

    The files are read again only when one has a new size or modified time: this runs on every page and file request."""
    root = Path(web_dir)
    files = sorted(p for p in root.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts))
    looks = tuple((p.relative_to(root).as_posix(), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    kept = _seen.get(root)
    if not kept or kept[0] != looks:
        digest = hashlib.sha256()
        for p in files:
            digest.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
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
