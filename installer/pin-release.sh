#!/usr/bin/env bash
#
# Pin the freshly-cloned Retro Box repo to the latest release tag.
#
# Runs inside the target at install time, from the autoinstall late-commands.
#
# A new box should start on a known release so the self-update system has a
# coherent starting point. The updater checks out tags named exactly vX.Y.Z, so
# that is what we look for.
#
# If the remote has no such tags, this stays on main and says so loudly rather
# than failing the install. That is safe today: the update checker reads the
# GitHub *Releases* API, and with no releases published it simply reports
# "Up to date." and does nothing. A box on main is therefore coherent, not
# broken - it just has nothing to update to yet.
#
set -euo pipefail

REPO_DIR="${RETROBOX_REPO_DIR:-/opt/retrobox}"

cd "${REPO_DIR}"

# --force so a re-run cannot be blocked by a tag that moved.
git fetch --tags --force > /dev/null 2>&1 || \
  echo "!!  could not fetch tags; using whatever the clone already has"

# sort -V is a version sort, so v1.0.10 correctly beats v1.0.9.
TAG="$(git tag -l 'v*' | sort -V | tail -n 1)"

if [ -n "${TAG}" ]; then
  echo "==> Pinning this box to release ${TAG}"
  git checkout --force "${TAG}"
else
  echo "!!  WARNING no vX.Y.Z tags exist on the remote."
  echo "!!  This box will run 'main' at $(git rev-parse --short HEAD)."
  echo "!!  That works, and the dashboard will report 'Up to date' because the"
  echo "!!  updater reads GitHub Releases and none are published yet."
  echo "!!  To ship boxes pinned to a release, cut one:"
  echo "!!      git tag v1.0.3 && git push --tags"
  echo "!!  then publish it as a Release on GitHub."
fi

echo "==> Repo is at: $(git describe --tags --exact-match 2> /dev/null || git rev-parse --short HEAD)"
