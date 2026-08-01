#!/usr/bin/env bash
#
# SECONDARY delivery: bake the answer file INTO the ISO so one stick does
# everything, with no separate CIDATA volume.
#
# Use the primary route (make-boot-iso.sh + make-cidata.sh) unless you
# specifically need a single stick. The primary route needs no third-party
# tooling at all, and changing the password there means rewriting a 4 MB stick
# instead of rebuilding and rewriting a 3 GB one.
#
# How this differs from make-boot-iso.sh:
#   make-boot-iso.sh    edits 394 bytes in place. Cannot add a file to the ISO.
#   this script         adds /autoinstall.yaml to the ISO9660 tree, which means
#                       genuinely rebuilding the image.
#
# Why the answer file goes at the ISO root: subiquity's config search order ends
# with /cdrom/autoinstall.yaml, and casper mounts the boot medium at /cdrom. So
# a file called autoinstall.yaml at the root of the ISO is found automatically,
# with no ds=nocloud kernel argument and therefore no GRUB semicolon escaping.
#
# It still also patches grub.cfg, because the bare `autoinstall` token on the
# kernel command line is the ONLY thing that skips the "Continue with
# autoinstall?" prompt. Shipping the YAML alone is not enough.
#
# NOTE ON PRECEDENCE: cloud-config data outranks /cdrom/autoinstall.yaml. If a
# CIDATA stick is plugged in as well, the CIDATA stick wins. That is useful -
# you can bake a house default and override it per unit - but it does mean a
# forgotten CIDATA stick will silently override this ISO.
#
# REQUIREMENTS - rebuilding this image is not something stock macOS can do.
# The ISO is a hybrid with a GRUB MBR, a GPT, two El Torito entries and a
# Canonical-signed EFI System Partition APPENDED as a real partition. hdiutil
# makehybrid handles exactly one El Torito entry and cannot emit a GPT, so it
# is not an option. You need one of:
#
#   Homebrew:  brew install xorriso
#   Docker:    any running Docker/OrbStack/Podman (an arm64 image is fine -
#              xorriso only shuffles bytes, it does not execute the ISO)
#
# This script finds whichever you have and uses it.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSWER="${HERE}/autoinstall.yaml"
SRC=""
OUT="${HERE}/build/retrobox-single-stick.iso"
ENGINE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)    OUT="${2:?--out needs a path}"; shift 2 ;;
    --answer) ANSWER="${2:?--answer needs a path}"; shift 2 ;;
    --engine) ENGINE="${2:?--engine needs xorriso or docker}"; shift 2 ;;
    -h|--help)
      sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
      exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *)  SRC="$1"; shift ;;
  esac
done

[[ -n "${SRC}" ]] || { echo "error: give the path to a stock Ubuntu Server 26.04 ISO." >&2; exit 2; }
[[ -f "${SRC}" ]] || { echo "no such file: ${SRC}" >&2; exit 1; }
[[ -f "${ANSWER}" ]] || {
  echo "error: ${ANSWER} does not exist. Run make-autoinstall.sh first." >&2
  exit 1
}
if grep -q '__[A-Z_]\{3,\}__' "${ANSWER}"; then
  echo "error: ${ANSWER} still contains template placeholders." >&2
  exit 1
fi

mkdir -p "$(dirname "${OUT}")"

# --- pick an engine ----------------------------------------------------------
if [[ -z "${ENGINE}" ]]; then
  if command -v xorriso > /dev/null 2>&1; then
    ENGINE="xorriso"
  elif command -v docker > /dev/null 2>&1 && docker info > /dev/null 2>&1; then
    ENGINE="docker"
  elif command -v podman > /dev/null 2>&1 && podman info > /dev/null 2>&1; then
    ENGINE="podman"
  else
    cat >&2 <<'EOF'
error: no way to rebuild an ISO on this machine.

Stock macOS cannot do it - hdiutil makehybrid supports a single El Torito boot
entry and cannot emit a GPT or an appended partition, and this ISO needs both.

Install ONE of these, then re-run:

  Homebrew + xorriso  (smaller, no daemon)
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      brew install xorriso

  Docker Desktop or OrbStack  (nothing to build)
      https://orbstack.dev  or  https://docker.com/products/docker-desktop

Or just use the primary two-stick route, which needs neither:
      ./installer/make-boot-iso.sh <iso>
      ./installer/make-cidata.sh --write /dev/diskN
EOF
    exit 1
  fi
fi
echo "==> Rebuild engine: ${ENGINE}"

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
cp "${ANSWER}" "${STAGE}/autoinstall.yaml"

# `-boot_image any replay` tells xorriso to reproduce the source image's boot
# arrangement exactly - both El Torito entries, the hybrid MBR, the GPT and the
# appended ESP - without having to restate any of it. The older
# `-b isolinux/isolinux.bin` recipes that litter the internet do not apply to
# this ISO: it has no isolinux at all, only GRUB.
run_xorriso() {
  xorriso -indev "${SRC}" -outdev "${OUT}" \
    -boot_image any replay \
    -map "${STAGE}/autoinstall.yaml" /autoinstall.yaml \
    -compliance no_emul_toc
}

run_container() {
  local engine="$1"
  local src_dir out_dir
  src_dir="$(cd "$(dirname "${SRC}")" && pwd)"
  out_dir="$(cd "$(dirname "${OUT}")" && pwd)"
  "${engine}" run --rm \
    -v "${src_dir}:/src:ro" \
    -v "${out_dir}:/out" \
    -v "${STAGE}:/stage:ro" \
    ubuntu:24.04 \
    bash -c "set -e
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq xorriso > /dev/null
      xorriso -indev '/src/$(basename "${SRC}")' -outdev '/out/$(basename "${OUT}")' \
        -boot_image any replay \
        -map /stage/autoinstall.yaml /autoinstall.yaml \
        -compliance no_emul_toc"
}

case "${ENGINE}" in
  xorriso) run_xorriso ;;
  docker)  run_container docker ;;
  podman)  run_container podman ;;
  *) echo "unknown engine: ${ENGINE}" >&2; exit 2 ;;
esac

[[ -f "${OUT}" ]] || { echo "error: the rebuild produced no output" >&2; exit 1; }

# The rebuilt ISO still needs the kernel argument. patch_iso.py parses the
# image to find grub.cfg, so it works on the rebuilt layout too - but it copies
# rather than edits in place, so patch to a temporary name and move back.
echo "==> Adding the autoinstall kernel argument"
python3 "${HERE}/lib/patch_iso.py" "${OUT}" "${OUT}.patched" autoinstall
mv "${OUT}.patched" "${OUT}"

echo
echo "==> Single-stick ISO ready: ${OUT}"
echo "    It carries /autoinstall.yaml AND the autoinstall kernel argument."
echo
echo "Write it with:"
echo "    diskutil unmountDisk /dev/diskN"
echo "    sudo dd if='${OUT}' of=/dev/rdiskN bs=4m"
echo "    diskutil eject /dev/diskN"
