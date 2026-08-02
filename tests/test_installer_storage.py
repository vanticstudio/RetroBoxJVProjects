"""The installer's disk sizing, which decides how big the box a customer bought is.

Ubuntu's guided LVM layout does not hand the root logical volume the whole
volume group. subiquity's default ``sizing-policy: scaled`` builds the VG across
the whole partition and then gives root roughly HALF of it on a disk between
20 GB and 200 GB, leaving the rest unallocated inside the VG where nothing
reports it and nothing uses it. Measured on real hardware: a 128 GB box came up
claiming 57 GB usable with 58 GB idle.

That matters more than wasted space. This is a media appliance; the size of the
disk is the one number a customer actually checks, and a 512 GB box that says
240 GB reads as a lie. It is also completely silent - the dashboard's System
page repeats the smaller figure without a murmur.

None of this can be proven from a Mac with no disk to install onto, so these
tests take the two halves that CAN be checked here:

  * the answer file is rendered for real and the parsed storage section is
    asserted to ask for all of the space, on a disk of any size;
  * installer/storage-grow.sh's arithmetic is driven directly through its
    ``--check-sizes`` mode, with the byte counts off the box that failed.

What is left for a human on the first test box is written up in the installer
report: ``lsblk -b``, ``vgs``, ``df -B1 /``.
"""

import configparser
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"
TEMPLATE = INSTALLER / "autoinstall.yaml.template"
GROW = INSTALLER / "storage-grow.sh"
PROVISION = INSTALLER / "provision.sh"

GIB = 1024 ** 3


