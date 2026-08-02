#!/usr/bin/env bash
#
# Pin the freshly-cloned Retro Box repo to the newest release tag.
# JV Projects.
#
# Runs inside the target at install time, from the autoinstall late-commands.
#
#   installer/pin-release.sh                    # pin, or refuse to build
#   installer/pin-release.sh --allow-unpinned   # bench box: main is acceptable
#   installer/pin-release.sh --repo-dir DIR     # default /opt/retrobox
#
# ---------------------------------------------------------------------------
# WHY THIS REFUSES TO BUILD FROM main
#
# It used to warn and carry on. That means every stick baked on a day with no
# tags produces a box running whatever HEAD happened to be at clone time -
# unreviewed code, on hardware that is then SOLD, switched off at the wall and
# carried somewhere the developer cannot reach. There is no way back from that
# box except collecting it from somebody's living room.
#
# So: no tag, no build. The message says what to do about it. A bench box that
# genuinely wants main can say so out loud with --allow-unpinned.
#
# ONLY vX.Y.Z COUNTS
# The updater builds the tag name it checks out as "v" + version
# (retrobox/updater.py), so a tag named 1.0.4 is invisible to it and a box
# pinned to one would roll itself back on the first update. Pre-release tags
# like v2.0.0-rc1 are filtered out too: a release candidate is not something to
# put in a customer's living room, and version-sorting them against releases is
# a coin toss nobody should be flipping at build time.
#
# WHAT THE UPDATER NEEDS TO FIND AFTERWARDS (retrobox/updater.py)
#   * a real clone at a stable absolute path, with a remote named origin -
#     Updater._step runs `git fetch --tags --force` there and a fetch with no
#     origin fetches nothing, silently, for ever.
#   * `git describe --tags --exact-match` answering with the tag. That call is
#     Updater._current_ref, and it is the version the dashboard shows and the
#     ref an update rolls back to. Detached HEAD is fine - it is the updater's
#     own normal state - but detached at a COMMIT gives it a bare sha to reason
#     about instead of a version, so this checks the answer rather than
#     assuming it.
#   * a tree whose retrobox.__version__ matches that tag, because the update
#     check compares __version__ against the newest published release. A box
#     shipped with those two disagreeing either reinstalls the same release for
#     ever or never offers an update at all.
# All three are verified below, on this box, before the build is allowed to
# continue.
# ---------------------------------------------------------------------------
#
set -euo pipefail

REPO_DIR="${RETROBOX_REPO_DIR:-/opt/retrobox}"
ALLOW_UNPINNED="${RETROBOX_ALLOW_UNPINNED:-0}"

usage() {
  sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo-dir)       REPO_DIR="${2:?--repo-dir needs a path}"; shift 2 ;;
    --allow-unpinned) ALLOW_UNPINNED=1; shift ;;
    -h|--help)        usage 0 ;;
    *) echo "pin-release: unknown argument: $1" >&2; usage 2 ;;
  esac
done

say() { echo "==> $*"; }
die() { echo "!!  FATAL: $*" >&2; exit 1; }

[ -d "${REPO_DIR}/.git" ] || die "${REPO_DIR} is not a git clone"
cd "${REPO_DIR}"

# The updater fetches from origin by name. Without it every future update on
# every box built from this stick quietly does nothing.
git remote get-url origin > /dev/null 2>&1 || die \
  "${REPO_DIR} has no remote named 'origin'. The self-updater runs
    git fetch --tags --force
  in this clone, and with no origin that fetches nothing and reports success,
  so every box built from here would be unable to update itself for ever.
  Re-clone with: git clone <url> ${REPO_DIR}"

# --force so a re-run cannot be blocked by a tag that moved.
if ! git fetch --tags --force > /dev/null 2>&1; then
  if [ -z "$(git tag -l 'v*')" ]; then
    die "could not fetch from origin and this clone has no tags of its own.
  Nothing here can be pinned. Check the network on the build bench and re-run."
  fi
  echo "!!  could not fetch from origin; using the tags this clone already has"
fi

# sort -V is a version sort, so v1.0.10 correctly beats v1.0.9. The grep is
# what keeps v2.0.0-rc1 and any hand-made tag out of a customer's box.
ALL_V_TAGS="$(git tag -l 'v*')"
TAG="$(printf '%s\n' "${ALL_V_TAGS}" \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
        | sort -V | tail -n 1 || true)"

if [ -z "${TAG}" ]; then
  if [ -n "${ALL_V_TAGS}" ]; then
    echo "!!  tags exist here but none of them is a release:" >&2
    printf '!!      %s\n' ${ALL_V_TAGS} >&2
    echo "!!  A release tag has to be exactly vX.Y.Z - the updater builds the" >&2
    echo "!!  ref it checks out as \"v\" + version, so nothing else is reachable." >&2
  fi

  if [ "${ALLOW_UNPINNED}" = "1" ]; then
    HEAD_SHA="$(git rev-parse --short HEAD)"
    cat >&2 <<UNPINNED
