"""The half of the install that decides whether the dashboard can reach the TV.

THE FAILURE THIS FILE EXISTS TO STOP, which shipped:

Both systemd units set ``XDG_RUNTIME_DIR=/run/user/<uid>``. logind only creates
that directory for a LOGIN SESSION, and this product has none - it is switched
on at the wall with no keyboard and nobody ever logs in. Unless *linger* is
enabled for the box's account the directory is simply never there, so the TV's
status file and its control socket cannot land in it.

``installer/provision.sh`` has always enabled linger. ``scripts/install.sh`` and
``scripts/install-service.sh`` - the path the README documents, and therefore
the path every person who clones this repo takes - did not. The result is a box
that plays television perfectly, renders its dashboard perfectly, reports "the
TV process is not running" while the picture is visibly on, and 503s every
button. Nothing in the journal says why at the default log level.

It was invisible at install time for a reason worth keeping in mind while
reading these tests: whoever runs the manual installer is SSH'd in, so logind
has already made ``/run/user/<uid>`` for THEIR session. Everything works while
they watch. It breaks when they log out, and on every boot after that.

None of this can be reproduced on a laptop: there is no logind here, no
``/run/user`` anybody may write to, and no systemd at all. What can be done is
to run the installer's own shell functions with stand-ins for ``sudo``,
``loginctl`` and ``systemctl`` first on PATH, against throwaway directories.
The stand-in sudo refuses any argument under ``/etc``, ``/var``, ``/run`` or
``/usr`` outright, so nothing here can reach this machine's real linger
directory or its real runtime directory.

The two probes that ask *where we are* - :func:`in_chroot` and
:func:`logind_here` - are the seam. A test that needs a live box redefines them
after sourcing, which is the only honest way to have a laptop stand in for one.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SERVICE = ROOT / "scripts" / "install-service.sh"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


#: Stand-in for sudo. Records the call, then runs the rest itself - with the
#: paths that matter on a real box refused outright, so a bug in the script
#: under test cannot write to this machine's /etc, /var or /run.
STUB_SUDO = r"""#!/usr/bin/env bash
set -uo pipefail
printf 'sudo %s\n' "$*" >> "${STUB_LOG}"

args=("$@")
i=0
while [[ ${i} -lt ${#args[@]} ]]; do
  case "${args[${i}]}" in
    -u|-U|-g) i=$((i + 2)) ;;
    -*) i=$((i + 1)) ;;
    *) break ;;
  esac
