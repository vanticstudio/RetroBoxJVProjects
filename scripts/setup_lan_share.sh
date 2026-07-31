#!/usr/bin/env bash
#
# Share the Retro Box media library on the LAN over SMB, so a Windows PC can
# open File Explorer -> Network, see this box, and drag files straight into the
# folder the channels are scanned from. No drive mapping, no path typing, no
# login prompt.
#
# Called by scripts/install.sh, but safe to run on its own and safe to re-run.
#
# Usage:
#   ./scripts/setup_lan_share.sh                      # share /media/retrobox
#   ./scripts/setup_lan_share.sh --path /srv/media    # share somewhere else
#
# NOTE ON ACCESS: the share is deliberately guest-writable, which is what makes
# "no login prompt" possible. Anyone who can reach this box on the network can
# read, add and delete files in the library. That is the right trade for a media
# box on a home LAN; do not put this on a network you don't control.
#
set -euo pipefail

SHARE_PATH="/media/retrobox"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --path) SHARE_PATH="${2:?--path needs a directory}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SMB_CONF="/etc/samba/smb.conf"
WORKGROUP="WORKGROUP"
SERVER_STRING="JV Projects Retro Box"

have_package() {
  apt-cache show "$1" > /dev/null 2>&1
}

# --- 1. Who actually owns the library? ---------------------------------------
# The share has to force-map guest writes onto a real local account, and getting
# this wrong means files land unreadable or the share refuses writes entirely.
# It is detected rather than assumed: "retrobox" is the box's hostname, and
# is very often NOT the login name.
if [[ -d "${SHARE_PATH}" ]]; then
  SHARE_USER="$(stat -c '%U' "${SHARE_PATH}")"
  if [[ "${SHARE_USER}" == "root" || "${SHARE_USER}" == "UNKNOWN" ]]; then
    # Owned by root (or an orphaned uid) - forcing root would be wrong, so fall
    # back to the human running the install and take ownership properly.
    SHARE_USER="${SUDO_USER:-$USER}"
    echo "==> ${SHARE_PATH} was owned by root; reassigning to ${SHARE_USER}"
    sudo chown -R "${SHARE_USER}" "${SHARE_PATH}"
  fi
else
  SHARE_USER="${SUDO_USER:-$USER}"
  echo "==> Creating ${SHARE_PATH} owned by ${SHARE_USER}"
  sudo mkdir -p "${SHARE_PATH}"
  sudo chown "${SHARE_USER}" "${SHARE_PATH}"
fi

if ! id -u "${SHARE_USER}" > /dev/null 2>&1; then
  echo "!! '${SHARE_USER}' is not a local account - cannot configure the share" >&2
  exit 1
fi

# The primary group is looked up rather than assumed equal to the username.
# Debian usually creates a matching per-user group, but not always.
SHARE_GROUP="$(id -gn "${SHARE_USER}")"
echo "==> Sharing ${SHARE_PATH} as ${SHARE_USER}:${SHARE_GROUP}"

# --- 2. Packages --------------------------------------------------------------
# wsdd is what makes the box appear under "Network" in File Explorer: modern
# Windows has dropped the old SMB1/NetBIOS browsing this used to rely on, and
# the config below sets a floor of SMB2. Without wsdd the share still works by
# \\hostname, but it will not show up on its own.
PACKAGES=(samba)
if have_package wsdd; then
  PACKAGES+=(wsdd)
else
  echo "!! 'wsdd' is not available on this distro."
  echo "   The share will work via \\\\$(hostname) but will NOT auto-appear"
  echo "   under Network in File Explorer. On older Ubuntu try: wsdd2"
  if have_package wsdd2; then
    PACKAGES+=(wsdd2)
    echo "   -> found wsdd2, installing that instead"
  fi
fi

echo "==> Installing: ${PACKAGES[*]}"
sudo apt-get install -y "${PACKAGES[@]}"

# --- 3. Config ----------------------------------------------------------------
# Back up the distro's original ONCE. A plain unconditional copy would, on the
# second run, overwrite the pristine backup with our own generated file.
if [[ -f "${SMB_CONF}" && ! -f "${SMB_CONF}.orig" ]]; then
  echo "==> Backing up the original config to ${SMB_CONF}.orig"
  sudo cp "${SMB_CONF}" "${SMB_CONF}.orig"
fi

STAGED="$(mktemp)"
trap 'rm -f "${STAGED}"' EXIT
cat > "${STAGED}" <<SMBCONF
# Managed by Retro Box scripts/setup_lan_share.sh - re-run it to regenerate.
# The distro's original is kept at ${SMB_CONF}.orig
[global]
   workgroup = ${WORKGROUP}
   server string = ${SERVER_STRING}
   security = user
   map to guest = Bad User
   guest account = nobody
   log file = /var/log/samba/log.%m
   max log size = 1000
   client min protocol = SMB2
   server min protocol = SMB2

[Library]
   comment = Retro Box show and movie library
   path = ${SHARE_PATH}
   browseable = yes
   guest ok = yes
   read only = no
   force user = ${SHARE_USER}
   force group = ${SHARE_GROUP}
   create mask = 0664
   directory mask = 0775
SMBCONF

# Validate BEFORE installing it, so a bad config can never take smbd down.
if ! testparm -s "${STAGED}" > /dev/null 2>&1; then
  echo "!! Generated smb.conf failed testparm - leaving the existing config alone" >&2
  testparm -s "${STAGED}" || true
  exit 1
fi

sudo cp "${STAGED}" "${SMB_CONF}"
echo "==> Wrote ${SMB_CONF} (validated with testparm)"

# --- 4. Services --------------------------------------------------------------
sudo systemctl restart smbd
sudo systemctl enable --now smbd
for unit in wsdd wsdd2; do
  if systemctl list-unit-files | grep -q "^${unit}\.service"; then
    sudo systemctl enable --now "${unit}"
    echo "==> ${unit} enabled (Windows network discovery)"
    break
  fi
done

# --- 5. Firewall, only if one is actually running -----------------------------
if command -v ufw > /dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -qi "Status: active"; then
  echo "==> ufw is active - opening Samba and WS-Discovery"
  sudo ufw allow samba || true
  sudo ufw allow 3702/udp || true
  sudo ufw allow 3702/tcp || true
else
  echo "==> No active ufw firewall; nothing to open"
fi

# --- Done ---------------------------------------------------------------------
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

==> LAN share ready.

    Share path : ${SHARE_PATH}
    Runs as    : ${SHARE_USER}:${SHARE_GROUP}
    Reachable  : \\\\$(hostname)\\Library${IP_ADDR:+  or  \\\\${IP_ADDR}\\Library}

    Verify from a Windows PC on the same network:
      1. Open File Explorer and click Network in the sidebar.
      2. Wait up to ~2 minutes for "$(hostname)" to appear (that is wsdd
         advertising the box; it is not instant).
      3. Open it, then open Library, and drag a video folder in.
      4. Back on this box, confirm it landed and is picked up:
           ls ${SHARE_PATH}
           retrobox --check

    If Network stays empty but \\\\${IP_ADDR:-<box-ip>}\\Library works when typed
    into the address bar, that is wsdd - check: systemctl status wsdd
EOF
