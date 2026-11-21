# Configuration

Every choice Usher makes is a key in `usher.toml`, in the install folder's `config/` folder (mounted read-only at `/config/usher.toml`; another place: `USHER_CONFIG`). [deploy/usher.example.toml](../deploy/usher.example.toml) lists every key with its default. Any key left out keeps its default. A test fails if a key is missing from this page.

Nothing about the home it runs in is kept in this repo. Usher is installed in one folder on the server, `/opt/usher` by default or wherever `USHER_HOME` says ([deploy/README.md](../deploy/README.md) covers installing, updating and upgrading). It holds these files:

- `config/usher.toml`: every choice Usher makes, in one documented file, mounted read-only at `/config/usher.toml` (another place: `USHER_CONFIG`). `deploy/usher.example.toml` lists every key with its default: address, time zone, the integrations' addresses, the viewing account and quality profiles by ID, library folders, family-friendly folders and ratings, the quality profile fallback, Discover's region and languages, how far ahead All upcoming looks, the low-space warning, how often a waiting request is searched for again, and when and how stalled downloads are replaced. Any key left out keeps its default. It is the only place for settings: with no file, Usher doesn't start: the log (`sudo docker logs usher`) says the file is missing and names `deploy/usher.example.toml`, and `usher-update` goes back to the previous version. Secrets never go in it.
- `.env`: secrets (the Transmission password, Jellyfin key, TMDb token, Web Push keys), root only, `USHER_VERSION` (the version of the published image that runs; `setup.sh` and `usher-update` write it), and the four things from this machine the container mounts: `USHER_SONARR_XML`, `USHER_RADARR_XML` and `USHER_PROWLARR_XML` (each app's `config.xml`, for its API key) and `USHER_RECYCLE_BIN` (Sonarr's and Radarr's recycle bin). `setup.sh` asks for them; without one, Docker Compose won't start Usher and names the missing one.
- `site.env`: only `USHER_REPO`, the repo address `setup.sh` and `usher-update` look up releases at, and `TZ`, the container's time zone (Docker Compose passes it in). Any other line in it is ignored and can be deleted. `setup.sh` reads the address, the viewing account and the Transmission user from `usher.toml`, offers to create the file from the example when there is none, and runs `config check` at the end.

Settings and secrets can also be saved by an admin (the first account, made on the Setup page, see below); right after the account is made, the Setup page's Connect your apps step does this for each app's address and key (the screen for changing them later is still to come). Usher then writes them itself: the settings into `usher.toml` (the file is rewritten from the keys it holds, so comments in it are not kept; the file as the operator wrote it stays beside it as `usher.toml.before-usher`) and the secrets into `secrets.json` beside it, readable by Usher alone and never shown back in full. A secret in the environment (`.env`) wins over one entered in the app, and the app says so. `server.bind` and `server.data_dir` can only be changed in the file, and a change made in the app takes effect the next time Usher starts. The file has to be writable, as in the published image's data volume (a read-only mount makes Setup say it can't write to the data folder); the install folder's read-only `config/usher.toml` stays edited by hand.

A new Usher with no accounts starts without `usher.toml` or `media_server.user_id` and prints a one-time setup code in its log (`sudo docker logs usher`). Entering it on the Setup page creates the first account, who is the admin, and makes `usher.toml` if there is none. The next step, Connect your apps, takes each app's address and key and has a Test button: it reads the app's version and says Supported, Not tested yet (it works, with a note) or Too old, or why it couldn't connect. Usher suggests addresses where something is listening on the app's usual port on this machine, the host and the app's usual container name; it doesn't scan the network. Only apps that connected are saved, and a key is never shown back. Once that account exists the Setup page is gone for good. Five wrong codes pause every try for a minute. The sign-in cookie is only kept over HTTPS (or on `localhost`), so open the Setup page that way. A server that already has accounts is checked as strictly as before.

The server checks the settings at start-up and won't start with an unknown key, a wrong type, a bad value, no `usher.toml` or no `media_server.user_id`; the log names the key. `sudo usher-user config check` runs the same check without restarting.

Usher refers to the Jellyfin viewing account and the quality profiles by ID (`sudo usher-user ids` lists them), so renaming anything in Jellyfin, Sonarr or Radarr changes nothing.

## Keys

Secrets never go in `usher.toml`. A list is written `["a", "b"]`; a whole number without quotes.


### `[server]`

| Key | Default | Means |
| --- | --- | --- |
| `server.origin` | (empty) | The address people open Usher at, e.g. https://usher.example.net |
| `server.bind` | `"127.0.0.1:8484"`; `"0.0.0.0:8484"` in the published image | Address and port the server listens on. The published image listens on all addresses, since a published port can't reach it otherwise; its compose file publishes the port on the host's `127.0.0.1` only, which decides who can reach it |
| `server.data_dir` | `"/data"` | Where accounts, requests and settings are kept |
| `server.utc_offset_minutes` | `0` | The server's time zone as minutes ahead of UTC, for times shown in Status and messages |
| `server.session_days` | `90` | Days a device stays signed in |
| `server.trusted_proxies` | `["127.0.0.1", "::1"]` | Addresses of the reverse proxy in front of Usher, whose X-Forwarded-For is believed |
| `server.push_contact` | (empty) | Contact sent with notifications to the push services; empty = server.origin |
| `server.shortcut_url` | (empty) | An iCloud Shortcuts link that installs the Connect Shortcut; when set, Settings → Device offers it. Must look like `https://www.icloud.com/shortcuts/<id>`, and the Shortcut must be named `Usher Connect` |

