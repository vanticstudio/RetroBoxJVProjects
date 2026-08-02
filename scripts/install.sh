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
# Run it as the account the box will run as, NOT with sudo - it asks for sudo
# itself, for the handful of steps that need root. See refuse_a_root_install().
#
# This script and installer/provision.sh build the same box by different roads:
# provision.sh is the unattended build on a blank disk, this is the documented
# manual path. Where they drift, customers find out and installers do not, so
# several of the steps below exist only to keep the two the same. Each says so.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Where the library lives. Same name and same default as provision.sh uses, and
# unlike before it is now passed on to everything that needs to know - the file
# share included, which used to create and share a second, empty folder of its
# own on any box built with a different one.
MEDIA_ROOT="${RETROBOX_MEDIA_ROOT:-/media/retrobox}"
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

# Say why, on stderr, and stop - with what to do next, always. Whoever reads
# this has a half-installed appliance in front of them and no other source of
# truth: no SSH once it ships, no log but this terminal, and a television that
# may or may not come on.
fail() {
  printf '\n' >&2
  printf '%s\n' "$@" >&2
  exit 1
}

# =============================================================================
# The steps that keep this path and the unattended one the same
# =============================================================================

# `sudo ./scripts/install.sh` is a natural thing to type for a script visibly
# full of sudo, and nothing used to stop it. It does not fail - it produces a
# box that plays television and quietly cannot do anything else: the venv, the
# editable install, config.yaml and the generated filler clips all owned by
# root, while the units render with User= the human. The dashboard then cannot
# save a setting, the self-updater fails both its writability gate and git's
# own "dubious ownership" refusal, and the OSD font lands in /root.
#
#   $1 our uid   $2 SUDO_USER   $3 HOME   $4 the home SUDO_USER really has
#
# installer/provision.sh is also root with SUDO_USER set, deliberately, and
# injects HOME so the font and the caches land in the box account's home. That
# is what tells the two apart: the mistake carries root's HOME, the build does
# not.
refuse_a_root_install() {
  local uid="$1" sudo_user="$2" home="$3" expected_home="$4"
  [[ "${uid}" == "0" ]] || return 0
  [[ -n "${sudo_user}" && "${sudo_user}" != "root" ]] || return 0
  [[ -n "${expected_home}" ]] || return 0
  [[ "${home}" != "${expected_home}" ]] || return 0
  fail \
    "error: this is running as root on behalf of '${sudo_user}', with root's" \
    "       home directory. That builds a box '${sudo_user}' does not own." \
    "" \
    "  Everything below - the virtual environment, config.yaml, the generated" \
    "  filler clips, the OSD font - would belong to root, while the two service" \
    "  units run as '${sudo_user}'. The television would play, and then the" \
    "  dashboard could not save a single setting, the self-updater would refuse" \
    "  to run, and none of it would be reported anywhere." \
    "" \
    "  Run it as '${sudo_user}', without sudo:" \
    "    ./scripts/install.sh" \
    "" \
    "  It asks for sudo itself where it needs root. Nothing has been changed."
}

# The library folder, and something for a brand-new box to play out of it.
#
#   $1 the media root   $2 the account   $3 its group   $4 the splash clip
#
# provision.sh has always done this. Here, the only thing that ever created the
# media root was scripts/setup_lan_share.sh - so `--no-share` produced a box
# with no library at all, and so did a share that failed for any other reason,
# because install.sh swallowed that failure and printed "Done!" underneath it.
#
# .welcome is deliberately a DOT folder: auto_channels skips hidden folders, so
# the placeholder channel can point at it without being rediscovered and
# duplicated on every start-up.
ensure_media_library() {
  local media="$1" user="$2" group="$3" splash="$4"
  sudo mkdir -p "${media}/.welcome"
  if [[ -f "${splash}" && ! -f "${media}/.welcome/${splash##*/}" ]]; then
    # So a box with nothing on it yet shows the JV Projects splash instead of
    # colour bars.
    sudo cp "${splash}" "${media}/.welcome/"
  fi
  # Recursive, and with the group named: the share's own chown was neither, so
  # the folder stayed group-root and files written into it by one route were
  # not readable by the other.
  sudo chown -R "${user}:${group}" "${media}"
  echo "    ${media} is ready (owned by ${user}:${group})"
}

