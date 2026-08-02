#!/usr/bin/env bash
#
# Retro Box: make this box boot when there is nothing plugged into it.
# JV Projects.
#
# Called from installer/provision.sh at INSTALL time, inside a chroot of the
# target. Safe to run again by hand on a live box: every step is idempotent.
#
#   installer/harden-boot.sh                 # act on this system
#   installer/harden-boot.sh --root /target  # act on a root mounted elsewhere
#
# ---------------------------------------------------------------------------
# THE FAILURE THIS EXISTS TO STOP
#
# A unit was carried to a house with no ethernet. It sat on the customer's
# television showing
#
#     [ *** ] A start job is running for Wait for Network to be Configured
#
# counting up to 2min, and no picture appeared until it gave up.
#
# That job is systemd-networkd-wait-online.service. It is enabled by default on
# Ubuntu Server, it is WantedBy=network-online.target, and with no carrier it
# blocks until its start timeout expires. Two things then follow, and only the
# first of them is certain:
#
#   CERTAIN. systemd paints that line on /dev/console, which on this product is
#   the television. Whatever else is true, the customer watches a red job count
#   to two minutes on the screen they bought the box for, on every cold start,
#   for ever, because nothing about their house is going to change.
#
#   LIKELY, AND NOT CONFIRMED OFF-BOX. Some of what the box needs is ordered
#   behind it. scripts/setup_lan_share.sh installs and enables smbd and wsdd,
#   and the Ubuntu packaging of both carries Wants=network-online.target and
#   After=network-online.target; cloud-init's stages are ordered against it too.
#   Whether that also delays retrobox.service itself depends on the rest of the
#   ordering graph, which cannot be read from a laptop. On a real box, check it
#   with:
#       systemd-analyze critical-chain retrobox.service
#       systemctl show -p Wants -p After smbd nmbd wsdd cloud-config.service
#
# Masking settles both, because a masked unit cannot enter the transaction at
# all: there is no message and there is nothing to be ordered behind.
#
# The two units this product writes are already clean - network.target, no
# Wants= - and tests/test_service_units.py keeps them that way. The units that
# pull the target in are third-party, so the fix cannot live in our unit files.
# It has to make the waiting itself impossible.
#
# WHY MASK AND NOT JUST DISABLE
#   * disable only removes the enablement symlink under /etc. A vendor preset
#     symlink under /usr/lib/systemd/system/network-online.target.wants/ is not
#     removable that way, and an apt upgrade can put an /etc one back.
#   * mask replaces the unit with a symlink to /dev/null, so the unit cannot be
#     loaded at all, whoever asks for it and however it was re-enabled.
#   Both are done here. The mask is the one that has to survive.
#
# WHY THE SYMLINKS ARE MADE BY HAND AND systemctl IS NEVER CALLED
#   This runs in a chroot with no systemd of its own. `systemctl mask` there is
#   either a no-op or - worse - talks to the LIVE INSTALLER's systemd and masks
#   the unit on the installer instead of on the box, which is the same trap
#   hostnamectl falls into (see installer/autoinstall.yaml.template). A mask IS
#   a symlink to /dev/null; writing it directly is exact, offline, and testable.
# ---------------------------------------------------------------------------
#
set -euo pipefail

ROOT="/"

