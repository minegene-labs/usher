# Security

How Usher protects the people who use it and the services behind it. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Threat model

Usher is meant for a household or small group: a handful of trusted people, one server, reached over HTTPS through a reverse proxy, at home or through a private network such as Tailscale. It is not designed to be exposed to the open internet without that proxy, and it assumes the integrations (Sonarr, Radarr, Prowlarr, Transmission, Jellyfin) are on a network only the server can reach. What it defends against: guessed or stolen passwords, forged requests from other websites, a hostile response from an integration or an image host, and a device or browser leaking secrets. What it does not: someone with root on the server, or an operator who misconfigures the proxy.

## Network exposure

- Run from source or in the install folder, Usher listens on `127.0.0.1` only; the reverse proxy is the one public door.
- The install folder's container uses host networking so it can reach the integrations on the server's own loopback (`127.0.0.1`), where they listen and where nothing outside the server can reach them; a separate container network would need each of them opened to it.
- That is safe here because that compose file sets Usher to listen on `127.0.0.1` only, so host networking exposes no new port, and because the container's filesystem is read-only and all its capabilities are dropped.
- The published image listens on all addresses inside its container, which a published port needs, and its compose file publishes the port on the host's `127.0.0.1` only, so that decides who can reach it. A compose file that publishes the port on every address, or shares the host's network, opens Usher to the network unless `server.bind` says otherwise.
- Only the server talks to TMDb and the integrations. Devices contact nothing else: posters and logos come through Usher.

## What is in place

- HTTPS only, through Caddy; Usher itself listens on `127.0.0.1` (in the published image, on all addresses inside the container, with the port published on the host's `127.0.0.1` only).
- Passwords hashed with scrypt (N=2^15, r=8, p=1). Five failures lock an account for 15 minutes; 20 failures from one address in 15 minutes block that address.
- Session cookie `__Host-usher`: 256-bit random, `Secure`, `HttpOnly`, `SameSite=Strict`, 90 days sliding. Only a SHA-256 of it is stored.
- Every POST needs an exact `Origin` match; signed-in POSTs also need the CSRF token.
- Strict CSP (`default-src 'self'`, no inline script), `nosniff`, `no-referrer`, COOP/CORP, a locked-down `Permissions-Policy`.
- What a device keeps (the last copy of each list, Home and Status, and artwork) is tied to the account: shown only once the server confirms the session, never to another account, and deleted on sign-out, when the server ends the session, or when another account signs in.
- Search-result posters are fetched by the server, only from TMDb and TheTVDB image hosts, with redirects refused; devices never contact those sites.
- Transmission 4.1 and later are spoken to in JSON-RPC 2.0, the protocol they recommend; older ones in the old protocol. Usher picks by what Transmission says when it connects, so an upgrade needs nothing.
- Jellyfin is sent the full `Authorization: MediaBrowser …` header its developers ask for (token, client, device, a device ID made once and kept in the data folder as `jellyfin-device-id`, and Usher's version), never the key in the address.
- API keys are read from Sonarr's, Radarr's and Prowlarr's own `config.xml`, mounted read-only. The Transmission password, Jellyfin key and TMDb token live in `.env` in the install folder (root, 600).
- The data folder (`data` in the install folder) is 700, and every file Usher keeps there (accounts database, push key, Discover rows, replacements) is written 600, whatever the umask.
- Container: runs as 1000:1001, read-only filesystem, all capabilities dropped, `no-new-privileges`, 256 MB memory cap.
- The install folder runs the image of a published release, which it pulls over HTTPS with no key or credential, and updates only when told to: by hand, or once a day to a newer release if the optional timer is turned on. Nothing reaches into the server (no runner, no inbound access), and an update never changes what runs as root. See [deploy/README.md](../deploy/README.md).

## Secrets

Secrets never go in `usher.toml` or in this repo. The Tests workflow looks for committed ones with gitleaks on every run (each commit of a pull request, and the tree on `main`) and names only the file and line of a finding; `.gitleaks.toml` leaves out only the made-up keys in the tests and the fake integrations. GitHub's secret scanning and push protection, where the repository has them, work alongside this check, which stays as a second layer. Secrets are read from the environment (`.env` in the install folder, root only, mode 600), from the integrations' own `config.xml` files mounted read-only or, when an admin enters them in the app, from `secrets.json` beside `usher.toml`, written mode 600 by Usher alone and never shown back in full (an admin sees whether one is set and the last four characters of a long one). The Setup page works only while Usher has no accounts and only with the one-time code from its log, which is kept in memory, compared in constant time and replaced after five wrong tries; the first account is the admin, and settings need an admin's session. Passwords, session tokens, API keys, push keys and `Authorization` headers are never written to the log or returned by Status. Everything from an integration or a device is treated as untrusted and validated before use.

## Known vulnerabilities

The weekly checks run pip-audit on `requirements.txt` and Grype on the image they build. A known vulnerability with a fix available fails the run and opens one issue labelled `vulnerabilities`, which the next clear run closes. A finding that doesn't affect Usher is accepted in `.github/accepted-vulnerabilities.toml` with its reason and an expiry date, after which it counts again. Dependabot proposes updates to the Python packages, the actions and the Dockerfile's base image once a month.

## Update reports

`usher-update` tells Usher how an update ended with `POST /internal/update-result` on `127.0.0.1`, the way `/healthz` gives its details only to requests made on the server itself: a request from another address, or one that came through the reverse proxy (it carries `X-Forwarded-For`), is refused. It takes a result (`built` or `rolled_back`) with the running version (which must match), or a check (`checked`) with the newest release seen and whether it is held or failed, and changes nothing else. It runs only when someone updates by hand or has turned on the daily timer; without it nothing reports in, and Usher's own once-a-day look at the newest release (`updates.check_releases`) is all Settings has. Usher keeps the last result and the latest check, with its time, in `update.json` in the data folder. Settings reads the state computed from them (nothing that names the server), and the app never contacts GitHub.

## Health checks

`/healthz` answers `{"ok": …, "version": …, "status": …, "problems": [...], "notes": [...]}` at once, from a check run in the background every 5 minutes: the viewing account exists and isn't disabled; each integration's key works; the quality profile and library folder new requests use still exist; Sonarr and Radarr each have Transmission turned on as a download client; Prowlarr is linked to both; Transmission accepts its login. Any failure makes `status` `degraded` and `ok` false, with the reason in `problems`. `notes` suggest IDs to add to `usher.toml` while something is still found by name, and `config` lists settings warnings. The HTTP status is always 200, so start-up waits are unaffected. `problems`, `notes` and `config` are only included for requests made on the server itself (not through the proxy), since `/healthz` needs no sign-in.
