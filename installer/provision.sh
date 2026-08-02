#!/usr/bin/env bash
#
# Retro Box unattended provisioning. JV Projects.
#
# Runs INSIDE the installed system, at INSTALL time, from the autoinstall
# late-commands via `curtin in-target`. It is not a first-boot script: the
# point of this product is that you walk away and come back to a FINISHED box,
# so the apt/pip work happens here on the bench and not on the customer's
# first boot.
#
# Context this runs in:
#   * as root, in a chroot of the target, with /dev /proc /run /sys bind-mounted
#   * no running systemd of its own (systemctl's online-only verbs no-op)
#   * network available (curtin copies the installer's resolv.conf in)
#
# Every step is idempotent and every failure is loud: a non-zero exit here
# aborts the whole autoinstall, which is the point. A box that fails visibly
# beats a box that boots to a black screen.
#
set -euo pipefail

RETROBOX_USER="${RETROBOX_USER:-retrobox}"
REPO_DIR="${RETROBOX_REPO_DIR:-/opt/retrobox}"
MEDIA_ROOT="${RETROBOX_MEDIA_ROOT:-/media/retrobox}"
LOG="/var/log/retrobox-install.log"

# Everything to both our own log (findable on the installed box) and stdout
# (captured by curtin, so a failed install still shows why).
exec > >(tee -a "${LOG}") 2>&1

say() { echo "==> $*"; }
warn() { echo "!!  $*"; }
die() { echo "!!  FATAL: $*" >&2; exit 1; }

say "Retro Box provisioning starting: $(date -u '+%Y-%m-%d %H:%M:%SZ')"
say "user=${RETROBOX_USER} repo=${REPO_DIR} media=${MEDIA_ROOT}"

# Samba and friends must never stop to ask a question with no tty attached.
export DEBIAN_FRONTEND=noninteractive

# --- 0. Preflight ------------------------------------------------------------
[[ "$(id -u)" -eq 0 ]] || die "must run as root"
id -u "${RETROBOX_USER}" > /dev/null 2>&1 || \
  die "user '${RETROBOX_USER}' does not exist - the autoinstall identity: section should have created it"
[[ -d "${REPO_DIR}/.git" ]] || die "${REPO_DIR} is not a git clone"
[[ -x "${REPO_DIR}/scripts/install.sh" ]] || die "${REPO_DIR}/scripts/install.sh is missing"
[[ -x "${REPO_DIR}/installer/harden-boot.sh" ]] || \
  die "${REPO_DIR}/installer/harden-boot.sh is missing - this box would wait for a network it may never have"

RUN_GROUP="$(id -gn "${RETROBOX_USER}")"
RUN_HOME="$(getent passwd "${RETROBOX_USER}" | cut -d: -f6)"
[[ -n "${RUN_HOME}" ]] || die "could not resolve the home directory of ${RETROBOX_USER}"
say "group=${RUN_GROUP} home=${RUN_HOME}"

say "Pinned to: $(git -C "${REPO_DIR}" describe --tags --exact-match 2> /dev/null || git -C "${REPO_DIR}" rev-parse --short HEAD)"

# --- 1. The media library ----------------------------------------------------
# scripts/install.sh never creates this, and a config whose media_root does not
# exist is a fatal ConfigError - so it is created here, before anything reads
# the config.
#
# .welcome is deliberately a DOT folder. auto_channels skips hidden folders, so
# the placeholder channel below can point at it without auto_channels ever
# rediscovering it and creating a duplicate.
say "Creating the media library at ${MEDIA_ROOT}"
mkdir -p "${MEDIA_ROOT}/.welcome"
if [[ -f "${REPO_DIR}/retrobox/assets/boot_splash.mp4" ]]; then
  # Gives channel 2 something to actually play on a box with no content yet,
  # so a brand-new unit shows the JV Projects splash instead of colour bars.
  cp -n "${REPO_DIR}/retrobox/assets/boot_splash.mp4" "${MEDIA_ROOT}/.welcome/" || true
fi
chown -R "${RETROBOX_USER}:${RUN_GROUP}" "${MEDIA_ROOT}"

