"""The systemd units, which ship to customers and cannot be run from here.

These are read as configuration rather than grepped as text: the questions are
about what the unit *means* - which port, which capability, what it waits for -
not how it happens to be spelled. Nothing here can substitute for booting a
real box, but each one catches a specific way the box arrives broken.
"""

import ast
import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PACKAGE = ROOT / "retrobox"


def unit(name):
    # strict=False because systemd tolerates repeated keys; comments starting
    # with '#' are handled by configparser's default comment prefixes.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str            # systemd keys are case-sensitive
    parser.read(SCRIPTS / name, encoding="utf-8")
    return parser


@pytest.fixture
def web():
    return unit("retrobox-web.service")


@pytest.fixture
def tv():
    return unit("retrobox.service")


# -- the address a customer types -----------------------------------------
def launch_port(exec_start):
    """The --port the unit actually launches with, as a number.

    Split into arguments rather than matched as a substring: "--port 80" is a
    substring of "--port 8080", so the obvious check passes on exactly the
    change it exists to catch.
    """
    parts = exec_start.split()
    return int(parts[parts.index("--port") + 1])


def test_the_dashboard_is_launched_on_port_80(web):
    # The whole point of the access layer: no port number in the URL.
    assert launch_port(web["Service"]["ExecStart"]) == 80


def test_the_dashboard_can_bind_a_low_port_without_being_root(web):
    # Remove this and the unit cannot bind 80 as an ordinary user, so the
    # documented address stops working on every box.
    assert web["Service"]["AmbientCapabilities"] == "CAP_NET_BIND_SERVICE"


def test_the_dashboard_does_not_run_as_root(web):
    # The other way somebody might "fix" port 80. A network-facing process
    # running as root to save four characters in a URL is a bad trade.
    user = web["Service"]["User"]
    assert user and user != "root"


# -- the box still has a way to reach root --------------------------------
# Every privileged thing this box does - restart the TV, reboot, set the
# clock, write a netplan file, scan for wifi - is `sudo` shelling out to a
# narrowly named command. sudo reaches root through the setuid bit on
# /usr/bin/sudo, and there are systemd settings that quietly make that
# impossible. They read like straightforward hardening and they turn every
# button on the System and Network pages into an error message on a box
# nobody can SSH into. These tests are here so that never ships.
UNITS_THAT_SHELL_OUT_TO_SUDO = ("retrobox-web.service", "retrobox.service")

#: What systemd counts as "on". A unit saying NoNewPrivileges=true is as
#: broken as one saying yes.
_TRUE = {"yes", "true", "on", "1"}

#: Settings that switch the kernel's no_new_privs flag on whether or not the
#: unit ever mentions NoNewPrivileges, because systemd.exec(5) documents them
#: as implying it. Someone hardening this unit later will reach for one of
#: these, and the failure - sudo refusing to run at all - looks nothing like
#: the setting that caused it.
IMPLY_NO_NEW_PRIVILEGES = (
    "DynamicUser",
    "LockPersonality",
    "MemoryDenyWriteExecute",
    "PrivateDevices",
    "ProtectClock",
    "ProtectHostname",
    "ProtectKernelLogs",
    "ProtectKernelModules",
    "ProtectKernelTunables",
    "RestrictAddressFamilies",
    "RestrictNamespaces",
    "RestrictSUIDSGID",
    "SystemCallArchitectures",
    "SystemCallFilter",
)

#: Not a no_new_privs setting, but it breaks sudo just as completely: inside a
#: user namespace the uid 0 sudo hands you is not the machine's root.
BREAKS_SUDO_TOO = ("PrivateUsers",)

_NETWORK_TOOLS = {"netplan", "iw", "nmcli", "ip", "wpa_supplicant", "dhclient"}


def sudo_commands_the_code_runs():
    """Every ``sudo ...`` command line written anywhere in the package.

    Read out of the source with ``ast`` rather than copied into this file. The
    privileged table has grown before and will grow again; a copy here would
    quietly stop describing the box the first time somebody adds to it, which
    is exactly when this check needs to be right.

    Only the fixed head of each command is collected - the tail is often a
    variable (an interface name, a timezone) and none of it matters here.
    """
    found = set()
    for source in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            words = []
            for element in node.elts:
                if not isinstance(element, ast.Constant):
                    break
                if not isinstance(element.value, str):
                    break
                words.append(element.value)
            if words and words[0] == "sudo":
                found.add(tuple(words))
    return sorted(found)


def capabilities_those_commands_need():
    """The capabilities root must still be able to hold, derived from above.

    CAP_SETUID and CAP_SETGID are the floor: sudo becomes root by changing the
    uid and rewriting the group list, and it can do neither without them - the
    setuid bit gets it a uid 0 that cannot finish the job. Everything else
    depends on what the box actually runs.
    """
    needed = {"CAP_SETUID", "CAP_SETGID"}
    words = {word for command in sudo_commands_the_code_runs() for word in command}
    if words & _NETWORK_TOOLS:
        # Bringing an interface up, writing an address, asking the card to
        # scan - all of it is CAP_NET_ADMIN, and scanning wants CAP_NET_RAW.
        needed |= {"CAP_NET_ADMIN", "CAP_NET_RAW"}
    return needed


