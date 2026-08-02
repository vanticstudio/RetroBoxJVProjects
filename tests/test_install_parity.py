"""What the documented install does, measured against the one that ships.

There are two ways a Retro Box comes into being. ``installer/provision.sh``
builds the units we sell, unattended, on a blank disk. ``scripts/install.sh`` is
the one the README documents, and it is what every person who clones this public
repo runs - including us, whenever a box is repaired or rebuilt by hand.

They had drifted, and every difference was a customer-visible fault that no
installer output mentioned: a library folder that was never created, a starter
config that pointed at five folders which do not exist, a box that waits two
minutes for a network it does not have while the customer watches the countdown
on their television, and an installer that printed "Done!" over a config the
television refuses to start with.

None of it can be installed here - there is no apt, no systemd and no logind on
this machine - so the installer's own shell functions are run directly, against
throwaway directories, with a stand-in ``sudo`` first on PATH that refuses any
argument under /etc, /var, /run or /usr outright. Nothing in this file can reach
the real machine.
"""

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
HARDEN = ROOT / "installer" / "harden-boot.sh"

#: Same stand-in as tests/test_install_runtime.py: record, refuse the paths that
#: matter, and otherwise run the command itself so the script's effects are real.
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


class Box:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.log = tmp_path / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        stub = self.bin_dir / "sudo"
        stub.write_text(STUB_SUDO, encoding="utf-8")
        stub.chmod(0o755)

    def run(self, snippet, **env_extra):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "RETROBOX_INSTALL_LIB_ONLY": "1",
            "STUB_LOG": str(self.log),
        })
        env.update({k: str(v) for k, v in env_extra.items()})
        script = f"source {shlex.quote(str(INSTALL))}\n{snippet}\n"
        return subprocess.run(
            ["bash", "-c", script], cwd=str(self.root), env=env,
            capture_output=True, text=True, timeout=180,
        )

    def exiting_with(self, name, code):
        """A stand-in program that exits with a chosen code."""
        path = self.bin_dir / name
        path.write_text(f"#!/usr/bin/env bash\nexit {code}\n", encoding="utf-8")
        path.chmod(0o755)
        return path


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def q(value):
    return shlex.quote(str(value))


def text():
    return INSTALL.read_text(encoding="utf-8")


# ==========================================================================
# The library folder, and something to play out of it
# ==========================================================================
def test_a_brand_new_box_has_a_library_folder_and_something_in_it(box):
    # provision.sh has always made this. install.sh made nothing: the only
    # thing that ever created /media/retrobox on the manual path was the file
    # share, so a box installed with --no-share had no library at all - and the
    # README tells the customer to copy their shows into it.
    media = box.root / "media"
    result = box.run(
        f"ensure_media_library {q(media)} $(id -un) $(id -gn) "
        f"{q(ROOT / 'retrobox' / 'assets' / 'boot_splash.mp4')}"
    )
    assert result.returncode == 0, result.stderr
    assert (media / ".welcome").is_dir(), (
        "a brand-new box has no folder to play from, so it shows colour bars"
    )
    assert (media / ".welcome" / "boot_splash.mp4").is_file(), (
        "nothing for a box with an empty library to actually play"
    )


def test_the_library_is_not_created_only_when_the_file_share_is(box):
    # `./scripts/install.sh --no-share --service` produced a box with no media
    # root whatsoever, and so did a share that failed for any other reason -
    # install.sh swallowed that failure and printed "Done!".
    lines = text().splitlines()
    share_block = [
        line for line in lines
        if "setup_lan_share.sh" in line or "SETUP_SHARE" in line
    ]
    assert not any("ensure_media_library" in line for line in share_block), (
        "the library is created by the file share, so --no-share leaves the "
        "folder the README names missing"
    )
    assert any(line.strip().startswith("ensure_media_library ") for line in lines)


def test_the_file_share_is_told_where_the_library_actually_is():
    # RETROBOX_MEDIA_ROOT was honoured when writing the config and ignored when
    # setting up the share, so a box built with a different library shared an
    # empty /media/retrobox. Files copied in over the network landed somewhere
    # nothing ever reads, and not one episode appeared on the television.
    assert '--path "${MEDIA_ROOT}"' in text()


# ==========================================================================
# The config a new box starts life with
# ==========================================================================
def starter(box, media):
    config = box.root / "config.yaml"
    result = box.run(f"write_starter_config {q(config)} {q(media)}")
    assert result.returncode == 0, result.stderr
    sys.path.insert(0, str(ROOT))
    from retrobox.config import load_config
    return config, load_config(config)


