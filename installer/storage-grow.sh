#!/usr/bin/env bash
#
# Retro Box: make the root filesystem own the whole disk, and say so loudly
# when it does not. JV Projects.
#
# WHY THIS EXISTS
# ---------------
# Ubuntu's guided LVM layout does not hand the root logical volume the whole
# volume group. subiquity's default `sizing-policy: scaled` builds the VG across
# the whole partition and then allocates roughly HALF of it to root on a disk
# between 20 GB and 200 GB (a flat 100 GiB above that), leaving the remainder
# unallocated inside the VG. Measured on real hardware: a 128 GB box reporting
# 57 GB usable with 58 GB idle.
#
# installer/autoinstall.yaml.template asks for `sizing-policy: all`, which is
# the correct declarative fix and the primary one. This script is the second,
# independent mechanism, because:
#
#   * the fault is SILENT - nothing in the product reports it, and the
#     dashboard's System page will happily show the smaller number as if it
#     were correct;
#   * the box is SOLD, to a customer who checks exactly this number and who
#     cannot be reached afterwards to fix it;
#   * `sizing-policy` is one key in one installer's schema, and a box built
#     from an older answer file, or restored onto a larger disk, or installed
#     by a subiquity that ignored the key, arrives with the same wasted half.
#
# It also catches the two other ways the space goes missing: a PV that is
# smaller than its partition (an image restored onto a bigger disk), and a
# filesystem that was never grown to match the LV underneath it.
#
# WHAT IT DOES
# ------------
#   1. pvresize   - the PV takes all of its partition          (no-op if it does)
#   2. lvextend   - the root LV takes all free extents in the VG (skipped if none)
#   3. resize2fs / xfs_growfs / btrfs resize - the filesystem takes all of the LV
#   4. audit      - compare the usable root filesystem against the physical disk
#                   and print a LOUD block if a significant fraction is missing
#
# Every step is idempotent. On a box that is already full-size this is a
# read-only no-op that prints one line of proof and exits 0.
#
# NOT FATAL, ON PURPOSE
# ---------------------
# A box using half its disk still plays television. Aborting the install would
# turn a wasted-space problem into no box at all, so a failure here shouts into
# /var/log/retrobox-install.log and returns non-zero for the caller to report -
# it never takes the box down. provision.sh treats it as a warning.
#
# WHERE IT RUNS
# -------------
#   * at install time, from installer/provision.sh, inside curtin's chroot;
#   * at every boot afterwards, from retrobox-growfs.service, which this script
#     installs into /usr/local/sbin and /etc/systemd/system. Boot is the one
#     place an online filesystem grow is guaranteed to work, and it means a disk
#     that is later replaced or enlarged is picked up without anyone asking.
#     It is installed OUTSIDE the git clone deliberately: the self-updater does
#     `git checkout --force <tag>`, and a unit whose ExecStart lives in the
#     clone would 203/EXEC on every boot the moment it checked out a tag from
#     before this file existed. The price of that is a copy that the updater
#     does not refresh: an edit to this file reaches new boxes at install time,
#     not existing ones. That is the right trade - a stale copy of a check is
#     harmless, a unit that cannot start is not.
#
# Usage:
#   storage-grow.sh                       grow, install the boot unit, audit
#   storage-grow.sh --no-unit             grow and audit only (what the unit runs)
#   storage-grow.sh --audit-only          report, change nothing
#   storage-grow.sh --check-sizes D F [L] audit two byte counts and exit; the
#                                         arithmetic on its own, for the tests
#
# Exit: 0 the root filesystem accounts for the disk
#       2 the sizes could not be determined (or bad arguments)
#       3 A SIGNIFICANT FRACTION OF THE DISK IS UNACCOUNTED FOR
#
# Deliberately not `set -e`: every failure here is handled and reported by hand,
# because a half-finished disk grow that exits silently is the exact failure
# this file exists to prevent.
set -uo pipefail

# --- how much missing space is "wrong" ---------------------------------------
# Two conditions, and BOTH must be met before we shout. A root filesystem is
# legitimately smaller than its disk: the ESP and /boot come off the top
# (~2.5 GiB on 26.04), then LVM metadata, then ext4's own metadata at ~1.5%.
#
#   ratio    - on a 128 GB disk a healthy box lands around 96%; the half-disk
#              fault lands at 47%. 85% sits well clear of both.
#   shortfall- protects small disks, where a fixed 2.5 GiB of /boot and ESP is
#              a large PERCENTAGE. A 16 GB box legitimately reports ~82%, and
#              its 2.7 GiB shortfall is under the floor, so it stays quiet.
#
# The half-disk fault always trips both: it wastes tens of gigabytes.
MIN_RATIO_PCT="${RETROBOX_DISK_MIN_RATIO_PCT:-85}"
MIN_SHORTFALL_GIB="${RETROBOX_DISK_MIN_SHORTFALL_GIB:-8}"