done
rest=("${args[@]:${i}}")
[[ ${#rest[@]} -gt 0 ]] || exit 0

for word in "${rest[@]}"; do
  case "${word}" in
    /etc/*|/var/*|/run/*|/usr/*|/Library/*|/System/*)
      printf 'stub sudo: refusing to touch %s\n' "${word}" >&2
      exit 111 ;;
  esac
done
exec "${rest[@]}"
"""

#: Stand-in for loginctl. `enable-linger` really does two things on a live box -
#: it writes the file logind reads at boot AND brings up user@<uid>.service,
#: which is what creates /run/user/<uid> there and then. Both are imitated, so a
#: test can tell "the installer asked" from "the directory appeared".
STUB_LOGINCTL = r"""#!/usr/bin/env bash
printf 'loginctl %s\n' "$*" >> "${STUB_LOG}"
if [[ "${STUB_LOGINCTL_EXIT:-0}" != "0" ]]; then
  printf 'Failed to enable linger: Interactive authentication required.\n' >&2
  exit "${STUB_LOGINCTL_EXIT}"
fi
if [[ -n "${STUB_LINGER_FILE:-}" ]]; then
  mkdir -p "$(dirname "${STUB_LINGER_FILE}")" && : > "${STUB_LINGER_FILE}"
fi
if [[ -n "${STUB_RUNTIME_DIR:-}" ]]; then
  mkdir -p "${STUB_RUNTIME_DIR}"
fi
exit 0
"""

#: Stand-in for systemctl. Only `is-active` has an answer worth controlling:
#: everything else about a unit on this machine is meaningless.
STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "${STUB_LOG}"
if [[ "${1:-}" == "is-active" ]]; then
  case "${2:-}" in
    retrobox-web*) state="${STUB_WEB_STATE:-active}" ;;
    *)             state="${STUB_TV_STATE:-active}" ;;
  esac
  printf '%s\n' "${state}"
  [[ "${state}" == "active" ]] && exit 0
  exit 3
fi
exit 0
"""

#: What a test says after sourcing the installer to stand in for a live box:
#: not a chroot, and logind is running. Both are environment probes, and a
#: laptop answers no to each.
LIVE_BOX = "in_chroot() { return 1; }\nlogind_here() { return 0; }\n"

#: ...and the image build the unattended installer does its work in: a chroot,
#: with no systemd of its own.
IMAGE_BUILD = "in_chroot() { return 0; }\n"


class Box:
    """A throwaway machine: stand-in sudo/loginctl/systemctl, own directories."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.log = tmp_path / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        for name, text in (("sudo", STUB_SUDO), ("loginctl", STUB_LOGINCTL),
                           ("systemctl", STUB_SYSTEMCTL)):
            stub = self.bin_dir / name
            stub.write_text(text, encoding="utf-8")
            stub.chmod(0o755)
        # A temp directory of its own, so retrobox.status's fallback lands here
        # and never in the /tmp this machine shares with everything else.
        self.tmp = tmp_path / "tmp"
        self.tmp.mkdir()
        self.linger_dir = tmp_path / "linger"
        self.runtime_dir = tmp_path / "run-user"

    @property
    def handshake_dir(self):
        """Where retrobox.status will put the two files, given this TMPDIR."""
        return self.tmp / f"retrobox-{os.getuid()}"

    def run(self, snippet, preamble=LIVE_BOX, **env_extra):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "RETROBOX_SUDOERS_LIB_ONLY": "1",
            "STUB_LOG": str(self.log),
            "TMPDIR": str(self.tmp),
        })
        # Nothing the developer's own shell happens to export may decide where
        # the product thinks the handshake lives.
        for leak in ("XDG_RUNTIME_DIR", "RETROBOX_STATUS_PATH",
                     "RETROBOX_CONTROL_SOCKET"):
            env.pop(leak, None)
        env.update({k: str(v) for k, v in env_extra.items()})
        script = f"source {shlex.quote(str(INSTALL_SERVICE))}\n{preamble}\n{snippet}\n"
        return subprocess.run(
            ["bash", "-c", script], cwd=str(self.root), env=env,
            capture_output=True, text=True, timeout=180,
        )

    @property
    def calls(self):
        return [l for l in self.log.read_text(encoding="utf-8").splitlines() if l]

    def config(self, text):
        path = self.root / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def q(value):
    return shlex.quote(str(value))


MINIMAL_CONFIG = """
channels:
  - number: 2
    name: "Retro Box"
    path: .
"""


# ==========================================================================
# Linger: the one line that decides whether /run/user/<uid> is ever there
# ==========================================================================
def test_the_installer_enables_linger_so_the_runtime_directory_exists_at_boot(box):
    # The whole bug in one test. installer/provision.sh has always done this;
    # the documented manual path never did, so every box built by following the
    # README came up with no /run/user/<uid> and a dashboard that could not see
    # the television.
    linger = box.linger_dir / "retro"
    result = box.run(
        f"ensure_linger retro {q(linger)}",
        STUB_LINGER_FILE=linger, STUB_RUNTIME_DIR=box.runtime_dir,
    )
    assert result.returncode == 0, result.stderr
    assert linger.exists(), "nothing tells logind to create /run/user/<uid> at boot"
    assert any("enable-linger retro" in call for call in box.calls), (
        "on a live box loginctl is what makes the directory appear NOW, without "
        "waiting for a reboot to find out whether it worked"
    )


def test_enabling_linger_on_a_box_that_already_has_it_changes_nothing(box):
    # Re-running the installer is the whole of the advice anybody gets when a
    # box misbehaves, so it has to be safe and quiet. Nothing may be rewritten
    # and logind may not be poked.
    linger = box.linger_dir / "retro"
    linger.parent.mkdir(parents=True)
    linger.touch()
    result = box.run(f"ensure_linger retro {q(linger)}")
    assert result.returncode == 0, result.stderr
    assert box.calls == [], f"a healthy box was changed anyway: {box.calls}"


def test_linger_falls_back_to_the_file_logind_reads_when_loginctl_cannot_answer(box):
    # `loginctl enable-linger` needs a running logind. The unattended installer
    # runs in a chroot that has none, and a live box can still refuse (polkit).
    # Either way the file itself is the thing logind reads at boot, so it is
    # written directly rather than the install giving up.
    linger = box.linger_dir / "retro"
    result = box.run(
        f"ensure_linger retro {q(linger)}",
        STUB_LOGINCTL_EXIT="1", STUB_LINGER_FILE=linger,
    )
    assert result.returncode == 0, result.stderr
    assert linger.exists(), (
        "loginctl said no and the installer left the box with no linger at all"
    )


def test_an_image_build_never_asks_a_logind_that_is_not_its_own(box):
    # In a chroot with /run bind-mounted from the live installer, loginctl talks
    # to the INSTALLER's logind, which knows nothing about the target's accounts
    # - so it either fails or, worse, succeeds against the wrong machine and the
    # target ships with no linger file at all.
    linger = box.linger_dir / "retro"
    result = box.run(f"ensure_linger retro {q(linger)}", preamble=IMAGE_BUILD)
    assert result.returncode == 0, result.stderr
    assert linger.exists()
    assert not any("enable-linger" in call for call in box.calls), (
        "the chroot asked a logind that belongs to a different machine"
    )


# ==========================================================================
# ...and then proving it, rather than assuming it
# ==========================================================================
def test_the_runtime_directory_is_verified_and_not_merely_hoped_for(box):
    linger = box.linger_dir / "retro"
    result = box.run(
        f"ensure_runtime_directory retro 1234 {q(linger)} {q(box.runtime_dir)} 3",
        STUB_LINGER_FILE=linger, STUB_RUNTIME_DIR=box.runtime_dir,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert box.runtime_dir.is_dir()


def test_a_box_whose_runtime_directory_never_appears_fails_the_install(box):
    # The stand-in loginctl is told to create nothing, which is exactly what a
    # box with a broken user@.service looks like. Left to carry on, this is the
    # box that plays television and has a dead dashboard.
    linger = box.linger_dir / "retro"
    result = box.run(
        f"ensure_runtime_directory retro 1234 {q(linger)} {q(box.runtime_dir)} 0",
        STUB_LINGER_FILE=linger,
    )
    assert result.returncode != 0, (
        "the install reported success over a box whose dashboard cannot reach "
        "the television"
    )
    said = result.stderr
    assert str(box.runtime_dir) in said, "name the directory that is missing"
    assert "loginctl enable-linger retro" in said, "say what to run next"
    assert "dashboard" in said, (
        "say what the customer would see, not just which path is absent"
    )


def test_when_the_check_cannot_be_made_it_says_so_instead_of_passing_quietly(box):
    # In the image build there is no logind to create the directory, so the
    # absence of /run/user/<uid> proves nothing at all. Skipping is right;
    # skipping silently is how a check becomes decoration.
    linger = box.linger_dir / "retro"
    result = box.run(
        f"ensure_runtime_directory retro 1234 {q(linger)} {q(box.runtime_dir)} 0",
        preamble=IMAGE_BUILD,
    )
    assert result.returncode == 0, result.stderr
    assert linger.exists(), "the linger file is written even where it cannot be checked"
    said = result.stdout + result.stderr
    assert "not checked" in said.lower()
    assert "first boot" in said.lower(), "say when it WILL be true"


# ==========================================================================
# The handshake itself: the status file and the control socket
# ==========================================================================
def test_the_installer_asks_the_product_where_the_handshake_lives(box):
    # Not a path spelled out again in shell. retrobox/status.py picks between
    # /run/user/<uid>/retrobox and a private temp directory by asking which one
    # is already occupied, so a copy of that rule in the installer would go
    # stale and check the wrong directory on exactly the boxes that need it.
    config = box.config(MINIMAL_CONFIG)
    result = box.run(
        f"handshake_paths {q(VENV_PYTHON)} {q(config)} 1234 $(id -un)"
    )
    assert result.returncode == 0, result.stderr
    found = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    expected = subprocess.run(
        [str(VENV_PYTHON), "-c",
         "from retrobox.status import status_path, control_socket_path;"
         "print(status_path()); print(control_socket_path())"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        env={**os.environ, "TMPDIR": str(box.tmp),
             "XDG_RUNTIME_DIR": "/run/user/1234"},
    )
    assert expected.returncode == 0, expected.stderr
    status, socket = expected.stdout.split()
    assert found["STATUS"] == status
    assert found["SOCKET"] == socket


def test_a_box_where_both_files_appear_is_the_one_that_passes(box):
    config = box.config(MINIMAL_CONFIG)
    box.handshake_dir.mkdir(parents=True)
    (box.handshake_dir / "status.json").write_text("{}", encoding="utf-8")
    (box.handshake_dir / "control.sock").touch()
    result = box.run(
        f"verify_the_box_works {q(VENV_PYTHON)} {q(config)} $(id -un) 1234 2"
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "status" in result.stdout.lower()


def test_a_missing_status_file_fails_the_install_and_names_it(box):
    # This is the exact symptom on the box that shipped: the dashboard's status
    # panel stays empty for ever because nothing ever writes the file it reads.
    config = box.config(MINIMAL_CONFIG)
    box.handshake_dir.mkdir(parents=True)
    (box.handshake_dir / "control.sock").touch()
    result = box.run(
        f"verify_the_box_works {q(VENV_PYTHON)} {q(config)} $(id -un) 1234 0"
    )
    assert result.returncode != 0
    assert "status.json" in result.stderr
    assert "journalctl -u retrobox" in result.stderr, "say where to look next"


def test_a_missing_control_socket_fails_the_install_and_names_it(box):
    # Without it every button on the dashboard 503s while the television plays.
    config = box.config(MINIMAL_CONFIG)
    box.handshake_dir.mkdir(parents=True)
    (box.handshake_dir / "status.json").write_text("{}", encoding="utf-8")
    result = box.run(
        f"verify_the_box_works {q(VENV_PYTHON)} {q(config)} $(id -un) 1234 0"
    )
    assert result.returncode != 0
    assert "control.sock" in result.stderr


def test_a_television_that_is_not_running_is_not_reported_as_installed(box):
    # Type=simple means `systemctl restart` returns 0 for a process that execs
    # and then dies on a bad config. The unit's own restart limit turns that
    # into a PERMANENTLY dead unit inside fifteen seconds, on a box with no SSH.
    config = box.config(MINIMAL_CONFIG)
    box.handshake_dir.mkdir(parents=True)
    (box.handshake_dir / "status.json").write_text("{}", encoding="utf-8")
    (box.handshake_dir / "control.sock").touch()
    result = box.run(
        f"verify_the_box_works {q(VENV_PYTHON)} {q(config)} $(id -un) 1234 0",
        STUB_TV_STATE="failed",
    )
    assert result.returncode != 0
    assert "retrobox.service" in result.stderr
    assert "journalctl" in result.stderr


def test_a_dashboard_that_is_not_running_is_not_reported_as_installed_either(box):
    # The [web] extra failing to install leaves .venv/bin/retrobox-web missing
    # and the unit 203/EXECs on every boot. The old script restarted it and
    # printed "Service installed."
    config = box.config(MINIMAL_CONFIG)
    box.handshake_dir.mkdir(parents=True)
    (box.handshake_dir / "status.json").write_text("{}", encoding="utf-8")
    (box.handshake_dir / "control.sock").touch()
    result = box.run(
        f"verify_the_box_works {q(VENV_PYTHON)} {q(config)} $(id -un) 1234 0",
        STUB_WEB_STATE="failed",
    )
    assert result.returncode != 0
    assert "retrobox-web.service" in result.stderr


def test_the_socket_is_not_demanded_when_the_config_switched_that_backend_off(box):
    # `input: {web: false}` is a supported choice, and a box that made it has no
    # control socket by design. Failing the install there would be the installer
    # inventing a fault.
    config = box.config(MINIMAL_CONFIG + "\ninput:\n  web: false\n")
    box.handshake_dir.mkdir(parents=True)
    (box.handshake_dir / "status.json").write_text("{}", encoding="utf-8")
    result = box.run(
        f"verify_the_box_works {q(VENV_PYTHON)} {q(config)} $(id -un) 1234 0"
    )
    assert result.returncode == 0, result.stderr + result.stdout
    said = result.stdout
    assert "web" in said.lower() and "config" in said.lower(), (
        "a box with no control socket by choice must say so, not stay silent"
    )


def test_the_handshake_check_says_plainly_that_it_did_not_run_in_an_image_build(box):
    # `systemctl restart` no-ops in a chroot, so nothing has been started and
    # there is nothing to look for. The condition is stated rather than the
    # check quietly evaporating.
    config = box.config(MINIMAL_CONFIG)
    result = box.run(
        f"verify_the_box_works {q(VENV_PYTHON)} {q(config)} $(id -un) 1234 0",
        preamble=IMAGE_BUILD,
    )
    assert result.returncode == 0, result.stderr
    said = result.stdout
    assert "not checked" in said.lower()
    assert "provision.sh" in said or "first boot" in said.lower()


# ==========================================================================
# The unit the box actually runs
# ==========================================================================
def test_the_group_the_unit_runs_as_is_looked_up_and_not_assumed(box):
    # Both units carry Group=__USER__ and the renderer used to substitute the
    # USER into it. Debian usually creates a matching per-user group and does
    # not always: an account created with -g users renders Group=users-that-
    # does-not-exist, systemd refuses the unit with 216/GROUP, and the box has
    # no television and no dashboard.
    template = box.root / "unit.template"
    template.write_text(
        "[Service]\nUser=__USER__\nGroup=__USER__\n"
        "Environment=XDG_RUNTIME_DIR=/run/user/__UID__\n"
        "WorkingDirectory=__REPO_DIR__\nEnvironment=HOME=__HOME__\n",
        encoding="utf-8",
    )
    out = box.root / "unit.rendered"
    result = box.run(
        f"render_unit {q(template)} {q(out)} retro media 1234 /home/retro /opt/rb"
    )
    assert result.returncode == 0, result.stderr
    text = out.read_text(encoding="utf-8")
    assert "User=retro" in text
    assert "Group=media" in text, "the unit names a group nothing looked up"
    assert "/run/user/1234" in text and "/opt/rb" in text


# ==========================================================================
# The shape of the script itself
# ==========================================================================
def script_text():
    return INSTALL_SERVICE.read_text(encoding="utf-8")


def test_linger_is_settled_before_anything_is_written_into_etc_systemd():
    # So that a box which cannot get a runtime directory stops with /etc
    # untouched, and the message saying so is true when somebody reads it.
    lines = [l for l in script_text().splitlines() if not l.strip().startswith("#")]
    linger = next(i for i, l in enumerate(lines)
                  if l.strip().startswith("ensure_runtime_directory "))
    systemd = next(i for i, l in enumerate(lines)
                   if "sudo cp " in l or "sudo tee /etc/systemd" in l)
    assert linger < systemd


def test_the_real_linger_file_and_runtime_directory_are_the_ones_systemd_uses():
    # The functions above take their paths as arguments so the tests can point
    # them somewhere harmless. This is the other half: the installer has to hand
    # them the paths logind actually reads and writes.
    text = script_text()
    assert "/var/lib/systemd/linger" in text
    assert "/run/user/${RUN_UID}" in text


def test_the_television_is_wired_in_before_the_optional_dashboard():
    # The web unit used to be enabled and restarted first, under `set -e`. A box
    # whose [web] extra had not installed aborted the script there - leaving
    # /etc/systemd/system/retrobox.service written but never enabled, so it
    # booted to a login prompt with no television AND no dashboard, because the
    # primary product was wired in behind the optional one.
    lines = script_text().splitlines()
    tv = next(i for i, l in enumerate(lines) if "enable retrobox.service" in l)
    web = next(i for i, l in enumerate(lines) if "enable retrobox-web.service" in l)
    assert tv < web


def test_the_dashboard_binary_is_checked_before_its_unit_is_installed():
    # provision.sh checks for it and says "the [web] extra did not install".
    # install-service.sh checked only .venv/bin/retrobox.
    assert ".venv/bin/retrobox-web" in script_text()


def test_the_television_is_ordered_ahead_of_the_login_prompt_on_tty1():
    # retrobox.service declares Conflicts=getty@tty1.service, and Conflicts
    # implies no ordering: getty wins the race often enough to paint a login
    # prompt on the customer's television before it is killed.
    text = script_text()
    assert "retrobox.service.d" in text
    assert "Before=getty@tty1.service" in text
