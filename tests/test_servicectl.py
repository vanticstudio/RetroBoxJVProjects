"""Restarting things, on a box anyone on the LAN can reach.

The dashboard has no authentication, and restart/reboot being available to
anyone on the home network is an accepted trade for a television. What is NOT
accepted is a sudoers rule broad enough to be turned into something worse than
a reboot, so the set of commands this module can run is closed and every one
of them is spelled out in full.
"""

import pytest

from retrobox import servicectl


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(servicectl, "_run", runner)
    return calls


# ==========================================================================
# The closed set
# ==========================================================================
def test_the_actions_are_a_fixed_list():
    assert set(servicectl.ACTIONS) == {
        "restart-tv", "restart-dashboard", "reboot", "shutdown",
    }


@pytest.mark.parametrize(
    "action",
    ["", "rm", "restart", "systemctl", "restart-tv; rm -rf /", "RESTART-TV",
     "../reboot", "restart-sshd", None, 42],
)
def test_anything_not_on_the_list_is_refused(ran, action):
    with pytest.raises(servicectl.ServiceError):
        servicectl.run(action)
    assert ran == [], "it tried to run something anyway"


def test_every_action_maps_to_a_fully_spelled_out_command():
    # Nothing here may be assembled from user input at call time.
    for action, argv in servicectl.COMMANDS.items():
        assert argv[0] == "sudo", action
        assert all(isinstance(part, str) for part in argv), action
        assert not any("*" in part for part in argv), action


# ==========================================================================
# What each one does
# ==========================================================================
def test_restarting_the_tv_restarts_only_the_tv(ran):
    servicectl.run("restart-tv")
    assert ran == [["sudo", "-n", "systemctl", "restart", "retrobox.service"]]


def test_rebooting_is_a_reboot(ran):
    servicectl.run("reboot")
    assert ran == [["sudo", "-n", "systemctl", "reboot"]]


def test_shutting_down_still_works(ran):
    servicectl.run("shutdown")
    assert ran == [["sudo", "-n", "systemctl", "poweroff"]]


def test_restarting_the_dashboard_does_not_wait_for_itself(ran):
    # The process running this command is the one being restarted. Without
    # --no-block systemctl waits for a restart that kills it first, and the
    # browser gets a dead connection instead of an answer.
    servicectl.run("restart-dashboard")
    assert ran == [[
        "sudo", "-n", "systemctl", "--no-block", "restart", "retrobox-web.service",
    ]]


def test_a_command_that_fails_says_so_rather_than_pretending(monkeypatch):
    monkeypatch.setattr(
        servicectl, "_run", lambda cmd, **kw: (1, "sudo: a password is required")
    )
    with pytest.raises(servicectl.ServiceError) as caught:
        servicectl.run("reboot")
    assert "password" in str(caught.value)


def test_sudo_is_never_allowed_to_sit_waiting_for_a_password(ran):
    # -n on every one of them: a prompt on a headless box is a hung request.
    for action in servicectl.ACTIONS:
        ran.clear()
        servicectl.run(action)
        assert "-n" in ran[0], action


# ==========================================================================
# The rule that gets installed
# ==========================================================================
def test_the_sudoers_rule_names_every_command_it_allows():
    rule = servicectl.sudoers_rule("retro")
    for argv in servicectl.COMMANDS.values():
        wanted = " ".join(argv[2:])          # drop "sudo -n"
        assert wanted in rule, wanted


def test_the_sudoers_rule_never_grants_systemctl_in_general():
    rule = servicectl.sudoers_rule("retro")
    for line in rule.splitlines():
        if "systemctl" not in line:
            continue
        assert "systemctl *" not in line
        assert not line.rstrip().endswith("systemctl")


def test_no_systemctl_rule_carries_a_wildcard():
    # The service rules name their units exactly. A wildcard on systemctl is
    # the difference between a reboot button and control of the whole machine.
    for line in servicectl.sudoers_rule("retro").splitlines():
        if "systemctl" in line and not line.lstrip().startswith("#"):
            assert "*" not in line, line


# Every wildcard in the rule, deliberately enumerated. Each is a fixed
# subcommand taking one argument that is checked in Python against a real
# whitelist first - the machine's timezone list, its own interface list, a
# hostname pattern, or a number this box chose. Anything not on this list
# appearing in the rule is a new hole and the test says so.
PERMITTED_WILDCARDS = (
    "timedatectl set-timezone *",
    "netplan try --timeout=*",
    "iw dev * scan",
    "hostnamectl set-hostname *",
)


def test_every_wildcard_in_the_rule_is_one_we_chose():
    for line in servicectl.sudoers_rule("retro").splitlines():
        if "*" not in line or line.lstrip().startswith("#"):
            continue
        for fragment in line.split(","):
            fragment = fragment.strip().rstrip("\\").strip()
            if "*" not in fragment:
                continue
            assert any(fragment.endswith(w) for w in PERMITTED_WILDCARDS), fragment


def test_the_netplan_files_are_named_exactly_never_wildcarded():
    # The file contents carry the wifi password. A wildcard on the path would
    # let anyone on the LAN write any file in /etc/netplan.
    for line in servicectl.sudoers_rule("retro").splitlines():
        if "/etc/netplan" not in line or line.lstrip().startswith("#"):
            continue
        assert "/etc/netplan/*" not in line
        assert "*" not in line.split("/etc/netplan")[1].split(",")[0]


def test_writing_a_netplan_file_never_takes_the_content_as_an_argument():
    # tee and cat name the target and nothing else; the document goes on stdin.
    rule = servicectl.sudoers_rule("retro")
    for path in ("/etc/netplan/90-retrobox-wired.yaml",
                 "/etc/netplan/91-retrobox-wifi.yaml"):
        assert f"tee {path}" in rule
        assert f"chmod 600 {path}" in rule


def test_a_timezone_the_box_does_not_know_is_refused(ran):
    with pytest.raises(servicectl.ServiceError):
        servicectl.set_timezone("Mars/Olympus", allowed=["Europe/London"])
    assert ran == []


@pytest.mark.parametrize(
    "zone",
    ["", "../../etc/passwd", "Europe/London; rm -rf /", "-", "x" * 300, None,
     "Europe/London\x00"],
)
def test_a_timezone_that_is_not_a_timezone_never_reaches_sudo(ran, zone):
    with pytest.raises(servicectl.ServiceError):
        servicectl.set_timezone(zone, allowed=["Europe/London"])
    assert ran == []


def test_a_known_timezone_is_set(ran):
    servicectl.set_timezone("Europe/London", allowed=["Europe/London", "UTC"])
    assert ran == [["sudo", "-n", "timedatectl", "set-timezone", "Europe/London"]]


def test_the_sudoers_rule_refuses_a_username_it_cannot_vouch_for():
    # It is written into a file that grants privilege. A name with a space or
    # a comma in it changes what that file means.
    for bad in ("", "root ALL=(ALL) NOPASSWD: ALL", "a b", "a,b", "a\nb", "x" * 200):
        with pytest.raises(ValueError):
            servicectl.sudoers_rule(bad)


def test_the_sudoers_rule_covers_both_places_systemctl_lives():
    # /usr/bin on modern distros, /bin on older ones. sudo matches the literal
    # path, so a rule with only one of them silently fails on the other.
    rule = servicectl.sudoers_rule("retro")
    assert "/usr/bin/systemctl" in rule and "/bin/systemctl" in rule
