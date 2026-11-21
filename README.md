# Usher

A web app for your media server: search for a movie or show, tap **Get it**, and follow it live from *Asked* to *Ready to watch*, without opening Sonarr, Radarr, Prowlarr or Transmission. Written in plain language.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/phone-search-dark.png">
    <img src="docs/images/phone-search-light.png" alt="Search on a narrow screen: the search box and filters, with Discover's rows of trending and popular titles below, each with a plain placeholder poster" width="250">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/phone-journey-dark.png">
    <img src="docs/images/phone-journey-light.png" alt="A request's journey on a narrow screen: Asked and Found are done, the show is downloading with a progress bar and the time left, then Getting it ready and Ready to watch" width="250">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/phone-downloads-dark.png">
    <img src="docs/images/phone-downloads-light.png" alt="Downloads on a narrow screen: a show someone just asked for with its progress, two more downloads with their progress and one waiting to start" width="250">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/phone-status-dark.png">
    <img src="docs/images/phone-status-light.png" alt="Status on a narrow screen: a card saying everything is working, then Health for each part of the setup and Storage below it" width="250">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/phone-settings-dark.png">
    <img src="docs/images/phone-settings-light.png" alt="Settings on a narrow screen: the account, then the notification choices for this device" width="250">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/ipad-status-dark.png">
    <img src="docs/images/ipad-status-light.png" alt="Status on a wide screen: the sidebar on the left, with Health and Storage side by side and a card saying everything is working" width="700">
  </picture>
</p>

## Features

- **Search and Discover**: results appear as each source answers, with badges for what is already in the library, requested, or on a streaming service; Discover adds picks based on what the viewing account watched, or on each person's own likes and requests, and trending and popular rows. Like or dislike a title from its details; a dislike leaves your Discover at once, and Settings → Account lists them to undo. See [docs/features.md](docs/features.md).
- **Get it**: one tap for a movie, or a choice of seasons for a show. Follow it live; retry, stop or remove from the app.
- **Home like Jellyfin**: continue watching, next up and the latest, with an app icon badge for what is ready.
- **Downloads**: everything you asked for, from asked to ready to watch, with live progress and a badge for what is new since you last opened the tab: a new request, a finished one or a problem.
- **Replaces stalled downloads** by itself with a healthier copy, and says so.
- **Tidy up** is a row in Status → Storage that says how many watched titles it can free; it lists watched movies and seasons to remove, biggest first. **Keep** takes a title off the list, and **Kept titles** puts it back.
- **Status** shows at a glance whether everything is working: the summary, Health and Storage (side by side on a wide screen, one column on a narrow screen or with larger text). **Settings** (the round account button with your initials at the top right of Status, or on a wide screen the "Signed in as …" button at the bottom of the sidebar) holds your account, this device's notifications and Open titles in, signed-in devices, About and Sign out.
- **Notifications** by Web Push, per person (Settings → Device). Where they can't work, the same place says why and what to do.
- **Updates**: Settings → About → Updates says whether the server is keeping up with new versions: Up to date, Update waiting, Held (a newer version needs a step on the server first), Last update rolled back, or Can't tell when the server hasn't reported for over an hour.
- **Changelog**: Settings → About → Changelog lists what changed in every version that changed something you would notice, newest first. Nothing pops up after an update.
- **Family-friendly folders** (optional): titles with a family rating are filed apart automatically.
- **Opens at once**: each screen shows its last copy straight away and refreshes in the background; artwork is kept on the device at the size it is shown, within a fixed limit, and the next screenful is fetched while the device is idle (never in Low Data Mode). Signing out deletes it all. See [docs/features.md](docs/features.md).
- **Private by design**: only the server talks to other services; sign-in, CSRF and a strict CSP. See [docs/security.md](docs/security.md).

## Requirements