# --- 2. The config -----------------------------------------------------------
# Written BEFORE install.sh runs, because install.sh copies config.example.yaml
# into place only when config.yaml is absent. Get in first and it leaves ours
# alone.
#
# THE SHAPE OF THIS FILE MATTERS. It carries an explicit `channels:` list AS
# WELL AS media_root + auto_channels, and that is not redundancy:
#
#   * With media_root set and NO `channels:` key, retrobox discovers channels
#     from the folders under media_root - and a brand-new box has none, so
#     config parsing raises "no channels found" and exits 2. retrobox.service
#     then fails five times in fifteen seconds, hits StartLimitBurst, and goes
#     PERMANENTLY dead. Dropping media in later does not bring it back; someone
#     has to SSH in and `systemctl reset-failed`. That is the exact black-screen
#     failure this installer exists to avoid.
#
#   * An explicit `channels:` list never touches the filesystem when it parses,
#     so the box always comes up, whatever is or is not on the disk.
#
#   * auto_channels still does its job: it folds new folders in at start-up,
#     numbering them from max(existing)+1, i.e. 3, 4, 5... It skips paths that
#     an explicit channel already claims and skips hidden folders, so channel 2
#     below can never be duplicated.
#
# ${MEDIA_ROOT} is interpolated into that document below as a single-quoted
# YAML scalar, twice. Single-quoting neutralises the characters that would
# otherwise be read specially by a YAML parser (':', '#') - but it cannot
# neutralise a single quote IN the value, and it cannot stop a literal
# newline from ending the line early and either truncating the document or
# injecting a bare, unquoted extra "line" into it. Caught here, before
# anything is written, because the alternative is a box that either builds
# its lineup from the wrong folder (a stray '#' silently starts a comment) or
# has a config.yaml that fails to parse on a fresh image, which is a unit
# that will not start.
if [[ "${MEDIA_ROOT}" == *"'"* || "${MEDIA_ROOT}" == *'"'* \
   || "${MEDIA_ROOT}" == *'#'* || "${MEDIA_ROOT}" == *':'* \
   || "${MEDIA_ROOT}" == *$'\n'* ]]; then
  die "RETROBOX_MEDIA_ROOT ('${MEDIA_ROOT}') contains a quote, hash, colon or " \
      "newline; that would corrupt the config.yaml this script writes. Pick " \
      "a path without those characters."
fi

if [[ ! -f "${REPO_DIR}/config.yaml" ]]; then
  say "Writing ${REPO_DIR}/config.yaml"
  cat > "${REPO_DIR}/config.yaml" <<YAML
# Written by the Retro Box unattended installer. Safe to edit, and the
# dashboard will rewrite parts of it.
#
# Drop a folder of videos into ${MEDIA_ROOT} (or into \\\\retrobox\\Library from
# a PC) and it becomes a channel on the next start-up.
media_root: '${MEDIA_ROOT}'
auto_channels: true

# Channel 2 is a placeholder so the box always has a valid lineup, even with an
# empty library. Discovered channels are numbered from 3 upwards. Delete this
# entry once the box has real content.
channels:
  - number: 2
    name: "Retro Box"
    path: '${MEDIA_ROOT}/.welcome'
YAML
else
  say "${REPO_DIR}/config.yaml already exists - leaving it alone"
fi

# --- 3. The product ----------------------------------------------------------
# scripts/install.sh and the two scripts it calls decide who the box belongs to
# with `${SUDO_USER:-$USER}`. Unattended there is no sudo session and no login,
# so that expands to root - which would render both systemd units with
# User=root and put a network-facing dashboard on port 80 as root, quietly
# throwing away the capability sandbox those units are built around.
#
# So the answer is injected rather than inferred. HOME goes with it so the OSD
# font lands in the right home directory instead of /root.
#
# We are root, so the `sudo` calls inside those scripts are a no-op passthrough
# and never prompt for a password. No temporary NOPASSWD rule is needed.
say "Running scripts/install.sh --service"
env \
  SUDO_USER="${RETROBOX_USER}" \
  USER="${RETROBOX_USER}" \
  HOME="${RUN_HOME}" \
  DEBIAN_FRONTEND=noninteractive \
  "${REPO_DIR}/scripts/install.sh" --service

# --- 4. Ownership ------------------------------------------------------------
# Everything above was created by root: the venv, the editable install, the
# generated filler clips, config.yaml. The services run as ${RETROBOX_USER},
# and two things break if it does not own them - the dashboard cannot save
# config.yaml, and the self-updater fails both its writability gate and git's
# own "dubious ownership" refusal.
say "Giving ${RETROBOX_USER} ownership of ${REPO_DIR} and ${RUN_HOME}"
chown -R "${RETROBOX_USER}:${RUN_GROUP}" "${REPO_DIR}"
chown -R "${RETROBOX_USER}:${RUN_GROUP}" "${RUN_HOME}"