def capability_ceiling(section):
    """``CapabilityBoundingSet=`` as (is_a_blocklist, capabilities), or None.

    systemd reads a leading ``~`` as "everything except these", which means
    the same line can be the most permissive or the most restrictive thing in
    the file depending on one character.
    """
    raw = section.get("CapabilityBoundingSet")
    if raw is None:
        return None
    raw = raw.strip()
    blocklist = raw.startswith("~")
    return blocklist, {word for word in raw.lstrip("~").split() if word}


def test_the_box_really_does_depend_on_sudo_for_everything_privileged():
    # The premise the next three tests rest on. If this ever fails it is good
    # news - it means privilege stopped going through sudo - but the hardening
    # below was chosen for sudo's sake and should be revisited, not trusted.
    assert sudo_commands_the_code_runs(), "nothing under retrobox/ runs sudo any more"


@pytest.mark.parametrize("name", UNITS_THAT_SHELL_OUT_TO_SUDO)
def test_a_unit_that_needs_sudo_does_not_set_no_new_privileges(name):
    # no_new_privs makes execve ignore the setuid bit, so sudo does not fail
    # on the sudoers rule - it never becomes root at all, and says so:
    # "sudo: The 'no new privileges' flag is set, which prevents sudo from
    # running as root". Every privileged feature dies at once.
    section = unit(name)["Service"]
    assert section.get("NoNewPrivileges", "no").strip().lower() not in _TRUE


@pytest.mark.parametrize("name", UNITS_THAT_SHELL_OUT_TO_SUDO)
def test_a_unit_that_needs_sudo_avoids_the_settings_that_turn_it_on_quietly(name):
    section = unit(name)["Service"]
    for setting in IMPLY_NO_NEW_PRIVILEGES + BREAKS_SUDO_TOO:
        assert setting not in section, (
            f"{name} sets {setting}=, which stops sudo reaching root. "
            f"Harden this box somewhere that is not the path to root."
        )


@pytest.mark.parametrize("name", UNITS_THAT_SHELL_OUT_TO_SUDO)
def test_the_capability_ceiling_leaves_root_able_to_do_the_work(name):
    # CapabilityBoundingSet is a ceiling on every process in the unit, and it
    # is applied *after* a setuid exec too: a bounding set of one capability
    # hands sudo a root that holds one capability, which is not root in any
    # useful sense. Nothing here objects to a ceiling - it objects to one that
    # cuts below what the commands in the code actually need.
    ceiling = capability_ceiling(unit(name)["Service"])
    if ceiling is None:
        # No ceiling at all, which is the state this unit ships in: root keeps
        # the full set and the sudoers table is what narrows it.
        return
    blocklist, listed = ceiling
    needed = capabilities_those_commands_need()
    missing = sorted(listed & needed) if blocklist else sorted(needed - listed)
    assert not missing, (
        f"{name} caps the bounding set below what its own sudo commands need; "
        f"root would be missing {', '.join(missing)}"
    )


# -- nothing about the network may hold the box up -------------------------
def test_the_dashboard_does_not_wait_for_the_network_to_be_online(web):
    # network-online.target pulls in a wait-online service that blocks for its
    # full timeout on a box with nothing plugged in. Listening on 0.0.0.0
    # needs no route, so that would be minutes of no dashboard for nothing.
    section = web["Unit"]
    for key in ("After", "Wants", "Requires"):
        assert "network-online" not in section.get(key, ""), key


def test_the_television_does_not_wait_for_the_network_at_all(tv):
    # The TV comes first, always. It plays files off a local disk; a box with
    # no network must still be a television.
    section = tv["Unit"]
    joined = " ".join(section.get(k, "") for k in ("After", "Wants", "Requires"))
    assert "network" not in joined


# -- the two units still agree with each other ----------------------------
def environment(name):
    """Every Environment= line in a unit, as a dict.

    Read from the file rather than through configparser, which keeps only the
    last value when a key repeats - and these units set Environment twice.
    """
    out = {}
    for line in (SCRIPTS / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("Environment=") and "=" in line[12:]:
            key, _, value = line[len("Environment="):].partition("=")
            out[key] = value
    return out


def test_both_units_look_in_the_same_runtime_directory():
    # They find each other through the status file and control socket under
    # XDG_RUNTIME_DIR. If these ever disagree, the dashboard goes deaf and the
    # TV never hears a button press from the browser.
    web = environment("retrobox-web.service")
    tv = environment("retrobox.service")
    assert web["XDG_RUNTIME_DIR"] == tv["XDG_RUNTIME_DIR"]
    assert web["XDG_RUNTIME_DIR"], "neither unit sets one at all"
