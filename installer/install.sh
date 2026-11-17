#!/usr/bin/env bash
# Ellalon Security Platform - single-file installer
#
#   sudo bash ellalon-installer-<version>.run [options]
#
# Options
#   --dir DIR          install location                 (default /opt/ellalon)
#   --data DIR         data and recordings location     (default /var/lib/ellalon)
#   --port N           web port                         (default 80)
#   --all-modules      also build optional modules (analytics, face recognition)
#   --gpu              also install the NVIDIA Container Toolkit
#   --restore-db FILE  load a database backup (from `ellalon backup`) into a new installation
#   --no-start         install and build, but do not start
#   --extract DIR      only unpack the files into DIR and exit
#
# Installs Docker when missing, unpacks Ellalon, generates secrets, builds (or loads, for offline
# installers) every image and starts the system. Running it again upgrades an existing
# installation and keeps its settings and data.
set -euo pipefail

ARGS=("$@")
DIR=/opt/ellalon; DATA_ARG=""; PORT=""; GPU=0; START=1; EXTRACT=""; RESTORE=""; ALL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR=$2; shift 2 ;;
    --data) DATA_ARG=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --all-modules) ALL="--all"; shift ;;
    --gpu) GPU=1; shift ;;
    --restore-db) RESTORE=$(readlink -f "$2"); shift 2 ;;
    --no-start) START=0; shift ;;
    --extract) EXTRACT=$2; shift 2 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

log()  { echo -e "\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33mWARNING:\033[0m $*" >&2; }
die()  { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

SELF=$(readlink -f "$0")
unpack() {
  local line
  line=$(awk '/^__ELLALON_PAYLOAD__$/ { print NR + 1; exit }' "$SELF")
  [[ -n "$line" ]] || die "Installer file is damaged (no payload). Download it again."
  tail -n +"$line" "$SELF" | tar -xzf - -C "$1" || die "Installer file is damaged. Download it again."
}

if [[ -n "$EXTRACT" ]]; then
  mkdir -p "$EXTRACT"; unpack "$EXTRACT"; echo "Unpacked to $EXTRACT/ellalon"; exit 0
fi

[[ $EUID -eq 0 ]] || exec sudo bash "$SELF" "${ARGS[@]}"

# ---------------------------------------------------------------- checks
. /etc/os-release 2>/dev/null || true
log "Ellalon installer - ${PRETTY_NAME:-unknown OS} on $(uname -m)"
case " ${ID:-} ${ID_LIKE:-} " in
  *" ubuntu "*|*" debian "*) ;;
  *) warn "Tested on Ubuntu and Debian; continuing on ${PRETTY_NAME:-this OS}" ;;
esac
MEM_GB=$(awk '/MemTotal/ { printf "%d", $2 / 1048576 }' /proc/meminfo)
(( MEM_GB >= 3 )) || warn "Only ${MEM_GB} GB RAM. 4 GB or more is recommended."

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
log "Unpacking"
unpack "$TMP"
SRC="$TMP/ellalon"
VERSION=$(cat "$SRC/VERSION")

# ---------------------------------------------------------------- docker
install_docker() {
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    log "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?') is installed"
    return
  fi
  log "Installing Docker"
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg openssl >/dev/null
    local distro=${ID:-ubuntu} codename=${VERSION_CODENAME:-}
    if [[ "$distro" =~ ^(ubuntu|debian)$ && -n "$codename" ]] &&
       curl -fsI "https://download.docker.com/linux/$distro/dists/$codename/Release" >/dev/null 2>&1; then
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL "https://download.docker.com/linux/$distro/gpg" -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
           "https://download.docker.com/linux/$distro $codename stable" > /etc/apt/sources.list.d/docker.list
      apt-get update -qq
      apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    else
      apt-get install -y -qq docker.io docker-compose-v2 docker-buildx \
        || apt-get install -y -qq docker.io docker-compose-plugin
    fi
  else
    command -v curl >/dev/null || die "Install curl or Docker first, then run the installer again"
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable --now docker
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is not available after installing Docker"
}
install_docker

if [[ $GPU -eq 1 ]]; then
  log "Installing NVIDIA Container Toolkit (the NVIDIA driver must already be installed)"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi

# ---------------------------------------------------------------- files
UPGRADE=0; [[ -f "$DIR/.env" ]] && UPGRADE=1
if [[ $UPGRADE -eq 1 ]]; then log "Upgrading the installation in $DIR to $VERSION"; else log "Installing Ellalon $VERSION to $DIR"; fi
mkdir -p "$DIR"
# empty the code folders in place (running containers bind-mount these folders, so they must
# keep their identity; deleting and recreating them would leave containers on stale copies)
for d in core modules web sdk templates docs installer; do
  [[ -d "$DIR/$d" ]] && find "$DIR/$d" -mindepth 1 -delete