### `[media_server]`

| Key | Default | Means |
| --- | --- | --- |
| `media_server.url` | `"http://127.0.0.1:8096"` | Jellyfin, as the server reaches it |
| `media_server.public_url` | (empty) | Jellyfin, as people open it in a browser |
| `media_server.user_id` | (empty) | Required: the viewing account whose watch history Usher shows, by ID (usher-user ids lists them) |

### `[pvr.tv]`

| Key | Default | Means |
| --- | --- | --- |
| `pvr.tv.url` | `"http://127.0.0.1:8989"` | Sonarr |
| `pvr.tv.quality_profile_id` | `0` | Quality profile for new shows, by ID; 0 = look up quality.profile_name |

### `[pvr.movies]`

| Key | Default | Means |
| --- | --- | --- |
| `pvr.movies.url` | `"http://127.0.0.1:7878"` | Radarr |
| `pvr.movies.quality_profile_id` | `0` | Quality profile for new movies, by ID; 0 = look up quality.profile_name |

### `[indexer_manager]`

| Key | Default | Means |
| --- | --- | --- |
| `indexer_manager.url` | `"http://127.0.0.1:9696"` | Prowlarr |

### `[download_client]`

| Key | Default | Means |
| --- | --- | --- |
| `download_client.url` | `"http://127.0.0.1:9091/transmission/rpc"` | Transmission's RPC address |
| `download_client.user` | (empty) | Transmission username (the password stays in .env as TR_PASS) |

### `[library]`

| Key | Default | Means |
| --- | --- | --- |
| `library.tv_folder` | `"/srv/media/TV Shows"` | Library folder for new shows |
| `library.movie_folder` | `"/srv/media/Movies"` | Library folder for new movies |
| `library.media_path` | `"/srv/media"` | Picks the disk whose free space Status shows |
| `library.recycle_dir` | `"/recycle"` | Sonarr's and Radarr's recycle bin, as mounted |

### `[family]`

| Key | Default | Means |
| --- | --- | --- |
| `family.tv_folder` | (empty) | Family-friendly library folder for shows; empty = off |
| `family.movie_folder` | (empty) | Family-friendly library folder for movies; empty = off |
| `family.ratings` | `["G", "PG", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G", "TV-PG"]` | Ratings that count as family-friendly |

### `[quality]`

| Key | Default | Means |
| --- | --- | --- |
| `quality.profile_name` | `"HD-1080p"` | Quality profile looked up by name when no ID is set |
| `quality.fallback_contains` | `"1080"` | Without that profile, the first whose name contains this; then simply the first |

### `[discover]`

| Key | Default | Means |
| --- | --- | --- |
| `discover.region` | `"US"` | Country for streaming availability |
| `discover.language` | `"en-US"` | Language for titles and descriptions |
| `discover.upcoming_language` | `"en"` | Original language of the shows in All upcoming (ISO 639-1); empty = every language |
| `discover.upcoming_days` | `60` | How far ahead All upcoming looks, in days |

### `[storage]`

| Key | Default | Means |
| --- | --- | --- |
| `storage.low_space_gb` | `30` | Status warns when the library disk has less free space than this, in GB |

### `[retries]`

| Key | Default | Means |
| --- | --- | --- |
| `retries.first_search_after_minutes` | `60` | A request still waiting is searched for again first after this long |
| `retries.early_every_hours` | `6` | Then this often |
| `retries.early_for_days` | `3` | For this many days, after which it's shown as not found yet |
| `retries.late_every_hours` | `24` | Then this often |
| `retries.missing_grace_hours` | `3` | A new episode of a show you have is searched for this long after it aired, giving the feeds first go |
| `retries.missing_max_searches` | `5` | At most this many episode searches are sent at a time, so search sites aren't flooded |

### `[downloads]`

| Key | Default | Means |
| --- | --- | --- |
| `downloads.replace` | `"report"` | Replace a stalled download with a healthier copy: on, report (only log what it would do) or off |
| `downloads.stalled_after_hours` | `6` | A download with no progress for this long is stalled and a better copy is looked for |
| `downloads.max_size_factor` | `2` | A replacement may be at most this many times the size of the stalled copy |
| `downloads.min_free_margin_gb` | `20` | Free space to keep, in GB, on top of Sonarr's and Radarr's own minimum, after a replacement |
| `downloads.max_parallel` | `2` | Replacements running at once, at most |
| `downloads.max_attempts` | `3` | Replacements per episode, season or movie |
| `downloads.retry_after_days` | `3` | After max_attempts, wait this many days before trying again |
| `downloads.folders` | `[]` | Transmission's download folders (unfinished and finished) as Usher sees them, checked for leftovers after a copy is removed; empty = ask Transmission only |

### `[updates]`

| Key | Default | Means |
| --- | --- | --- |
| `updates.check_releases` | `"on"` | Ask GitHub once a day for the newest release, for Settings → About → Updates: `"on"` or `"off"` (off = no request is made) |
