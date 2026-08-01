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
pip install -e "${REPO_DIR}[hardware,web]"

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

# --- Reachable by name -------------------------------------------------------
# So the customer types http://retrobox.local and not an IP address they had to
# find first. mDNS is the only name resolution that works out of the box across
# macOS, iOS, Android and Windows 10+ without the router having to cooperate.
#
# Deliberately BEFORE the file share below: that script reads $(hostname) to
# name the share and to print how to reach it, so the rename has to have
# happened first or the two disagree about what the box is called.
#
# Bare "retrobox" with no suffix only works when the router registers DHCP
# hostnames into its own DNS. Plenty do; plenty do not. It is a bonus, never
# the documented address.
echo "==> Setting up mDNS so the box answers to retrobox.local"
if have_package avahi-daemon; then
  sudo apt-get install -y avahi-daemon avahi-utils || \
    echo "   (avahi install failed - the box is still reachable by IP)"
else
  echo "   (no avahi-daemon package on this distro - the box stays reachable by IP)"
fi

# The mDNS name follows the system hostname, so retrobox.local needs the
# hostname to actually be "retrobox". The installer sets it rather than telling
# people to, because getting it wrong means the documented URL does not work.
CURRENT_HOSTNAME="$(hostname)"
if [[ "${CURRENT_HOSTNAME}" != "retrobox" ]]; then
  echo "==> Hostname is '${CURRENT_HOSTNAME}'; setting it to 'retrobox'"
  sudo hostnamectl set-hostname retrobox 2>/dev/null || \
    echo "   (could not set the hostname - the box will answer to ${CURRENT_HOSTNAME}.local)"
  # /etc/hosts must follow, or sudo pauses on every command trying to resolve
  # a hostname that no longer has a loopback entry.
  if ! grep -qE "^127\.0\.1\.1[[:space:]]+retrobox" /etc/hosts 2>/dev/null; then
    if grep -qE "^127\.0\.1\.1" /etc/hosts 2>/dev/null; then
      sudo sed -i -E "s|^127\.0\.1\.1.*|127.0.1.1\tretrobox|" /etc/hosts || true
    else
      printf '127.0.1.1\tretrobox\n' | sudo tee -a /etc/hosts > /dev/null || true
    fi
  fi
fi

if systemctl list-unit-files 2>/dev/null | grep -q "^avahi-daemon\.service"; then
  # systemd-resolved also speaks mDNS on some images. Two responders on UDP
  # 5353 answer the same query twice, which shows up as a box that resolves
  # intermittently. Avahi is the one Samba and the rest of the desktop world
  # integrate with, so it wins and resolved's copy is turned off.
  if [[ -f /etc/systemd/resolved.conf ]] && \
     systemctl is-active --quiet systemd-resolved 2>/dev/null; then
    if resolvectl mdns 2>/dev/null | grep -qi "yes"; then
      echo "==> Turning off systemd-resolved's mDNS (avahi is answering instead)"
      sudo sed -i -E 's|^#?MulticastDNS=.*|MulticastDNS=no|' /etc/systemd/resolved.conf || true
      grep -q "^MulticastDNS=" /etc/systemd/resolved.conf 2>/dev/null || \
        echo "MulticastDNS=no" | sudo tee -a /etc/systemd/resolved.conf > /dev/null
      sudo systemctl restart systemd-resolved || true
    fi
  fi
  # Avahi answers on every interface that is up and follows them appearing and
  # disappearing, so unplugging the ethernet and joining wifi keeps
  # retrobox.local working. That is the DEFAULT, not something we configure -
  # but it stops being true the moment somebody pins the interface list, and
  # then the documented address silently dies on a box that changed network.
  # So check rather than assume, and say so rather than quietly overriding a
  # choice somebody may have made deliberately.
  AVAHI_CONF=/etc/avahi/avahi-daemon.conf
  if [[ -f "${AVAHI_CONF}" ]] && \
     grep -qE '^[[:space:]]*(allow|deny)-interfaces[[:space:]]*=' "${AVAHI_CONF}"; then
    echo "!! ${AVAHI_CONF} pins which interfaces mDNS uses:"
    grep -E '^[[:space:]]*(allow|deny)-interfaces[[:space:]]*=' "${AVAHI_CONF}" | sed 's/^/     /'
    echo "   retrobox.local will stop resolving if the box moves to an interface"
    echo "   that is not listed. Comment those lines out to answer on all of them."
  fi

  # Enabled, not started-and-waited-for: if the box boots with no network at
  # all, nothing here may hold up the television.
  sudo systemctl enable avahi-daemon 2>/dev/null || true
  # Restart rather than start: on a re-run avahi is already up, and `start` on
  # a running unit does nothing - it would keep announcing the old hostname.
  sudo systemctl restart avahi-daemon 2>/dev/null || \
    echo "   (avahi did not start - no network yet? it will come up on boot)"

  # Confirm the name actually resolves rather than declaring victory. Needs a
  # network to succeed, so a failure here is reported, never fatal.
  if command -v avahi-resolve > /dev/null 2>&1; then
    if avahi-resolve -4 -n retrobox.local > /dev/null 2>&1; then
      echo "==> mDNS ready: retrobox.local resolves"
    else
      echo "==> mDNS enabled. retrobox.local did not resolve yet - normal if the"
      echo "    network is still coming up; it will answer once an interface is up."
    fi
  else
    echo "==> mDNS ready: the box should answer to retrobox.local"
  fi
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
