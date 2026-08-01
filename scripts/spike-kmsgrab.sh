#!/usr/bin/env bash
#
# THROWAWAY SPIKE - not product code. Delete it when it has done its job.
#
# Answers one question: can this box capture its own HDMI output with ffmpeg's
# kmsgrab and encode it with VAAPI, without spoiling the picture on the
# television?
#
# Run it ON THE BOX, WITH THE TELEVISION PLAYING, and watch the screen while it
# runs. The measurement this script cannot take is the one that decides the
# feature: whether the picture stutters. Sit in front of it.
#
#   ./scripts/spike-kmsgrab.sh
#
# It changes nothing permanent. No packages, no setcap, no config, no units.
# Every capture is a few seconds into /tmp and is deleted at the end.
#
set -uo pipefail          # deliberately NOT -e: this script is a series of
                          # experiments and a failing one is a result

SECONDS_TO_CAPTURE=6
WIDTH=1280
HEIGHT=720
BITRATE=3M
WORK="$(mktemp -d /tmp/kmsgrab-spike.XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }
head2() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

VERDICT_BLOCKERS=()
block() { VERDICT_BLOCKERS+=("$1"); fail "$1"; }

# =============================================================================
head2 "1. What this machine is"
# =============================================================================
note "kernel:   $(uname -sr)"
note "machine:  $(uname -m)"
if [[ -r /proc/device-tree/model ]] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  PLATFORM=pi
  note "platform: Raspberry Pi -- $(tr -d '\0' < /proc/device-tree/model)"
  note "          NOTE: a Pi has no VAAPI. It encodes through V4L2 M2M, so the"
  note "          command lines below are the wrong ones for this board and the"
  note "          x86 path does not apply. See the report at the end."
else
  PLATFORM=pc
  note "platform: generic PC (VAAPI path)"
fi

# =============================================================================
head2 "2. Does this ffmpeg have the pieces?"
# =============================================================================
if ! command -v ffmpeg > /dev/null 2>&1; then
  block "ffmpeg is not installed - nothing below can run"
  ffmpeg_ok=0
else
  ffmpeg_ok=1
  note "ffmpeg:   $(ffmpeg -version 2>/dev/null | head -1)"

  if ffmpeg -hide_banner -devices 2>/dev/null | grep -q "kmsgrab"; then
    pass "kmsgrab input device is compiled in"
  else
    block "this ffmpeg has NO kmsgrab - it must be rebuilt or replaced"
  fi

  if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "h264_vaapi"; then
    pass "h264_vaapi encoder is available"
  elif [[ "${PLATFORM}" == "pi" ]]; then
    if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "h264_v4l2m2m"; then
      pass "h264_v4l2m2m is available (the Pi's encoder)"
    else
      block "no hardware H.264 encoder found for this board"
    fi
  else
    block "no h264_vaapi encoder - software encoding is a different feature"
  fi
fi

# =============================================================================
head2 "3. The DRM devices, and who owns them"
# =============================================================================
if [[ ! -d /dev/dri ]]; then
  block "/dev/dri does not exist - this box has no KMS to grab"
else
  ls -l /dev/dri | sed 's/^/        /'
  CARD="$(ls /dev/dri/card* 2>/dev/null | head -1)"
  note "using:    ${CARD:-none}"

  if [[ -r /sys/class/drm ]]; then
    for connector in /sys/class/drm/card*-*/status; do
      [[ -r "$connector" ]] || continue
      printf '        %-28s %s\n' "$(basename "$(dirname "$connector")")" "$(cat "$connector")"
    done
  fi

  # Who is holding DRM master? mpv, if the television is playing - which is
  # precisely why the capture cannot have it.
  if command -v lsof > /dev/null 2>&1 && [[ -n "${CARD:-}" ]]; then
    holders="$(sudo -n lsof "${CARD}" 2>/dev/null | awk 'NR>1 {print $1}' | sort -u | tr '\n' ' ')"
    note "processes with ${CARD} open: ${holders:-none visible (try with sudo)}"
  fi
  if pgrep -x mpv > /dev/null 2>&1; then
    pass "mpv is running - this is the state the capture has to work in"
  else
    fail "mpv is NOT running - START THE TELEVISION FIRST, or this proves nothing"
    note "      kmsgrab against an idle console is not the test."
  fi
fi

