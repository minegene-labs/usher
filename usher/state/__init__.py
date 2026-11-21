"""Polls the integrations, turns their data into plain-language state, and tells open screens when it changes.

State is the coordinator: it owns the data and the locks, and each job lives in its own module as a mixin
(polling, Home's rows, media-server checks, the request journey, Status, change events, Library and Upcoming).
"""

import threading

from ..cache import TTLCache
from ..titles import Titles
from .events import Events
from .journey import Journey
from .lists import Lists
from .media_server import MediaServer
from .polling import Polling
from .rows import Rows
from .status import StatusParts


class State(Polling, Rows, StatusParts, Journey, MediaServer, Events, Lists):
    def __init__(self, settings, sonarr, radarr, prowlarr, transmission, jellyfin, store=None):
        self.s = settings
        self.store = store
        self._kick = threading.Event()
        self.queue_ids: dict = {}
        self.notifier = None
        self.kept = None  # kept.py: titles kept out of Tidy up (set when the poller starts)
        self.replacer = self.missing = None  # replace.py swaps stalled downloads; missing.py searches for episodes
        self.sonarr, self.radarr, self.prowlarr = sonarr, radarr, prowlarr
        self.tr, self.jf = transmission, jellyfin
        self.version = 0
        self.snapshot = {"version": 0, "loading": True}
        self._json = b""
        self._cond = threading.Condition()
        self._raw = {
            "queue_tv": [],
            "queue_movies": [],
            "missing_tv": [],
            "movies": [],
            "torrents": {},
            "ready": [],
            "health": {},
            "disk": None,
            "ok": {},
            "series": [],
            "recycle": None,
            "bin": {"tv": None, "movie": None},  # each app's recycle bin setting; None until it has been read
        }
        self._cache = TTLCache(60, 20)  # Library, Upcoming and Tidy up, each with its own age limit
        self._progress: dict = {}  # download key -> (progress, when it last moved)
        self._stop = threading.Event()
        # One identity per title, linked across the integrations; every change to a title goes through it.
        self.titles = Titles()
        self.titles.clears(self._cache)
        self._build_lock = threading.Lock()
        self._unplayed: dict | None = None  # media server ID -> item, unplayed at the last look
        self._gone_jf: dict = {}  # media server IDs it no longer has -> when that was found
        self._art_checked: dict = {}  # media server ID -> when a missing picture was last checked
        self._recheck: set = set()  # (kind, Sonarr/Radarr ID) whose media server ID must be found again
