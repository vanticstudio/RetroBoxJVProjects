#!/usr/bin/env bash
#
# Build the Retro Box config volume - the second USB stick.
#
# This is the primary delivery mechanism: the boot stick stays a stock Ubuntu
# ISO (plus the one-word `autoinstall` kernel argument, see make-boot-iso.sh),
# and the answer file travels on its own small volume. Nothing has to be
# remastered, and a new password or a new wifi network means rebuilding a 4 MB
# stick rather than a 3 GB one.
#
# How the installer finds it: cloud-init's NoCloud datasource scans every block
# device for a vfat or iso9660 filesystem LABELLED "CIDATA" and reads user-data
# and meta-data from its root. That happens on the label alone - no ds=nocloud
# kernel argument is needed, which conveniently sidesteps having to escape the
# semicolon in ds=nocloud;s=... inside a GRUB config.
#
# Both files must exist. meta-data may be empty, but deleting it makes
# cloud-init skip the datasource entirely and you get an interactive install.
#
# Usage:
#   ./installer/make-cidata.sh --write /dev/disk5      # a real stick
#   ./installer/make-cidata.sh --image /tmp/cidata.img # an image, for testing
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSWER="${HERE}/autoinstall.yaml"
WRITE=""
IMAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --write)  WRITE="${2:?--write needs a device, e.g. /dev/disk5}"; shift 2 ;;
    --image)  IMAGE="${2:?--image needs a path}"; shift 2 ;;
    --answer) ANSWER="${2:?--answer needs a path}"; shift 2 ;;
    -h|--help)
      sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -f "${ANSWER}" ]]; then
  echo "error: ${ANSWER} does not exist." >&2
  echo "Build it first:" >&2
  echo "    ./installer/make-autoinstall.sh --ssh-key ~/.ssh/id_ed25519.pub" >&2
  exit 1
fi

# Refuse to ship a template. The placeholders would produce a box with a
# literal '__PASSWORD_HASH__' in /etc/shadow, i.e. an account nobody can log
# into, and an install that fails at the identity step.
if grep -q '__[A-Z_]\{3,\}__' "${ANSWER}"; then
  echo "error: ${ANSWER} still contains template placeholders." >&2
  grep -o '__[A-Z_]\{3,\}__' "${ANSWER}" | sort -u | sed 's/^/    /' >&2
  echo "Generate a real answer file with make-autoinstall.sh." >&2
  exit 1
fi

# Cheap sanity check before committing it to a stick.
if ! python3 -c 'import sys,ast' 2> /dev/null; then :; fi
python3 - "${ANSWER}" <<'PY'
import sys
text = open(sys.argv[1]).read()
if not text.lstrip().startswith("#cloud-config"):
    sys.exit("error: %s must begin with a #cloud-config header" % sys.argv[1])
if "autoinstall:" not in text:
    sys.exit("error: %s has no autoinstall: section" % sys.argv[1])
PY

stage() {
  # $1 = directory to populate
  local dir="$1"
  cp "${ANSWER}" "${dir}/user-data"
  cat > "${dir}/meta-data" <<'META'
instance-id: retrobox
local-hostname: retrobox
META
  chmod 0644 "${dir}/user-data" "${dir}/meta-data"
}

# --- image mode --------------------------------------------------------------
if [[ -n "${IMAGE}" ]]; then
  TMPDMG="${IMAGE%.img}.dmg"
  rm -f "${TMPDMG}" "${IMAGE}"
  hdiutil create -size 8m -fs "MS-DOS FAT16" -volname CIDATA -layout NONE \
    "${IMAGE%.img}" > /dev/null
  DEV="$(hdiutil attach -nobrowse "${TMPDMG}" | awk '/\/Volumes\//{print $1}')"
  stage /Volumes/CIDATA
  ls -l /Volumes/CIDATA
  hdiutil detach "${DEV}" > /dev/null
  mv "${TMPDMG}" "${IMAGE}"
  echo "==> Wrote ${IMAGE} (FAT, volume label CIDATA)"
  exit 0
fi

if [[ -z "${WRITE}" ]]; then
  echo "error: give --write /dev/diskN or --image <path>." >&2
  echo "Find the stick with: diskutil list" >&2
  exit 2
fi

# --- device mode -------------------------------------------------------------
if ! [[ "${WRITE}" =~ ^/dev/disk[0-9]+$ ]]; then
  echo "error: --write wants a whole disk like /dev/disk5, not '${WRITE}'." >&2
  exit 2
fi
if ! diskutil info "${WRITE}" > /dev/null 2>&1; then
  echo "error: ${WRITE} is not a disk this Mac knows about." >&2
  exit 1
fi

LOCATION="$(diskutil info "${WRITE}" | awk -F': *' '/Device Location/ {print $2}' | tr -d ' ')"
INTERNAL="$(diskutil info "${WRITE}" | awk -F': *' '/Internal:/ {print $2}' | tr -d ' ')"
if [[ "${LOCATION}" == "Internal" || "${INTERNAL}" == "Yes" ]]; then
  echo "error: ${WRITE} is an INTERNAL disk. Refusing." >&2
  exit 1
fi

echo
echo "About to ERASE this disk and write the Retro Box config volume:"
diskutil info "${WRITE}" | grep -E "Device Identifier|Device / Media Name|Disk Size|Protocol|Removable Media" || true
echo
printf 'Type the disk identifier (%s) to confirm, anything else to abort: ' "${WRITE##*/}"
read -r CONFIRM
if [[ "${CONFIRM}" != "${WRITE##*/}" ]]; then
  echo "Aborted. Nothing was written."
  exit 1
fi

# The label must be exactly CIDATA. FAT stores labels uppercase, and
# cloud-init looks for both cases, so this is belt and braces.
echo "==> Formatting ${WRITE} as FAT with volume label CIDATA"
diskutil eraseDisk MS-DOS CIDATA MBRFormat "${WRITE}"

# eraseDisk mounts it for us, but wait for the mount point to appear.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -d /Volumes/CIDATA ]] && break
  sleep 1
done
[[ -d /Volumes/CIDATA ]] || { echo "error: /Volumes/CIDATA did not mount" >&2; exit 1; }

stage /Volumes/CIDATA
sync
echo
echo "==> Config volume ready:"
ls -l /Volumes/CIDATA
diskutil info "${WRITE}"s1 2> /dev/null | grep -E "Volume Name|File System Personality" || true
diskutil eject "${WRITE}" || true
echo "==> Done. Label CIDATA, files user-data + meta-data in the root."