def test_the_starter_config_points_at_the_library_the_readme_promises(box):
    # README: "Anything dropped in lands in /media/retrobox, which is where
    # channels are scanned from". With config.example.yaml copied verbatim that
    # was not true: media_root was unset and auto_channels was off, so a folder
    # of shows dragged into \\retrobox\Library became nothing at all - no
    # channel, no error, no clue. The dashboard's upload page returned HTTP 400
    # for the same reason.
    media = box.root / "media"
    (media / ".welcome").mkdir(parents=True)
    _, config = starter(box, media)
    assert config.media_root == media, "the box does not know where its library is"
    assert config.auto_channels is True, (
        "a folder dropped into the library never becomes a channel"
    )


def test_the_starter_config_gives_an_empty_box_a_lineup_it_can_start_with(box):
    # An explicit channel, for the reason provision.sh spells out: with
    # media_root set and no channels at all, a box with an empty library raises
    # "no channels found" and exits 2. The unit then burns its five restarts in
    # fifteen seconds and goes permanently dead, and dropping media in later
    # does not revive it.
    media = box.root / "media"
    (media / ".welcome").mkdir(parents=True)
    _, config = starter(box, media)
    assert config.channels, "a brand-new box would refuse to start"
    for channel in config.channels:
        assert str(channel.path).startswith(str(media)), channel.path


def test_the_starter_config_is_never_written_over_somebody_elses(box):
    # The dashboard rewrites parts of config.yaml, and people edit it by hand.
    # Re-running the installer is documented and must not undo that.
    config = box.root / "config.yaml"
    config.write_text("# mine\nchannels: []\n", encoding="utf-8")
    result = box.run(f"write_starter_config {q(config)} {q(box.root / 'media')}")
    assert result.returncode == 0, result.stderr
    assert config.read_text(encoding="utf-8") == "# mine\nchannels: []\n"


# ==========================================================================
# "Done!" printed over a box that will never show a picture
# ==========================================================================
def test_a_config_the_television_would_refuse_stops_the_install(box):
    # `retrobox --check` is three-valued: 2 means the config is broken and the
    # TV will not start. install.sh sent every non-zero exit into a friendly
    # hint and carried on to install, start and declare success. The unit then
    # hits its start limit and stays dead for ever, on a box with no SSH.
    checker = box.exiting_with("retrobox-check", 2)
    result = box.run(f"validate_config {q(checker)} {q(box.root / 'config.yaml')}")
    assert result.returncode != 0, (
        "the installer went on to report success over a config the TV refuses"
    )
    said = result.stderr
    assert "retrobox --setup" in said or "config.yaml" in said, "say what to do"


def test_a_new_box_with_no_media_yet_is_not_treated_as_broken(box):
    # Exit 1 is the normal state of a box that has just been built: the config
    # is fine, there is nothing to play yet. Failing here would fail every
    # first install there has ever been.
    checker = box.exiting_with("retrobox-check", 1)
    result = box.run(f"validate_config {q(checker)} {q(box.root / 'config.yaml')}")
    assert result.returncode == 0, result.stderr
    assert "no media yet" in result.stdout


# ==========================================================================
# The two minutes a customer watches on their own television
# ==========================================================================
def fake_root(tmp_path):
    """A throwaway root that looks enough like a freshly installed box."""
    root = tmp_path / "target"
    (root / "etc" / "systemd" / "system").mkdir(parents=True)
    (root / "etc" / "netplan").mkdir(parents=True)
    (root / "etc" / "os-release").write_text('ID=ubuntu\n', encoding="utf-8")
    (root / "etc" / "netplan" / "00-installer-config.yaml").write_text(
        yaml.safe_dump({"network": {"version": 2, "ethernets": {
            "wired-en": {"match": {"name": "en*"}, "dhcp4": True,
                         "optional": True}}}}),
        encoding="utf-8",
    )
    return root


