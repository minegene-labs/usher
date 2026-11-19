"""Settings and secrets entered in the app: usher.toml and secrets.json are written here, and described for an admin.

Settings go to usher.toml, the file Usher reads at start-up; secrets go to secrets.json beside it, readable by Usher
alone (files.py). A secret is never read back in full: an admin sees whether one is set and, for a long one, its last
four characters. A change takes effect at the next start.
"""

import json
import logging
import re
import threading
from pathlib import Path

from . import config
from .files import write_private

log = logging.getLogger("usher.settings")

HEADER = "# Settings saved by Usher. Every key is described in docs/configuration.md.\n"
# Where the server listens and where it keeps its data are the container's business: the app can't move them, since a
# wrong value would lock everyone out or start Usher with an empty set of accounts.
LOCKED = ("server.bind", "server.data_dir")
MAX_SECRET = 256
BACKUP = "usher.toml.before-usher"  # the operator's own file, kept once before Usher first rewrites it
_save_lock = threading.Lock()
USERINFO = re.compile(r"(://)[^/@\s]*@")


def _toml_value(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (str, list)) else str(v)


def toml_text(values: dict) -> str:
    """{key name: value} -> usher.toml text, one section for each part of the key before its last dot."""
    sections: dict = {}
    for name in sorted(values):
        section, _, key = name.rpartition(".")
        sections.setdefault(section, []).append(f"{key} = {_toml_value(values[name])}")
    return HEADER + "".join(f"\n[{section}]\n" + "\n".join(lines) + "\n" for section, lines in sections.items())


def ensure_file():
    """Make sure usher.toml exists, so a later start finds the install's settings file where it expects it."""
    path = Path(config.config_path())
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, HEADER)


def keep_original():
    """Usher rewrites usher.toml from the keys it holds, which drops comments: keep the operator's own file once."""
    path = Path(config.config_path())
    backup = path.with_name(BACKUP)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    if not text.startswith(HEADER) and not backup.exists():
        write_private(backup, text)


def _clean_secret(name, value):
    if not isinstance(value, str) or len(value) > MAX_SECRET or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise config.ConfigError(f"{name} must be text on one line, at most {MAX_SECRET} characters")
    return value.strip()


def save(settings: dict, secrets: dict) -> list:
    """Write the settings (None clears one) and secrets ("" clears one) given, keeping the rest of each file.
    Raises ConfigError naming the setting at fault, before anything is written. Returns the names saved."""
    for name in settings:
        if name in LOCKED:
            raise config.ConfigError(f"{name} can only be changed in usher.toml on the server")
        if name not in config.KEYS:  # before any text is built from it: a name is written into the file as it is
            raise config.ConfigError(f"{str(name)[:60]!r} is not a setting Usher knows")
    for name in secrets:
        if name not in config.SECRETS:
            raise config.ConfigError(f"{name} is not a secret Usher knows")
    with _save_lock:
        return _save(settings, secrets)


def _save(settings, secrets):
    current = config.read().values or {}
    merged = {**current, **{k: v for k, v in settings.items() if v is not None}}
    for name, v in settings.items():
        if v is None:
            merged.pop(name, None)
    text = toml_text(merged)
    config.parse(text)  # the same check start-up makes, so a value that wouldn't start Usher is refused here
    kept = config.read_secrets()
    for name, v in secrets.items():
        value = _clean_secret(name, v)
        if value:
            kept[name] = value
        else:
            kept.pop(name, None)
    try:
        text.encode("utf-8")  # a lone surrogate sent as JSON can't be written: refuse it rather than fail half way
    except UnicodeEncodeError:
        raise config.ConfigError("A setting holds a character that can't be saved") from None
    if settings:
        keep_original()
        write_private(config.config_path(), text)
    if secrets:
        write_private(config.secrets_path(), json.dumps(kept, indent=2) + "\n")
    # What changed, without a secret's name or value: the settings by name, the secrets only counted.
    log.info("settings saved: %s; %d secret(s) changed", ", ".join(sorted(settings)) or "no settings", len(secrets))
    return sorted(settings) + sorted(secrets)


def _hint(value: str) -> str:
    return "…" + value[-4:] if len(value) >= 20 else ""


def describe() -> dict:
    """What an admin sees: each setting's saved value (or its default), and for each secret whether it is set."""
    saved = config.read().values or {}
    settings = {}
    for name, key in config.KEYS.items():
        if name in LOCKED:
            continue
        value = saved.get(name, key.default)
        settings[name] = {"value": USERINFO.sub(r"\1…@", value) if isinstance(value, str) else value, "saved": name in saved, "help": key.help}
    stored = config.read_secrets()
    secrets = {}
    for name in config.SECRETS:
        env = config._env(name)
        value = env or stored.get(name, "").strip()
        secrets[name] = {"set": bool(value), "hint": _hint(value), "fromEnvironment": bool(env)}
    return {"settings": settings, "secrets": secrets, "locked": list(LOCKED)}
