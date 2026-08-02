#!/usr/bin/env bash
#
# Install & enable the Retro Box systemd service so the box boots into TV mode.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_DIR}/scripts/retrobox.service"
TARGET="/etc/systemd/system/retrobox.service"
SUDOERS_DIR="/etc/sudoers.d"

# Say why, on stderr, and stop. Every line of it, with a blank line in front:
# this gets read off a television or out of an SSH session somebody is about to
# close, and a one-word "failed" that scrolls past is how the last one shipped.
fail() {
  printf '\n' >&2
  printf '%s\n' "$@" >&2
  exit 1
}

# =============================================================================
# The sudo rules
# =============================================================================
# Every privileged thing this box does is `sudo` to one specifically named
# command. Nothing here is optional, and none of it may fail quietly.
#
# A unit that had been sold had only the older /etc/sudoers.d/retrobox-poweroff
# on it. It installed cleanly, booted cleanly and played video cleanly, and
# then Shut Down worked while Restart, Reboot and the whole Network page came
# back with "sudo: interactive authentication is required" - on a television,
# to somebody who cannot SSH in and cannot be asked to read an install log. The
# installer had printed a warning weeks earlier and gone on to report success.
#
# So: if the rule cannot be generated, or does not parse, or parses and does
# not actually grant, the install STOPS. A box that stops and says why can be
# fixed in the five minutes somebody is still standing next to it. A box that
# is quietly missing half its management surface cannot be fixed at all.