def test_nothing_on_a_manual_box_waits_for_a_network_before_the_television(box):
    # installer/harden-boot.sh never ran on this path and the README never
    # mentioned it - while install.sh apt-installs and enables smbd and wsdd,
    # whose Ubuntu packaging is precisely what drags
    # systemd-networkd-wait-online into every boot. The customer carries the box
    # to a friend's house, switches it on with no cable in it, and watches
    # "A start job is running for Wait for Network to be Configured" count to
    # 2min on the screen they bought the box for.
    root = fake_root(box.root)
    result = box.run(f"harden_boot {q(HARDEN)} {q(root)}")
    assert result.returncode == 0, result.stderr + result.stdout
    for unit in ("systemd-networkd-wait-online.service",
                 "NetworkManager-wait-online.service"):
        link = root / "etc" / "systemd" / "system" / unit
        assert link.is_symlink() and os.readlink(link) == "/dev/null", unit


def test_a_box_that_can_still_wait_for_a_network_fails_the_install(box):
    # Verified rather than assumed, for the same reason provision.sh dies on it:
    # this is the most visible failure the product has, and it is invisible to
    # whoever installed it.
    root = fake_root(box.root)
    result = box.run(f"verify_no_wait_for_network {q(root)}")
    assert result.returncode != 0
    said = result.stderr
    assert "systemd-networkd-wait-online.service" in said
    assert "television" in said, "say what the customer sees, not just a unit name"


# ==========================================================================
# Running the installer the wrong way round
# ==========================================================================
def test_installing_as_root_over_somebody_elses_account_is_refused(box):
    # `sudo ./scripts/install.sh --service` is a natural thing to type for a
    # script visibly full of sudo, and the README never warns against it. It
    # leaves the venv, config.yaml and the generated clips owned by root while
    # the units run as the human - so the box plays television and the dashboard
    # cannot save a single setting, the self-updater refuses to run, and the OSD
    # font is installed into /root. Nothing reports any of it.
    result = box.run("refuse_a_root_install 0 jake /root /home/jake")
    assert result.returncode != 0
    said = result.stderr
    assert "without sudo" in said, "say exactly how to run it instead"
    assert "jake" in said


def test_the_unattended_installer_running_as_root_is_not_refused(box):
    # installer/provision.sh is root by design and injects HOME deliberately, so
    # the guard has to tell that apart from the mistake above or it breaks every
    # box we build.
    result = box.run("refuse_a_root_install 0 retrobox /home/retrobox /home/retrobox")
    assert result.returncode == 0, result.stderr


# ==========================================================================
# What the box says about itself when it is finished
# ==========================================================================
def test_the_box_says_where_its_dashboard_is_on_the_console(box):
    # A manual-path box greeted anybody who plugged a keyboard in with a bare
    # "retrobox login:" and no hint that a dashboard exists or what address it
    # is on.
    etc = box.root / "etc"
    (etc / "update-motd.d").mkdir(parents=True)
    result = box.run(f"write_console_banners {q(etc)}")
    assert result.returncode == 0, result.stderr
    issue = (etc / "issue").read_text(encoding="utf-8")
    assert "retrobox.local" in issue
    assert "\\4" in issue, (
        "agetty expands \\4 at display time, so the address stays right across "
        "DHCP leases"
    )
    motd = etc / "update-motd.d" / "99-retrobox"
    assert motd.stat().st_mode & stat.S_IXUSR, "a motd script that cannot run"
    assert "retrobox.local" in motd.read_text(encoding="utf-8")


def test_the_last_thing_a_person_reads_does_not_tell_them_to_redo_it(box):
    # The closing block was fixed, so somebody who had just run
    # `./scripts/install.sh --service` exactly as the README says finished with
    # "Auto-start on boot: ./scripts/install.sh --service".
    done = box.run("closing_notes pc 1 /media/retrobox /opt/retrobox")
    assert done.returncode == 0, done.stderr
    assert "--service" not in done.stdout, (
        "it tells somebody who just installed the service to install it again"
    )
    assert "retrobox.local" in done.stdout

    not_done = box.run("closing_notes pc 0 /media/retrobox /opt/retrobox")
    assert not_done.returncode == 0, not_done.stderr
    assert "--service" in not_done.stdout, (
        "a box with no service installed still needs to be told how"
    )


def test_the_disk_the_customer_paid_for_is_at_least_audited():
    # Ubuntu's guided LVM default leaves roughly half the disk unallocated: a
    # 128 GB box came up with 57 GB usable. provision.sh grows it and audits it;
    # the manual path did neither and said nothing. Growing a disk somebody else
    # partitioned is not ours to do without asking - reporting it is.
    assert "storage-grow.sh" in text()
    assert "--audit-only" in text()