GIB=1073741824
UNIT_NAME="retrobox-growfs.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
SBIN_PATH="/usr/local/sbin/retrobox-growfs"

say() { echo "==> $*"; }
warn() { echo "!!  $*"; }

# Bytes as GiB. awk rather than bash arithmetic so the number has a decimal
# point: "57.0 GiB" and "119.2 GiB" are the two figures a human compares.
gib() { awk -v b="$1" 'BEGIN { printf "%.1f GiB", b / 1073741824 }'; }

# The same, right-aligned, so the three figures in the warning block line up
# under each other. Two numbers a human is meant to compare must be comparable
# at a glance or the block does not do its job.
gib_pad() { awk -v b="$1" 'BEGIN { printf "%9.1f GiB", b / 1073741824 }'; }

is_number() {
  case "${1:-}" in
    '' | *[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

# --- the audit ---------------------------------------------------------------
# Split out from the discovery above it so the arithmetic can be driven
# directly by the test suite (--check-sizes), on a machine with no LVM and no
# disk to install onto.
audit_sizes() {
  local disk="${1:-}" fs="${2:-}" what="${3:-the disk}"
  local shortfall ratio floor

  if ! is_number "${disk}" || [[ "${disk}" -le 0 ]]; then
    warn "could not determine the physical disk size (got '${disk}')."
    warn "The whole-disk check did NOT run. Check this box by hand."
    return 2
  fi
  if ! is_number "${fs}" || [[ "${fs}" -le 0 ]]; then
    warn "could not determine the root filesystem size (got '${fs}')."
    warn "The whole-disk check did NOT run. Check this box by hand."
    return 2
  fi

  shortfall=$((disk - fs))
  ratio=$((fs * 100 / disk))
  floor=$((MIN_SHORTFALL_GIB * GIB))

  if [[ "${ratio}" -ge "${MIN_RATIO_PCT}" ]] || [[ "${shortfall}" -le "${floor}" ]]; then
    say "Disk check OK: ${what} $(gib "${disk}") -> root filesystem $(gib "${fs}") (${ratio}% of the disk)"
    return 0
  fi

  warn "======================================================================"
  warn "DISK SPACE WARNING - THIS BOX IS NOT USING ITS WHOLE DISK"
  echo "!!"
  warn "    physical disk   : $(gib_pad "${disk}")   (${what})"
  warn "    root filesystem : $(gib_pad "${fs}")   (${ratio}% of the disk)"
  warn "    unaccounted for : $(gib_pad "${shortfall}")"
  echo "!!"
  warn "The size of the disk is the one number a customer actually checks, and"
  warn "nothing else in this product reports it - the dashboard's System page"
  warn "will show the smaller figure as if it were correct. DO NOT SHIP THIS BOX."
  echo "!!"
  warn "Almost always this is the root LV not owning the volume group. Fix:"
  warn "    sudo lvextend -l +100%FREE /dev/mapper/<vg>-<lv>"
  warn "    sudo resize2fs /dev/mapper/<vg>-<lv>     # or: sudo xfs_growfs /"
  warn "Confirm with:  lsblk -b -o NAME,SIZE,TYPE,MOUNTPOINT ; vgs ; df -h /"
  warn "If vgs shows no free space, the PARTITION does not span the disk and"
  warn "the box needs reinstalling with installer/autoinstall.yaml."
  warn "======================================================================"
  return 3
}

# --- discovery ---------------------------------------------------------------
root_source() {
  local src
  src="$(findmnt -n -o SOURCE / 2> /dev/null)"
  if [[ -z "${src}" ]]; then
    # findmnt is util-linux and always present on Ubuntu, but a chroot that
    # cannot read /proc would leave us here rather than guessing.
    src="$(df --output=source / 2> /dev/null | tail -n 1 | tr -d '[:space:]')"
  fi
  echo "${src}"
}

root_fstype() { findmnt -n -o FSTYPE / 2> /dev/null; }

# Total size of the root filesystem in bytes, as the filesystem itself reports
# it. This is "size", not "available": the 5% root reserve and whatever the box
# has already written are both beside the point here.
root_fs_bytes() {
  df -B1 --output=size / 2> /dev/null | tail -n 1 | tr -d '[:space:]'
}

# Walk up from any block device to the whole disk that carries it:
# /dev/mapper/vg-lv -> sda3 -> sda. lsblk's PKNAME gives one level at a time.
top_disk_of() {
  local dev="$1" name pk
  name="$(lsblk -ndo KNAME "${dev}" 2> /dev/null | head -n 1)"
  [[ -n "${name}" ]] || return 1
  while :; do
    pk="$(lsblk -ndo PKNAME "/dev/${name}" 2> /dev/null | head -n 1)"
    [[ -n "${pk}" ]] || break
    name="${pk}"
  done
  echo "/dev/${name}"
}

# The physical disks underneath the root filesystem, deduplicated. For LVM this
# is every PV in the root VG (an appliance has one, but a VG spanning two disks
# must not be measured against only one of them).
root_disks() {
  local vg="${1:-}" dev="${2:-}" pv d seen=" "
  local out=""
  if [[ -n "${vg}" ]]; then
    for pv in $(pvs --noheadings -o pv_name,vg_name 2> /dev/null | awk -v v="${vg}" '$2 == v { print $1 }'); do
      d="$(top_disk_of "${pv}")" || continue
      case "${seen}" in *" ${d} "*) continue ;; esac
      seen="${seen}${d} "
      out="${out}${d} "
    done
  fi
  if [[ -z "${out}" ]]; then
    d="$(top_disk_of "${dev}")" || return 1
    out="${d} "
  fi
  echo "${out}"
}

disks_bytes() {
  local total=0 d sz
  for d in $1; do
    sz="$(lsblk -bndo SIZE "${d}" 2> /dev/null | head -n 1)"
    is_number "${sz}" || continue
    total=$((total + sz))
  done
  echo "${total}"
}

# --- growing -----------------------------------------------------------------
# Returns 0 if the root device is LVM and we did whatever was needed, 1 if it is
# not LVM at all (which is fine - the audit below still runs).
grow_lvm() {
  local dev="$1" vg pv free
  command -v lvs > /dev/null 2>&1 || return 1
  vg="$(lvs --noheadings -o vg_name "${dev}" 2> /dev/null | tr -d '[:space:]')"
  [[ -n "${vg}" ]] || return 1
  ROOT_VG="${vg}"
  say "Root is LVM: ${dev} in volume group ${vg}"

  # 1. The PV may be smaller than the partition holding it - an image restored
  #    onto a larger disk, or a VM disk that was resized. pvresize is a clean
  #    no-op when the PV already fills its partition.
  for pv in $(pvs --noheadings -o pv_name,vg_name 2> /dev/null | awk -v v="${vg}" '$2 == v { print $1 }'); do
    if pvresize "${pv}" > /dev/null 2>&1; then
      :
    else
      warn "pvresize ${pv} failed - continuing, the LV extend below may still help"
    fi
  done

  # 2. Claim every free extent. Guarded by the free-space read rather than
  #    swallowing lvextend's exit status, because lvextend exits 5 both when
  #    there is nothing to do and when the command was wrong, and those two
  #    must not look alike.
  free="$(vgs --noheadings -o vg_free --units b --nosuffix "${vg}" 2> /dev/null | tr -d '[:space:]')"
  free="${free%%.*}"
  is_number "${free}" || free=0
  if [[ "${free}" -lt 4194304 ]]; then
    say "Volume group ${vg} has no unallocated space - the root LV already owns it"
  else
    say "Volume group ${vg} has $(gib "${free}") unallocated - extending the root LV"
    if lvextend -l +100%FREE "${dev}"; then
      say "Root LV extended"
    else
      warn "lvextend -l +100%FREE ${dev} FAILED - ${vg} still has $(gib "${free}") idle"
    fi
  fi
  return 0
}

grow_fs() {
  local dev="$1" fstype="$2"
  case "${fstype}" in
    ext2 | ext3 | ext4)
      if ! command -v resize2fs > /dev/null 2>&1; then
        warn "resize2fs is missing - cannot grow an ${fstype} root filesystem"
        return 1
      fi
      # Online grow of the mounted root. Already-full is "Nothing to do!", exit 0.
      resize2fs "${dev}" || {
        warn "resize2fs ${dev} FAILED"
        return 1
      }
      ;;
    xfs)
      if ! command -v xfs_growfs > /dev/null 2>&1; then
        warn "xfs_growfs is missing - cannot grow an xfs root filesystem"
        return 1
      fi
      # xfs grows by mount point, not by device. Already-full is a no-op.
      xfs_growfs / || {
        warn "xfs_growfs / FAILED"
        return 1
      }
      ;;
    btrfs)
      btrfs filesystem resize max / || {
        warn "btrfs filesystem resize max / FAILED"
        return 1
      }
      ;;
    *)
      warn "root filesystem type '${fstype}' is not one this script knows how to grow."
      warn "The audit below still runs, so a short box is still reported."
      return 1
      ;;
  esac
  return 0
}