# The documented manual path is `cd ~/RetroBox`. Keep that true.
if [[ ! -e "${RUN_HOME}/RetroBox" ]]; then
  ln -s "${REPO_DIR}" "${RUN_HOME}/RetroBox"
  chown -h "${RETROBOX_USER}:${RUN_GROUP}" "${RUN_HOME}/RetroBox"
fi

# --- 5. Let the two units find each other ------------------------------------
# Both units set XDG_RUNTIME_DIR=/run/user/<uid>. That directory is created by
# logind for a LOGIN SESSION, and an unattended box never has one - so without
# this it simply never exists. Nothing crashes: the TV plays and the dashboard
# serves, but the status file and the control socket never appear, so the
# dashboard's status panel stays empty and every button silently does nothing.
#
# `loginctl enable-linger` is the fix, but it needs a running logind, which a
# chroot does not have. Creating the file it would have created does the same
# job, offline.
say "Enabling linger for ${RETROBOX_USER} (so /run/user/<uid> exists at boot)"
mkdir -p /var/lib/systemd/linger
touch "/var/lib/systemd/linger/${RETROBOX_USER}"

# --- 6. Straight into the TV, no login prompt --------------------------------
# retrobox.service already declares Conflicts=getty@tty1.service, but Conflicts
# implies no ordering: getty still wins the race often enough to paint a login
# prompt before being killed. Before= closes that.
#
# Deliberately NOT `systemctl mask getty@tty1.service`: masking would mean a box
# whose TV fails to start has no console at all. With this drop-in, getty stays
# available as the recovery path and simply never runs while the TV is up.
say "Ordering the TV ahead of getty on tty1"
mkdir -p /etc/systemd/system/retrobox.service.d
cat > /etc/systemd/system/retrobox.service.d/10-tty1.conf <<'UNIT'
# Installed by the Retro Box unattended installer.
# retrobox.service Conflicts= getty@tty1, but Conflicts implies no ordering, so
# without this getty paints a login prompt before it is killed.
[Unit]
Before=getty@tty1.service
UNIT

# --- 7. Say where the dashboard is -------------------------------------------
# The first thing anyone wants is the dashboard, so the box announces itself in
# the two places someone will actually look: the console (agetty expands \4 to
# the current IPv4 address at display time, so this stays correct across DHCP
# leases) and the SSH login banner.
say "Writing the console and SSH banners"
cat > /etc/issue <<'ISSUE'
Retro Box - JV Projects

  Dashboard : http://retrobox.local/   or   http://\4/
  Library   : \\retrobox\Library   (drag video folders in from any PC)
  Log       : /var/log/retrobox-install.log

ISSUE

install -m 0755 /dev/stdin /etc/update-motd.d/99-retrobox <<'MOTD'
#!/bin/sh
# Installed by the Retro Box unattended installer.
ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '\n Retro Box - JV Projects\n\n'
printf '   Dashboard : http://retrobox.local/'
[ -n "$ip" ] && printf '   or   http://%s/' "$ip"
printf '\n   Library   : \\\\retrobox\\Library\n'
printf '   TV        : systemctl status retrobox\n\n'
MOTD

# --- 8. The whole disk -------------------------------------------------------
# Ubuntu's guided LVM layout does not give the root LV the whole volume group.
# subiquity's default sizing-policy allocates roughly HALF the VG on a disk
# between 20 GB and 200 GB and leaves the rest unallocated, where nothing
# reports it and nothing uses it. Measured on hardware: a 128 GB box came up
# with 57 GB usable and 58 GB idle.
#
# autoinstall.yaml.template asks for `sizing-policy: all`, which is the correct
# declarative fix. This is the second, independent mechanism - it claims
# anything still unallocated and, more importantly, AUDITS the finished box and
# shouts into this log if the root filesystem still does not account for the
# disk. On a media appliance the size of the disk is the one number a customer
# checks, and this failure is otherwise completely silent.
#
# Deliberately not fatal. A box using half its disk still plays television;
# aborting the install would turn wasted space into no box at all. It warns
# here, and it leaves retrobox-growfs.service behind to retry on every boot,
# which is where an online filesystem grow is certain to work.
#
# Run after install.sh rather than before it: install.sh needs a few hundred MB
# for the venv and the generated filler clips, which fits on even the halved
# disk, and running last means the audit measures the box as it will ship.
say "Making sure the root filesystem owns the whole disk"
if ! "${REPO_DIR}/installer/storage-grow.sh"; then
  warn "the whole-disk check did NOT pass - read the DISK SPACE block above"
  warn "before this box goes anywhere. Nothing else in the product reports it."
