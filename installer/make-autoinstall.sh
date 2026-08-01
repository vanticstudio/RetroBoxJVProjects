#!/usr/bin/env bash
#
# Build the real Retro Box autoinstall answer file from the committed template.
#
# The template is public and contains placeholders. This produces
# installer/autoinstall.yaml, which contains a password hash and an SSH public
# key and is gitignored.
#
# Usage:
#   ./installer/make-autoinstall.sh --ssh-key ~/.ssh/id_ed25519.pub
#   ./installer/make-autoinstall.sh --ssh-key ~/.ssh/id_ed25519.pub \
#       --wifi-ssid "Home WiFi" --wifi-password "hunter2hunter2"
#
# The password is asked for interactively unless --password is given, and is
# passed to the renderer in the environment so it never appears in `ps` or in
# your shell history.
#
# Runs on stock macOS: only /bin/bash and /usr/bin/python3 are required. No
# Homebrew, no mkpasswd, no openssl - see installer/lib/sha512crypt.py for why
# none of those would work here anyway.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${HERE}/autoinstall.yaml.template"
OUT="${HERE}/autoinstall.yaml"

USERNAME="retrobox"
HOSTNAME_="retrobox"
SSH_KEY=""
PASSWORD=""
WIFI_SSID=""
WIFI_PASSWORD=""
FORCE=0

usage() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-key)       SSH_KEY="${2:?--ssh-key needs a path}"; shift 2 ;;
    --password)      PASSWORD="${2:?--password needs a value}"; shift 2 ;;
    --username)      USERNAME="${2:?--username needs a value}"; shift 2 ;;
    --hostname)      HOSTNAME_="${2:?--hostname needs a value}"; shift 2 ;;
    --wifi-ssid)     WIFI_SSID="${2:?--wifi-ssid needs a value}"; shift 2 ;;
    --wifi-password) WIFI_PASSWORD="${2:?--wifi-password needs a value}"; shift 2 ;;
    --out)           OUT="${2:?--out needs a path}"; shift 2 ;;
    --force)         FORCE=1; shift ;;
    -h|--help)       usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done

[[ -f "${TEMPLATE}" ]] || { echo "missing template: ${TEMPLATE}" >&2; exit 1; }

if [[ -z "${SSH_KEY}" ]]; then
  echo "error: --ssh-key is required." >&2
  echo "Remote access to these boxes is key-only; without a key you could" >&2
  echo "never reach one over the network. Generate one with:" >&2
  echo "    ssh-keygen -t ed25519 -C retrobox" >&2
  exit 2
fi

if [[ -e "${OUT}" && "${FORCE}" -ne 1 ]]; then
  echo "error: ${OUT} already exists." >&2
  echo "It holds the credentials for boxes you may already have shipped." >&2
  echo "Re-run with --force to replace it." >&2
  exit 2
fi

# --- the password ------------------------------------------------------------
# Asked for twice, never echoed. This is the console password that every unit
# ships with, so it is worth getting right once.
if [[ -z "${PASSWORD}" ]]; then
  printf 'Console password for every Retro Box unit (input hidden): ' >&2
  read -r -s PASSWORD
  printf '\n' >&2
  printf 'Again: ' >&2
  read -r -s PASSWORD_CONFIRM
  printf '\n' >&2
  if [[ "${PASSWORD}" != "${PASSWORD_CONFIRM}" ]]; then
    echo "error: the two passwords do not match." >&2
    exit 1
  fi
  unset PASSWORD_CONFIRM
fi

# Prove the hashing is right before using it. Cheap, and it is the one part of
# this that fails silently and dangerously if the platform is uncooperative.
if ! python3 "${HERE}/lib/sha512crypt.py" > /dev/null; then
  echo "error: the SHA-512 crypt self-test FAILED. Refusing to generate a" >&2
  echo "password hash that might be wrong." >&2
  exit 1
fi

RETROBOX_PASSWORD="${PASSWORD}" \
RETROBOX_USERNAME="${USERNAME}" \
RETROBOX_HOSTNAME="${HOSTNAME_}" \
RETROBOX_SSH_KEY="${SSH_KEY}" \
RETROBOX_WIFI_SSID="${WIFI_SSID}" \
RETROBOX_WIFI_PASSWORD="${WIFI_PASSWORD}" \
  python3 "${HERE}/lib/render_autoinstall.py" "${TEMPLATE}" "${OUT}"

cat >&2 <<EOF

Next:
  ./installer/make-cidata.sh              # build the config stick
  ./installer/make-boot-iso.sh <iso>      # add 'autoinstall' to the boot ISO

${OUT} is gitignored. Keep it off the public repo.
EOF
