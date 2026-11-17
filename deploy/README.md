# Installing and deploying Usher

How the operator installs Usher on a server, how it keeps itself up to date from the `stable` branch, and why that is safe. What the app does is in the [main README](../README.md); every setting is in [usher.example.toml](usher.example.toml).

## The install folder

Usher lives in one folder on the server: `/opt/usher` by default. To use another, set `USHER_HOME` the first time `setup.sh` runs (`sudo USHER_HOME=/some/folder bash setup.sh`). `setup.sh` writes it to `/etc/default/usher`, where `usher-update` and later runs of `setup.sh` read it. Nothing in this repo names a folder on your machine; the examples below use the default.

| In the install folder | Holds |
| --- | --- |
| `src/` | This repo, exactly as on `stable`. Never edit it: each deploy resets it. |
| `compose.yaml` | Copied from `deploy/compose.yaml` on each deploy |
| `config/usher.toml` | Every setting (start from `deploy/usher.example.toml`) |
| `.env` | Root only (600): the secrets, and the four things from this machine the container mounts (below) |
| `site.env` | `USHER_REPO` (the repo address the server pulls from) and `TZ`. See `deploy/site.env.example`. |
| `data/` | Accounts, requests, the Web Push key; only the container's user can read it (700) |

The four mounts `compose.yaml` reads from `.env`, all full paths on this machine:

| Variable | What it is |
| --- | --- |
| `USHER_SONARR_XML` | Sonarr's `config.xml` (Usher reads its API key; mounted read-only) |
| `USHER_RADARR_XML` | Radarr's `config.xml` |
| `USHER_PROWLARR_XML` | Prowlarr's `config.xml` |
| `USHER_RECYCLE_BIN` | The recycle bin Sonarr and Radarr move removed titles into. Use a folder they see at the same path, inside your media folder, so a removal is a quick move. |

If one is missing, Docker Compose refuses to start Usher and names it.

## Requirements