# --- the boot-time repeat ----------------------------------------------------
install_boot_unit() {
  local self="$1"
  if [[ "${self}" != "${SBIN_PATH}" ]]; then
    install -D -m 0755 "${self}" "${SBIN_PATH}" 2> /dev/null || {
      warn "could not install ${SBIN_PATH}; the boot-time re-check will not exist"
      return 1
    }
  fi

  cat > "${UNIT_PATH}" << UNIT
# Installed by installer/storage-grow.sh (Retro Box unattended installer).
#
# Re-runs the whole-disk check on every boot. On a healthy box this is a
# read-only no-op that prints one line to the journal. It exists because an
# online filesystem grow is only certain to work on a running system, and
# because a disk that is later replaced or enlarged should just be absorbed.
#
# If this unit is FAILED, the box is not using its whole disk. That is the
# point: 'systemctl --failed' is how a box with no one watching says so.
[Unit]
Description=Retro Box: claim all free disk space for the root filesystem
Documentation=file://${SBIN_PATH}
ConditionPathIsMountPoint=/

[Service]
Type=oneshot
RemainAfterExit=no
ExecStart=${SBIN_PATH} --no-unit
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
UNIT

  # The symlink by hand rather than 'systemctl enable': this runs in a chroot
  # with no systemd of its own, where systemctl's answers are their own
  # adventure. The symlink is what enable would have created.
  mkdir -p /etc/systemd/system/multi-user.target.wants
  ln -sfn "${UNIT_PATH}" "/etc/systemd/system/multi-user.target.wants/${UNIT_NAME}"
  say "Installed ${UNIT_NAME} (re-checks the disk on every boot)"
  return 0
}