done
cp -a "$SRC/." "$DIR/"
rm -rf "$DIR/images"
chmod +x "$DIR/ellalon.sh" "$DIR"/installer/*.sh
ln -sf "$DIR/ellalon.sh" /usr/local/bin/ellalon

# ---------------------------------------------------------------- settings
rnd() { openssl rand -hex "$1" 2>/dev/null || head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
set_env() {
  if grep -q "^$1=" "$DIR/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"; else echo "$1=$2" >> "$DIR/.env"; fi
}
ADMIN_PW=""
if [[ $UPGRADE -eq 0 ]]; then
  ADMIN_PW=$(rnd 6)
  sed -e "s|change-me-db|$(rnd 16)|" -e "s|change-me-to-a-long-random-string|$(rnd 32)|" \
      -e "s|change-me-admin|$ADMIN_PW|" "$DIR/.env.example" > "$DIR/.env"
fi
chmod 600 "$DIR/.env"
[[ -n "$PORT" ]] && set_env ELLALON_HTTP_PORT "$PORT"
[[ -n "$DATA_ARG" ]] && set_env ELLALON_DATA_ROOT "$DATA_ARG"
DATA=$(grep -E '^ELLALON_DATA_ROOT=' "$DIR/.env" | cut -d= -f2-); DATA=${DATA:-/var/lib/ellalon}
HTTP_PORT=$(grep -E '^ELLALON_HTTP_PORT=' "$DIR/.env" | cut -d= -f2-); HTTP_PORT=${HTTP_PORT:-80}
mkdir -p "$DATA"/{postgres,redis,modules,backups}

# ---------------------------------------------------------------- images
cd "$DIR"
if [[ -f "$SRC/images/images.tar.gz" ]]; then
  [[ "$(cat "$SRC/images/ARCH")" == "$(uname -m)" ]] \
    || die "This offline installer is for $(cat "$SRC/images/ARCH") machines, this one is $(uname -m)"
  log "Loading the bundled Docker images (offline installer)"
  gunzip -c "$SRC/images/images.tar.gz" | docker load
else
  log "Building Ellalon (the first time takes several minutes)"
  ./ellalon.sh build $ALL
fi

# ---------------------------------------------------------------- database restore
if [[ -n "$RESTORE" ]]; then
  [[ -f "$RESTORE" ]] || die "Backup file not found: $RESTORE"
  log "Restoring the database from $RESTORE"
  docker compose up -d postgres
  for _ in $(seq 60); do docker compose exec -T postgres pg_isready -U ellalon >/dev/null 2>&1 && break; sleep 2; done
  sleep 3
  docker compose exec -T postgres psql -q -v ON_ERROR_STOP=1 -U ellalon ellalon < "$RESTORE" >/dev/null \
    || die "Database restore failed. --restore-db only works on a new installation with an empty database."
fi

# ---------------------------------------------------------------- start
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then usermod -aG docker "$SUDO_USER" || true; fi
OK=0
if [[ $START -eq 1 ]]; then
  log "Starting Ellalon"
  # module containers are recreated by core from the (possibly new) images
  docker ps -aq --filter label=ellalon.module | xargs -r docker rm -f >/dev/null
  docker compose up -d --remove-orphans
  # always restart core so it runs the new code and re-reads the module folders
  docker compose up -d --force-recreate --no-deps core
  for _ in $(seq 90); do
    if docker compose exec -T core python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" >/dev/null 2>&1; then
      OK=1; break
    fi
    sleep 2
  done
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
URL="http://${IP:-localhost}$([[ "$HTTP_PORT" == 80 ]] || echo ":$HTTP_PORT")/"
echo
if [[ $START -eq 0 ]]; then
  log "Installed. Start it with: sudo ellalon up"
elif [[ $OK -eq 1 ]]; then
  log "Ellalon $VERSION is running"
else
  warn "Ellalon started but did not answer yet. Check with: sudo ellalon status   and   sudo ellalon logs core"
fi
cat <<EOF

  Web UI         $URL
  User           admin
  Password       ${ADMIN_PW:-unchanged (existing installation)}
  Program files  $DIR
  Data           $DATA   (recordings, database, module settings)
  Commands       sudo ellalon status | logs | restart | backup | password | bundle

  Modules start automatically. Optional modules (analytics, face recognition):
  sudo ellalon build <module>, then enable them in the web UI under System.
EOF
[[ -n "$ADMIN_PW" ]] && echo -e "\n  Change the admin password after the first sign-in (Change password, top right)."
exit 0
__ELLALON_PAYLOAD__