# --------------------------------------------------------------------------
# The rendered answer file
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def rendered(tmp_path_factory):
    """The template put through the real renderer, parsed as YAML.

    Rendered rather than read as text because the thing that ships is the
    rendered file, and a placeholder substitution that broke the indentation
    would leave the template looking perfectly correct.
    """
    work = tmp_path_factory.mktemp("autoinstall")
    key = work / "id_test.pub"
    key.write_text(
        "ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAINOTAREALKEYNOTAREALKEYNOTAREALKEY01 "
        "test@retrobox\n"
    )
    out = work / "autoinstall.yaml"

    env = dict(os.environ)
    env["RETROBOX_PASSWORD"] = "a-test-console-password"
    env["RETROBOX_SSH_KEY"] = str(key)
    env.pop("RETROBOX_WIFI_SSID", None)
    env.pop("RETROBOX_WIFI_PASSWORD", None)

    proc = subprocess.run(
        [sys.executable, str(INSTALLER / "lib" / "render_autoinstall.py"),
         str(TEMPLATE), str(out)],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"the template would not render:\n{proc.stderr}"
    return yaml.safe_load(out.read_text())["autoinstall"]


@pytest.fixture(scope="session")
def storage(rendered):
    return rendered["storage"]


def test_the_answer_file_still_asks_for_a_guided_lvm_install(storage):
    # Guided means "no questions on unknown hardware", which is the primary
    # requirement everything below has to stay inside.
    assert storage["layout"]["name"] == "lvm"


def test_the_root_volume_claims_the_whole_volume_group(storage):
    # THE test. Without this key subiquity uses `scaled`, which is what put
    # 57 GB on a 128 GB box and left 58 GB unallocated. Delete it and every
    # unit shipped afterwards is roughly half the size it was sold as, and
    # nothing anywhere says so.
    assert storage["layout"]["sizing-policy"] == "all", (
        "storage.layout.sizing-policy must be 'all'. Anything else (including "
        "the default, 'scaled') gives the root LV part of the volume group and "
        "silently wastes the rest of the customer's disk."
    )


def test_the_layout_names_no_size_at_all(storage):
    # The target is a secondhand machine whose disk is unknown until it is in
    # your hands. 'largest' and 'all' are the only two quantities allowed here;
    # anything numeric turns "installs on whatever we bought this week" into
    # "installs on the one box it was written for".
    sizes = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("size", "sizing-policy"):
                    sizes.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(storage)
    assert sizes, "the storage section stopped expressing any sizing at all"
    assert set(sizes) <= {"largest", "all"}, (
        f"the storage layout names a concrete size: {sizes}. It must work on a "
        f"disk of unknown size with no questions asked."
    )


def test_every_disk_match_still_carries_a_size_key(storage):
    # Not cosmetic. The presence of `size` (or `ssd`) is what activates
    # subiquity's implicit "skip disks with an in-use partition" filter, which
    # is what stops the installer wiping the USB stick it booted from. Tidying
    # these away while adjusting the sizing policy would be a very quiet way to
    # start destroying installer sticks.
    match = storage["layout"]["match"]
    assert match, "the ordered disk match list is empty"
    for entry in match:
        assert "size" in entry, (
            f"match entry {entry} has no `size` key, so it loses the in-use "
            f"partition filter that protects the boot stick"
        )


def test_the_template_itself_still_spells_the_key_out(rendered):
    # Cheap and blunt, so the failure message names the line to look at even
    # when the YAML parse above has been broken by something else.
    text = TEMPLATE.read_text()
    assert re.search(r"^\s*sizing-policy:\s*all\s*$", text, re.M), (
        "installer/autoinstall.yaml.template no longer contains "
        "'sizing-policy: all'"
    )


# --------------------------------------------------------------------------
# installer/storage-grow.sh - the runtime half
# --------------------------------------------------------------------------
def test_the_grow_script_exists_and_is_executable():
    # curtin runs it by path out of the clone; a lost mode bit is a failed
    # step, not a skipped one.
    assert GROW.is_file(), f"{GROW} is missing"
    assert os.access(GROW, os.X_OK), f"{GROW} is not executable"


@pytest.mark.parametrize("script", [GROW, PROVISION])
def test_the_installer_scripts_parse(script):
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"bash -n {script.name} failed:\n{proc.stderr}"


@pytest.mark.parametrize("script", [GROW, PROVISION])
def test_the_installer_scripts_pass_shellcheck(script):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed on this machine")
    proc = subprocess.run(
        ["shellcheck", "-x", str(script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def grow(*args, **env):
    """Run storage-grow.sh and hand back (exit code, combined output)."""
    environ = dict(os.environ)
    environ.update({k: str(v) for k, v in env.items()})
    proc = subprocess.run(
        ["bash", str(GROW), *args],
        capture_output=True, text=True, env=environ,
    )
    return proc.returncode, proc.stdout + proc.stderr


# The sizes are byte counts, not round numbers, because that is what lsblk and
# df actually hand back and the arithmetic has to survive them.
DISK_128GB = 128_035_676_160          # a 128 GB SSD, as lsblk -b reports it
DISK_512GB = 512_110_190_592
DISK_16GB = 16_000_000_000


def test_the_box_that_failed_is_reported_loudly():
    # The exact fault from the field: 128 GB disk, ~57 GB root, ~58 GB idle.
    rc, out = grow("--check-sizes", str(DISK_128GB), str(57_000_000_000), "/dev/sda")
    assert rc == 3
    assert "NOT USING ITS WHOLE DISK" in out
    assert "DO NOT SHIP" in out.upper()
    # The numbers a human compares must both be in the message.
    assert "119.2 GiB" in out
    assert "53.1 GiB" in out


def test_the_512gb_box_a_customer_would_call_a_lie_is_reported():
    rc, out = grow("--check-sizes", str(DISK_512GB), str(240_000_000_000), "/dev/nvme0n1")
    assert rc == 3
    assert "NOT USING ITS WHOLE DISK" in out


def test_a_healthy_box_says_so_rather_than_saying_nothing():
    # Proof-of-work matters as much as the warning: a silent pass and a check
    # that never ran look identical in a log.
    rc, out = grow("--check-sizes", str(DISK_128GB), str(123_000_000_000), "/dev/sda")
    assert rc == 0
    assert "Disk check OK" in out
    assert "119.2 GiB" in out


def test_a_full_size_disk_is_a_clean_pass():
    rc, out = grow("--check-sizes", str(DISK_128GB), str(DISK_128GB))
    assert rc == 0
    assert "Disk check OK" in out


def test_a_small_disk_is_not_accused_over_its_boot_partition():
    # A 16 GB box legitimately spends ~2.5 GiB on the ESP and /boot, which is a
    # big PERCENTAGE but a small amount. It must not cry wolf: an installer
    # that warns on healthy boxes is an installer nobody reads.
    rc, out = grow("--check-sizes", str(DISK_16GB), str(12_200_000_000))
    assert rc == 0, out


def test_a_big_disk_with_a_big_but_proportionate_overhead_is_a_pass():
    # 4 TB with 100 GiB of overhead: fails the shortfall floor, passes the
    # ratio. Both conditions have to be met before the box is accused, and
    # this is the half that the small-disk case does not cover.
    four_tb = 4 * 10 ** 12
    rc, out = grow("--check-sizes", str(four_tb), str(int(four_tb * 0.97)))
    assert rc == 0, out


def test_sizes_that_could_not_be_read_are_never_mistaken_for_a_pass():
    # "I could not measure it" and "it is fine" are the two answers that must
    # never look alike, because one of them ships a box.
    for disk, fs in ((" ", "123"), ("0", "123"), ("128035676160", "")):
        rc, out = grow("--check-sizes", disk, fs)
        assert rc == 2, f"({disk!r},{fs!r}) gave {rc}, expected 2\n{out}"
        assert "did NOT run" in out


def test_the_no_argument_path_bails_out_instead_of_crashing():
    # Two ways this dies rather than reports. `"$@"` with no positional
    # parameters is an unbound variable under `set -u` in bash 3.x, and every
    # discovery command here can be absent. Emptying PATH reproduces both at
    # once, deterministically, on any machine.
    bash = shutil.which("bash")
    assert bash, "bash is not on PATH; the installer scripts cannot run at all"
    proc = subprocess.run(
        [bash, str(GROW)],
        capture_output=True, text=True, env={"PATH": "/nonexistent"},
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "did NOT run" in proc.stdout + proc.stderr
    assert "unbound variable" not in proc.stderr


def test_the_thresholds_can_be_tightened_from_the_environment():
    # The pass/fail line is a judgement call, so it is a named constant rather
    # than a magic number buried in an expression.
    rc, _ = grow(
        "--check-sizes", str(DISK_128GB), str(100_000_000_000),
        RETROBOX_DISK_MIN_RATIO_PCT=99, RETROBOX_DISK_MIN_SHORTFALL_GIB=1,
    )
    assert rc == 3


def test_the_grow_claims_every_free_extent():
    # `-l +100%FREE` is the whole point. `-L +50G` or any fixed figure would
    # reintroduce exactly the bug, on the disks nobody tested with.
    text = GROW.read_text()
    assert "lvextend -l +100%FREE" in text


def test_the_grow_knows_how_to_extend_whatever_filesystem_is_there():
    # Growing the LV and leaving the filesystem alone gains the customer
    # nothing at all - df would still report the old figure.
    text = GROW.read_text()
    assert "resize2fs" in text, "no ext2/3/4 grow"
    assert "xfs_growfs" in text, "no xfs grow"
    assert "btrfs filesystem resize" in text, "no btrfs grow"


def test_the_grow_never_repartitions_anything():
    # This runs unattended, as root, on a box that already holds a customer's
    # media library, and again on every boot. Growing an LV and a filesystem is
    # safe and idempotent. Touching the partition table is neither, and must
    # never arrive here by accident.
    text = GROW.read_text()
    forbidden = ("mkfs", "sgdisk", "parted", "growpart", "wipefs", "dd if=", "mkswap")
    for word in forbidden:
        assert word not in text, (
            f"installer/storage-grow.sh contains {word!r}. It runs unattended "
            f"on every boot of a box holding customer media; it may extend, "
            f"never re-create."
        )


def test_the_grow_only_ever_extends_never_shrinks():
    text = GROW.read_text()
    assert "lvreduce" not in text
    assert "vgreduce" not in text
    assert re.search(r"resize2fs\s+[-\w\"$]*\s*-M", text) is None, (
        "resize2fs -M shrinks the filesystem to its minimum"
    )


# --------------------------------------------------------------------------
# The boot-time repeat
# --------------------------------------------------------------------------
def shell_assignment(text, name):
    """The literal value of a `NAME="value"` line in a shell script."""
    match = re.search(rf'^{name}="([^"]*)"', text, re.M)
    assert match, f"{name} is not assigned in installer/storage-grow.sh"
    return match.group(1)


@pytest.fixture(scope="session")
def boot_unit():
    """The systemd unit storage-grow.sh writes, parsed as a unit file."""
    text = GROW.read_text()
    body = re.search(r"cat > \"\$\{UNIT_PATH\}\" << UNIT\n(.*?)\nUNIT\n", text, re.S)
    assert body, "the retrobox-growfs.service heredoc could not be found"
    unit = body.group(1)
    for name in ("SBIN_PATH", "UNIT_PATH", "UNIT_NAME"):
        unit = unit.replace("${%s}" % name, shell_assignment(text, name))
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(unit)
    return parser


def test_the_boot_unit_reruns_the_check_on_every_boot(boot_unit):
    # An online filesystem grow is only certain to work on a running system,
    # and a disk that is later swapped for a bigger one should just be
    # absorbed. This is the belt to the install-time braces.
    assert boot_unit["Service"]["Type"] == "oneshot"
    assert boot_unit["Install"]["WantedBy"] == "multi-user.target"
    assert boot_unit["Service"]["ExecStart"].endswith("--no-unit")


def test_the_boot_unit_does_not_live_inside_the_git_clone(boot_unit):
    # The self-updater does `git checkout --force <tag>` in /opt/retrobox. A
    # unit whose ExecStart pointed into the clone would 203/EXEC on every boot
    # the moment it checked out a tag from before this file existed.
    exec_start = boot_unit["Service"]["ExecStart"]
    assert exec_start.startswith("/usr/local/sbin/"), exec_start
    assert "/opt/retrobox" not in exec_start


def test_the_boot_unit_cannot_hang_the_boot(boot_unit):
    # This box is switched off at the wall and carried to a friend's house. A
    # storage check may never be the reason the television does not come up.
    assert "Requires" not in boot_unit["Unit"]
    assert "Before" not in boot_unit["Unit"]
    assert int(boot_unit["Service"]["TimeoutStartSec"]) <= 300


# --------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------
def test_provisioning_actually_runs_the_check():
    # A check nobody calls is a check nobody runs.
    text = PROVISION.read_text()
    assert "installer/storage-grow.sh" in text, (
        "installer/provision.sh no longer runs storage-grow.sh, so a box with "
        "half its disk would ship without a word being said"
    )


def test_a_short_disk_warns_rather_than_aborting_the_install():
    # provision.sh is `set -e` and every other failure there is fatal on
    # purpose. This one must not be: a box using half its disk still plays
    # television, and aborting would turn wasted space into no box at all.
    text = PROVISION.read_text()
    call = re.search(r"^(.*)\"\$\{REPO_DIR\}/installer/storage-grow\.sh\"", text, re.M)
    assert call, "storage-grow.sh is not invoked from provision.sh"
    assert call.group(1).strip().startswith("if !"), (
        "storage-grow.sh is called unguarded under `set -e`, so a box that is "
        "merely short of space would abort the whole install"
    )