- Linux with systemd (`systemctl`), Docker Engine with Docker Compose 2.20.2 or later (for `up --wait-timeout` and the health check's `start_interval`), `git`, `curl`, `python3` (3.11 or later), `flock` and `logger` (util-linux), and `cmp` (diffutils). `setup.sh` checks for each of them before it changes anything and names any that are missing; it installs none of them, so use your distribution's packages.
- Docker Engine 25 or later makes the first health check quick: while Usher starts, Docker checks it every 2 seconds, so a deploy finishes within seconds of Usher answering. An older engine first checks after a minute, so a deploy takes longer, and a start that fails its first check may reach the 2-minute limit and be rolled back. `usher-update` holds an update while Docker Compose is older than 2.20.2, and says so.
- Jellyfin, Sonarr, Radarr, Prowlarr and Transmission already running, reachable from the server (Usher uses the host's network). Their addresses go in `usher.toml`; the defaults are the usual ports on `127.0.0.1`.
- A reverse proxy with HTTPS in front of Usher, such as Caddy. Usher itself listens on `127.0.0.1:8484` only.
- A copy of this repo on GitHub the server can read, with a `stable` branch.

## Install

1. Create the install folder and `site.env`:
   ```bash
   sudo install -d -m 755 /opt/usher
   ```
   Copy `deploy/site.env.example` to `/opt/usher/site.env` and fill in `USHER_REPO` and `TZ`.
2. Copy `deploy/setup.sh` to the server (the repo isn't there yet), then run it:
   ```bash
   sudo bash setup.sh
   ```
3. Follow what it asks. It is safe to run again at any time, and only asks for what is still missing:
   - checks that it can read `USHER_REPO`, which is the public https:// address of the repo
   - clones `stable` into `src/`
   - offers to create `config/usher.toml` from the example, and stops until it names the address, the viewing account and the Transmission user
   - asks for the Transmission password, a Jellyfin API key (checked against the viewing account) and, optionally, a TMDb token for Discover, and keeps them in `.env`
   - asks for the four mounts above and keeps them in `.env`
   - writes `/etc/default/usher`, builds and starts the container, waits up to 2 minutes for Docker to report it healthy, and runs `usher-user config check`
   - sets Sonarr's and Radarr's recycle bin to `USHER_RECYCLE_BIN`, emptied after 7 days
   - installs `usher-user`, `usher-update` and the auto-deploy timer
   - asks for the first accounts if there are none

## First run

1. Point your reverse proxy's address for Usher at `127.0.0.1:8484` (with Caddy: `reverse_proxy 127.0.0.1:8484`), and put the same address in `server.origin` in `usher.toml`.
2. `sudo usher-user ids` lists the Jellyfin accounts and quality profiles by ID. Put the viewing account in `media_server.user_id` and the profiles in `pvr.tv.quality_profile_id` and `pvr.movies.quality_profile_id`.
3. `sudo usher-user config check`, then `cd /opt/usher && sudo docker compose restart`.
4. Open the address on a phone, sign in, and **Add to Home Screen**. `curl -s 127.0.0.1:8484/healthz` on the server lists anything that still needs fixing.

## Auto-deploy

Pull requests merge into `main` once their tests pass, and the Release workflow then moves `stable` to the merge. `stable` only ever holds tested code, and the server deploys only `stable`.

`usher-autodeploy.timer` runs `usher-update --if-changed` every 15 minutes (5 minutes after boot, with up to a minute's random delay):

- **Nothing new:** it fetches `stable` over HTTPS, finds nothing has moved, and stays quiet.
- **An app change:** it resets `src/` to the new commit, rebuilds, restarts, and lets Docker Compose wait up to 2 minutes for the new container to be healthy (its health check asks `/healthz`). The container is made from the image just built, so a healthy one runs the new version.
- **Held:** if `Dockerfile` or `deploy/compose.yaml` differs from what is running (including a hand edit of the installed `compose.yaml`, or a Dockerfile change since the running image was built), it deploys nothing. It logs the hold once and waits for a person to read the change and run `sudo usher-update`. Pull requests touching those files are labelled `needs-server`.
- **Rollback:** if the new version doesn't build or isn't healthy in time, it puts back the previous code, `compose.yaml` and image, restarts, and waits the same way for the old version to be healthy. It doesn't retry the failed commit until `stable` moves again.
- **GitHub unreachable:** it stays quiet through a short outage, and logs an error once fetching has failed for over a day. Run by hand, it says so and exits with an error.

When an update finishes or is rolled back, `usher-update` reports it to Usher on the server itself (`/internal/update-result`) so Settings → Advanced shows the last result. Every run that reaches GitHub, including a quiet one, also reports the newest version it saw on `stable` and whether a change is held, so Settings → About → Updates can say Up to date, Update waiting, Held, Last update rolled back, or Can't tell (no report for over an hour, such as when GitHub can't be reached or the timer has stopped). A failed report never fails the update. Until the script is reinstalled (see Upgrading), the row says Can't tell.

`sudo usher-update` does the same at once, without the hold. Its lines are in the journal: `journalctl -t usher-update` (the timer's runs also under `journalctl -u usher-autodeploy`). Its state (lock, held and failed commits, the last good fetch, the previous `compose.yaml`) is in `/var/lib/usher-deploy`.

## Security model

- **The server only pulls.** It fetches `stable` over HTTPS from the public repository, with no key and no credential of any kind; TLS and GitHub's certificate are what vouch for it, and `setup.sh` refuses an address that isn't `https://`. There is no CI runner on the server, no webhook, and nothing on the internet can connect to it to deploy; GitHub holds no credential for the server.
- **Only tested code is deployed.** `stable` moves only after the tests pass on exactly that content.
- **What runs as root never arrives by a deploy.** `usher-update`, the timer and service units and `setup.sh` are installed by hand from `deploy/` and run from `/usr/local/bin` and `/etc/systemd/system`, so a merged pull request can't change what runs as root. Changes that decide what Docker runs (`Dockerfile`, `compose.yaml`) wait for a person, as above.
- **The container is locked down:** runs as 1000:1001, read-only filesystem, all capabilities dropped, `no-new-privileges`, memory and process limits. It mounts only its data, its settings, the three `config.xml` files read-only, and the recycle bin, and never the Docker socket.
- **Secrets stay on the server,** in `.env` (root, 600), never in `usher.toml` or this repo.
- What remains is trust in whoever can merge into `main`: they decide what the container runs (within the limits above), so keep write access to the repo to people you trust.

## Upgrading

- **An install made before the repository was public** has an SSH address as its source. Switch it before reinstalling `usher-update`: set `USHER_REPO` in `site.env` to the HTTPS address and run `sudo bash setup.sh` again (it points `src/` at it), or run `sudo git -C <install folder>/src remote set-url origin https://github.com/<owner>/<repo>.git`. Then reinstall `usher-update` as below, delete `deploy_key`, `deploy_key.pub` and `known_hosts` from the install folder, and remove the deploy key in the repository's settings on GitHub.
- **Usually nothing:** the timer deploys each new version within 15 minutes.
- **A held change** (`needs-server`): read the pull request, do what it says, then `sudo usher-update`.
- **A change to `usher-update`, the units or `setup.sh`:** they aren't updated by a deploy. Re-run `sudo bash /opt/usher/src/deploy/setup.sh`, or reinstall them by hand:
  ```bash
  sudo install -m 755 /opt/usher/src/deploy/usher-update /usr/local/bin/usher-update
  ```
  ```bash
  sudo install -m 644 /opt/usher/src/deploy/usher-autodeploy.service /opt/usher/src/deploy/usher-autodeploy.timer /etc/systemd/system/
  ```
  ```bash
  sudo systemctl daemon-reload
  ```
- **Moving the install folder:** `sudo docker compose down` in the old folder, move it, set `USHER_HOME=<new folder>` in `/etc/default/usher`, then `sudo docker compose up -d` in the new one.