# The config a box starts life with.
#
# This used to be `cp config.example.yaml config.yaml`, and that file is a
# reference for every option the box has - not a starting lineup. It leaves
# media_root unset and auto_channels off, with five explicit channels pointing
# at folders that do not exist. The consequences are all customer-facing: the
# README's "anything dropped in lands in ${MEDIA_ROOT}" is simply untrue, the
# dashboard's upload page returns HTTP 400 "set media_root in config.yaml", and
# the television shows five channels of nothing.
#
# The shape below is provision.sh's, and the explicit channel in it is not
# redundant. With media_root set and NO channels key, a brand-new box with an
# empty library raises "no channels found" and exits 2; retrobox.service then
# fails five times in fifteen seconds, hits StartLimitBurst and goes PERMANENTLY
# dead, and dropping media in later does not bring it back. An explicit channel
# never touches the filesystem when it parses, so the box always comes up.
write_starter_config() {
  local path="$1" media="$2"
  if [[ -f "${path}" ]]; then
    echo "    ${path} already exists - leaving it alone"
    return 0
  fi
  cat > "${path}" <<YAML
# Written by scripts/install.sh. Safe to edit, and the dashboard rewrites parts
# of it. Every option the box has is documented in config.example.yaml.
#
# Drop a folder of videos into ${media} (or into \\\\retrobox\\Library from a PC)
# and it becomes a channel on the next start-up.
media_root: ${media}
auto_channels: true

# Channel 2 is a placeholder so the box always has a valid lineup, even with an
# empty library. Discovered channels are numbered from 3 upwards. Delete this
# entry once the box has real content.
channels:
  - number: 2
    name: "Retro Box"
    path: ${media}/.welcome
YAML
  echo "    wrote ${path}"
}

# `retrobox --check` is three-valued and it was being read as two-valued.
#
#   2 = the config is broken and the television will not start
#   1 = the config is fine, there is no media yet   <- a new box, every time
#   0 = fine, with media
#
# Every non-zero exit used to be swallowed into a friendly hint, and the script
# went on to install AND START the service and print "Done!". On exit 2 that is
# a unit that burns its five restarts in about fifteen seconds and stays dead
# for ever - the exact black-screen failure with no SSH behind it that the
# unattended installer refuses to ship.
validate_config() {
  local checker="$1" config="$2" rc=0
  "${checker}" --check --config "${config}" || rc=$?
  case "${rc}" in
    0) echo "    config OK, and the library already has content" ;;
    1) echo "    config OK - no media yet, which is expected on a new box" ;;
    *)
      fail \
        "error: ${config} is not a config the television can start with" \
        "       (retrobox --check exited ${rc})." \
        "" \
        "  The reason is printed above this message. Nothing has been started" \
        "  and no service has been installed - deliberately: a unit given a" \
        "  config it refuses gives up after five failed starts in a minute and" \
        "  stays dead until somebody clears it by hand, which on a box with no" \
        "  keyboard and no SSH means it never comes back." \
        "" \
        "  Fix it and run this script again:" \
        "    retrobox --setup                 # build a lineup interactively" \
        "    \$EDITOR ${config}                # or edit it by hand"
      ;;
  esac
}

# Nothing on this box may wait for a network it has not got.
#
# This is the most visible failure the product has and the one nobody who
# installs it ever sees. installer/harden-boot.sh has always been run by the
# unattended build and never by this one - while THIS script is what installs
# and enables smbd and wsdd, whose Ubuntu packaging is exactly what drags
# systemd-networkd-wait-online into every boot transaction. The customer
# unplugs the box, carries it to a friend's house, switches it on with no cable
# in it, and watches a red "A start job is running for Wait for Network to be
# Configured" count to 2min on the television they bought the box for.
#
#   $1 the harden-boot.sh script   $2 the root to act on (default /)
harden_boot() {
  local script="$1" root="${2:-/}"
  if [[ ! -f "${script}" ]]; then
    fail \
      "error: ${script} is missing, so nothing has stopped this box waiting" \
      "       for a network at boot." \
      "" \
      "  Every cold start would show 'A start job is running for Wait for" \
      "  Network to be Configured' counting to two minutes on the television" \
      "  before any picture appears." \
      "" \
      "  It is part of this repository: check the clone is complete" \
      "  (git status) and run this script again."
  fi
  if [[ "${root}" == "/" ]]; then
    sudo bash "${script}" || fail \
      "error: ${script} failed, so this box may still wait for a network at" \
      "       boot. What it said is above." \
      "" \
      "  Fix that and run this script again, or run it on its own:" \
      "    sudo ./installer/harden-boot.sh"
  else
    sudo bash "${script}" --root "${root}" || fail \
      "error: ${script} failed against ${root}. What it said is above."
  fi
  verify_no_wait_for_network "${root}"
}