# The television's own power-off. Deliberately separate from the generated rule
# below, and deliberately still here: the TV process switches the box off with
# `power_off_command` from config.yaml, which defaults to a plain
# `sudo poweroff` (retrobox/config.py) - not `systemctl poweroff`, which is
# what the dashboard's Shut Down button runs and the only shape the generated
# rule grants. Volume-down-past-zero and the sleep timer both go through it.
# Delete this fragment and the picture stops going off at the end of the night
# on a box with no keyboard attached.
poweroff_sudoers_rule() {
  local user="$1"
  # The same shape of name servicectl.sudoers_rule insists on, for the same
  # reason: this string is written into a file that grants root, and a name
  # with a comma, a space or a newline in it does not name a user - it changes
  # what the line means. The generated rule has always refused one; the
  # hand-written one beside it used to take whatever it was handed.
  if [[ ! "${user}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    fail \
      "error: '${user}' is not a name this box can write a sudo rule for." \
      "" \
      "  That rule is what lets the television switch itself off, and it names" \
      "  the account the box runs as. Nothing has been changed." \
      "" \
      "  Run this script as the account the box runs as, or with sudo from it."
  fi
  cat <<EOF
# Managed by Retro Box scripts/install-service.sh.
#
# How the television itself switches the box off - volume-down-past-zero and
# the sleep timer, which run config.yaml's power_off_command. Separate from
# retrobox-system because that file is generated from the dashboard's command
# table, and a plain \`poweroff\` is not in it.

${user} ALL=(root) NOPASSWD: /sbin/poweroff, /usr/sbin/poweroff, /sbin/shutdown, /usr/sbin/shutdown, /usr/bin/systemctl poweroff
EOF
}

# What the dashboard's System and Network pages may do. Generated from
# retrobox/servicectl.py rather than written out here, so the file on disk
# cannot come to permit more - or less - than the code actually runs. Every
# command is named in full; there is no blanket systemctl rule.
generate_system_sudoers() {
  local python="$1" user="$2" out="$3"
  local complaint
  # 2>&1 >file, in that order: the rule goes to the file and anything python
  # says about it comes back here to be quoted at whoever is watching.
  if ! complaint="$("${python}" -c 'import sys
from retrobox.servicectl import sudoers_rule
sys.stdout.write(sudoers_rule(sys.argv[1]))' "${user}" 2>&1 >"${out}")"; then
    fail \
      "error: Retro Box could not work out which commands it needs to run as root." \
      "" \
      "  python said:" \
      "${complaint:-    (nothing)}" \
      "" \
      "  That list is generated from retrobox/servicectl.py, so this normally" \
      "  means the Python side of the install is incomplete or broken. Run" \
      "  ./scripts/install.sh first, then run this script again." \
      "" \
      "  Nothing has been changed: no sudo rule, no service file, nothing" \
      "  restarted. The install stops here on purpose - a box that installs" \
      "  cleanly and then cannot be restarted, rebooted, shut down or put on" \
      "  wifi from the dashboard is worse than one that stops and says why."
  fi
}

# Ask sudo whether it would run each of these WITHOUT a password - without
# running any of them.
#
# There is no side-effect-free entry in servicectl.COMMANDS to test with: all
# four of them restart, reboot or power off the box, and an installer that
# proved the reboot button worked by rebooting the box would be its own bug
# report. `sudo -l <command>` asks exactly the question that matters here -
# "would you allow this, and would you want a password first" - and executes
# nothing.
#
# -n so it fails instead of waiting for a password nobody is going to type;
# finding out whether a password would be wanted is the entire point. -k so
# that a sudo password typed a minute ago (this script needs several) cannot
# answer yes on the rule's behalf.
#
# All of them, not a sample: the box this was found on had only the poweroff
# rule, so `systemctl poweroff` was granted and nothing else was. One sample
# would have called that box healthy.
#
# And asked about the user the box will RUN as, which is not always the user
# running this script. installer/provision.sh builds the shipped image as root
# with SUDO_USER set to the box's own account, so a plain `sudo -l` there would
# be listing root's privileges - which are everything - and would have said
# yes to a rule that grants the box nothing. `-l -U <user>` asks about that
# account instead, and is only needed when the two differ, because an ordinary
# account is not allowed to ask about anybody.
SUDOERS_REFUSED=""
SUDOERS_REFUSED_REASON=""
sudoers_grants() {
  local user="$1"
  shift
  local command words said me
  me="$(id -un)"
  SUDOERS_REFUSED=""
  SUDOERS_REFUSED_REASON=""
  for command in "$@"; do
    words=()
    read -r -a words <<< "${command}"
    if [[ "${me}" == "${user}" ]]; then
      said="$(sudo -n -k -l "${words[@]}" 2>&1 >/dev/null)" && continue
    else
      said="$(sudo -n -k -l -U "${user}" "${words[@]}" 2>&1 >/dev/null)" && continue
    fi
    SUDOERS_REFUSED="${command}"
    SUDOERS_REFUSED_REASON="${said}"
    return 1
  done
  return 0
}

# Ask visudo whether a file is a valid sudoers file, and stop the install if it
# is not. Never skipped, never softened: a syntactically bad file in
# /etc/sudoers.d breaks sudo for EVERYTHING, on a box whose only way back to
# root is sudo.
#
#   $1  the file name it is destined for in /etc/sudoers.d
#   $2  the file holding the content, staged well away from /etc
#   $3  what stops working without it, in a customer's words
sudoers_validate() {
  local name="$1" staged="$2" consequence="$3"
  local complaint
  if ! complaint="$(sudo visudo -c -f "${staged}" 2>&1)"; then
    fail \
      "error: the sudo rule for ${name} is not valid, so it was NOT installed." \
      "" \
      "  visudo said:" \
      "${complaint:-    (nothing)}" \
      "" \
      "  Nothing has been changed. ${SUDOERS_DIR} is untouched and sudo still" \
      "  works on this box - which is why the rule is checked before it is" \
      "  installed and not after. A bad file there locks everybody out of" \
      "  root, and sudo is this box's only way back." \
      "" \
      "  The install stops here. Without this rule ${consequence}." \
      "  Fix the error above, or report it, and run this script again."
  fi
}

# Put the file where sudo will read it, in one step that cannot half-happen.
#
# These boxes are switched off at the wall. Writing straight over the live file
# empties it first, and a sudoers file cut off halfway through is not a missing
# permission - it is a syntax error, and sudo refuses everything while there is
# one, including the sudo somebody would need to put it right. So the text goes
# to a name beside the live one and is renamed onto it, and a rename either
# happened or it did not.
#
# The parked name has a dot in it deliberately: sudo reads every file in
# /etc/sudoers.d on every single call EXCEPT the ones whose names contain a
# dot. So a box that loses power between the write and the rename comes back up
# with a stray file sudo will not even look at, and the next run of this script
# replaces it.
#
# Spelled ".<name>.new" to match retrobox/servicectl.py's repair(), which puts
# the same file back the same way when root runs it. One shape of leftover to
# recognise, not two.
sudoers_place() {
  local staged="$1" target="$2"
  local parked="${target%/*}/.${target##*/}.new"
  sudo install -m 440 -o root -g root "${staged}" "${parked}"
  sudo mv -f "${parked}" "${target}"
}

# Put one sudoers fragment on the box and prove it works.
#
#   $1   the file name in /etc/sudoers.d
#   $2   a file holding the content it is supposed to have
#   $3   what stops working without it, in the words somebody who owns one
#        would use - it ends up in the error message
#   $4   the account the box runs as, which the rule is for
#   $5+  the commands it is supposed to grant, one per argument
#
# Safe to re-run: the content is compared with what is on disk and only written
# when it differs, so an installer run on a healthy box says so instead of
# quietly rewriting a file that grants root.
sudoers_ensure() {
  local name="$1" staged="$2" consequence="$3" user="$4"
  shift 4
  local target="${SUDOERS_DIR}/${name}"
  local wrote=0

  # Checked again here, even though install_sudo_rules has already checked
  # every fragment before installing any of them. It costs a moment and it
  # means nothing can ever reach ${SUDOERS_DIR} through this function without
  # having been through visudo, whoever calls it and in whatever order.
  sudoers_validate "${name}" "${staged}" "${consequence}"

  # 2>/dev/null because on a box that has never had this file, cmp's complaint
  # about the missing one is the normal case, not something to alarm anybody
  # with. Any failure to compare means "install it", which is the safe way to
  # be wrong.
  if sudo cmp -s "${staged}" "${target}" 2>/dev/null; then
    echo "    ${target} is already up to date"
  else
    sudoers_place "${staged}" "${target}"
    echo "    installed ${target} (checked with visudo first)"
    wrote=1
  fi

  # "visudo parsed it" and "sudo acts on it" are different questions, and the
  # difference between them is exactly how this shipped broken. Ask sudo.
  if ! sudoers_grants "${user}" "$@"; then
    if [[ "${wrote}" -eq 0 ]]; then
      # The contents already matched so nothing was written - but the contents
      # are not the whole story. sudo ignores a fragment whose permissions or
      # owner are wrong, and says nothing about it. Write it once properly and
      # ask again before giving up on the box.
      sudoers_place "${staged}" "${target}"
      echo "    rewrote ${target} (it was there but sudo was ignoring it)"
      if sudoers_grants "${user}" "$@"; then
        return 0
      fi
    fi
    fail \
      "error: ${target} was installed, but sudo will not act on it." \
      "" \
      "  This command still needs a password:" \
      "    ${SUDOERS_REFUSED}" \
      "" \
      "  sudo said:" \
      "    ${SUDOERS_REFUSED_REASON:-(nothing)}" \
      "" \
      "  The rule passed visudo, so the usual cause is something else on this" \
      "  box overriding it: another file in ${SUDOERS_DIR} read after it, or" \
      "  an /etc/sudoers that asks for a password before it will list" \
      "  anything. 'sudo -l' shows what this user is really allowed." \
      "" \
      "  The install stops here. Left alone, this is the failure that has" \
      "  already shipped once: the box installs, boots and plays video, and" \
      "  then weeks later ${consequence} - every press coming back with" \
      "  \"sudo: interactive authentication is required\", on a television" \
      "  nobody can log in to."
  fi
}

# Both fragments, in one step, because a box needs both and neither is
# optional. Called with the venv's python, the user the box runs as, and a
# directory to build the files in before they are checked.
#
# Everything is built and checked BEFORE anything is installed. Not tidiness:
# the messages above promise that nothing has been changed, and somebody reads
# that off a television and decides the box can be left alone until the morning.
# Built one-then-installed-then-built, a rule that failed to generate would
# leave that promise a lie, and half a box's permissions behind it.
POWEROFF_CONSEQUENCE="turning the volume down past zero and the sleep timer cannot switch the box off"
SYSTEM_CONSEQUENCE="the dashboard cannot restart the television, reboot the box, shut it down, set the clock or save anything on the Network page"
install_sudo_rules() {
  local python="$1" user="$2" staging="$3"

  poweroff_sudoers_rule "${user}" > "${staging}/retrobox-poweroff"
  generate_system_sudoers "${python}" "${user}" "${staging}/retrobox-system"

  local commands=()
  local line
  while IFS= read -r line; do
    [[ -n "${line}" ]] && commands+=("${line}")
  done < <("${python}" -c 'from retrobox.servicectl import COMMANDS
for argv in COMMANDS.values():
    print(" ".join(argv[2:]))')
  if [[ "${#commands[@]}" -eq 0 ]]; then
    fail "error: Retro Box could not list the commands the dashboard runs as root." \
      "" \
      "  Run ./scripts/install.sh first, then run this script again. Nothing" \
      "  has been changed."
  fi

  sudoers_validate retrobox-poweroff "${staging}/retrobox-poweroff" \
    "${POWEROFF_CONSEQUENCE}"
  sudoers_validate retrobox-system "${staging}/retrobox-system" \
    "${SYSTEM_CONSEQUENCE}"

  sudoers_ensure retrobox-poweroff "${staging}/retrobox-poweroff" \
    "${POWEROFF_CONSEQUENCE}" "${user}" "poweroff"
  # What is asked about, and what that proves. The commands are the ones the
  # dashboard's Power buttons run, spelled exactly as servicectl runs them -
  # bare `systemctl`, not a path, because that is what sudo will be handed at
  # the time. The clock, the Network page and the wifi scan are granted by the
  # SAME file, further down it, so a box that answers yes to these is a box
  # that is reading and acting on that file: what would still hide from this
  # check is some other rule elsewhere in /etc/sudoers.d that overrides one of
  # the later lines and not these. Nothing on a Retro Box writes one, and
  # retrobox/servicectl.py's own check asks about every command on every page
  # of the dashboard, on the box, for as long as it is switched on.
  sudoers_ensure retrobox-system "${staging}/retrobox-system" \
    "${SYSTEM_CONSEQUENCE}" "${user}" "${commands[@]}"
}

# =============================================================================
# Where the television and the dashboard meet
# =============================================================================
# The TV process and the dashboard are two separate programs. Everything they
# say to each other goes through two files in one directory: status.json, which
# the TV writes and the dashboard reads, and control.sock, which the TV listens
# on and the dashboard connects to. Both units name that directory the same way:
#
#     Environment=XDG_RUNTIME_DIR=/run/user/<uid>
#
# logind creates /run/user/<uid> for a LOGIN SESSION. This product has none. It
# is switched on at the wall, has no keyboard, and nobody ever logs into it - so
# unless the account is marked as LINGERING, that directory is never created and
# the two processes have nowhere to meet.
#
# Nothing crashes when that happens, which is what makes it dangerous. The
# television plays perfectly. The dashboard renders perfectly. The status panel
# says "the TV process is not running" while the picture is visibly on, and
# every button comes back 503 because there is no socket to reach. Nothing in
# `journalctl -u retrobox` says why at the default log level.
#
# It hid on this path for one specific reason: whoever runs this script is
# logged in over SSH, so logind has ALREADY made /run/user/<uid> for THEIR
# session. Both services start, both find the directory, the installer sees
# green - and the directory is removed under the two running processes the
# moment that session closes, and never comes back on any later boot.
#
# retrobox/status.py now falls back to a private temp directory rather than
# writing into a hole, and picks the same one in both processes. That keeps a
# box working. It is not a reason to leave linger off: the fallback lives in
# /tmp, which is cleared on every boot, and it exists for boxes we have already
# sold, not for boxes we are building today.

# Are we inside a chroot rather than on the box itself?
#
# installer/provision.sh runs this script inside a chroot of the target during
# an image build. There is no logind of its own there, and /run may be bind-
# mounted from the LIVE INSTALLER - so anything that talks over D-Bus talks to a
# different machine's systemd, about accounts that machine has never heard of.
#
# The test is systemd's own: pid 1's root is the real root of the running
# system, so a root that is not the same directory means we are inside a chroot.
# /proc/1/root is readable only by root, which is exactly right here - the image
# build runs as root and the manual path does not, and a plain user who cannot
# read it is, by that fact, not the case this is guarding against.
in_chroot() {
  local here there
  here="$(stat -Lc '%d:%i' / 2> /dev/null || true)"
  there="$(stat -Lc '%d:%i' /proc/1/root 2> /dev/null || true)"
  [[ -n "${here}" && -n "${there}" && "${here}" != "${there}" ]]
}

# Is there a logind here that can answer for THIS root? Everything that follows
# is conditional on this, and says so out loud when the answer is no, because a
# check that quietly evaporates in the one build nobody is watching is worse
# than no check at all.
logind_here() {
  if in_chroot; then
    return 1
  fi
  [[ -d /run/systemd/system ]] || return 1
  command -v loginctl > /dev/null 2>&1
}

# Run something as the account the box runs as, which is not always the account
# running this script (installer/provision.sh is root with SUDO_USER set).
run_as() {
  local user="$1"
  shift
  if [[ "$(id -un)" == "${user}" ]]; then
    "$@"
  else
    sudo -u "${user}" -- "$@"
  fi
}

# Mark the account as lingering, so logind creates /run/user/<uid> at boot with
# nobody logged in.
#
#   $1  the account the box runs as
#   $2  the file logind reads, i.e. /var/lib/systemd/linger/<user>
#
# Two mechanisms, and which one is right depends on where this is running.
# `loginctl enable-linger` is the correct one on a live box: it writes that same
# file AND starts user@<uid>.service, so the directory exists immediately and
# this install can PROVE it rather than promise it for next boot. In a chroot it
# is the wrong one for the reason in in_chroot() above, and installer/
# provision.sh has always written the file directly for exactly that reason.
#
# Silent and side-effect-free when the box already has it: re-running this
# script is the whole of the advice anybody gets when a box misbehaves.
ensure_linger() {
  local user="$1" file="$2"
  local said

  if [[ -e "${file}" ]]; then
    return 0
  fi

  if logind_here; then
    if said="$(sudo loginctl enable-linger "${user}" 2>&1)"; then
      echo "    linger enabled for '${user}'"
      return 0
    fi
    echo "    loginctl would not enable linger (${said:-no reason given});"
    echo "    writing ${file} instead - that is the file logind reads at boot"
  fi

  sudo mkdir -p "${file%/*}"
  sudo touch "${file}"
  echo "    linger enabled for '${user}' (${file})"
}

# Wait for every one of these paths to exist, up to $1 seconds. Polls once
# first, so a timeout of 0 means "look now and answer".
wait_for_paths() {
  local seconds="$1"
  shift
  local waited=0 path missing
  while true; do
    missing=0
    for path in "$@"; do
      [[ -e "${path}" ]] || missing=1
    done
    if [[ "${missing}" -eq 0 ]]; then
      return 0
    fi
    if [[ "${waited}" -ge "${seconds}" ]]; then
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
}

# Linger, and then the directory it is supposed to produce.
#
#   $1  the account the box runs as   $4  /run/user/<uid>
#   $2  its uid                       $5  how long to wait for it, in seconds
#   $3  the linger file
#
# The verification is CONDITIONAL and the condition is stated: in an image build
# there is no logind to create the directory, so its absence proves nothing and
# the check is skipped out loud. On a live box there is no excuse for it to be
# missing, and the install stops.
ensure_runtime_directory() {
  local user="$1" uid="$2" file="$3" dir="$4" seconds="${5:-5}"

  ensure_linger "${user}" "${file}"

  if ! logind_here; then
    echo "    ${dir} not checked here: this root has no systemd of its own"
    echo "    (an image build in a chroot). logind creates it on the box's"
    echo "    first boot, from ${file}."
    return 0
  fi

  if wait_for_paths "${seconds}" "${dir}"; then
    echo "    ${dir} exists, so the two processes have somewhere to meet"
    return 0
  fi

  # Marked as lingering and the directory still is not there. The usual cause is
  # that the mark was written by hand (or by an image build) and logind has not
  # been asked since, so ask it once properly before giving up on the box.
  sudo loginctl enable-linger "${user}" > /dev/null 2>&1 || true
  if wait_for_paths "${seconds}" "${dir}"; then
    echo "    ${dir} exists, so the two processes have somewhere to meet"
    return 0
  fi

  fail \
    "error: ${dir} does not exist, so the television and the dashboard" \
    "       would have nowhere to meet." \
    "" \
    "  '${user}' is marked as lingering (${file}), but logind has not created" \
    "  the directory. Both service units set XDG_RUNTIME_DIR to it, and that is" \
    "  where the TV writes its status file and listens for the dashboard." \
    "" \
    "  Left alone, this box plays television perfectly and its dashboard says" \
    "  \"the TV process is not running\" while the picture is on, with every" \
    "  button on it doing nothing." \
    "" \
    "  On the box:" \
    "    sudo loginctl enable-linger ${user}" \
    "    ls -ld ${dir}" \
    "    systemctl status user@${uid}.service" \
    "  then run ./scripts/install-service.sh again." \
    "" \
    "  Nothing has been written into /etc/systemd. The two sudo rules above are" \
    "  installed and are harmless on their own."
}

# Ask the product itself where the handshake lives, in the environment the units
# give it.
#
#   $1 python   $2 config.yaml   $3 the uid the units run as   $4 that account
#
# Deliberately not a copy of the rule in shell: retrobox/status.py chooses
# between /run/user/<uid>/retrobox and a private temp directory by asking which
# one is ALREADY OCCUPIED - that is what keeps the TV and the dashboard together
# when they start at different moments - and a second copy of that rule here
# would go stale and check the wrong directory on exactly the boxes that need
# checking. XDG_RUNTIME_DIR is set to what the unit sets, and the two overrides
# a developer might have in their own shell are cleared, so the answer is the
# one the services will get and not the one this terminal would.
handshake_paths() {
  local python="$1" config="$2" uid="$3" user="$4"
  run_as "${user}" env \
    -u RETROBOX_STATUS_PATH -u RETROBOX_CONTROL_SOCKET \
    "XDG_RUNTIME_DIR=/run/user/${uid}" \
    "${python}" -c '
import sys

from retrobox.status import control_socket_path, status_path

socket = control_socket_path()
web = True
try:
    from retrobox.config import load_config

    options = load_config(sys.argv[1]).input_options or {}
    web = bool(options.get("web", True))
    socket = options.get("web_socket") or socket
except Exception as exc:                       # noqa: BLE001 - reported, not raised
    print("NOTE=%s" % (str(exc).splitlines() or [""])[0])
print("STATUS=%s" % status_path())
print("SOCKET=%s" % socket)
print("WEB=%s" % ("yes" if web else "no"))
' "${config}"
}

# The point of this whole file: prove the box actually works before saying so.
#
#   $1 python  $2 config.yaml  $3 the account  $4 its uid  $5 seconds to wait
#
# What is proved, and what is not. On a live box: that systemd says both units
# are active, that the TV has written its status file, and that it is listening
# on its control socket - which together are the entire management surface. In
# an image build nothing has been started (systemctl's start verbs no-op in a
# chroot), so there is nothing to look for and this says so instead of passing
# quietly; installer/provision.sh does the checks that are possible there.
verify_the_box_works() {
  local python="$1" config="$2" user="$3" uid="$4" seconds="${5:-20}"

  if ! logind_here; then
    echo "    not checked here: nothing has been started, because this root has"
    echo "    no systemd of its own (an image build in a chroot). The status"
    echo "    file and the control socket appear on the box's first boot;"
    echo "    installer/provision.sh checks what can be checked at build time."
    return 0
  fi

  local out
  if ! out="$(handshake_paths "${python}" "${config}" "${uid}" "${user}" 2>&1)"; then
    fail \
      "error: could not work out where the television writes its status file." \
      "" \
      "  python said:" \
      "${out:-    (nothing)}" \
      "" \
      "  Both units are installed and the box may well be running. What could" \
      "  not be done is check it. Run ./scripts/install.sh to repair the Python" \
      "  side, then ./scripts/install-service.sh again."
  fi

  local line status="" socket="" web="yes" note=""
  while IFS= read -r line; do
    case "${line}" in
      STATUS=*) status="${line#STATUS=}" ;;
      SOCKET=*) socket="${line#SOCKET=}" ;;
      WEB=*) web="${line#WEB=}" ;;
      NOTE=*) note="${line#NOTE=}" ;;
    esac
  done <<< "${out}"
  if [[ -z "${status}" || -z "${socket}" ]]; then
    fail \
      "error: the television could not say where its status file goes." \
      "" \
      "  It answered:" \
      "${out:-    (nothing)}" \
      "" \
      "  Run ./scripts/install.sh, then this script again."
  fi
  [[ -n "${note}" ]] && echo "    note: ${note}"

  # 1. Is it running at all? Type=simple means `systemctl restart` returns 0 for
  #    a process that execs and then dies, so "the restart worked" is not an
  #    answer to this question.
  local state
  state="$(systemctl is-active retrobox.service 2> /dev/null || true)"
  if [[ "${state}" != "active" ]]; then
    fail \
      "error: retrobox.service is ${state:-not running}, so this box has no" \
      "       television." \
      "" \
      "  It was installed and started, and it is not up. On this unit that is" \
      "  usually a config the TV refuses (retrobox --check --config" \
      "  ${config} says which), or a missing media folder." \
      "" \
      "  systemctl status retrobox.service" \
      "  journalctl -u retrobox -n 50 --no-pager" \
      "" \
      "  Fix what it says there and run ./scripts/install-service.sh again. The" \
      "  unit gives up entirely after five failed starts in a minute; running" \
      "  this script clears that first, so a box that has given up gets another" \
      "  go without anybody having to know that."
  fi

  state="$(systemctl is-active retrobox-web.service 2> /dev/null || true)"
  if [[ "${state}" != "active" ]]; then
    fail \
      "error: retrobox-web.service is ${state:-not running}, so this box has no" \
      "       dashboard." \
      "" \
      "  The television may be perfectly fine. Everything a customer can" \
      "  actually reach - the guide, the channel list, uploads, wifi, shut down" \
      "  - is on the dashboard, and this box has no other way in." \
      "" \
      "  The usual cause is that the [web] extra did not install, which leaves" \
      "  the unit exec'ing a program that is not there:" \
      "    ls -l ${REPO_DIR}/.venv/bin/retrobox-web" \
      "    journalctl -u retrobox-web -n 50 --no-pager" \
      "" \
      "  Run ./scripts/install.sh to put it back, then this script again."
  fi

  # 2. Have the two files the dashboard needs actually appeared?
  local wanted=("${status}")
  if [[ "${web}" == "yes" ]]; then
    wanted+=("${socket}")
  else
    echo "    the control socket is not expected on this box: config.yaml sets"
    echo "    input.web: false, so the dashboard's buttons are switched off"
  fi

  if ! wait_for_paths "${seconds}" "${wanted[@]}"; then
    local missing=""
    [[ -e "${status}" ]] || missing="${missing}    ${status}  (the TV's status snapshot)
