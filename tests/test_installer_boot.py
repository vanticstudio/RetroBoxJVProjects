"""The installer's boot behaviour, which cannot be booted from here.

A unit that had been sold was carried to a house with no ethernet and did not
come on. It sat on the customer's television showing

    [ *** ] A start job is running for Wait for Network to be Configured

counting up to 2min, and the picture only appeared after it gave up. There is
no SSH into that box and no way to ask it anything: it is switched off at the
wall, unplugged and carried around, and the only acceptable end state for every
one of those journeys is a television playing.

None of that can be reproduced on a laptop. What can be done here is everything
short of the boot itself, and that is what this file does - and it does it by
*running* the installer's scripts rather than by reading them, because a
comment that says a unit is masked and a symlink that masks it are different
things:

  * the answer file is rendered from the committed template, parsed as YAML,
    and every interface in it is checked for ``optional: true``;
  * ``installer/harden-boot.sh`` is executed against throwaway roots and the
    symlinks it leaves behind are inspected;
  * the netplan out of the rendered answer file is fed to that same script, so
    the document the installer ships is proved to pass the check the box runs;
  * ``installer/pin-release.sh`` is executed against throwaway git repositories
    - with no tags, with a release candidate, with two tags, with a version
    that disagrees with its tag - and the clone it leaves behind is read back
    with the exact command ``retrobox/updater.py`` uses.

Delete any of the three fixes and something here goes red.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"
SCRIPTS = ROOT / "scripts"

TEMPLATE = INSTALLER / "autoinstall.yaml.template"
RENDER = INSTALLER / "lib" / "render_autoinstall.py"
HARDEN = INSTALLER / "harden-boot.sh"
PIN = INSTALLER / "pin-release.sh"
PROVISION = INSTALLER / "provision.sh"

#: Every netplan device type. Checked as a set rather than "the two we happen
#: to write", so an interface added later is not silently exempt.
NETPLAN_KINDS = ("ethernets", "wifis", "bonds", "bridges", "vlans", "modems",
                 "tunnels")

#: The units that can hold a boot up waiting for a network that is not there.
WAIT_UNITS = (
    "systemd-networkd-wait-online.service",
    "systemd-networkd-wait-online@.service",   # the per-interface template
    "NetworkManager-wait-online.service",      # if NM is ever pulled in
)


# ==========================================================================
# Running things
# ==========================================================================
def _clean_env():
    """An environment where git and the scripts cannot read the developer's
    own configuration, and where ``python3`` is the interpreter running the
    tests - which is the one known to have PyYAML."""
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Retro Box tests",
        "GIT_AUTHOR_EMAIL": "tests@example.invalid",
        "GIT_COMMITTER_NAME": "Retro Box tests",
        "GIT_COMMITTER_EMAIL": "tests@example.invalid",
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    })
    # Never let a stray value from the developer's shell decide what these
    # tests prove.
    for leak in ("RETROBOX_ALLOW_UNPINNED", "RETROBOX_REPO_DIR"):
        env.pop(leak, None)
    return env


def _sh(script, *args, cwd=None, env_extra=None):
    """Run one of the installer's shell scripts and hand back the result.

    Invoked through ``bash`` rather than by exec bit so a failure here is about
    the script's behaviour; the exec bits are a separate test.
    """
    env = _clean_env()
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(script), *[str(a) for a in args]],
        cwd=str(cwd or ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


def _git(repo, *args):
    result = subprocess.run(
        ["git", *[str(a) for a in args]], cwd=str(repo), env=_clean_env(),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(map(str, args))}: {result.stderr}"
    return result.stdout.strip()


def _git_out(repo, *args):
    """git, but a non-zero exit is an answer rather than a failure."""
    result = subprocess.run(
        ["git", *[str(a) for a in args]], cwd=str(repo), env=_clean_env(),
        capture_output=True, text=True, timeout=60,
    )
    return result.returncode, result.stdout.strip()


# ==========================================================================
# The answer file
# ==========================================================================
def _render(out_dir, *, wifi=False, template=None):
    key = out_dir / "id_test.pub"
    key.write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH0000000000000000000000000000"
        "0000000000000 retrobox-test\n",
        encoding="utf-8",
    )
    out = out_dir / "autoinstall.yaml"
    env = _clean_env()
    env.update({
        "RETROBOX_PASSWORD": "a-long-enough-password",
        "RETROBOX_SSH_KEY": str(key),
    })
    if wifi:
        # Deliberately nasty: a colon, a hash and spaces, all of which break a
        # naive f-string into a document that is not YAML.
        env["RETROBOX_WIFI_SSID"] = "My: Home #1"
        env["RETROBOX_WIFI_PASSWORD"] = "pass word 123"
    result = subprocess.run(
        [sys.executable, str(RENDER), str(template or TEMPLATE), str(out)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return out.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def answer_files(tmp_path_factory):
    """The rendered answer file, with and without wireless."""
    base = tmp_path_factory.mktemp("render")
    plain = base / "plain"
    plain.mkdir()
    wifi = base / "wifi"
    wifi.mkdir()
    return {
        "no wireless": _render(plain),
        "with wireless": _render(wifi, wifi=True),
    }


@pytest.fixture(scope="module")
def parsed(answer_files):
    return {name: yaml.safe_load(text)["autoinstall"]
            for name, text in answer_files.items()}


def interfaces(document):
    """Every interface the answer file declares, as (kind.name, config)."""
    network = document.get("network") or {}
    found = []
    for kind in NETPLAN_KINDS:
        for name, cfg in (network.get(kind) or {}).items():
            found.append((f"{kind}.{name}", cfg))
    return found


def test_the_answer_file_is_valid_yaml(answer_files):
    # subiquity will not tell you it is not; it will fail the install on a
    # machine you have already walked away from. The specific way this has
    # broken is a placeholder mentioned twice, which lands a netplan block in
    # the middle of a comment.
    for name, text in answer_files.items():
        document = yaml.safe_load(text)
        assert isinstance(document, dict) and "autoinstall" in document, name


def test_every_interface_the_installer_writes_is_optional(parsed):
    # netplan defaults to optional: false, which it renders as
    # RequiredForOnline=yes. One of those is all systemd-networkd-wait-online
    # needs to hold up the boot of a box with nothing plugged in.
    for name, document in parsed.items():
        found = interfaces(document)
        assert found, f"{name}: the answer file declares no interfaces at all"
        for label, cfg in found:
            assert cfg.get("optional") is True, (
                f"{name}: {label} is not `optional: true`, so it would be "
                f"RequiredForOnline=yes and this box would wait for it"
            )


def test_the_wireless_fallback_is_optional_too(parsed):
    # The one most likely to be forgotten: it is injected by
    # lib/render_autoinstall.py, not written in the template, so it is not
    # visible in the file a person reads.
    wifis = [label for label, _ in interfaces(parsed["with wireless"])
             if label.startswith("wifis.")]
    assert wifis, "rendering with --wifi-ssid produced no wireless interface"
    for label in wifis:
        cfg = dict(interfaces(parsed["with wireless"]))[label]
        assert cfg.get("optional") is True, label


def test_a_box_with_a_cable_and_wifi_prefers_the_cable(parsed):
    document = parsed["with wireless"]
    metrics = {}
    for label, cfg in interfaces(document):
        overrides = cfg.get("dhcp4-overrides") or {}
        if "route-metric" in overrides:
            metrics[label] = overrides["route-metric"]
    wired = [v for k, v in metrics.items() if k.startswith("ethernets.")]
    wireless = [v for k, v in metrics.items() if k.startswith("wifis.")]
    assert wired and wireless, metrics
    assert max(wired) < min(wireless), (
        f"wireless must be the fallback, not the preference: {metrics}"
    )


def test_the_install_pins_a_release_and_then_provisions(parsed):
    # Order matters: provisioning runs the product's own scripts out of the
    # clone, so the clone has to be on the release first.
    for name, document in parsed.items():
        commands = [str(c) for c in document["late-commands"]]
        joined = "\n".join(commands)
        pin = next(i for i, c in enumerate(commands) if "pin-release.sh" in c)
        provision = next(i for i, c in enumerate(commands) if "provision.sh" in c)
        assert pin < provision, f"{name}: {joined}"


def test_the_installer_never_writes_a_wait_for_the_network_into_a_unit():
    # The two units this project authors are clean and
    # tests/test_service_units.py keeps them that way. This is the other half:
    # the installer must not reintroduce it from the side, in a drop-in or a
    # generated unit.
    for path in sorted(INSTALLER.rglob("*")):
        if not path.is_file() or path.suffix in (".iso", ".img"):
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for directive in ("Wants=network-online.target",
                              "After=network-online.target",
                              "Requires=network-online.target"):
                assert directive not in stripped, (
                    f"{path.relative_to(ROOT)}: {stripped}"
                )


def test_the_wifi_placeholder_may_only_appear_once(tmp_path):
    # The renderer replaces every occurrence, so mentioning the placeholder in
    # a comment substitutes a whole `wifis:` block into the middle of a
    # sentence and the answer file silently stops being YAML. It has happened.
    doctored = tmp_path / "doubled.template"
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "__WIFI_BLOCK__" in text
    doctored.write_text(text.replace(
        "  network:", "  # __WIFI_BLOCK__\n  network:", 1), encoding="utf-8")

    key = tmp_path / "k.pub"
    key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH0 t\n", encoding="utf-8")
    env = _clean_env()
    env.update({"RETROBOX_PASSWORD": "a-long-enough-password",
                "RETROBOX_SSH_KEY": str(key)})
    result = subprocess.run(
        [sys.executable, str(RENDER), str(doctored), str(tmp_path / "out.yaml")],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, "the renderer accepted a doubled placeholder"
    assert "__WIFI_BLOCK__" in result.stderr


# ==========================================================================
# harden-boot.sh, run for real
# ==========================================================================
def fake_root(tmp_path, *, netplan=True, optional=True, vendor_preset=False,
              enabled=True):
    """A throwaway root that looks enough like a freshly installed box."""
    root = tmp_path / "target"
    (root / "etc" / "systemd" / "system").mkdir(parents=True)
    (root / "etc" / "netplan").mkdir(parents=True)
    # harden-boot.sh refuses a root that is not an installed Linux, so that a
    # mistyped --root cannot scatter unit masks across the build machine.
    (root / "etc" / "os-release").write_text(
        'ID=ubuntu\nVERSION_ID="26.04"\n', encoding="utf-8")

    if enabled:
        wants = root / "etc" / "systemd" / "system" / "network-online.target.wants"
        wants.mkdir(parents=True)
        (wants / "systemd-networkd-wait-online.service").symlink_to(
            "/lib/systemd/system/systemd-networkd-wait-online.service")
    if vendor_preset:
        vendor = (root / "usr" / "lib" / "systemd" / "system"
                  / "network-online.target.wants")
        vendor.mkdir(parents=True)
        (vendor / "systemd-networkd-wait-online.service").symlink_to(
            "/lib/systemd/system/systemd-networkd-wait-online.service")

    if netplan:
        plan = {"network": {"version": 2, "ethernets": {
            "wired-en": {"match": {"name": "en*"}, "dhcp4": True},
        }}}
        if optional:
            plan["network"]["ethernets"]["wired-en"]["optional"] = True
        (root / "etc" / "netplan" / "00-installer-config.yaml").write_text(
            yaml.safe_dump(plan), encoding="utf-8")
    return root


def masked(root, unit):
    path = root / "etc" / "systemd" / "system" / unit
    return path.is_symlink() and os.readlink(path) == "/dev/null"


def test_it_masks_everything_that_can_wait_for_a_network(tmp_path):
    root = fake_root(tmp_path)
    result = _sh(HARDEN, "--root", root)
    assert result.returncode == 0, result.stderr + result.stdout
    for unit in WAIT_UNITS:
        assert masked(root, unit), (
            f"{unit} is not masked - a box with no cable would wait for it"
        )


def test_a_mask_is_a_symlink_to_dev_null_and_not_a_file(tmp_path):
    # systemd only treats a unit as masked when it resolves to /dev/null. An
    # empty file in the same place is a unit with no directives, which loads
    # fine and can still be pulled into the transaction.
    root = fake_root(tmp_path)
    _sh(HARDEN, "--root", root)
    path = root / "etc" / "systemd" / "system" / WAIT_UNITS[0]
    assert path.is_symlink()
    assert os.readlink(path) == "/dev/null"


def test_it_disables_them_as_well_as_masking_them(tmp_path):
    root = fake_root(tmp_path, enabled=True)
    link = (root / "etc" / "systemd" / "system" / "network-online.target.wants"
            / "systemd-networkd-wait-online.service")
    assert link.is_symlink()
    result = _sh(HARDEN, "--root", root)
    assert result.returncode == 0, result.stderr
    assert not link.exists() and not link.is_symlink(), (
        "the enablement symlink survived; a package update could rely on it"
    )


def test_a_vendor_preset_is_reported_rather_than_deleted(tmp_path):
    # /usr is not ours to edit and `systemctl disable` cannot remove a want
    # from there either. The mask is what covers it, and it says so instead of
    # pretending the file is not there.
    root = fake_root(tmp_path, vendor_preset=True)
    result = _sh(HARDEN, "--root", root)
    assert result.returncode == 0, result.stderr
    vendor = (root / "usr" / "lib" / "systemd" / "system"
              / "network-online.target.wants"
              / "systemd-networkd-wait-online.service")
    assert vendor.is_symlink(), "it deleted a file under /usr"
    assert "vendor preset" in result.stdout
    assert masked(root, "systemd-networkd-wait-online.service")


def test_running_it_twice_is_the_same_as_running_it_once(tmp_path):
    # It runs from provision.sh, and the whole installer is built to be
    # re-runnable by hand when something has to be recovered.
    root = fake_root(tmp_path)
    first = _sh(HARDEN, "--root", root)
    assert first.returncode == 0, first.stderr
    second = _sh(HARDEN, "--root", root)
    assert second.returncode == 0, second.stderr
    for unit in WAIT_UNITS:
        assert masked(root, unit)
    assert (root / "etc" / "cloud" / "cloud-init.disabled").is_file()


def test_it_switches_cloud_init_off(tmp_path):
    # cloud-init has nothing left to do once the install has finished, but it
    # runs four ordered stages on every boot for ever and keeps the NoCloud
    # datasource live, so a stray FAT stick can influence how the box comes up.
    root = fake_root(tmp_path)
    result = _sh(HARDEN, "--root", root)
    assert result.returncode == 0, result.stderr
    flag = root / "etc" / "cloud" / "cloud-init.disabled"
    assert flag.is_file(), "cloud-init is still enabled on every box we ship"
    # cloud-init only looks for the file's existence, but a bare empty file in
    # /etc with no explanation is how a future person deletes it by accident.
    assert "cloud-init" in flag.read_text(encoding="utf-8")


def test_it_will_not_switch_cloud_init_off_if_nothing_else_configures_the_network(tmp_path):
    """The one case where disabling cloud-init would be worse than the wait.

    If /etc/netplan declares no interface at all then something writes the
    network configuration at first boot, and the only realistic candidate is
    cloud-init. Disabling it there is a box that never has a network again -
    strictly worse than losing a few seconds of boot, so it is skipped and
    says why.
    """
    root = fake_root(tmp_path, netplan=False)
    result = _sh(HARDEN, "--root", root)
    assert result.returncode == 0, result.stderr
    assert not (root / "etc" / "cloud" / "cloud-init.disabled").exists()
    assert "NOT switching cloud-init off" in result.stdout
    # ...and the masking still happened. That part is never conditional.
    for unit in WAIT_UNITS:
        assert masked(root, unit)


def test_it_refuses_a_root_that_is_not_an_installed_linux(tmp_path):
    # A mistyped --root, or running it on the build machine by mistake, must
    # not scatter unit masks across somebody's laptop.
    root = tmp_path / "not-a-system"
    (root / "etc").mkdir(parents=True)
    result = _sh(HARDEN, "--root", root)
    assert result.returncode != 0
    assert "os-release" in (result.stdout + result.stderr)
    assert not (root / "etc" / "systemd").exists()


def test_it_names_a_netplan_interface_that_is_not_optional(tmp_path):
    # Not fatal - with the units masked there is nothing left to act on
    # RequiredForOnline=yes - but it must not be silent, because this is
    # exactly what the dashboard's Network page writes at runtime.
    root = fake_root(tmp_path, optional=False)
    result = _sh(HARDEN, "--root", root)
    assert result.returncode == 0, result.stderr
    assert "required-for-online" in result.stdout
    assert "wired-en" in result.stdout


def test_the_answer_files_own_netplan_passes_the_check_the_box_runs(tmp_path, parsed):
    """End to end: what the installer ships satisfies what the box verifies.

    The `network:` section of the answer file becomes the target's netplan, so
    writing it out and handing it to harden-boot.sh asks the real question -
    would a box built from this document have an interface that something can
    wait for?
    """
    for name, document in parsed.items():
        root = tmp_path / name.replace(" ", "-")
        (root / "etc" / "systemd" / "system").mkdir(parents=True)
        (root / "etc" / "netplan").mkdir(parents=True)
        (root / "etc" / "os-release").write_text("ID=ubuntu\n", encoding="utf-8")
        (root / "etc" / "netplan" / "00-installer-config.yaml").write_text(
            yaml.safe_dump({"network": document["network"]}), encoding="utf-8")

        result = _sh(HARDEN, "--root", root)
        assert result.returncode == 0, result.stderr
        assert "required-for-online" not in result.stdout, (
            f"{name}: the answer file this installer ships declares an "
            f"interface that a boot can wait for:\n{result.stdout}"
        )
        assert "is optional: true" in result.stdout, result.stdout
        assert (root / "etc" / "cloud" / "cloud-init.disabled").is_file()


#: A line that *runs* harden-boot.sh, as opposed to one that mentions it in a
#: comment, tests for it with [[ -x ]], or names it in an error message. The
#: difference is the whole point: an installer that talks about masking a unit
#: and an installer that masks it are not the same installer.
_RUNS_HARDEN = re.compile(
    r'^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"?[^"\s]*installer/harden-boot\.sh"?(?:\s|$)'
)


def test_provisioning_runs_it():
    lines = [line.strip()
             for line in PROVISION.read_text(encoding="utf-8").splitlines()]
    invocations = [line for line in lines
                   if not line.startswith("#") and _RUNS_HARDEN.match(line)]
    assert invocations, (
        "provision.sh does not run installer/harden-boot.sh; every box off "
        "this build would wait for a network it may not have"
    )


def test_provisioning_aborts_if_the_mask_did_not_land():
    # A verification that warns is a verification nobody reads. This one has to
    # kill the install, because the alternative is finding out from a customer.
    text = PROVISION.read_text(encoding="utf-8")
    _, _, after = text.partition("Verifying nothing will wait for the network")
    assert after, "provision.sh no longer verifies the mask"
    block = after[:1400]
    assert "systemd-networkd-wait-online.service" in block
    assert "/dev/null" in block
    assert "die " in block, "an unmasked box is warned about, not refused"


# ==========================================================================
# The two units this project writes
# ==========================================================================
def test_no_unit_this_project_ships_waits_for_the_network():
    """Audited across every unit in scripts/, not just the two known ones.

    The units that actually pull network-online.target in on this box are
    third-party - smbd and wsdd, installed and enabled by
    scripts/setup_lan_share.sh - which is why masking is the fix. That is no
    reason to let ours drift.
    """
    units = sorted(SCRIPTS.glob("*.service"))
    assert units, "no unit files found under scripts/"
    for unit in units:
        for line in unit.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() in ("After", "Wants", "Requires", "BindsTo",
                               "Requisite", "PartOf"):
                assert "network-online" not in value, (
                    f"{unit.name}: {stripped}"
                )


# ==========================================================================
# pin-release.sh, run against real repositories
# ==========================================================================
def make_origin(tmp_path, *, version="2.0.0", tags=(), name="origin"):
    """A repository that looks like this one: a version, and some tags.

    Each tag gets its own commit. Piling several tags onto one commit is a
    different situation with its own answer (see the two-tags test below) and
    it would make `git describe --exact-match` here a coin toss.
    """
    origin = tmp_path / name
    (origin / "retrobox").mkdir(parents=True)
    (origin / "retrobox" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    _git(origin, "init", "-q", "-b", "main", ".")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "the product")
    for index, tag in enumerate(tags):
        (origin / "CHANGELOG.md").write_text(
            f"release {index}: {tag}\n", encoding="utf-8")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", f"cut {tag}")
        _git(origin, "tag", tag)
    return origin


def make_clone(tmp_path, origin, name="clone"):
    clone = tmp_path / name
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    return clone


def test_it_refuses_to_build_a_box_when_there_is_no_release_tag(tmp_path):
    """The whole point. This repository has had zero tags the entire time.

    Left as a warning, every stick baked produces a box running whatever main
    happened to be at clone time - unreviewed code, sold, then unreachable.
    """
    clone = make_clone(tmp_path, make_origin(tmp_path, tags=()))
    before = _git(clone, "rev-parse", "HEAD")

    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)

    assert result.returncode != 0, (
        "pin-release.sh built a box from main with no release tag:\n"
        + result.stdout
    )
    assert _git(clone, "rev-parse", "HEAD") == before, "it moved the clone anyway"
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_the_refusal_tells_the_operator_what_to_do_about_it(tmp_path):
    # A build that stops without saying why is a build somebody works around.
    clone = make_clone(tmp_path, make_origin(tmp_path, tags=()))
    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    said = result.stdout + result.stderr
    assert "git tag" in said
    assert "git push" in said
    assert "Release" in said, "it does not mention publishing a GitHub Release"


def test_a_release_tag_is_checked_out(tmp_path):
    clone = make_clone(tmp_path, make_origin(tmp_path, version="2.0.0",
                                             tags=("v2.0.0",)))
    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(clone, "describe", "--tags", "--exact-match") == "v2.0.0"


def test_the_clone_it_leaves_behind_is_one_the_updater_can_work_with(tmp_path):
    """Read back with the exact commands retrobox/updater.py runs.

    Updater._current_ref is `git describe --tags --exact-match`, and its first
    step of any update is `git fetch --tags --force` - which needs a remote
    named origin or it fetches nothing and reports success for ever.
    """
    clone = make_clone(tmp_path, make_origin(tmp_path, version="2.0.0",
                                             tags=("v1.0.0", "v2.0.0")))
    assert _sh(PIN, "--repo-dir", clone, cwd=tmp_path).returncode == 0

    assert (clone / ".git").is_dir(), "not a real git clone"
    code, ref = _git_out(clone, "describe", "--tags", "--exact-match")
    assert code == 0 and ref == "v2.0.0", (
        "the updater would see a bare commit id where a version should be"
    )
    assert _git(clone, "remote", "get-url", "origin")
    # The fetch the updater runs before every update has to work here.
    code, _ = _git_out(clone, "fetch", "--tags", "--force")
    assert code == 0
    # And the tag list came with it, so an update has something to check out.
    assert "v2.0.0" in _git(clone, "tag", "-l").split()


def test_two_tags_on_one_commit_are_pinned_but_flagged(tmp_path):
    """Not an error - the code is identical - but not silent either.

    `git describe --exact-match` answers with one of them, and that is the
    version the box will report for the rest of its life. Somebody should know
    which one before it ships.
    """
    origin = make_origin(tmp_path, version="2.0.0", tags=("v2.0.0",))
    _git(origin, "tag", "v2.0.1")            # same commit, later number
    clone = make_clone(tmp_path, origin)

    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    said = result.stdout + result.stderr
    assert "not the only tag on this commit" in said, said
    # v2.0.1 was selected as newest, and __version__ still says 2.0.0, so this
    # build is correctly refused on the version check as well.
    assert result.returncode != 0
    assert "__version__" in said


def test_it_picks_the_newest_release_by_version_and_not_alphabetically(tmp_path):
    # "v1.0.9" sorts after "v1.0.10" as text. A box pinned to 1.0.9 by a
    # careless sort is a box shipped a release behind.
    clone = make_clone(tmp_path, make_origin(
        tmp_path, version="1.0.10", tags=("v1.0.2", "v1.0.9", "v1.0.10")))
    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "v1.0.10" in _git(clone, "tag", "--points-at", "HEAD").split()


def test_a_release_candidate_is_not_a_release(tmp_path):
    """v2.0.0-rc1 is not something to leave in somebody's living room.

    The version deliberately AGREES with the tag here, so the later
    tag-vs-__version__ check cannot be what rejects this. The only thing that
    can refuse it is the rule that a release tag is exactly vX.Y.Z.
    """
    clone = make_clone(tmp_path, make_origin(
        tmp_path, version="2.0.0-rc1", tags=("v2.0.0-rc1",)))
    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    assert result.returncode != 0, (
        "a release candidate was pinned into a shippable box:\n" + result.stdout
    )
    said = result.stdout + result.stderr
    assert "v2.0.0-rc1" in said
    assert "none of them is a release" in said, said
    # And it did not quietly check it out on the way to failing: a pinned clone
    # is detached, so still being on a branch means nothing was checked out.
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main", (
        "the clone was moved onto the release candidate anyway"
    )


def test_a_tag_without_the_v_prefix_is_not_a_release_either(tmp_path):
    # The updater builds the ref it checks out as "v" + version, so a tag
    # named 2.0.0 is unreachable to it and a box pinned to one would roll
    # itself back on the first update. Again the version agrees, so only the
    # naming rule can reject this.
    clone = make_clone(tmp_path, make_origin(
        tmp_path, version="2.0.0", tags=("2.0.0",)))
    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    assert result.returncode != 0, result.stdout


def test_it_refuses_when_the_tag_and_the_code_disagree_about_the_version(tmp_path):
    # Every box decides whether to update by comparing its own __version__
    # against the newest published release. Ship those two disagreeing and the
    # box either reinstalls the same release for ever or never updates at all.
    clone = make_clone(tmp_path, make_origin(tmp_path, version="1.9.0",
                                             tags=("v2.0.0",)))
    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    assert result.returncode != 0, result.stdout
    said = result.stdout + result.stderr
    assert "__version__" in said and "v2.0.0" in said


def test_a_bench_box_can_still_be_built_but_is_told_it_is_not_sellable(tmp_path):
    clone = make_clone(tmp_path, make_origin(tmp_path, tags=()))
    result = _sh(PIN, "--repo-dir", clone, "--allow-unpinned", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    said = result.stdout + result.stderr
    assert "DO NOT SELL" in said
    assert "unpinned" in said.lower()


def test_the_escape_hatch_is_not_the_default(tmp_path):
    # If --allow-unpinned were ever the default the refusal would be theatre.
    clone = make_clone(tmp_path, make_origin(tmp_path, tags=()))
    assert _sh(PIN, "--repo-dir", clone, cwd=tmp_path).returncode != 0


def test_it_refuses_a_directory_that_is_not_a_clone(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = _sh(PIN, "--repo-dir", plain, cwd=tmp_path)
    assert result.returncode != 0
    assert "git clone" in (result.stdout + result.stderr)


def test_it_refuses_a_clone_with_no_origin(tmp_path):
    # `git fetch --tags` with no remote named origin fetches nothing and
    # succeeds, so every future update on that box would silently do nothing.
    clone = make_clone(tmp_path, make_origin(tmp_path, tags=("v2.0.0",)))
    _git(clone, "remote", "remove", "origin")
    result = _sh(PIN, "--repo-dir", clone, cwd=tmp_path)
    assert result.returncode != 0
    assert "origin" in (result.stdout + result.stderr)


def test_the_repo_dir_can_come_from_the_environment(tmp_path):
    # This is how the autoinstall late-command passes it in.
    clone = make_clone(tmp_path, make_origin(tmp_path, tags=("v2.0.0",)))
    result = _sh(PIN, cwd=tmp_path, env_extra={"RETROBOX_REPO_DIR": str(clone)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(clone, "describe", "--tags", "--exact-match") == "v2.0.0"


# ==========================================================================
# Hygiene - these scripts run as root on somebody else's hardware
# ==========================================================================
def installer_scripts():
    return sorted(INSTALLER.glob("*.sh"))


@pytest.mark.parametrize("script", installer_scripts(), ids=lambda p: p.name)
def test_every_installer_script_parses(script):
    result = subprocess.run(["bash", "-n", str(script)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", installer_scripts(), ids=lambda p: p.name)
def test_every_installer_script_is_executable(script):
    # curtin execs these directly out of the clone; a missing exec bit is a
    # failed install on hardware you have already walked away from.
    assert script.stat().st_mode & stat.S_IXUSR, f"{script.name} is not executable"


@pytest.mark.parametrize("script", installer_scripts(), ids=lambda p: p.name)
def test_shellcheck_is_happy(script):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(script)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout
