#!/usr/bin/env bash
# Ellalon control script (also installed as the `ellalon` command)
#   ellalon build [--all | module]   build core + standard modules (--all adds optional ones)
#   ellalon up | down | restart | status
#   ellalon logs [core | <module> | <service>]
#   ellalon password [user]          set a new password for a user (default admin)
#   ellalon backup                   save the database and module settings to the data folder
#   ellalon bundle [--offline]       create an installer file in ./dist (offline = with all images)
#   ellalon update                   download and install the latest version (online installs)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
env_get() { grep -E "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }

build_module() {
  local dir="modules/$1" image auto
  [[ -f "$dir/module.yaml" ]] || { echo "No such module: $1" >&2; return 1; }
  [[ -f "$dir/Dockerfile" ]] || { echo "   $1: uses an upstream image, nothing to build"; return 0; }
  image=$(awk '/^image:/{print $2}' "$dir/module.yaml")
  auto=$(awk '/^autostart:/{print $2}' "$dir/module.yaml")
  echo "==> Building $1 -> $image"
  if ! docker build -t "$image" -f "$dir/Dockerfile" .; then
    [[ "$auto" == "true" ]] && { echo "ERROR: module $1 failed to build" >&2; return 1; }
    echo "WARNING: optional module $1 failed to build" >&2
  fi
}

case "${1:-help}" in
  build)
    if [[ -n "${2:-}" && "$2" != "--all" ]]; then build_module "$2"; exit; fi
    docker compose build core
    for d in modules/*/; do
      m=$(basename "$d")
      if [[ "${2:-}" == "--all" ]] || grep -q '^autostart: true' "$d/module.yaml" \
         || [[ -n "$(docker images -q "ellalon/$m" 2>/dev/null)" ]]; then
        build_module "$m"       # standard, or optional but built before (keep it up to date)
      else
        echo "   $m: optional, skipped (build later with: ellalon build $m)"
      fi
    done ;;
  up)
    docker compose up -d
    echo "==> Web UI: http://$(hostname -I | awk '{print $1}'):$(env_get ELLALON_HTTP_PORT)/" ;;
  down)
    docker compose down
    docker ps -aq --filter label=ellalon.module | xargs -r docker rm -f ;;
  restart) "$0" down; "$0" up ;;
  status)
    docker compose ps
    docker ps -a --filter label=ellalon.module --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' ;;
  logs)
    if [[ -n "${2:-}" ]] && docker inspect "ellalon-$2" >/dev/null 2>&1; then
      docker logs -f --tail 200 "ellalon-$2"
    else docker compose logs -f --tail 200 ${2:-}; fi ;;
  password)
    docker exec -it ellalon-core python -c "import getpass; from app.db import SessionLocal, User; \
from app.auth import hash_password; s=SessionLocal(); u=s.query(User).filter_by(username='${2:-admin}').first(); \
assert u, 'no such user'; u.password_hash=hash_password(getpass.getpass('New password: ')); s.commit(); print('Password changed')" ;;
  backup)
    data=$(env_get ELLALON_DATA_ROOT); data=${data:-/var/lib/ellalon}
    mkdir -p "$data/backups"; stamp=$(date +%Y%m%d-%H%M%S)
    docker compose exec -T postgres pg_dump -U ellalon --no-owner --no-privileges ellalon \
      > "$data/backups/ellalon-db-$stamp.sql"
    tar -czf "$data/backups/ellalon-modules-$stamp.tar.gz" --exclude='./vms-recorder/recordings' \
      -C "$data/modules" .
    echo "==> Backup written to $data/backups/ (ellalon-db-$stamp.sql, ellalon-modules-$stamp.tar.gz)" ;;
  bundle) shift; exec installer/make-installer.sh "$@" ;;
  update) exec bash install-online.sh --dir "$PWD" ;;
  *) sed -n '2,10p' "$0" ;;
esac