"
    if [[ "${web}" == "yes" && ! -e "${socket}" ]]; then
      missing="${missing}    ${socket}  (the dashboard's way in)
"
    fi
    fail \
      "error: the television is running, but it has not produced what the" \
      "       dashboard needs within ${seconds} seconds." \
      "" \
      "  Missing:" \
      "${missing%$'\n'}" \
      "" \
      "  This is the failure that ships silently: the picture is on, the" \
      "  dashboard renders, its status panel says the TV process is not running" \
      "  and every button comes back with an error." \
      "" \
      "  One innocent explanation, worth ruling out first: the television scans" \
      "  the library before it starts, so a box with an enormous one can take" \
      "  longer than this to get going. If the picture is on and the dashboard" \
      "  works, that is what this was - run this script again to confirm it." \
      "" \
      "  Otherwise: both processes decide on that directory the same way (see" \
      "  retrobox/status.py), so a file missing here means the TV could not" \
      "  write it at all:" \
      "    ls -ld ${status%/*}" \
      "    journalctl -u retrobox -n 50 --no-pager" \
      "" \
      "  Then run ./scripts/install-service.sh again. Everything is installed;" \
      "  what is not proved is that the dashboard can reach the television."
  fi

  echo "    checked: the TV is writing ${status}"
  if [[ "${web}" == "yes" ]]; then
    echo "    checked: it is listening on ${socket}"
  fi
  case "${status}" in
    "/run/user/${uid}/"*) : ;;
    *)
      echo "    note: the two are meeting in ${status%/*}, not /run/user/${uid}."
      echo "    That works, and it is cleared on every boot - they will move to"
      echo "    /run/user/${uid} together the next time the box starts."
      ;;
  esac
}