# Asked rather than assumed, exactly as provision.sh asks it. A mask IS a
# symlink to /dev/null, so this is the real question: could this unit still be
# loaded and pulled into a boot?
verify_no_wait_for_network() {
  local root="${1:-/}" unit link
  for unit in systemd-networkd-wait-online.service \
              NetworkManager-wait-online.service; do
    link="${root%/}/etc/systemd/system/${unit}"
    if [[ -L "${link}" && "$(readlink "${link}")" == "/dev/null" ]]; then
      continue
    fi
    fail \
      "error: ${unit} is not masked." \
      "" \
      "  With nothing plugged in, this box counts a red 'Wait for Network to be" \
      "  Configured' job up to two minutes on the customer's television, on" \
      "  every boot, for ever - and the picture only appears after it gives up." \
      "" \
      "  Try it directly and read what it says:" \
      "    sudo ./installer/harden-boot.sh" \
      "  then run this script again."
  done
  echo "    checked: nothing on this box waits for a network at boot"
}

# Where the dashboard is, in the two places somebody will actually look.
#
# A manual-path box greeted anybody who plugged a keyboard into it with a bare
# "retrobox login:" and no hint that a dashboard exists, let alone its address.
# agetty expands \4 to the current IPv4 address at DISPLAY time, so the console
# banner stays right across DHCP leases without anything rewriting it.
#
#   $1 the /etc to write into (a real path here, a throwaway one in the tests)
write_console_banners() {
  local etc="${1:-/etc}"
  sudo mkdir -p "${etc}/update-motd.d"
  printf '%s\n' \
    'Retro Box - JV Projects' \
    '' \
    '  Dashboard : http://retrobox.local/   or   http://\4/' \
    '  Library   : \\retrobox\Library   (drag video folders in from any PC)' \
    '  TV        : systemctl status retrobox' \
    '' | sudo tee "${etc}/issue" > /dev/null
  sudo tee "${etc}/update-motd.d/99-retrobox" > /dev/null <<'MOTD'
#!/bin/sh
# Installed by scripts/install.sh.
ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '\n Retro Box - JV Projects\n\n'
printf '   Dashboard : http://retrobox.local/'
[ -n "$ip" ] && printf '   or   http://%s/' "$ip"
printf '\n   Library   : \\\\retrobox\\Library\n'
printf '   TV        : systemctl status retrobox\n\n'
MOTD
  sudo chmod 0755 "${etc}/update-motd.d/99-retrobox"
  echo "    the console and the SSH banner now say where the dashboard is"
}

# The last thing anybody reads, and it used to be wrong in the one case that
# matters: somebody who had just run `./scripts/install.sh --service`, exactly
# as the README tells them to, finished with "Auto-start on boot:
# ./scripts/install.sh --service" - an instruction to redo what they had just
# done - and was pointed at a way of adding channels that does not work.
#
#   $1 platform   $2 1 if the service was installed   $3 media root   $4 repo
closing_notes() {
  local platform="$1" service="$2" media="$3" repo="$4"
  local readonly_help
  if [[ "${platform}" == "pi" ]]; then
    readonly_help="  sudo raspi-config  ->  Performance Options  ->  Overlay File System"
  else
    readonly_help="  sudo apt install overlayroot, then set 'overlayroot=tmpfs' in /etc/overlayroot.conf"
  fi

  if [[ "${service}" == "1" ]]; then
    cat <<EOF

==> Done! The box is installed and running.

  Television : on the HDMI output, now and on every boot
  Dashboard  : http://retrobox.local/
  Library    : \\\\retrobox\\Library from any PC, or ${media} on the box
               drop a folder of videos in - it becomes a channel

  systemctl status retrobox     # is it running?
  journalctl -u retrobox -f     # live logs
EOF
  else
    cat <<EOF

==> Done! Nothing starts on its own yet.

Next steps:
  1. Drop a folder of videos into ${media} - it becomes a channel.
     (Fine-tune the lineup with 'retrobox --setup', or edit
     ${repo}/config.yaml by hand.)
  2. Test it:   retrobox --check
                retrobox                 # starts the TV
  3. Boot straight into TV mode, with the dashboard on
     http://retrobox.local/ :
       ./scripts/install.sh --service
EOF
  fi

  cat <<EOF

Optional - make the root filesystem read-only so a power cut can never
corrupt it (${platform}):
${readonly_help}

Enjoy your nostalgia box!
EOF
}

