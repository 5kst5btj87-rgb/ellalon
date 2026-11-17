#!/usr/bin/env bash
# Ellalon online installer and updater (private GitHub repository)
#
#   Install on a new server:
#     curl -fsSL -H "Authorization: Bearer <TOKEN>" \
#       https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/install-online.sh \
#       | sudo bash -s -- --token <TOKEN> [installer options, e.g. --port 8080 --data /mnt/rec]
#
#   Update an installed server to the latest version:
#     sudo ellalon update
#
# <TOKEN> is a GitHub access token that can read the repository (see README, "Online install").
# The token is stored in <install dir>/.update (readable by root only) for later updates.
set -euo pipefail
REPO=5kst5btj87-rgb/ellalon; BRANCH=main; TOKEN=${ELLALON_TOKEN:-}; DIR=/opt/ellalon; ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --token) TOKEN=$2; shift 2 ;;
    --repo) REPO=$2; shift 2 ;;
    --branch) BRANCH=$2; shift 2 ;;
    --dir) DIR=$2; ARGS+=("$1" "$2"); shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
if [[ -z "$TOKEN" && -f "$DIR/.update" ]]; then . "$DIR/.update"; fi
if [[ -z "$TOKEN" && -r /dev/tty ]]; then
  read -rsp "GitHub access token for $REPO: " TOKEN </dev/tty; echo
fi
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
echo -e "\033[1;32m==>\033[0m Downloading Ellalon ($REPO, $BRANCH)"
AUTH=(); [[ -n "$TOKEN" ]] && AUTH=(-H "Authorization: Bearer $TOKEN")
curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/$REPO/tarball/$BRANCH" -o "$TMP/src.tgz" \
  || { echo "Download failed: check the token and that it can read $REPO"; exit 1; }
mkdir "$TMP/src"
tar -xzf "$TMP/src.tgz" --strip-components=1 -C "$TMP/src"
bash "$TMP/src/installer/make-installer.sh" >/dev/null
bash "$TMP"/src/dist/ellalon-installer-*.run "${ARGS[@]}"

umask 077
printf 'REPO=%q\nBRANCH=%q\nTOKEN=%q\n' "$REPO" "$BRANCH" "$TOKEN" > "$DIR/.update"
echo -e "\033[1;32m==>\033[0m Online updates enabled. Later, update with:  sudo ellalon update"