usage() {
  sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="${2:?--root needs a path}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "harden-boot: unknown argument: $1" >&2; usage 2 ;;
  esac
done

# "/" becomes "" so every path below is a plain concatenation.
ROOT="${ROOT%/}"
SYSTEMD_DIR="${ROOT}/etc/systemd/system"
NETPLAN_DIR="${ROOT}/etc/netplan"
CLOUD_DIR="${ROOT}/etc/cloud"

say()  { echo "==> $*"; }
warn() { echo "!!  $*"; }
die()  { echo "!!  FATAL: $*" >&2; exit 1; }

[ -d "${ROOT}/etc" ] || die "${ROOT}/etc does not exist - is --root right?"
# Everything below writes systemd unit paths. Getting --root wrong - or running
# this on the build machine instead of the target - would scatter masks across
# somebody's laptop, so the root is required to look like an installed Linux
# before anything is touched. /etc/os-release is a symlink into /usr on Ubuntu,
# and -f follows it.
[ -f "${ROOT}/etc/os-release" ] || die \
  "${ROOT:-/} does not look like an installed Linux system (no /etc/os-release).
  Point --root at the target's root filesystem."

say "Making this box boot with no network (root=${ROOT:-/})"

# --- 1. Mask everything that can wait for a network --------------------------
# systemd-networkd-wait-online@.service is the per-interface template used by
# newer systemd; masking the template masks every instance of it.
# NetworkManager-wait-online is not installed on Ubuntu Server, and masking a
# unit that does not exist is both harmless and exactly the insurance wanted:
# if anything ever pulls NetworkManager in, it still cannot hold the boot up.
WAIT_UNITS="
systemd-networkd-wait-online.service
systemd-networkd-wait-online@.service
NetworkManager-wait-online.service
"

mkdir -p "${SYSTEMD_DIR}"
for unit in ${WAIT_UNITS}; do
  ln -sfn /dev/null "${SYSTEMD_DIR}/${unit}"
  say "  masked ${unit}"
done

# --- 2. Disable them as well -------------------------------------------------
# Belt to the mask's braces: drop the enablement symlinks so the unit is not
# even referenced. Only symlinks inside a *.wants/ directory are touched, which
# is what keeps this from eating the masks written just above.
for dir in "${SYSTEMD_DIR}" "${ROOT}/usr/lib/systemd/system"; do
  [ -d "${dir}" ] || continue
  found="$(find "${dir}" -type l -path '*.wants/*' \
    \( -name 'systemd-networkd-wait-online*.service' \
       -o -name 'NetworkManager-wait-online.service' \) 2> /dev/null || true)"
  [ -n "${found}" ] || continue
  echo "${found}" | while IFS= read -r link; do
    [ -n "${link}" ] || continue
    case "${link}" in
      "${ROOT}/usr/lib/"*)
        # Vendor territory. Not ours to delete, and the mask covers it anyway;
        # say so rather than pretending it is not there.
        warn "  vendor preset still present: ${link} (neutralised by the mask)"
        ;;
      *)
        rm -f "${link}"
        say "  disabled ${link}"
        ;;
    esac
  done
done

# --- 3. Check every interface is optional ------------------------------------
# netplan's default is `optional: false`, which it renders as
# RequiredForOnline=yes. One interface like that is all it takes to give the
# waiting something to wait for. The installer's own netplan (written from the
# `network:` section of the autoinstall document) marks every interface
# optional, and tests/test_installer_boot.py fails the build if that stops
# being true - but the box is checked here too, because subiquity, cloud-init
# and the dashboard's Network page can all put files in this directory.
#
# This is a warning, not a failure: with the units above masked there is no
# longer anything that can act on RequiredForOnline=yes. It is reported so a
# regression is visible in /var/log/retrobox-install.log rather than silent.
HAVE_NETPLAN=0
NETPLAN_RC=0
NETPLAN_OUT=""

if command -v python3 > /dev/null 2>&1; then
  # The closing paren of the command substitution goes AFTER the heredoc
  # terminator. Bash accepts nothing else here.
  NETPLAN_OUT="$(python3 - "${NETPLAN_DIR}" 2>&1 <<'PY'
import glob
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit(78)

directory = sys.argv[1]
paths = sorted(glob.glob(os.path.join(directory, "*.yaml"))
               + glob.glob(os.path.join(directory, "*.yml")))

KINDS = ("ethernets", "wifis", "bonds", "bridges", "vlans", "modems", "tunnels")

seen = 0
required = []
for path in paths:
    try:
        with open(path, "r") as handle:
            doc = yaml.safe_load(handle) or {}
    except Exception as exc:  # a file we cannot read is a file we cannot vouch for
        print("could not read %s: %s" % (path, exc))
        continue
    network = doc.get("network") if isinstance(doc, dict) else None
    if not isinstance(network, dict):
        continue
    for kind in KINDS:
        block = network.get(kind)
        if not isinstance(block, dict):
            continue
        for name, cfg in block.items():
            seen += 1
            if not isinstance(cfg, dict) or cfg.get("optional") is not True:
                required.append("%s: %s.%s" % (path, kind, name))