fi

# --- 9. Nothing waits for a network ------------------------------------------
# This box gets unplugged, carried to a friend's house and switched on with no
# cable in it. That must end with a television playing, not with
# "A start job is running for Wait for Network to be Configured" counting up to
# 2min on somebody's TV while the picture stays black.
#
# Why the fix cannot live in our own unit files is written out at the top of
# installer/harden-boot.sh. In short: scripts/setup_lan_share.sh enables smbd
# and wsdd, whose Ubuntu packaging carries Wants=network-online.target, and
# that target is what drags systemd-networkd-wait-online into every boot. The
# two units this product authors are already clean.
#
# Run late, deliberately: install.sh has by now apt-installed samba and wsdd,
# so their enablement symlinks already exist and this cleans up after them.
say "Making sure nothing on this box waits for a network"
"${REPO_DIR}/installer/harden-boot.sh"

# --- 10. Verify --------------------------------------------------------------
# `retrobox --check` is three-valued: 2 = the config is broken and the TV will
# not start, 1 = the config is fine but there is no media yet, 0 = fine with
# media. A brand-new box legitimately returns 1, so only 2 is a failure.
say "Verifying the configuration"
check_rc=0
"${REPO_DIR}/.venv/bin/retrobox" --check --config "${REPO_DIR}/config.yaml" || check_rc=$?
case "${check_rc}" in
  0) say "config OK, and the library already has content" ;;
  1) say "config OK - no media yet, which is expected on a new box" ;;
  *) die "config is invalid (retrobox --check exit ${check_rc}); the TV would not start" ;;
esac

# Assert the units are actually wired into boot. Checking the symlink rather
# than asking systemctl, because systemctl's answers in a chroot are their own
# adventure.
say "Verifying the services are enabled"
for unit in retrobox.service retrobox-web.service; do
  if [[ -L "/etc/systemd/system/multi-user.target.wants/${unit}" ]]; then
    say "  ${unit} enabled"
  else
    die "${unit} is NOT enabled - the box would boot without it"
  fi
done

# These are the things that make the difference between a unit that starts and
# a unit that 203/EXECs on every boot.
[[ -x "${REPO_DIR}/.venv/bin/retrobox" ]] || die "${REPO_DIR}/.venv/bin/retrobox is missing"
[[ -x "${REPO_DIR}/.venv/bin/retrobox-web" ]] || \
  die "${REPO_DIR}/.venv/bin/retrobox-web is missing - the [web] extra did not install"
[[ -f /etc/sudoers.d/retrobox-poweroff ]] || warn "the poweroff sudoers rule is missing; the remote's shutdown gesture will not work"
[[ -f /etc/sudoers.d/retrobox-system ]] || warn "the system sudoers rule is missing; the dashboard cannot restart the TV or apply updates"

# The boot hang, checked rather than assumed. A mask IS a symlink to /dev/null,
# so this asks the exact question: can this unit still be loaded? If it can,
# every box off this build waits up to two minutes for a network before the
# television appears, and nobody finds out until one is in a living room.
say "Verifying nothing will wait for the network at boot"
for unit in systemd-networkd-wait-online.service NetworkManager-wait-online.service; do
  masked="/etc/systemd/system/${unit}"
  if [[ -L "${masked}" && "$(readlink "${masked}")" == "/dev/null" ]]; then
    say "  ${unit} masked"
  else
    die "${unit} is NOT masked - with nothing plugged in, this box would count a red 'Wait for Network to be Configured' job up to two minutes on the customer's television, on every boot, for ever"
  fi
done
[[ -f /etc/cloud/cloud-init.disabled ]] || \
  warn "cloud-init is still enabled - see the harden-boot.sh output above for why it was left alone"

say "Retro Box provisioning finished OK."
say "On boot: TV on HDMI, dashboard at http://retrobox.local/"
