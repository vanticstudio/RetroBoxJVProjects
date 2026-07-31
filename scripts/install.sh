#!/usr/bin/env bash
#
# Retro Box installer for Debian/Ubuntu.
#
# One script covers both supported shapes of box:
#   * a generic x86 mini PC (Intel NUC, ThinkCentre Tiny, small form-factor
#     desktop), where hardware video decode comes from VA-API
#   * a Raspberry Pi, which decodes through its own V4L2 stack instead
#
# It detects which it is on and installs accordingly, then optionally installs
# a systemd service so the box boots straight into "TV mode".
#
# Usage:
#   ./scripts/install.sh              # install deps + assets
#   ./scripts/install.sh --service    # ...and install & enable the systemd unit
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SERVICE=0
SETUP_SHARE=1
for arg in "$@"; do
  case "$arg" in
    --service) INSTALL_SERVICE=1 ;;
    --no-share) SETUP_SHARE=0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

have_package() {
  apt-cache show "$1" > /dev/null 2>&1
}

# --- Which machine is this? --------------------------------------------------
# The device tree model is the reliable Pi tell; everything else is treated as
# a generic PC, which is also the right fallback for an unknown SBC.
ARCH="$(uname -m)"
PLATFORM="pc"
if [[ -r /proc/device-tree/model ]] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  PLATFORM="pi"
fi
echo "==> Detected platform: ${PLATFORM} (${ARCH})"

echo "==> Updating package lists"
sudo apt-get update

# Needed on every platform. alsa-utils provides aplay, which is how the
# hardware detector enumerates HDMI audio outputs.
PACKAGES=(mpv ffmpeg python3 python3-pip python3-venv python3-evdev alsa-utils)

# libmpv's package name changed: Debian 12+ / Ubuntu 24.04 ship libmpv2, older
# releases ship libmpv1. Take whichever this distro actually has.
for candidate in libmpv2 libmpv1; do
  if have_package "$candidate"; then
    PACKAGES+=("$candidate")
    break
  fi
done

if [[ "${PLATFORM}" == "pc" ]]; then
  # pciutils gives us lspci, which is how hwdetect identifies the GPU vendor;
  # vainfo lets it confirm hardware decode actually came up afterwards. The
  # GPU-specific decode drivers are deliberately NOT listed here - hwdetect
  # picks the right ones for the GPU it finds, once the package is installed.
  PACKAGES+=(pciutils)
  for candidate in vainfo; do
    if have_package "$candidate"; then
      PACKAGES+=("$candidate")
    fi
  done
fi

# HDMI-CEC, so the TV's own remote can drive the box. Standard on a Pi; on a PC
# it generally needs a USB-CEC adapter, but the package is harmless either way
# and Retro Box skips the CEC backend silently when nothing answers.
if have_package cec-utils; then
  PACKAGES+=(cec-utils)
fi

echo "==> Installing system packages: ${PACKAGES[*]}"
sudo apt-get install -y "${PACKAGES[@]}"

echo "==> Creating a virtual environment in ${REPO_DIR}/.venv"
# --system-site-packages so the apt-installed python3-evdev is visible.
python3 -m venv --system-site-packages "${REPO_DIR}/.venv"
# shellcheck source=/dev/null
source "${REPO_DIR}/.venv/bin/activate"

echo "==> Installing Retro Box and Python dependencies"
pip install --upgrade pip
# Editable install so that updating the code needs no reinstall (just restart
# the service afterwards).
pip install -e "${REPO_DIR}[hardware]"

if [[ "${PLATFORM}" == "pc" ]]; then
  echo "==> Detecting graphics and HDMI audio hardware"
  # Deliberately placed AFTER the pip install above: this imports the freshly
  # installed retrobox package, so running it any earlier fails on import.
  # The venv is active at this point, so `python3` is the venv's interpreter.
  # --install lets it apt-install the VA-API driver matching the detected GPU.
  python3 -m retrobox.hwdetect --install || \
    echo "   (hardware detection failed - the box still works on software decode)"
else
  echo "==> Skipping VA-API setup (the Pi decodes through V4L2, not VA-API)"
fi

if [[ "${SETUP_SHARE}" -eq 1 ]]; then
  echo "==> Setting up the LAN file share"
  # Every unit wants this, so it is part of the standard flow rather than a
  # manual step afterwards. Pass --no-share to skip it.
  "${REPO_DIR}/scripts/setup_lan_share.sh" || \
    echo "   (LAN share setup failed - the TV still works; re-run scripts/setup_lan_share.sh)"
else
  echo "==> Skipping the LAN file share (--no-share)"
fi

echo "==> Generating filler assets (static, glitch + colour bars)"
python -m retrobox.static_gen || echo "   (asset generation skipped/failed - box still works)"

echo "==> Installing the retro OSD font (VT323)"
# Retro Box also copies this into mpv's font dir at runtime, but installing it
# system-wide makes it available everywhere (and to fontconfig).
mkdir -p "${HOME}/.local/share/fonts" "${HOME}/.config/mpv/fonts"
if compgen -G "${REPO_DIR}/retrobox/assets/fonts/*.ttf" > /dev/null; then
  cp "${REPO_DIR}"/retrobox/assets/fonts/*.ttf "${HOME}/.local/share/fonts/" || true
  cp "${REPO_DIR}"/retrobox/assets/fonts/*.ttf "${HOME}/.config/mpv/fonts/" || true
  if command -v fc-cache > /dev/null; then
    fc-cache -f "${HOME}/.local/share/fonts" || true
  fi
fi

if [[ ! -f "${REPO_DIR}/config.yaml" ]]; then
  echo "==> Creating a starter config.yaml (or run: retrobox --setup)"
  cp "${REPO_DIR}/config.example.yaml" "${REPO_DIR}/config.yaml"
fi

echo "==> Validating configuration"
retrobox --check --config "${REPO_DIR}/config.yaml" || \
  echo "   (run 'retrobox --setup' to build one interactively)"

if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
  echo "==> Installing systemd service"
  "${REPO_DIR}/scripts/install-service.sh"
fi

# --- Platform-appropriate closing notes --------------------------------------
if [[ "${PLATFORM}" == "pi" ]]; then
  READONLY_HELP="  sudo raspi-config  ->  Performance Options  ->  Overlay File System"
else
  READONLY_HELP="  sudo apt install overlayroot, then set 'overlayroot=tmpfs' in /etc/overlayroot.conf"
fi

cat <<EOF

==> Done!

Next steps:
  1. Build your channel lineup:   retrobox --setup
     (or edit ${REPO_DIR}/config.yaml by hand)
  2. Copy your video files onto the box (e.g. /media/retrobox/<channel>/).
  3. Test it:   retrobox --check
                retrobox                 # starts the TV
  4. Auto-start on boot:   ./scripts/install.sh --service

Optional - make the root filesystem read-only so a power cut can never
corrupt it (${PLATFORM}):
${READONLY_HELP}

Enjoy your nostalgia box!
EOF