for item in required:
    print("required-for-online: %s" % item)
print("interfaces: %d" % seen)

if seen == 0:
    sys.exit(2)
sys.exit(1 if required else 0)
PY
)" || NETPLAN_RC=$?
fi

case "${NETPLAN_RC}" in
  0)
    say "  every interface in ${NETPLAN_DIR}/ is optional: true"
    HAVE_NETPLAN=1
    ;;
  1)
    warn "  a netplan interface is NOT optional: true - it would be"
    warn "  RequiredForOnline=yes. Harmless while the wait-online units above"
    warn "  are masked, but it should not be there:"
    echo "${NETPLAN_OUT}" | sed 's/^/!!    /'
    HAVE_NETPLAN=1
    ;;
  2)
    warn "  no interfaces are declared in ${NETPLAN_DIR}/"
    ;;
  78)
    warn "  python3 has no yaml module here; falling back to a text check"
    if grep -qsE '^[[:space:]]*(ethernets|wifis|bonds|bridges|vlans):' \
        "${NETPLAN_DIR}"/*.yaml; then
      HAVE_NETPLAN=1
    fi
    ;;
  *)
    warn "  could not check ${NETPLAN_DIR}/ (exit ${NETPLAN_RC})"
    echo "${NETPLAN_OUT}" | sed 's/^/!!    /'
    if grep -qsE '^[[:space:]]*(ethernets|wifis|bonds|bridges|vlans):' \
        "${NETPLAN_DIR}"/*.yaml; then
      HAVE_NETPLAN=1
    fi
    ;;
esac

# --- 4. Switch cloud-init off ------------------------------------------------
# cloud-init did its job during the install and has nothing left to do, but it
# stays enabled and runs four ordered stages on EVERY boot for ever:
# cloud-init-local, cloud-init (the network stage, which is ordered against
# systemd-networkd-wait-online and Before=network-online.target), cloud-config
# and cloud-final - and cloud-final pulls in snapd.seeded. That is a few
# seconds of python on every cold start of a box whose whole promise is that
# you switch it on and a television appears. Measure it on a box with
# `systemd-analyze blame` or `cloud-init analyze boot`; 3-10s is typical on the
# small machines this product runs on, on top of the wait-online case above.
#
# It is also a live NoCloud datasource. A customer who leaves a random FAT
# stick plugged in can otherwise influence how their box boots.
#
# The flag file is the supported off switch: cloud-init's systemd generator
# looks for it and never brings cloud-init.target into the transaction at all,
# so the units are not merely fast, they are absent.
#
# GATED, deliberately. If nothing in /etc/netplan declares an interface then
# something else is writing the network configuration at first boot - and the
# only realistic candidate is cloud-init. Disabling it in that state would be a
# box that never has a network again. Losing a few seconds of boot is worth
# strictly less than that, so in that case this is skipped and says why.
if [ "${HAVE_NETPLAN}" -eq 1 ]; then
  mkdir -p "${CLOUD_DIR}"
  cat > "${CLOUD_DIR}/cloud-init.disabled" <<'FLAG'
# Written by installer/harden-boot.sh (Retro Box, JV Projects).
#
# cloud-init finished its work during the unattended install. Left enabled it
# runs four stages on every boot, is ordered around network-online.target, and
# keeps the NoCloud datasource live so a stray USB stick can influence how this
# box comes up. The network configuration this box uses is a static netplan
# file in /etc/netplan and does not depend on cloud-init.
#
# cloud-init's systemd generator looks only for the existence of this file, not
# its contents. Delete it to turn cloud-init back on.
FLAG
  say "  cloud-init switched off (${CLOUD_DIR}/cloud-init.disabled)"
else
  warn "  NOT switching cloud-init off: nothing in ${NETPLAN_DIR}/ declares an"
  warn "  interface, so cloud-init may be what configures the network at first"
  warn "  boot. Disabling it here could leave this box with no network at all."
fi

say "Boot hardening done: nothing on this box waits for a network."
