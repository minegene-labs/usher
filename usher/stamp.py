"""The running version, written into the two app files that carry it to a phone.

`web/updater.js` and `web/sw.js` hold a placeholder instead of a number. The server swaps in `__version__` when it serves
them, so each release gives the phone a new service worker and a new build number, while `usher/__init__.py` stays the
only place the version is written."""

from . import __version__

PLACEHOLDER = "__USHER_VERSION__"
STAMPED = ("updater.js", "sw.js")  # paths below web/, where the placeholder is replaced


def stamp(rel, data):
    """`data` (bytes) of the file at `rel` (its path below web/), with the placeholder replaced where the file carries it."""
    if rel not in STAMPED:
        return data
    if PLACEHOLDER.encode() not in data:
        raise ValueError(f"web/{rel} has no {PLACEHOLDER} to put the version in")  # it would otherwise be served with a stale number
    return data.replace(PLACEHOLDER.encode(), __version__.encode())


def etag(rel, st):
    """Which copy of the file this is: its modified time and size, and for a stamped file the version too, since the same
    file reads differently after an update."""
    version = f"-{__version__}" if rel in STAMPED else ""
    return f'"{int(st.st_mtime)}-{st.st_size}{version}"'
