"""Status: whether each integration works, storage, and what a person has to act on."""

from .helpers import plain_health
from .storage import storage_split


class StatusParts:
    def _status(self, r):
        ok = r["ok"]
        parts = []
        parts.append(
            {
                "name": "Downloads",
                "ok": ok.get("transmission", False),
                "text": "Working" if ok.get("transmission", False) else "The downloader isn't responding.",
            }
        )

        # A search site being down is reported once, under Search. Prowlarr doesn't always notice (when a site
        # fails only for searches made through it), so problems TV or Movies see with search sites count too.
        def is_indexer(i):
            return i.get("source", "").startswith(("IndexerStatus", "IndexerLongTermStatus", "IndexerSearch", "IndexerRss"))

        bad = lambda items: [i for i in items if i.get("type") in ("warning", "error")]  # noqa: E731
        health = {n: bad(r["health"].get(n, [])) for n in ("Search", "TV", "Movies")}
        seen = {i.get("message") for i in health["Search"]}
        for n in ("TV", "Movies"):
            for i in health[n]:
                if is_indexer(i) and i.get("message") not in seen:
                    health["Search"].append(i)
                    seen.add(i.get("message"))
            health[n] = [i for i in health[n] if not is_indexer(i)]
        # Prowlarr keeps a site warning (and one about its proxy) until the next success, so with every site
        # working they are stale and say nothing. `sites_down` is None when the per-site state couldn't be read.
        sites_down = r.get("sites_down")
        if sites_down == []:
            health["Search"] = []
        for name, app_ok in (("Search", ok.get("prowlarr", False)), ("TV", ok.get("sonarr", False)), ("Movies", ok.get("radarr", False))):
            issues = health[name]
            if not app_ok:
                parts.append({"name": name, "ok": False, "text": "Not responding.", "details": []})
            elif issues or (name == "Search" and sites_down):
                details = sorted({plain_health(name, i) for i in issues}) or [""]
                text = details[0]
                if name == "Search" and sites_down:
                    text = f"Not responding: {', '.join(sites_down)}. Others still work, and it usually fixes itself."
                parts.append({"name": name, "ok": False, "text": text, "details": [i.get("message", "") for i in issues][:5]})
            else:
                parts.append({"name": name, "ok": True, "text": "Working", "details": []})
        jf_ok = ok.get("jellyfin_up", False)
        parts.append({"name": "Watching", "ok": jf_ok, "text": "Working" if jf_ok else "Jellyfin isn't responding."})

        storage = None
        disks = r["disk"] or []
        best = None
        for d in disks:
            path = d.get("path", "")
            if self.s.media_path.startswith(path.rstrip("/") or "/") and (not best or len(path) > len(best["path"])):
                best = d
        if best:
            free, total = best.get("freeSpace", 0), best.get("totalSpace", 0)
            low = free < self.s.low_space_gb * 1_000_000_000
            storage = {
                "free": free,
                "total": total,
                "low": low,
                "lowAt": self.s.low_space_gb * 1_000_000_000,
                "recycle": r["recycle"],
                "bin": r.get("bin") or {},
                "split": storage_split(self.s, r, total - free),
            }
            parts.append({"name": "Storage", "ok": not low, "text": f"{free / 1e9:.0f} GB free" + (" — getting full" if low else "")})
        bin_ = r.get("bin") or {}
        notes = [
            f"Removed {what} can't be got back: {app} has no recycle bin set, so removing deletes the files for good."
            for key, what, app in (("tv", "TV shows", "Sonarr"), ("movie", "movies", "Radarr"))
            if (bin_.get(key) or {}).get("on") is False
        ]
        return {"parts": parts, "storage": storage, "bin": bin_, "notes": notes, "ok": all(p["ok"] for p in parts)}

    def attention(self):
        """Downloads someone has to act on, one line each, for /healthz on the server (the daily digest).

        A slow or stalled download isn't listed: it keeps trying by itself and needs nobody. A replacement's failed
        clean-up, or one held back for lack of space, is (replace.py)."""
        out = []
        for d in self.snapshot.get("downloading") or []:
            name = f"{d['title']} ({d['subtitle']})" if d.get("subtitle") else d["title"]
            if d.get("wrongTitle"):
                out.append(f"wrong title? {name} is downloading \"{d['wrongTitle']}\"")
        return out + (self.replacer.attention() if self.replacer else [])
