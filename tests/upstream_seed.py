"""Gives freshly started copies of the integrations the minimal settings the upstream check needs.

    python3 -m tests.upstream_seed

Reads the same environment as tests/upstream_check.py plus UPSTREAM_ENV_FILE, where it appends the Jellyfin
API key it creates. Sonarr, Radarr and Prowlarr are started with their key already set, and Transmission with
its login, so this waits for each and then does Jellyfin's first-run steps and adds a library folder to each
PVR. Every value here is made up and lives only for the run."""

import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

JF_HEADER = 'MediaBrowser Client="Usher check", Device="runner", DeviceId="upstream-check", Version="1"'


def request(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def wait(url, headers=None, tries=90):
    """Until the app answers; a slow first start is normal, a missing one is an error."""
    for _ in range(tries):
        try:
            request(url, headers=headers)
            return
        except urllib.error.HTTPError as e:
            if e.code in (401, 409):  # Transmission answers 409 until it is given a session id: it is up
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(2)
    raise SystemExit(f"{url.split('?')[0]} did not answer in time")


def wait_jellyfin(base, tries=90):
    """Until the first-run setup calls answer: the public info page comes up first, and they answer 503 until Jellyfin has finished starting."""
    url = f"{base}/Startup/Configuration"
    for _ in range(tries):
        try:
            request(url)
            return
        except urllib.error.HTTPError as e:
            if e.code != 503:
                raise
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    raise SystemExit("Jellyfin's setup calls kept answering 503 or not at all; it never got ready")


def setup_call(url, method="GET", body=None, tries=15):
    """A setup call that may still meet a 503 just after Jellyfin got ready; any other error is a real one."""
    for _ in range(tries):
        try:
            return request(url, method, body)
        except urllib.error.HTTPError as e:
            if e.code != 503:
                raise
        time.sleep(2)
    raise SystemExit(f"{url.split('?')[0]} kept answering 503; Jellyfin's setup did not get ready")


def seed_pvr(base, key, folder):
    headers = {"X-Api-Key": key}
    wait(f"{base}/api/v3/system/status", headers)
    request(f"{base}/api/v3/rootfolder", "POST", {"path": folder}, headers)


def seed_jellyfin(base, user, password, env_file):
    """First-run wizard, then an API key made the way a person makes one in the Dashboard."""
    wait(f"{base}/System/Info/Public")
    wait_jellyfin(base)
    auth = {"Authorization": JF_HEADER}
    setup_call(f"{base}/Startup/Configuration", "POST", {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
    setup_call(f"{base}/Startup/User")  # the wizard creates the first account here
    setup_call(f"{base}/Startup/User", "POST", {"Name": user, "Password": password})
    setup_call(f"{base}/Startup/RemoteAccess", "POST", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
    setup_call(f"{base}/Startup/Complete", "POST")
    token = request(f"{base}/Users/AuthenticateByName", "POST", {"Username": user, "Pw": password}, auth)["AccessToken"]
    admin = {"Authorization": f'{JF_HEADER}, Token="{token}"'}
    request(f"{base}/Auth/Keys?app={urllib.parse.quote('Usher check')}", "POST", headers=admin)
    keys = request(f"{base}/Auth/Keys", headers=admin)["Items"]
    key = next(k["AccessToken"] for k in keys if k["AppName"] == "Usher check")
    print(f"::add-mask::{key}")
    with open(env_file, "a", encoding="utf-8") as f:
        f.write(f"JELLYFIN_API_KEY={key}\n")


def main():
    env = os.environ
    wait(env["TRANSMISSION_URL"], {"Authorization": "Basic " + base64.b64encode(f"{env['TR_USER']}:{env['TR_PASS']}".encode()).decode()})
    seed_pvr(env["SONARR_URL"], env["SONARR_API_KEY"], "/tv")
    seed_pvr(env["RADARR_URL"], env["RADARR_API_KEY"], "/movies")
    wait(f"{env['PROWLARR_URL']}/api/v1/system/status", {"X-Api-Key": env["PROWLARR_API_KEY"]})
    password = secrets.token_hex(12)
    print(f"::add-mask::{password}")
    seed_jellyfin(env["JELLYFIN_URL"], env["JELLYFIN_USER"], password, env["UPSTREAM_ENV_FILE"])


if __name__ == "__main__":
    sys.exit(main())