# =============================================================================
head2 "4. Which plane has the picture on it"
# =============================================================================
# This decides whether the browser sees a TRUE MIRROR or just the video.
# kmsgrab captures ONE plane. If mpv is scanning video out on an overlay plane
# and drawing the OSD on the primary, capturing one gets you a picture with no
# channel banner - or a banner floating over nothing.
if command -v modetest > /dev/null 2>&1; then
  sudo -n modetest -p 2>/dev/null | sed -n '/^Planes:/,/^$/p' | head -30 | sed 's/^/        /'
  note "Look for how many planes have a non-zero FB. More than one means the"
  note "picture and the overlay are on SEPARATE planes, and a single kmsgrab"
  note "will not capture both - that breaks the 'true mirror' claim."
else
  note "modetest not installed (apt install libdrm-tests) - cannot enumerate planes."
  note "Worth installing: this is the check that tells you whether the CRT effect"
  note "and the channel banner will actually appear in the browser."
fi

# =============================================================================
head2 "5. The capture, at the lowest privilege that works"
# =============================================================================
if [[ "${ffmpeg_ok}" -eq 1 && -n "${CARD:-}" ]]; then

  if [[ "${PLATFORM}" == "pi" ]]; then
    FILTER="hwdownload,format=nv12"
    ENCODER=(-c:v h264_v4l2m2m -b:v "${BITRATE}")
    note "Pi path: kmsgrab -> V4L2 M2M. This is NOT zero-copy and is expected"
    note "to cost more. Measure it honestly rather than assuming."
  else
    # Zero copy: kmsgrab hands over DRM objects, hwmap turns them into VAAPI
    # surfaces, and the encoder reads them on the GPU. Frames never touch
    # system memory.
    FILTER="hwmap=derive_device=vaapi,scale_vaapi=w=${WIDTH}:h=${HEIGHT}:format=nv12"
    ENCODER=(-c:v h264_vaapi -b:v "${BITRATE}")
  fi

  try_capture() {
    local label="$1"; shift
    local out="${WORK}/$(echo "$label" | tr ' ' '_').mp4"
    local log="${out}.log"

    printf '\n  --- trying: %s\n' "$label"
    ( "$@" ffmpeg -hide_banner -loglevel warning -y \
        -device "${CARD}" -f kmsgrab -i - \
        -vf "${FILTER}" "${ENCODER[@]}" \
        -t "${SECONDS_TO_CAPTURE}" "${out}" > "${log}" 2>&1 ) &
    local pid=$!

    # Sample the CPU of the whole process tree while it runs.
    local samples=0 total=0
    sleep 1
    while kill -0 "$pid" 2>/dev/null; do
      local cpu
      cpu="$(ps -eo pid,ppid,pcpu,comm | awk -v p="$pid" '$1==p || $2==p {s+=$3} END {print s+0}')"
      total="$(awk -v t="$total" -v c="$cpu" 'BEGIN {print t+c}')"
      samples=$((samples + 1))
      sleep 1
    done
    wait "$pid"; local rc=$?

    if [[ $rc -ne 0 ]]; then
      fail "$label - ffmpeg exited $rc"
      sed 's/^/          /' "${log}" | head -8
      return 1
    fi
    local size avg
    size="$(stat -c %s "${out}" 2>/dev/null || echo 0)"
    avg="$(awk -v t="$total" -v n="$samples" 'BEGIN {printf "%.1f", (n?t/n:0)}')"
    if [[ "${size}" -lt 10000 ]]; then
      fail "$label - produced only ${size} bytes, that is not a capture"
      return 1
    fi
    pass "$label - ${size} bytes, mean CPU ${avg}% across the capture"
    CAPTURED="${out}"
    WORKING_PRIVILEGE="$label"
    return 0
  }

  CAPTURED=""
  WORKING_PRIVILEGE=""

  # Tier 0: no extra privilege at all. Expected to fail while mpv holds DRM
  # master - if it PASSES, the feature needs no privilege and that is the
  # single best outcome available.
  try_capture "as this user, no extra privilege"

  # Tier 1: CAP_SYS_ADMIN only, still as this user. This is the shape the
  # production systemd unit would take.
  if [[ -z "${CAPTURED}" ]] && command -v systemd-run > /dev/null 2>&1; then
    try_capture "CAP_SYS_ADMIN, still unprivileged user" \
      sudo -n systemd-run --quiet --pipe --wait --collect \
        --uid="$(id -u)" --gid="$(id -g)" \
        -p AmbientCapabilities=CAP_SYS_ADMIN \
        -p CapabilityBoundingSet=CAP_SYS_ADMIN --
  fi

  # Tier 2: full root. Only run to tell "needs more privilege" apart from
  # "does not work on this hardware at all". NOT a shipping configuration.
  if [[ -z "${CAPTURED}" ]]; then
    try_capture "as root (diagnostic only, never ship this)" sudo -n
  fi

  if [[ -z "${CAPTURED}" ]]; then
    block "kmsgrab could not capture at ANY privilege level on this box"
    note "This is the STOP condition. Do not build the streaming feature on it."
    note "Common causes: a driver that refuses plane access, a kernel without"
    note "DRM_CAP_..., or ffmpeg built without the right libdrm."
  else
    pass "minimum privilege that worked: ${WORKING_PRIVILEGE}"
  fi