# tests/test_install_parity.py sources this file for the functions above and
# runs them against throwaway directories with a stand-in sudo, because none of
# this can be installed on a laptop. Nothing else sets this.
if [[ "${RETROBOX_INSTALL_LIB_ONLY:-}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

# =============================================================================
# Installing
# =============================================================================
RUN_USER="${SUDO_USER:-$USER}"
# Looked up, not assumed to match the user's name: Debian usually creates a
# matching per-user group and does not always, and everything this script
# chowns is chowned to it.
if ! RUN_GROUP="$(id -gn "${RUN_USER}" 2> /dev/null)" || [[ -z "${RUN_GROUP}" ]]; then
  fail \
    "error: '${RUN_USER}' is not an account on this box, so there is nobody to" \
    "       install it for." \
    "" \
    "  Run this as the account the box will run as (check with: id)." \
    "  Nothing has been changed."
fi
RUN_HOME=""
if [[ -n "${SUDO_USER:-}" ]]; then
  RUN_HOME="$(getent passwd "${SUDO_USER}" | cut -d: -f6 || true)"
fi
refuse_a_root_install "$(id -u)" "${SUDO_USER:-}" "${HOME:-}" "${RUN_HOME}"

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

# The library itself, BEFORE the share and regardless of it. The share is a
# convenience; the folder the channels are scanned from is not.
echo "==> Creating the media library at ${MEDIA_ROOT}"
ensure_media_library "${MEDIA_ROOT}" "${RUN_USER}" "${RUN_GROUP}" \
  "${REPO_DIR}/retrobox/assets/boot_splash.mp4"

if [[ "${SETUP_SHARE}" -eq 1 ]]; then
  echo "==> Setting up the LAN file share"
  # Every unit wants this, so it is part of the standard flow rather than a
  # manual step afterwards. Pass --no-share to skip it.
  #
  # --path, so the folder shared as \\retrobox\Library is the folder the
  # channels are scanned from. Without it the share fell back to its own
  # hard-coded default, and a box built with RETROBOX_MEDIA_ROOT elsewhere
  # shared an empty directory: shows copied in over the network, successfully,
  # to a place nothing ever reads.
  "${REPO_DIR}/scripts/setup_lan_share.sh" --path "${MEDIA_ROOT}" || \
    echo "   (LAN share setup failed - the TV still works; re-run scripts/setup_lan_share.sh)"
else
  echo "==> Skipping the LAN file share (--no-share)"
fi

# Deliberately after the share: the units that pull network-online.target into
# the boot are smbd and wsdd, and this cleans up after whatever just enabled
# them.
echo "==> Making sure nothing on this box waits for a network at boot"
harden_boot "${REPO_DIR}/installer/harden-boot.sh"

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

echo "==> Making sure there is a config.yaml to start from"
write_starter_config "${REPO_DIR}/config.yaml" "${MEDIA_ROOT}"

echo "==> Validating configuration"
validate_config "${REPO_DIR}/.venv/bin/retrobox" "${REPO_DIR}/config.yaml"

# The size of the disk is the one number a customer checks, and Ubuntu's guided
# LVM default leaves roughly half of it unallocated where nothing reports it:
# measured on hardware, a 128 GB box came up with 57 GB usable and 58 GB idle.
#
# AUDITED ONLY, deliberately. The unattended installer grows the filesystem
# because it partitioned that disk itself moments earlier. This script is run on
# a machine somebody else set up, and quietly resizing their volumes is not
# ours to do - so it reports, in the words that say what to run.
echo "==> Checking the root filesystem owns the whole disk"
sudo "${REPO_DIR}/installer/storage-grow.sh" --audit-only || \
  echo "   (the disk check did not pass - read the block above; to claim the" \
       "rest of the disk: sudo ./installer/storage-grow.sh)"

echo "==> Saying where the dashboard is, on the console and over SSH"
write_console_banners /etc

if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
  echo "==> Installing systemd service"
  "${REPO_DIR}/scripts/install-service.sh"
fi

closing_notes "${PLATFORM}" "${INSTALL_SERVICE}" "${MEDIA_ROOT}" "${REPO_DIR}"