- A server with Linux, systemd, Docker with Docker Compose 2.20.2 or later, `git`, `curl` and Python 3.11+ (the full list is in [deploy/README.md](deploy/README.md#requirements)).
- Jellyfin, Sonarr, Radarr, Prowlarr and Transmission already running and reachable from the server.
- A reverse proxy with HTTPS in front of Usher, such as Caddy. Usher listens on `127.0.0.1` only (the published image listens on all addresses inside its container, with its port published on `127.0.0.1` only).
- A free TMDb token for Discover (optional).

## Supported versions

Usher is checked every week against the latest release of each integration, and every month against announced betas. Each app is supported in its current and previous major version; the last column is the version the last green check saw. Each new release opens a `Support <App> <version>` issue, which closes by itself once every call Usher makes passes on it. If something doesn't work with your version, use the "Doesn't work with my version of…" issue form.

<!-- support-matrix:start -->
| App | Supported | Last green run |
| --- | --- | --- |
| Jellyfin | current and previous major version | not yet |
| Sonarr | current and previous major version | not yet |
| Radarr | current and previous major version | not yet |
| Prowlarr | current and previous major version | not yet |
| Transmission | current and previous major version | not yet |
<!-- support-matrix:end -->

## Quick start

1. Follow [deploy/README.md](deploy/README.md): it installs Usher into one folder, sets up auto-deploy and covers upgrading. To run the published image from one Compose file instead, follow [Running the published image](deploy/README.md#running-the-published-image), which has its own steps for settings and the first account; steps 2 and 3 below are for the install folder.
2. Copy [deploy/usher.example.toml](deploy/usher.example.toml) to `config/usher.toml` in the install folder and set at least `media_server.user_id` (`sudo usher-user ids` lists the IDs). Every key is in [docs/configuration.md](docs/configuration.md).
3. Add an account: `sudo usher-user add <name> [Display Name]`. A new Usher with no accounts also prints a setup code in its log: open its address, enter the code and create the first account there. The next step connects each app: enter its address and key, press Test, and Usher says whether that version is supported.

## Documentation

| | |
| --- | --- |
| [docs/features.md](docs/features.md) | How each part behaves, in detail |
| [docs/configuration.md](docs/configuration.md) | Every `usher.toml` key, health checks and the server commands |
| [docs/architecture.md](docs/architecture.md) | Server, web client, integrations, data flow |
| [docs/security.md](docs/security.md) | Threat model, secrets, network exposure |
| [docs/glossary.md](docs/glossary.md) | The words used here and in the code |
| [docs/credits.md](docs/credits.md) | TMDB and JustWatch credits |
| [deploy/README.md](deploy/README.md) | Installing, auto-deploy, upgrading |
| [SUPPORT.md](SUPPORT.md) | Where to ask a question and what to include |
| [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) | Changes, how to contribute, reporting a vulnerability |

## On the server

| Command | Does |
| --- | --- |
| `sudo bash /opt/usher/src/deploy/setup.sh` | First-time setup (safe to re-run; also sets the recycle bin). Shown for the default install folder; see [deploy/README.md](deploy/README.md) for another |
| `sudo usher-update` | Deploy the latest `stable` now: rebuild, restart, and roll back if the new version doesn't answer within 2 minutes. A timer (`usher-autodeploy.timer`) runs it every 15 minutes, so this is only needed for a change it holds back (Dockerfile or `deploy/compose.yaml` differing from what is running, including a hand edit of the installed `compose.yaml`; their pull requests are labelled `needs-server`) |
| `sudo usher-user add <name> [Display Name]` | Add an account |
| `sudo usher-user ids` | List the Jellyfin account and quality profile IDs `usher.toml` can hold |
| `sudo usher-user config check` | Check `usher.toml` the way start-up does |
| `sudo usher-user passwd <name>` | Change a password (signs that person out everywhere) |
| `sudo usher-user list` / `remove <name>` | List or remove accounts |
| `sudo docker logs --tail 50 usher` | Recent log lines: sign-ins, failures, integrations that stopped answering |

## Developing

The pictures at the top come from Usher's test fakes, with a made-up account and generated placeholder posters instead of real artwork. Redo them with `python3 tools/screenshots.py` whenever a screen they show changes.

```
python3 -m tests.mock_apps &          # fake Sonarr, Radarr, Prowlarr, Transmission, Jellyfin on 9901-9905, TMDb on 9906
mkdir -p data && cat > data/usher.toml <<'EOF'   # made-up settings that point at the fakes
[server]
origin = "http://localhost:8484"
data_dir = "./data"
[media_server]
url = "http://127.0.0.1:9905"
user_id = "88888888888888888888888888888888"
[pvr.tv]
url = "http://127.0.0.1:9901"
[pvr.movies]
url = "http://127.0.0.1:9902"
[indexer_manager]
url = "http://127.0.0.1:9903"
[download_client]
url = "http://127.0.0.1:9904/transmission/rpc"
user = "x"
EOF
export USHER_CONFIG=./data/usher.toml TR_PASS=x JELLYFIN_API_KEY=$(printf 'b%.0s' {1..32}) \
  SONARR_API_KEY=$(printf 'a%.0s' {1..32}) RADARR_API_KEY=$(printf 'a%.0s' {1..32}) PROWLARR_API_KEY=$(printf 'a%.0s' {1..32})
python3 -m usher.cli add alex Alex
python3 -m usher.server                # then open http://localhost:8484
python3 -m unittest discover -s tests -t .
pip install -r requirements-dev.txt && ruff check . && ruff format --check .   # the style checks CI runs
actionlint && zizmor --offline .      # the workflow linters CI runs (installed by the line above)
gitleaks git --config .gitleaks.toml --redact   # the secret scan CI runs, if gitleaks is installed
git config core.hooksPath .githooks   # look for identifying details before every push (CONTRIBUTING.md)
python3 tools/screenshots.py          # redo the README pictures in docs/images/ (install requirements-screenshots.txt first: see the script)
pip install -r requirements-audit.txt && pip-audit -r requirements.txt   # the weekly dependency audit
```

## Licence

Usher is released under the [MIT licence](LICENSE). Third-party data (TMDb, JustWatch) stays under its own terms; see [docs/credits.md](docs/credits.md).

Usher is built with AI coding tools: a maintainer directs the work and reviews every change, and the tests and checks run on each pull request.