fi

# =============================================================================
head2 "6. Is the capture actually the television?"
# =============================================================================
if [[ -n "${CAPTURED:-}" ]] && command -v ffprobe > /dev/null 2>&1; then
  ffprobe -hide_banner -loglevel error -show_entries \
    format=duration,size:stream=codec_name,width,height,avg_frame_rate \
    -of default=noprint_wrappers=1 "${CAPTURED}" 2>&1 | sed 's/^/        /'

  # Pull a frame out so it can be looked at with human eyes. This is the only
  # way to confirm the CRT effect and the channel banner made it through.
  ffmpeg -hide_banner -loglevel error -y -i "${CAPTURED}" \
    -vf "select=eq(n\,30)" -vframes 1 "/tmp/kmsgrab-spike-frame.png" 2>/dev/null
  if [[ -s /tmp/kmsgrab-spike-frame.png ]]; then
    pass "a frame was written to /tmp/kmsgrab-spike-frame.png"
    note "LOOK AT IT. Does it show the curved CRT picture? If a channel banner"
    note "or the menu was up, is it in the frame? If the answer is no, kmsgrab"
    note "is capturing the wrong plane and the mirror is not a mirror."
  fi
fi

# =============================================================================
head2 "7. Audio - can it reach two places at once?"
# =============================================================================
if [[ -r /proc/asound/cards ]]; then
  sed 's/^/        /' /proc/asound/cards
fi
if modinfo snd-aloop > /dev/null 2>&1; then
  if lsmod 2>/dev/null | grep -q "^snd_aloop"; then
    pass "snd-aloop is loaded (an ALSA loopback is available)"
  else
    note "snd-aloop is available but not loaded (modprobe snd-aloop to try it)."
  fi
else
  note "snd-aloop is not available on this kernel - getting the same audio to"
  note "the HDMI output and the encoder will need another approach."
fi
note "Whatever is tried here: do NOT change mpv's audio device to get sound"
note "into the browser. Silent television is a straight regression and is"
note "worse than a silent stream."

# =============================================================================
head2 "VERDICT"
# =============================================================================
if [[ ${#VERDICT_BLOCKERS[@]} -gt 0 ]]; then
  printf '\n\033[31mSTOP.\033[0m This box cannot do it as specified:\n\n'
  for b in "${VERDICT_BLOCKERS[@]}"; do printf '  - %s\n' "$b"; done
  printf '\nReport this rather than building a workaround.\n'
else
  printf '\n\033[32mThe mechanism works.\033[0m Minimum privilege: %s\n' "${WORKING_PRIVILEGE:-none needed}"
fi

cat <<'QUESTIONS'

  THE THREE ANSWERS THIS SCRIPT CANNOT GIVE YOU
  ---------------------------------------------
  Write them down before deciding anything:

  1. While the capture was running, did the picture on the television
     stutter, tear, or drop a frame?          ................ yes / no

     If yes: the feature is dead in this form. Nothing else matters.

  2. Does /tmp/kmsgrab-spike-frame.png show the CRT curve AND any overlay
     that was up at the time?                 ................ yes / no

     If no: kmsgrab is on the wrong plane and this is not a mirror.

  3. What did the CPU figure above look like next to an idle box?
     (run `top` for a moment with nothing capturing, and compare)

QUESTIONS
