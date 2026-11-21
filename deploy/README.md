# Installing and deploying Usher

How the operator installs Usher on a server, how it is updated (by hand, or once a day if turned on) from the published image, and why that is safe. What the app does is in the [main README](../README.md); every setting is in [usher.example.toml](usher.example.toml).

## The install folder

Usher lives in one folder on the server: `/opt/usher` by default. To use another, set `USHER_HOME` the first time `setup.sh` runs (`sudo USHER_HOME=/some/folder bash setup.sh`). `setup.sh` writes it to `/etc/default/usher`, where `usher-update` and later runs of `setup.sh` read it. Nothing in this repo names a folder on your machine; the examples below use the default.

| In the install folder | Holds |
| --- | --- |
| `src/` | The deploy files of the release `setup.sh` last installed (`compose.yaml`, `usher-update`, the units, the example settings). Never edit it: running `setup.sh` again resets it. Usher itself isn't here; it runs from the published image. |
| `compose.yaml` | Copied from `deploy/compose.yaml` by `setup.sh`. It runs the image `ghcr.io/minegene-labs/usher` at the version in `USHER_VERSION`. |
| `config/usher.toml` | Every setting (start from `deploy/usher.example.toml`) |
| `.env` | Root only (600): the secrets, `USHER_VERSION` (the version that runs) and the four things from this machine the container mounts (below) |
| `site.env` | `USHER_REPO` (the repo address `setup.sh` and `usher-update` look up releases at) and `TZ`. See `deploy/site.env.example`. |
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