# Fill in a unit template. Everything the box's own account is called, in one
# place, so the two units cannot come to disagree about it.
#
#   $1 template  $2 where to write it  $3 user  $4 group  $5 uid  $6 home  $7 repo
#
# The group is substituted FIRST and on its own line. Both templates spell it
# `Group=__USER__`, and the __USER__ substitution below is global - so done the
# other way round, every unit this installer has ever written named a group that
# is only usually there. Debian normally creates a matching per-user group and
# does not always (an account made with `-g users` does not have one), and a
# unit whose Group= does not resolve is refused by systemd with 216/GROUP: no
# television, no dashboard, and "Failed to determine group credentials" in a
# journal nobody can reach.
render_unit() {
  local template="$1" out="$2" user="$3" group="$4" uid="$5" home="$6" repo="$7"
  sed \
    -e "s|^Group=__USER__$|Group=${group}|" \
    -e "s|__USER__|${user}|g" \
    -e "s|__UID__|${uid}|g" \
    -e "s|__HOME__|${home}|g" \
    -e "s|__REPO_DIR__|${repo}|g" \
    "${template}" > "${out}"
}

# tests/test_install_sudoers.py and tests/test_install_runtime.py source this
# file for the functions above and run them against stand-ins for sudo, loginctl
# and systemctl, because none of this can be installed on a laptop. Nothing else
# sets this.
if [[ "${RETROBOX_SUDOERS_LIB_ONLY:-}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

# =============================================================================
# Installing
# =============================================================================
RUN_USER="${SUDO_USER:-$USER}"
RUN_UID="$(id -u "${RUN_USER}")"
RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
CONFIG="${REPO_DIR}/config.yaml"

# Looked up, never assumed to be the user's own name - see render_unit() for
# what a group that does not exist costs.
if ! RUN_GROUP="$(id -gn "${RUN_USER}" 2> /dev/null)" || [[ -z "${RUN_GROUP}" ]]; then
  fail \
    "error: could not work out which group '${RUN_USER}' belongs to." \
    "" \
    "  Both service units need it: a unit whose Group= does not resolve is" \
    "  refused by systemd, and the box has no television and no dashboard." \
    "" \
    "  Check the account exists (id ${RUN_USER}) and run this script again."
fi

if [[ ! -x "${REPO_DIR}/.venv/bin/retrobox" ]]; then
  fail \
    "error: ${REPO_DIR}/.venv/bin/retrobox not found, so there is no television" \
    "       to install." \
    "" \
    "  Run ./scripts/install.sh first, then this script again. Nothing has" \
    "  been changed."
fi

# Checked here rather than discovered by systemd at boot. This is the program
# the dashboard unit execs; without it that unit 203/EXECs on every start, and
# a box with no dashboard has no management surface at all.
if [[ -f "${REPO_DIR}/scripts/retrobox-web.service" && \
      ! -x "${REPO_DIR}/.venv/bin/retrobox-web" ]]; then
  fail \
    "error: ${REPO_DIR}/.venv/bin/retrobox-web not found - the [web] extra did" \
    "       not install." \
    "" \
    "  The television would still play, and nothing else would work: the guide," \
    "  the channel list, uploads, wifi and Shut Down are all on the dashboard," \
    "  and a sold box has no other way in." \
    "" \
    "  Run ./scripts/install.sh (it installs retrobox[hardware,web]) and then" \
    "  this script again. Nothing has been changed."
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

# The sudo rules go first, before anything in /etc/systemd is touched, so that
# stopping here leaves the box exactly as it was found.
echo "==> Allowing '${RUN_USER}' exactly the commands the box runs as root"
install_sudo_rules "${REPO_DIR}/.venv/bin/python" "${RUN_USER}" "${STAGING}"
echo "    checked: sudo will run the Power buttons, and the television's own"
echo "    power-off, for '${RUN_USER}' without asking for a password"

# Second, and still before /etc/systemd: without this the two units start,
# report success and cannot see each other for the life of the box.
echo "==> Making sure /run/user/${RUN_UID} exists with nobody logged in"
ensure_runtime_directory "${RUN_USER}" "${RUN_UID}" \
  "/var/lib/systemd/linger/${RUN_USER}" "/run/user/${RUN_UID}" 5

echo "==> Rendering service units for '${RUN_USER}' (group '${RUN_GROUP}')"
render_unit "${TEMPLATE}" "${STAGING}/retrobox.service" \
  "${RUN_USER}" "${RUN_GROUP}" "${RUN_UID}" "${RUN_HOME}" "${REPO_DIR}"

echo "==> Installing ${TARGET}"
sudo cp "${STAGING}/retrobox.service" "${TARGET}"

# Straight into the television, with no login prompt flashing up first.
# retrobox.service already declares Conflicts=getty@tty1.service, but Conflicts
# implies no ordering, so getty wins the race often enough to paint
# "retrobox login:" on the customer's television before it is killed. Before=
# closes that. Deliberately not a mask: getty stays available as the recovery
# path on a box whose TV fails to start, and simply never runs while it is up.
echo "==> Ordering the television ahead of the login prompt on tty1"
sudo mkdir -p /etc/systemd/system/retrobox.service.d
printf '%s\n' \
  '# Installed by scripts/install-service.sh.' \
  '# retrobox.service Conflicts= getty@tty1, and Conflicts implies no ordering,' \
  '# so without this getty paints a login prompt before it is killed.' \
  '[Unit]' \
  'Before=getty@tty1.service' \
  | sudo tee /etc/systemd/system/retrobox.service.d/10-tty1.conf > /dev/null

# The web dashboard is a second unit alongside the TV.
if [[ -f "${REPO_DIR}/scripts/retrobox-web.service" ]]; then
  render_unit "${REPO_DIR}/scripts/retrobox-web.service" \
    "${STAGING}/retrobox-web.service" \
    "${RUN_USER}" "${RUN_GROUP}" "${RUN_UID}" "${RUN_HOME}" "${REPO_DIR}"
  sudo cp "${STAGING}/retrobox-web.service" /etc/systemd/system/retrobox-web.service
fi

echo "==> Enabling and starting the services"
sudo systemctl daemon-reload

# The television first, and the dashboard after it. Both orders work on a box
# where everything is fine; they differ on a box where something is not. This
# script runs under `set -e`, so a unit that fails to start ABORTS it - and with
# the optional dashboard wired in first, that abort used to leave
# /etc/systemd/system/retrobox.service written but never enabled. The box then
# booted to a login prompt with no television AND no dashboard, because the
# primary product was standing behind the secondary one.
#
# reset-failed before each restart, and not as a formality. StartLimitBurst=5 in
# sixty seconds is deliberately there to stop a broken box hiding a crash loop
# in the journal - but once it has been hit, `systemctl restart` refuses with
# "start request repeated too quickly" and does nothing. Re-running this script
# is the whole of the advice anybody gets when a box misbehaves, and without
# this it could not repair the one state it most needs to.
sudo systemctl reset-failed retrobox.service > /dev/null 2>&1 || true
sudo systemctl enable retrobox.service
sudo systemctl restart retrobox.service

if [[ -f /etc/systemd/system/retrobox-web.service ]]; then
  sudo systemctl reset-failed retrobox-web.service > /dev/null 2>&1 || true
  sudo systemctl enable retrobox-web.service
  sudo systemctl restart retrobox-web.service
  echo "==> Web dashboard enabled on port 80 (http://retrobox.local/)"
fi

# And now the only question that matters: does the dashboard reach the TV?
echo "==> Checking the dashboard can actually reach the television"
# 45 seconds, not five. The status file appears within a second of the main
# loop starting, but the television scans the whole library before it gets
# there, and this script is also how a box in the field with thousands of
# episodes on it gets repaired. Waiting is only ever paid for by a box that is
# genuinely broken.
verify_the_box_works "${REPO_DIR}/.venv/bin/python" "${CONFIG}" \
  "${RUN_USER}" "${RUN_UID}" 45

cat <<EOF

==> Service installed, and checked.

Handy commands:
  systemctl status retrobox     # is it running?
  journalctl -u retrobox -f     # live logs
  sudo systemctl stop retrobox  # stop the TV
  sudo systemctl disable retrobox   # don't start on boot
EOF
