#!/usr/bin/env bash
# Build the single-file installer into ./dist
#   installer/make-installer.sh            source only: the target downloads Docker images
#   installer/make-installer.sh --offline  includes every Docker image (no internet needed for
#                                          Ellalon itself; target must have the same CPU type)
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$ROOT"
VERSION=$(cat VERSION)
OFFLINE=0; [[ "${1:-}" == "--offline" ]] && OFFLINE=1
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/ellalon"
tar --exclude=./.env --exclude=./.update --exclude=./.git --exclude=./dist --exclude='__pycache__' --exclude='*.pyc' -cf - . \
  | tar -xf - -C "$STAGE/ellalon"
NAME="ellalon-installer-$VERSION"

if [[ $OFFLINE -eq 1 ]]; then
  ARCH=$(uname -m); NAME="$NAME-offline-$ARCH"
  mapfile -t ALL < <( { grep -hE '^\s*image:' docker-compose.yml modules/*/module.yaml \
      | awk '{print $2}' | sed "s/\${ELLALON_VERSION:-[^}]*}/$VERSION/"; } | sort -u)
  PRESENT=()
  for img in "${ALL[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null 2>&1; then
      PRESENT+=("$img")
    else
      echo "   skipping $img (not built - optional module?)"
    fi
  done
  echo "==> Saving ${#PRESENT[@]} images for $ARCH (this takes a while)"
  mkdir -p "$STAGE/ellalon/images"
  echo "$ARCH" > "$STAGE/ellalon/images/ARCH"
  docker save "${PRESENT[@]}" | gzip -1 > "$STAGE/ellalon/images/images.tar.gz"
fi

mkdir -p dist
OUT="dist/$NAME.run"
cp installer/install.sh "$OUT"
(cd "$STAGE" && tar -czf - ellalon) >> "$OUT"
chmod +x "$OUT"
(cd dist && sha256sum "$NAME.run" > "$NAME.run.sha256")
echo "==> Created $ROOT/$OUT ($(du -h "$OUT" | cut -f1))"
echo "    Copy it to the new server and run:  sudo bash $NAME.run"
