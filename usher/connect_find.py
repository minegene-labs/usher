"""Finding the apps for the Connect step: the addresses where something is listening on an app's usual port.

Only the usual places are tried (this machine, the host, the name the usual compose guides give the container); the
network is never scanned. A probe is a plain connection that sends nothing, so no key or request leaves Usher.
"""

import concurrent.futures
import socket
import struct
import urllib.parse
from pathlib import Path

from .connect import APPS, ORDER

PROBE_TIMEOUT = 0.4  # seconds to wait for a port to open
BUDGET = 3.0  # seconds for all of them: a slow name lookup must not hold the page up


def _gateway():
    """The container's default gateway, which is the host on a Docker network; None where it can't be read."""
    try:
        lines = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return None  # not Linux, or not readable: no host to suggest
    for line in lines:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            try:
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
            except ValueError:
                return None  # a routing table that isn't in the usual form: suggest no host
    return None


def candidates(app_id: str) -> list:
    """Where the app usually is: this machine, the host, and the name the usual compose guides give its container."""
    app = APPS[app_id]
    if not app.port:
        return []
    hosts = ["127.0.0.1", "host.docker.internal", app_id]
    if gateway := _gateway():
        hosts.insert(1, gateway)
    return [f"http://{host}:{app.port}{app.path}" for host in dict.fromkeys(hosts)]


def _listening(url: str) -> bool:
    """Whether something accepts connections there."""
    parts = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def suggestions() -> dict:
    """{app id: [addresses with something listening]} for every app that has a usual port. An address that hasn't
    answered within BUDGET is left out."""
    todo = [(app_id, url) for app_id in ORDER for url in candidates(app_id)]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=12)
    try:
        probes = [(app_id, url, pool.submit(_listening, url)) for app_id, url in todo]
        done, _ = concurrent.futures.wait([probe for _, _, probe in probes], timeout=BUDGET)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # a lookup still running finishes on its own
    out: dict = {app_id: [] for app_id in ORDER}
    for app_id, url, probe in probes:
        if probe in done and probe.result():
            out[app_id].append(url)
    return out
