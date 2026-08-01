#!/usr/bin/env bash
#
# Build the Retro Box boot ISO: a stock Ubuntu Server 26.04 ISO with the
# `autoinstall` kernel argument added, so the installer never stops to ask
# "Continue with autoinstall?".
#
# Without that argument this is not a walk-away build - subiquity finds the
# answer file, then sits at a yes/no prompt forever waiting for a keypress.
# Supplying the config alone is NOT enough; the flag has to be on the kernel
# command line, and it has to be the bare word `autoinstall` (`autoinstall=1`
# is parsed as a key/value pair and does not work).
#
# This does NOT remaster the ISO. It copies it and overwrites the 394 bytes of
# /boot/grub/grub.cfg in place, which both the BIOS and UEFI paths read. The
# MBR, GPT, El Torito catalog and the Canonical-signed EFI System Partition are
# left byte-for-byte identical, so Secure Boot is unaffected.
#
# Needs nothing but stock macOS: /bin/bash and /usr/bin/python3. No Homebrew,
# no Docker, no xorriso.
#
# Usage:
#   ./installer/make-boot-iso.sh ~/Desktop/ubuntu-26.04-live-server-amd64.iso
#   ./installer/make-boot-iso.sh <iso> --out /tmp/retrobox-boot.iso
#   ./installer/make-boot-iso.sh <iso> --write /dev/disk4
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC=""
OUT=""
WRITE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)   OUT="${2:?--out needs a path}"; shift 2 ;;
    --write) WRITE="${2:?--write needs a device, e.g. /dev/disk4}"; shift 2 ;;
    -h|--help)
      sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
      exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *)  SRC="$1"; shift ;;
  esac
done

if [[ -z "${SRC}" ]]; then
  echo "error: give the path to a stock Ubuntu Server 26.04 ISO." >&2
  echo "Download: https://ubuntu.com/download/server" >&2
  exit 2
fi
[[ -f "${SRC}" ]] || { echo "no such file: ${SRC}" >&2; exit 1; }

if [[ -z "${OUT}" ]]; then
  OUT="${HERE}/build/retrobox-boot.iso"
fi
mkdir -p "$(dirname "${OUT}")"

python3 "${HERE}/lib/patch_iso.py" "${SRC}" "${OUT}" autoinstall

echo
echo "==> Boot ISO ready: ${OUT}"

if [[ -z "${WRITE}" ]]; then
  cat <<EOF

Write it to a USB stick with:
  ./installer/make-boot-iso.sh "${SRC}" --write /dev/diskN

...or by hand, after finding the disk with 'diskutil list':
  diskutil unmountDisk /dev/diskN
  sudo dd if="${OUT}" of=/dev/rdiskN bs=4m status=progress
  diskutil eject /dev/diskN
EOF
  exit 0
fi

# --- writing to a real device ------------------------------------------------
# This erases the target. Everything below is here to make it hard to erase the
# wrong one.
if ! [[ "${WRITE}" =~ ^/dev/disk[0-9]+$ ]]; then
  echo "error: --write wants a whole disk like /dev/disk4, not '${WRITE}'." >&2
  echo "Do not give a slice (/dev/disk4s1)." >&2
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
echo "About to ERASE this disk and write the boot ISO to it:"
diskutil info "${WRITE}" | grep -E "Device Identifier|Device / Media Name|Disk Size|Protocol|Removable Media" || true
echo
printf 'Type the disk identifier (%s) to confirm, anything else to abort: ' "${WRITE##*/}"
read -r CONFIRM
if [[ "${CONFIRM}" != "${WRITE##*/}" ]]; then
  echo "Aborted. Nothing was written."
  exit 1
fi

echo "==> Unmounting ${WRITE}"
diskutil unmountDisk "${WRITE}"
echo "==> Writing (this takes a few minutes; sudo will ask for your password)"
# /dev/rdiskN is the raw device and is many times faster than /dev/diskN.
RAW="/dev/r${WRITE#/dev/}"
sudo dd if="${OUT}" of="${RAW}" bs=4m
sync
echo "==> Ejecting"
diskutil eject "${WRITE}" || true
echo "==> Boot stick ready."
