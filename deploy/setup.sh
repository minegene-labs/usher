#!/usr/bin/env bash
# Usher setup on the server. Safe to run again. See deploy/README.md.
# Usher lives in one install folder: USHER_HOME (sudo USHER_HOME=/some/folder bash setup.sh), else the one recorded
# in /etc/default/usher by an earlier run, else /opt/usher by default. It needs site.env there with USHER_REPO (and TZ
# for the container; see deploy/site.env.example). Every setting is read from config/usher.toml there. Nothing about
# the home is stored in the repo.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with: sudo bash $0"; exit 1; }

# The tools deploy/README.md lists under Requirements, all checked before anything is changed. Nothing is installed
# here: how to install them differs between distributions.
atleast() {   # atleast 2.20.2 v2.29.1-desktop.1: the second version is the first or later
  local IFS=. want have i v=${2#v}
  read -ra want <<< "$1"; read -ra have <<< "${v%%[!0-9.]*}"
  for i in 0 1 2; do
    [ "$((10#${have[i]:-0}))" -eq "$((10#${want[i]:-0}))" ] || { [ "$((10#${have[i]:-0}))" -gt "$((10#${want[i]:-0}))" ]; return; }
  done
}
MISSING=()
for tool in git curl flock logger systemctl docker; do command -v "$tool" >/dev/null || MISSING+=("$tool"); done
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || MISSING+=("python3 3.11 or later")
if command -v docker >/dev/null && ! atleast 2.20.2 "$(docker compose version --short 2>/dev/null || true)"; then
  MISSING+=("Docker Compose 2.20.2 or later")   # --wait-timeout, and start_interval in compose.yaml
fi
if [ "${#MISSING[@]}" -gt 0 ]; then
  printf -v LIST '%s, ' "${MISSING[@]}"
  echo "FAIL: missing ${LIST%, }. Install them (see Requirements in deploy/README.md), then run this again."
  exit 1
fi

CONF=/etc/default/usher     # where usher-update finds the install folder
if [ -z "${USHER_HOME:-}" ] && [ -r "$CONF" ]; then
  USHER_HOME=$(sed -n 's/^USHER_HOME=//p' "$CONF" | tail -n 1); USHER_HOME=${USHER_HOME#\"}; USHER_HOME=${USHER_HOME%\"}
fi
DIR=${USHER_HOME:-/opt/usher}
case $DIR in /*) ;; *) echo "FAIL: USHER_HOME must be a full path, not '$DIR'"; exit 1 ;; esac
SITE="$DIR/site.env"
[ -s "$SITE" ] || { echo "FAIL: $SITE is missing. Copy deploy/site.env.example there and fill it in."; exit 1; }
set -a
# shellcheck source=/dev/null
. "$SITE"
set +a
[ -n "${USHER_REPO:-}" ] || { echo "FAIL: USHER_REPO is empty in $SITE"; exit 1; }
case $USHER_REPO in https://*) ;; *) echo "FAIL: USHER_REPO in $SITE must be an https:// address, not '$USHER_REPO'"; exit 1 ;; esac
export GIT_TERMINAL_PROMPT=0   # a repository that asks for a login fails instead of waiting
TOML="$DIR/config/usher.toml"
say() { printf '\n== %s\n' "$1"; }

# 1. Install folder and access ------------------------------------------------------
say "Access to the repository"
mkdir -p "$DIR" && chmod 755 "$DIR"
git ls-remote "$USHER_REPO" HEAD >/dev/null 2>&1 \
  || { echo "FAIL: can't read $USHER_REPO. Check USHER_REPO in $SITE (an https:// address) and the network."; exit 1; }
echo "PASS the server can read the repo"

# 2. Deploy files: the newest release's, in src (Usher itself runs from the published image) -------
say "Newest release"
VER=$(git ls-remote --tags --refs "$USHER_REPO" 'v*' \
      | sed -n 's#.*refs/tags/v\([0-9]\{1,6\}\.[0-9]\{1,6\}\.[0-9]\{1,6\}\)$#\1#p' \
      | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)
[ -n "$VER" ] || { echo "FAIL: $USHER_REPO has no release yet."; exit 1; }
if [ -d "$DIR/src/.git" ]; then
  git -C "$DIR/src" remote set-url origin "$USHER_REPO"
  git -C "$DIR/src" fetch --depth 1 origin "+refs/tags/v$VER:refs/tags/v$VER" && git -C "$DIR/src" reset --hard "v$VER"
else
  git clone --depth 1 --branch "v$VER" "$USHER_REPO" "$DIR/src"
fi
echo "PASS the deploy files are from release $VER"

# 2b. Settings: usher.toml, the only place for them ------------------------------------------------
say "Settings"
tomlget() {   # tomlget section key -> the value in usher.toml, or nothing
  python3 - "$TOML" "$1" "$2" <<'PY'
import sys, tomllib
path, section, key = sys.argv[1:]
with open(path, "rb") as f:
    node = tomllib.load(f)
for part in section.split("."):
    node = node.get(part, {})
print(node.get(key, ""))
PY
}
mkdir -p "$DIR/config" && chmod 755 "$DIR/config"     # usher.toml goes here (deploy/usher.example.toml)
if [ ! -f "$TOML" ]; then
  echo "There is no $TOML yet."
  read -rp "Create it now from the example? [Y/n] " ANS || ANS=n
  case "${ANS:-Y}" in
    [Nn]*) echo "FAIL: setup needs $TOML. Copy $DIR/src/deploy/usher.example.toml there, fill it in, then run this again."; exit 1 ;;
    *) cp "$DIR/src/deploy/usher.example.toml" "$TOML.new"
       chmod 644 "$TOML.new"; mv "$TOML.new" "$TOML"
       echo "Created from the example. Edit $TOML (origin, media_server.user_id, download_client.user), then run this again."; exit 0 ;;
  esac
fi
ORIGIN=$(tomlget server origin); JF_ID=$(tomlget media_server user_id); TR_USER=$(tomlget download_client user)
echo "Using $TOML"
[ -n "$ORIGIN" ] || { echo "FAIL: the address (server.origin in $TOML) is empty"; exit 1; }
[ -n "$JF_ID" ] || { echo "FAIL: the viewing account (media_server.user_id in $TOML) is empty; usher-user ids lists them"; exit 1; }
[ -n "$TR_USER" ] || { echo "FAIL: the Transmission user (download_client.user in $TOML) is empty"; exit 1; }
JF_URL=$(tomlget media_server url 2>/dev/null || true); JF_URL=${JF_URL:-http://127.0.0.1:8096}
SONARR_URL=$(tomlget pvr.tv url 2>/dev/null || true); SONARR_URL=${SONARR_URL:-http://127.0.0.1:8989}
RADARR_URL=$(tomlget pvr.movies url 2>/dev/null || true); RADARR_URL=${RADARR_URL:-http://127.0.0.1:7878}

# 3. Secrets (root only) ----------------------------------------------------------
say "Secrets"
touch "$DIR/.env" && chmod 600 "$DIR/.env"
setkey() { grep -v "^$1=" "$DIR/.env" > "$DIR/.env.new" || true; printf '%s=%s\n' "$1" "$2" >> "$DIR/.env.new"
           chmod 600 "$DIR/.env.new"; mv "$DIR/.env.new" "$DIR/.env"; }
if ! grep -q '^TR_PASS=.' "$DIR/.env"; then
  read -rsp "Transmission password (hidden): " TR_PASS; echo
  setkey TR_PASS "$TR_PASS"; unset TR_PASS
fi
if ! grep -q '^JELLYFIN_API_KEY=.' "$DIR/.env"; then
  echo "Create a Jellyfin API key for Usher: Jellyfin → Dashboard → API Keys → +"
  while :; do
    read -rsp "Jellyfin API key (hidden): " JF; echo
    USERS=$(curl -fsS --max-time 10 -H "Authorization: MediaBrowser Client=\"Usher\", Token=\"$JF\"" \
            "${JF_URL%/}/Users" 2>/dev/null || true)
    if printf '%s' "$USERS" | tr -d '-' | grep -qi "\"Id\":\"${JF_ID//-/}\""; then echo "PASS key works and the viewing account exists"; break; fi
    echo "That key didn't work, or Jellyfin has no viewing account like that. Try again."
  done
  setkey JELLYFIN_API_KEY "$JF"; unset JF USERS
fi
if ! grep -q '^TMDB_TOKEN=.' "$DIR/.env"; then
  read -rsp "TMDb API Read Access Token for Discover (hidden, Return to skip): " TMDB; echo
  if [ -n "$TMDB" ]; then
    TMDB=$(printf '%s' "$TMDB" | tr -d '[:space:]')
    if curl -fsS --max-time 10 -H "Authorization: Bearer $TMDB" https://api.themoviedb.org/3/configuration >/dev/null 2>&1 \
       || curl -fsS --max-time 10 "https://api.themoviedb.org/3/configuration?api_key=$TMDB" >/dev/null 2>&1; then
      setkey TMDB_TOKEN "$TMDB"; echo "PASS TMDb accepted the token"
    else echo "TMDb didn't accept that (${#TMDB} characters). Use the API Read Access Token or the API Key from themoviedb.org → Settings → API. Run this again to retry."; fi
  fi
  unset TMDB
fi
echo "Secrets in $DIR/.env: $(cut -d= -f1 "$DIR/.env" | paste -sd' ')"

# 3b. Folders and files from this machine that Usher's container mounts (compose.yaml reads them from .env) --------
say "Folders Usher uses"
envget() { sed -n "s/^$1=//p" "$DIR/.env" | tail -n 1; }
askpath() {   # askpath VAR "what it is" file|dir: ask once, keep it in .env
  local v; v=$(envget "$1")
  while [ -z "$v" ] || { [ "$3" = file ] && [ ! -f "$v" ]; } || [[ "$v" != /* ]]; do
    [ -z "$v" ] || echo "Not found or not a full path: $v"
    read -rp "$2 (full path): " v
  done
  [ "$v" = "$(envget "$1")" ] || setkey "$1" "$v"
  echo "PASS $1=$v"
}
askpath USHER_SONARR_XML "Sonarr's config.xml on this machine (Usher reads its API key)" file
askpath USHER_RADARR_XML "Radarr's config.xml on this machine" file
askpath USHER_PROWLARR_XML "Prowlarr's config.xml on this machine" file
askpath USHER_RECYCLE_BIN "Recycle bin folder for Sonarr and Radarr, at the same path they see it, e.g. inside your media folder" dir
RECYCLE=$(envget USHER_RECYCLE_BIN)

# 4. Container -------------------------------------------------------------------
say "Start Usher"
install -d -m 755 "$(dirname "$CONF")"
printf '# Usher install folder, read by usher-update (written by setup.sh)\nUSHER_HOME=%s\n' "$DIR" > "$CONF.new"
chmod 644 "$CONF.new"; mv "$CONF.new" "$CONF"
install -m 644 "$DIR/src/deploy/compose.yaml" "$DIR/compose.yaml"
chmod 644 "$SITE"
mkdir -p "$DIR/data" && chown 1000:1001 "$DIR/data" && chmod 700 "$DIR/data"
# The version to run, written once: an update after this is usher-update, or a new version here and the same command.
grep -q '^USHER_VERSION=.' "$DIR/.env" || setkey USHER_VERSION "$VER"
(cd "$DIR" && docker compose pull && docker compose up -d --wait --wait-timeout 120) \
  || { echo "FAIL: Usher didn't start or isn't healthy. Look at: sudo docker logs --tail 50 usher"; exit 1; }
curl -fsS http://127.0.0.1:8484/healthz && echo
docker exec usher python -m usher.cli config check || { echo "FAIL: fix the settings above, then run this again"; exit 1; }

# 5. Recycle bin: anything removed from Usher can be recovered for 7 days ---------------
say "Recycle bin in Sonarr and Radarr ($RECYCLE, 7 days)"
install -d -o 1000 -g 1001 -m 2775 "$RECYCLE"
python3 - "$RECYCLE" "$(envget USHER_SONARR_XML)" "$SONARR_URL" "$(envget USHER_RADARR_XML)" "$RADARR_URL" <<'PY'
import json, re, sys, urllib.request
recycle, apps = sys.argv[1], (("sonarr", *sys.argv[2:4]), ("radarr", *sys.argv[4:6]))
for app, xml, base in apps:
    with open(xml, encoding="utf-8") as f:
        found = re.search(r"<ApiKey>(\w+)</ApiKey>", f.read())
    if not found:
        sys.exit(f"FAIL {app}: no <ApiKey> in {xml}")
    url = f"{base.rstrip('/')}/api/v3/config/mediamanagement"
    hdr = {"X-Api-Key": found.group(1), "Content-Type": "application/json"}
    cfg = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=15))
    if cfg.get("recycleBin") == recycle and cfg.get("recycleBinCleanupDays") == 7:
        print(f"PASS {app}: already set"); continue
    cfg.update(recycleBin=recycle, recycleBinCleanupDays=7)
    req = urllib.request.Request(f"{url}/{cfg['id']}", data=json.dumps(cfg).encode(), headers=hdr, method="PUT")
    urllib.request.urlopen(req, timeout=15)
    print(f"PASS {app}: recycle bin set")
PY

# 6. Helper commands ---------------------------------------------------------------
say "Commands: usher-user, usher-update"
cat > /usr/local/bin/usher-user <<'EOF'
#!/usr/bin/env bash
# Manage Usher accounts: usher-user add <name> [Display Name] | passwd <name> | remove <name> | list
exec docker exec -it usher python -m usher.cli "$@"
EOF
install -m 755 "$DIR/src/deploy/usher-update" /usr/local/bin/usher-update
chmod 755 /usr/local/bin/usher-user /usr/local/bin/usher-update
# Automatic updates are optional and off unless turned on. The timer an earlier setup.sh turned on itself followed
# every merge every 15 minutes, which is not what is turned on here, so it is turned off; one already of the new
# kind (once a day, releases only) stays as the person left it.
if [ -f /etc/systemd/system/usher-autodeploy.timer ] && grep -q '^OnUnitActiveSec=' /etc/systemd/system/usher-autodeploy.timer; then
  systemctl disable --now usher-autodeploy.timer 2>/dev/null || true
  OLD_TIMER=1
fi
install -m 644 "$DIR/src/deploy/usher-autodeploy.service" "$DIR/src/deploy/usher-autodeploy.timer" /etc/systemd/system/
systemctl daemon-reload
if [ -n "${OLD_TIMER:-}" ]; then
  echo "The old timer, which updated Usher every 15 minutes, is off. Automatic updates are now optional and run once a day, to a new release only. To turn them on: sudo systemctl enable --now usher-autodeploy.timer"
elif systemctl is-enabled --quiet usher-autodeploy.timer 2>/dev/null; then
  echo "Automatic updates are on: once a day, to a new release only. To turn them off: sudo systemctl disable --now usher-autodeploy.timer"
else
  echo "Automatic updates are off. To update by hand: sudo usher-update. To turn them on: sudo systemctl enable --now usher-autodeploy.timer"
fi

# 7. Accounts (only asked for when there are none) -------------------------------------
say "Accounts"
if [ -z "$(docker exec usher python -m usher.cli list)" ]; then
  read -rp "Usernames to create, separated by spaces: " NAMES
  for n in $NAMES; do echo "Password for $n:"; docker exec -it usher python -m usher.cli add "$n"; done
else
  docker exec usher python -m usher.cli list
fi

# 8. Check ---------------------------------------------------------------------------
say "Check"
curl -fsS -o /dev/null -w "$ORIGIN/ → %{http_code}\n" "$ORIGIN/"
curl -sS -o /dev/null -w "$ORIGIN/api/state without signing in → %{http_code} (401 expected)\n" "$ORIGIN/api/state" || true
docker ps --filter name=usher --format '{{.Names}}: {{.Status}}'