# --- main --------------------------------------------------------------------
main() {
  local audit_only=0 want_unit=1 self
  ROOT_VG=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check-sizes)
        shift
        [[ $# -ge 2 ]] || {
          warn "--check-sizes needs <disk-bytes> <fs-bytes> [label]"
          return 2
        }
        audit_sizes "$1" "$2" "${3:-the disk}"
        return $?
        ;;
      --audit-only)
        audit_only=1
        want_unit=0
        shift
        ;;
      --no-unit)
        want_unit=0
        shift
        ;;
      -h | --help)
        sed -n '2,70p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
        return 0
        ;;
      *)
        warn "unknown argument: $1"
        return 2
        ;;
    esac
  done

  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

  local dev fstype
  dev="$(root_source)"
  fstype="$(root_fstype)"
  if [[ -z "${dev}" ]]; then
    warn "could not work out which device the root filesystem is on."
    warn "The whole-disk check did NOT run. Check this box by hand."
    return 2
  fi
  say "Root filesystem: ${dev} (${fstype:-unknown type})"

  if [[ "${audit_only}" -eq 0 ]]; then
    if [[ "$(id -u)" -ne 0 ]]; then
      warn "not root - skipping the grow, auditing only"
    else
      grow_lvm "${dev}" || say "Root is not on LVM - nothing to extend"
      grow_fs "${dev}" "${fstype}" || true
    fi
  fi

  local disks disk_bytes fs_bytes
  disks="$(root_disks "${ROOT_VG}" "${dev}")"
  disk_bytes="$(disks_bytes "${disks}")"
  fs_bytes="$(root_fs_bytes)"

  local rc=0
  audit_sizes "${disk_bytes}" "${fs_bytes}" "$(echo "${disks}" | tr -s ' ' | sed 's/ $//')" || rc=$?

  if [[ "${want_unit}" -eq 1 && "$(id -u)" -eq 0 ]]; then
    install_boot_unit "${self}" || true
  fi

  return "${rc}"
}

main "$@"
