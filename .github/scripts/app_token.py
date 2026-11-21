"""Gets a short-lived token for the release GitHub App, which the Release workflow pushes its commit to main with.

The `protect-main` ruleset has no bypass for GitHub Actions itself, only for apps installed on the repository, so the
release commit is pushed as an app that is on the ruleset's bypass list (CONTRIBUTING.md, Releases). The app's private
key is the one secret; this script signs a ten-minute JWT with it (RS256, through the `openssl` the runner already
has), finds the app's installation on this repository and trades the JWT for an installation token that

- is limited to this repository and to the Contents permission (write), whatever else the app may be granted, and
- expires after an hour.

The token goes to the step's output, masked in the logs. The key and the token are never printed and never put in an
error message; GitHub's own error text is. Standard library only. tests/test_app_token.py has the cases.

Environment: APP_ID (the app's ID or client ID), APP_KEY (the private key, PEM), GH_REPO (owner/name), and
GITHUB_OUTPUT (where the workflow collects step outputs), GITHUB_API_URL (defaults to the public API).

Usage: python3 .github/scripts/app_token.py (run from the repository root)
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
JWT_LIFETIME = 540  # seconds; GitHub accepts at most ten minutes
CLOCK_SKEW = 60  # seconds the JWT is dated back, so a runner clock a little ahead doesn't get it refused
TIMEOUT = 30
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.ASCII)
SETUP = "See CONTRIBUTING.md, Releases, for setting up the release app."


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign(message: bytes, key_pem: str) -> bytes:
    """RS256 signature of `message`. The key is written to a private folder for openssl and removed again."""
    if "-----BEGIN" not in key_pem or "PRIVATE KEY-----" not in key_pem:
        raise ValueError(f"RELEASE_APP_KEY is not a private key in PEM form. {SETUP}")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "key.pem"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(key_pem.strip() + "\n")
        done = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(path)], input=message, capture_output=True, timeout=TIMEOUT)
    if done.returncode != 0:
        # openssl's own text is left out: it can quote parts of the key.
        raise ValueError(f"The release app's private key could not be used to sign. {SETUP}")
    return done.stdout


def make_jwt(app_id: str, key_pem: str, now: int) -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"iat": now - CLOCK_SKEW, "exp": now + JWT_LIFETIME, "iss": app_id}).encode())
    signed = f"{header}.{payload}"
    return f"{signed}.{b64url(sign(signed.encode('ascii'), key_pem))}"


def call(method: str, url: str, credential: str, body: dict | None = None) -> dict:
    """One GitHub API call; a refusal raises with the status and GitHub's message, never the credential."""
    request = urllib.request.Request(
        url,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "usher-release",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            return json.load(answer)
    except urllib.error.HTTPError as e:
        try:
            message = str(json.load(e).get("message", ""))[:200]
        except (ValueError, AttributeError):
            message = ""
        raise RuntimeError(f"GitHub answered {e.code} to {method} {url.split('//', 1)[-1].split('/', 1)[-1]}: {message}. {SETUP}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise RuntimeError(f"Could not reach GitHub for {method} {url.split('//', 1)[-1].split('/', 1)[-1]}: {type(e).__name__}") from e


def installation_token(app_id: str, key_pem: str, repo: str, api: str, now: int) -> str:
    """A token for the app's installation on `repo` (owner/name), limited to that repository and to Contents: write."""
    if not REPO.fullmatch(repo):
        raise ValueError(f"Not a repository name: {repo[:60]!r}")
    jwt = make_jwt(app_id, key_pem, now)
    installation = call("GET", f"{api}/repos/{repo}/installation", jwt)
    if not isinstance(installation.get("id"), int):
        raise RuntimeError(f"The release app is not installed on {repo}. {SETUP}")
    grant = call(
        "POST",
        f"{api}/app/installations/{installation['id']}/access_tokens",
        jwt,
        {"repositories": [repo.split("/", 1)[1]], "permissions": {"contents": "write"}},
    )
    token = grant.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("GitHub gave no installation token.")
    return token


def main(env: dict[str, str]) -> int:
    app_id, key, repo, output = env.get("APP_ID", ""), env.get("APP_KEY", ""), env.get("GH_REPO", ""), env.get("GITHUB_OUTPUT", "")
    if not app_id or not key:
        print(f"::error::The release app is not set up: RELEASE_APP_ID or RELEASE_APP_KEY is empty. {SETUP}")
        return 1
    if not output:
        print("::error::GITHUB_OUTPUT is not set: run this in a workflow step.")
        return 1
    try:
        token = installation_token(app_id, key, repo, env.get("GITHUB_API_URL") or API, int(time.time()))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"::error::Can't get a token for the release app: {e}")
        return 1
    print(f"::add-mask::{token}")  # before it is written anywhere a log could show it
    with open(output, "a", encoding="utf-8") as handle:
        handle.write(f"token={token}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
