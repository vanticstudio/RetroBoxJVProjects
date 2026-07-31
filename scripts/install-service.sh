#!/usr/bin/env bash
#
# Install & enable the Retro Box systemd service so the box boots into TV mode.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_DIR}/scripts/retrobox.service"
TARGET="/etc/systemd/system/retrobox.service"

RUN_USER="${SUDO_USER:-$USER}"
RUN_UID="$(id -u "${RUN_USER}")"
RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"

if [[ ! -x "${REPO_DIR}/.venv/bin/retrobox" ]]; then
  echo "error: ${REPO_DIR}/.venv/bin/retrobox not found." >&2
  echo "Run ./scripts/install.sh first." >&2
  exit 1
fi

echo "==> Rendering service unit for user '${RUN_USER}'"
tmp="$(mktemp)"
sed \
  -e "s|__USER__|${RUN_USER}|g" \
  -e "s|__UID__|${RUN_UID}|g" \
  -e "s|__HOME__|${RUN_HOME}|g" \
  -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  "${TEMPLATE}" > "${tmp}"

echo "==> Installing ${TARGET}"
sudo cp "${tmp}" "${TARGET}"
rm -f "${tmp}"

echo "==> Allowing '${RUN_USER}' to power off without a password (for the"
echo "    volume-down-past-zero shutdown)"
sudo tee /etc/sudoers.d/retrobox-poweroff > /dev/null <<EOF
${RUN_USER} ALL=(root) NOPASSWD: /sbin/poweroff, /usr/sbin/poweroff, /sbin/shutdown, /usr/sbin/shutdown, /usr/bin/systemctl poweroff
EOF
sudo chmod 440 /etc/sudoers.d/retrobox-poweroff

echo "==> Enabling and starting the service"
sudo systemctl daemon-reload
# The web dashboard is a second unit alongside the TV.
if [[ -f "${REPO_DIR}/scripts/retrobox-web.service" ]]; then
  sed -e "s|__USER__|${RUN_USER}|g" -e "s|__UID__|${RUN_UID}|g" \
      -e "s|__HOME__|${RUN_HOME}|g" -e "s|__REPO_DIR__|${REPO_DIR}|g" \
      "${REPO_DIR}/scripts/retrobox-web.service" \
    | sudo tee /etc/systemd/system/retrobox-web.service > /dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable retrobox-web.service
  sudo systemctl restart retrobox-web.service
  echo "==> Web dashboard enabled on port 8080"
fi

sudo systemctl enable retrobox.service
sudo systemctl restart retrobox.service

cat <<EOF

==> Service installed.

Handy commands:
  systemctl status retrobox     # is it running?
  journalctl -u retrobox -f     # live logs
  sudo systemctl stop retrobox  # stop the TV
  sudo systemctl disable retrobox   # don't start on boot
EOF