!!
!!  ============================================================
!!   BUILDING AN UNPINNED BOX - --allow-unpinned was given
!!  ============================================================
!!  This box will run whatever 'main' was at ${HEAD_SHA}: code that
!!  has not been cut as a release and has not been reviewed as one.
!!
!!  That is a bench box. DO NOT SELL IT. A unit in a customer's living
!!  room cannot be reached, cannot be rolled back by hand, and the
!!  dashboard will report a bare commit id where a version should be.
!!  ============================================================
!!
UNPINNED
    say "Repo left on: ${HEAD_SHA} (unpinned)"
    exit 0
  fi

  cat >&2 <<'NOTAG'
!!  FATAL: this repository has no vX.Y.Z release tag, so there is nothing to
!!  pin this box to.
!!
!!  Refusing to build. Without a tag every stick baked today produces a box
!!  running unreviewed HEAD, and these boxes are sold, switched off at the wall
!!  and carried to places nobody can reach them from.
!!
!!  WHAT TO DO
!!    1. Make sure retrobox/__init__.py's __version__ is the number you mean to
!!       release, and that CHANGELOG.md describes it.
!!    2. Cut and push the tag, on the commit you want in customers' hands:
!!
!!           git tag v<version>          # e.g. git tag v2.0.0
!!           git push origin v<version>
!!
!!    3. Publish it as a RELEASE on GitHub. A bare tag is invisible to the
!!       update checker, which reads the Releases API - a box pinned to a tag
!!       with no Release object never sees another update.
!!    4. Re-run the build. Nothing else needs changing.
!!
!!  Building a bench box on purpose? installer/pin-release.sh --allow-unpinned
!!  (or RETROBOX_ALLOW_UNPINNED=1). It says loudly that the result is not
!!  sellable.
NOTAG
  exit 1
fi

say "Pinning this box to release ${TAG}"
# Detached HEAD is the correct and expected end state here - it is what
# `git checkout <tag>` means, and it is the state the updater itself leaves the
# clone in on every update. The twelve lines of advice git prints about it are
# noise in an install log that somebody reads only when something has gone
# wrong, so they are switched off for this one command.
git -c advice.detachedHead=false checkout --force "${TAG}"

# --- Prove the updater can read this clone back ------------------------------
# `git describe --tags --exact-match` is literally Updater._current_ref. If it
# does not answer with the tag, the box reports a commit id as its version and
# an update has no coherent ref to roll back to.
ACTUAL="$(git describe --tags --exact-match 2> /dev/null || true)"
[ -n "${ACTUAL}" ] || die \
  "checked out ${TAG} but the clone does not describe itself as being on a tag.
  The self-updater reads exactly this to decide what version a box is running,
  so this box would report a bare commit id instead of a version and would have
  no coherent ref to roll back to. Do not ship it."

if [ "${ACTUAL}" != "${TAG}" ]; then
  # More than one tag on one commit. Not wrong - the code is identical - but
  # the updater will call this box ${ACTUAL}, so say so rather than letting the
  # dashboard show a version nobody expected.
  if git tag --points-at HEAD | grep -qx -- "${TAG}"; then
    echo "!!  ${TAG} is not the only tag on this commit; git describe answers"
    echo "!!  '${ACTUAL}', so that is the version this box will report."
    echo "!!  Same code either way. Delete the duplicate tag if it matters."
  else
    die "checked out ${TAG} but the clone describes itself as '${ACTUAL}'.
  Those are different commits, so this box is not running the release it says
  it is. Do not ship it."
  fi
fi

# The version in the tree has to be the version on the tin. tests/test_version.py
# enforces this in CI for tagged commits; enforced again here because a tag that
# was pushed without CI still ends up on hardware.
if [ -f retrobox/__init__.py ]; then
  TREE_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
                    retrobox/__init__.py | head -n 1)"
  if [ -z "${TREE_VERSION}" ]; then
    echo "!!  could not read __version__ out of retrobox/__init__.py; not checking it"
  elif [ "${TREE_VERSION}" != "${TAG#v}" ]; then
    die "tag ${TAG} but retrobox/__init__.py says __version__ = \"${TREE_VERSION}\".
  Every box decides whether to update by comparing its own __version__ against
  the newest published release. With these two disagreeing, this box either
  reinstalls the same release for ever or never offers an update at all.
  Fix __version__, re-tag, and build again."
  fi
fi

say "Repo is at: ${ACTUAL} (a real clone of $(git remote get-url origin))"