- Linux with systemd (`systemctl`), Docker Engine with Docker Compose 2.20.2 or later (for `up --wait-timeout` and the health check's `start_interval`), `git`, `curl`, `python3` (3.11 or later), and `flock` and `logger` (util-linux). `setup.sh` checks for each of them before it changes anything and names any that are missing; it installs none of them, so use your distribution's packages.
- Docker Engine 25 or later makes the first health check quick: while Usher starts, Docker checks it every 2 seconds, so an update finishes within seconds of Usher answering. An older engine first checks after a minute, so an update takes longer, and a start that fails its first check may reach the 2-minute limit and be rolled back. `usher-update` holds an update while Docker Compose is older than 2.20.2, and says so.
- Jellyfin, Sonarr, Radarr, Prowlarr and Transmission already running, reachable from the server (Usher uses the host's network). Their addresses go in `usher.toml`; the defaults are the usual ports on `127.0.0.1`.
- A reverse proxy with HTTPS in front of Usher, such as Caddy. Usher itself listens on `127.0.0.1:8484` only.
- This repo's address on GitHub (public, https://), which the server reads the releases from, and the registry the images come from (`ghcr.io`).

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
   - finds the newest release and puts its deploy files in `src/`
   - offers to create `config/usher.toml` from the example, and stops until it names the address, the viewing account and the Transmission user
   - asks for the Transmission password, a Jellyfin API key (checked against the viewing account) and, optionally, a TMDb token for Discover, and keeps them in `.env`
   - asks for the four mounts above and keeps them in `.env`
   - writes `/etc/default/usher` and `USHER_VERSION` (the newest release) to `.env`, pulls that version's image, starts the container, waits up to 2 minutes for Docker to report it healthy, and runs `usher-user config check`
   - sets Sonarr's and Radarr's recycle bin to `USHER_RECYCLE_BIN`, emptied after 7 days
   - installs `usher-user`, `usher-update` and the units for the optional daily update (it turns nothing on; see Updating)
   - asks for the first accounts if there are none

## First run

1. Point your reverse proxy's address for Usher at `127.0.0.1:8484` (with Caddy: `reverse_proxy 127.0.0.1:8484`), and put the same address in `server.origin` in `usher.toml`.
2. `sudo usher-user ids` lists the Jellyfin accounts and quality profiles by ID. Put the viewing account in `media_server.user_id` and the profiles in `pvr.tv.quality_profile_id` and `pvr.movies.quality_profile_id`.
3. `sudo usher-user config check`, then `cd /opt/usher && sudo docker compose restart`.
4. `curl -s 127.0.0.1:8484/healthz` on the server lists anything that still needs fixing.

## Updating

Usher runs from the image of a release, at the version in `USHER_VERSION` in `.env`. Nothing changes on the server until you update it, or turn on the daily update below. Each release is made by hand when enough changes have collected; Settings → About → Updates says when a newer one is out, with its notes and this command.

- **Update by hand:** `sudo usher-update` finds the newest release, sets `USHER_VERSION` in `.env` to it, pulls its image, restarts, and lets Docker Compose wait up to 2 minutes for the new container to be healthy (its health check asks `/healthz`). If it isn't, `usher-update` puts the previous version back and tells you. Or do it yourself: set `USHER_VERSION` in `.env` to the version you want and run `cd /opt/usher && sudo docker compose pull && sudo docker compose up -d`.
- **Go back:** set `USHER_VERSION` in `.env` to the previous version and run the same two commands. The data folder keeps accounts and requests.
- **Update every day, if you want it:** off by default. `sudo systemctl enable --now usher-autodeploy.timer` turns it on and `sudo systemctl disable --now usher-autodeploy.timer` off again. Once a day (at some random minute within an hour of midnight, and after a missed day when the machine is next on) the timer runs `usher-update --auto`, which looks up the newest release and does nothing, quietly, unless it is newer than the running version. It never installs anything that isn't a published release, so a merged change reaches the server only with the next release.

What `usher-update` does and doesn't do:

- **Rollback:** if the new version isn't healthy in time, it sets `USHER_VERSION` back, restarts the previous image (still on this machine) and waits the same way for it to be healthy. The timer doesn't try that version again; the next release is tried.
- **Image not there yet:** a release's tag exists a few minutes before its image does. If the image can't be downloaded, nothing is changed and the next run tries again.
- **Held:** if Docker Compose is older than 2.20.2 it changes nothing, logs the hold once, and waits for you to update Compose and run `sudo usher-update`.
- **GitHub unreachable:** the timer stays quiet through a short outage, and logs an error once it has failed to read the releases for over a day. Run by hand, it says so and exits with an error.

When an update finishes or is rolled back, `usher-update` reports it to Usher on the server itself (`/internal/update-result`) so Settings → Advanced shows the last result. Every run that reaches GitHub, including a quiet one, also reports the newest release it saw and whether the update is held, so Settings → About → Updates can say Up to date, Update waiting, Held, Last update rolled back, or Can't tell (no report for over two days, such as when GitHub can't be reached or the timer has stopped). With the daily update off, nothing reports in, and the row says whether a newer release is out (Usher asks GitHub once a day; see `updates.check_releases`). A failed report never fails the update.

Its lines are in the journal: `journalctl -t usher-update` (the timer's runs also under `journalctl -u usher-autodeploy`). Its state (the lock, the last version that failed, the last good look at the releases) is in `/var/lib/usher-deploy`.

## Security model

- **The server only pulls.** It asks the public repository for its release tags over HTTPS and pulls the image of a release from `ghcr.io`, with no key and no credential of any kind; TLS vouches for both, and `setup.sh` refuses an address that isn't `https://`. There is no CI runner on the server, no webhook, and nothing on the internet can connect to it to update it; GitHub holds no credential for the server.
- **Only a published release is installed.** The image of a release is built by the Release workflow from the release commit, once the tests have passed on it, and its build provenance is attested (see below). A merged change reaches the server only with the next release, and only when you update or the daily update runs.
- **What runs as root never arrives with an update.** `usher-update`, the timer and service units and `setup.sh` are installed by hand from `deploy/` and run from `/usr/local/bin` and `/etc/systemd/system`, so an update can't change what runs as root. A change to them waits for a person to run `setup.sh` again (see Upgrading).
- **The container is locked down:** runs as 1000:1001, read-only filesystem, all capabilities dropped, `no-new-privileges`, memory and process limits. It mounts only its data, its settings, the three `config.xml` files read-only, and the recycle bin, and never the Docker socket.
- **Secrets stay on the server,** in `.env` (root, 600), never in `usher.toml` or this repo.
- What remains is trust in whoever can publish a release: they decide what the container runs (within the limits above), so keep write access to the repo to people you trust. Turning on the daily update extends that trust to every future release.

## Upgrading

- **An install made before the repository was public** has an SSH address as its source. Switch it before reinstalling `usher-update`: set `USHER_REPO` in `site.env` to the HTTPS address and run `sudo bash setup.sh` again (it points `src/` at it), or run `sudo git -C <install folder>/src remote set-url origin https://github.com/<owner>/<repo>.git`. Then reinstall `usher-update` as below, delete `deploy_key`, `deploy_key.pub` and `known_hosts` from the install folder, and remove the deploy key in the repository's settings on GitHub.
- **A new release:** see Updating. `sudo usher-update`, or `docker compose pull && docker compose up -d` with the new version in `USHER_VERSION`.
- **An install that builds from source (an earlier way of installing):** its `compose.yaml` builds `usher:local` from `src/` and a timer every 15 minutes follows the `stable` branch. Do this with the first release that has the image-based update, and not before: until then its old `usher-update` holds the change and logs "Check it, then run: sudo usher-update", which you should not follow, because that old script would install the new `compose.yaml` without pulling the image or replacing itself. Instead, stop the old timer so it can't reset `src/` underneath you, bring `src/` to the release and run `setup.sh` again, which replaces `compose.yaml`, `usher-update` and the units, writes `USHER_VERSION` to `.env` and pulls that version's image. Replace `<version>` with the release (`git ls-remote --tags <USHER_REPO>` lists them):
  ```bash
  sudo systemctl disable --now usher-autodeploy.timer
  ```
  ```bash
  sudo git -C /opt/usher/src fetch --depth 1 origin tag v<version>
  ```
  ```bash
  sudo git -C /opt/usher/src checkout -q v<version>
  ```
  ```bash
  sudo bash /opt/usher/src/deploy/setup.sh
  ```
  `setup.sh` leaves the old timer off, since automatic updates are now optional (see Updating); turn the daily one on if you want it. The images this machine built before (`usher:local`, `usher:prev`) can go: `sudo docker image rm usher:local usher:prev`.
- **A change to `usher-update`, the units or `setup.sh`:** they aren't updated by an update. Bring `src/` to the release that has the change as above and run `sudo bash /opt/usher/src/deploy/setup.sh` again, or reinstall them by hand:
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

## Running the published image

Everyone else can run Usher without building it. Each release, made by hand when enough changes have collected, publishes a container image for `linux/amd64` and `linux/arm64` at `ghcr.io/minegene-labs/usher`, tagged with the version and `latest`, with its build provenance attested. [`compose.yaml`](../compose.yaml) at the top of the repository runs it with nothing from the machine: the port on `127.0.0.1:8484` only, one named volume for Usher's data, the container's hardening described under Security model (non-root, read-only, no capabilities, memory and process limits), no remote parts and no Docker socket. It is separate from the install folder above, which runs the same image with the host's network and the apps' own files, and has its own steps for settings and accounts, because `setup.sh` and `usher-user` belong to that install.

1. Download `compose.yaml` into an empty folder and read it: `docker compose config` prints what it will run. To stay on one version, set `USHER_VERSION` (for example `USHER_VERSION=1.2.0`) in a `.env` file beside it.
2. Put the secrets in that `.env` file, one per line: `JELLYFIN_API_KEY`, `SONARR_API_KEY`, `RADARR_API_KEY`, `PROWLARR_API_KEY`, `TR_PASS` and, for Discover, `TMDB_TOKEN`. Keep it readable by you alone.
3. Write the settings file into the data volume. Setting Usher up in the browser is still to come, so until then Usher doesn't start without it. Copy [usher.example.toml](usher.example.toml) to `usher.toml` in the same folder and set:
   - leave `server.bind` out: inside the container Usher listens on all addresses, and the host only publishes the port on `127.0.0.1`. Delete the example's `bind` line, or it keeps Usher from answering on the published port;
   - `server.origin`, exactly the address people open Usher at, with `https://` and no trailing slash: Usher refuses a sign-in from any other address, and its sign-in cookie is only kept over HTTPS, so put a reverse proxy with HTTPS (or Tailscale Serve) in front of `127.0.0.1:8484` first;
   - `server.trusted_proxies`: through the published port Usher sees the Docker network's gateway, not `127.0.0.1`, as the sender, so add that address (`docker network inspect` on the project's `default` network shows it) for the proxy's `X-Forwarded-For` to be believed; without it every device looks like one address to the sign-in limits and the log;
   - the addresses of your apps (`http://host.docker.internal:8989` reaches an app on this machine whose port is published; `compose.yaml` maps that name).

   Then copy the file into the volume (run it again after any change to the file, and `docker compose restart`):
   ```bash
   docker compose run --rm --no-deps -T --entrypoint sh usher -c 'cat > /data/usher.toml' < usher.toml
   ```
4. `docker compose run --rm --no-deps usher python -m usher.cli ids` lists the Jellyfin accounts and the Sonarr and Radarr quality profiles by ID. Set `media_server.user_id` (required) and the two `quality_profile_id` keys in `usher.toml`, and copy it into the volume again.
5. `docker compose up -d --wait`, then add the first account. With no account yet Usher prints a setup code (`docker compose logs usher`); open your address, enter the code and create the account on the Setup page. It becomes the admin, who can save settings and secrets in the app from then on. Or: `docker compose exec usher python -m usher.cli add <name>` asks for a password, as `usher-user add` does on the install above. Then sign in.
6. To update, `docker compose pull && docker compose up -d`; to go back, set the previous version in `USHER_VERSION` and run the same command. The data volume keeps accounts and requests. Settings → About → Updates says when a newer release is out.

To check where an image came from, `gh attestation verify oci://ghcr.io/minegene-labs/usher:<version> --repo minegene-labs/usher` verifies its provenance.
