"""Changing the network of a machine, through a page served over that network.

Everything here is written against one fact: a bad value does not produce an
error message, it produces a box that has vanished from the LAN and can only
be recovered with a keyboard and a monitor - which is the exact situation this
whole product exists to avoid.

So values are rejected before anything is written, the configuration is built
as data and serialised rather than formatted into a string, and no field from
the network ever becomes a command-line argument.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from retrobox import netconf
from retrobox.netconf import NetworkError


def fake_command(code, printed="", warned=""):
    """A stand-in for one command, with the two streams kept apart.

    Real commands write their answer to stdout and their grumbling to stderr.
    Anything that glues the two together records the grumbling as data, which
    is the whole point of the tests below.
    """
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(list(argv), code, printed, warned)

    return run


#: What every single sudo on this box prints once the hostname has been
#: changed without /etc/hosts being changed with it. The command still works
#: and still exits 0 - it just says this first, every time, for ever.
SUDO_CANNOT_RESOLVE_HOST = (
    "sudo: unable to resolve host retrobox: Name or service not known\n"
)


# ==========================================================================
# Validating what a person typed
# ==========================================================================
def test_a_sensible_static_configuration_is_accepted():
    plan = netconf.static_plan(
        interface="eth0", address="192.168.1.50", prefix=24,
        gateway="192.168.1.1", dns=["1.1.1.1", "8.8.8.8"],
    )
    parsed = yaml.safe_load(plan)
    eth = parsed["network"]["ethernets"]["eth0"]
    assert eth["addresses"] == ["192.168.1.50/24"]
    assert eth["nameservers"]["addresses"] == ["1.1.1.1", "8.8.8.8"]
    assert eth["dhcp4"] is False


@pytest.mark.parametrize(
    "address", ["", "not an address", "192.168.1", "192.168.1.999", "1.2.3.4.5",
                "192.168.1.50/24", " ", None, "::1", "0.0.0.0"]
)
def test_an_address_that_is_not_an_address_is_refused(address):
    with pytest.raises(NetworkError):
        netconf.static_plan(interface="eth0", address=address, prefix=24,
                            gateway="192.168.1.1", dns=[])


@pytest.mark.parametrize("prefix", [-1, 0, 31, 32, 33, "twenty four", None, 1.5])
def test_a_netmask_that_cannot_hold_a_network_is_refused(prefix):
    with pytest.raises(NetworkError):
        netconf.static_plan(interface="eth0", address="192.168.1.50", prefix=prefix,
                            gateway="192.168.1.1", dns=[])


def test_a_gateway_outside_the_subnet_is_refused():
    # The classic way to lose a box: an address on one network and a gateway
    # on another. It applies cleanly and nothing can reach it.
    with pytest.raises(NetworkError) as caught:
        netconf.static_plan(interface="eth0", address="192.168.1.50", prefix=24,
                            gateway="10.0.0.1", dns=[])
    assert "same network" in str(caught.value).lower()


def test_a_gateway_that_is_the_box_itself_is_refused():
    with pytest.raises(NetworkError):
        netconf.static_plan(interface="eth0", address="192.168.1.1", prefix=24,
                            gateway="192.168.1.1", dns=[])


@pytest.mark.parametrize("address", ["192.168.1.0", "192.168.1.255"])
def test_the_network_and_broadcast_addresses_are_refused(address):
    with pytest.raises(NetworkError):
        netconf.static_plan(interface="eth0", address=address, prefix=24,
                            gateway="192.168.1.1", dns=[])


@pytest.mark.parametrize("server", ["banana", "192.168.1.999", "1.1.1.1;evil", ""])
def test_a_dns_server_that_is_not_an_address_is_refused(server):
    with pytest.raises(NetworkError):
        netconf.static_plan(interface="eth0", address="192.168.1.50", prefix=24,
                            gateway="192.168.1.1", dns=[server])


def test_no_gateway_at_all_is_allowed():
    # A box on an isolated segment is a legitimate thing to have.
    plan = yaml.safe_load(netconf.static_plan(
        interface="eth0", address="192.168.1.50", prefix=24, gateway=None, dns=[],
    ))
    assert "routes" not in plan["network"]["ethernets"]["eth0"]


@pytest.mark.parametrize(
    "interface", ["", "eth0; rm -rf /", "../../etc", "eth 0", "x" * 40, None, "-eth0"]
)
def test_an_interface_name_that_is_not_one_is_refused(interface):
    with pytest.raises(NetworkError):
        netconf.static_plan(interface=interface, address="192.168.1.50", prefix=24,
                            gateway="192.168.1.1", dns=[])


def test_dhcp_is_the_simple_case():
    plan = yaml.safe_load(netconf.dhcp_plan(interface="eth0"))
    assert plan["network"]["ethernets"]["eth0"]["dhcp4"] is True


# ==========================================================================
# Wifi, where the hostile input lives
# ==========================================================================
def test_a_wifi_plan_carries_the_network_and_the_password():
    plan = yaml.safe_load(netconf.wifi_plan(
        interface="wlan0", ssid="Home Network", password="hunter22",
    ))
    wifi = plan["network"]["wifis"]["wlan0"]
    assert wifi["access-points"]["Home Network"]["password"] == "hunter22"
    assert wifi["dhcp4"] is True


@pytest.mark.parametrize(
    "ssid",
    [
        'my"net',
        "my'net",
        "net; rm -rf /",
        "net`whoami`",
        "net$(id)",
        "net\nmore: yes",
        "net\\escape",
        "  spaced  ",
        "네트워크",
        "net#hash",
        "{brace}",
        "- dash",
    ],
)
def test_an_ssid_full_of_metacharacters_round_trips_exactly(ssid):
    # These are all legitimate in a home network name. The configuration is
    # built as data and serialised, never formatted into a string, so quoting
    # is the serialiser's problem and it gets it right.
    plan = netconf.wifi_plan(interface="wlan0", ssid=ssid, password="pw12345678")
    parsed = yaml.safe_load(plan)
    assert list(parsed["network"]["wifis"]["wlan0"]["access-points"]) == [ssid]


@pytest.mark.parametrize(
    "password",
    ['pass"word', "pass'word", "pw; reboot", "password`id`", "password$(id)",
     "password\nkey: x", "password\\slash", "12345678"],
)
def test_a_password_full_of_metacharacters_round_trips_exactly(password):
    plan = netconf.wifi_plan(interface="wlan0", ssid="Home", password=password)
    parsed = yaml.safe_load(plan)
    assert parsed["network"]["wifis"]["wlan0"]["access-points"]["Home"]["password"] == password


def test_a_newline_in_an_ssid_cannot_inject_another_key():
    # The specific failure a string-formatted config would have.
    plan = netconf.wifi_plan(
        interface="wlan0", ssid="Home\n      evil-key: yes", password="pw12345678",
    )
    wifis = yaml.safe_load(plan)["network"]["wifis"]["wlan0"]
    assert "evil-key" not in wifis
    assert "evil-key" not in wifis["access-points"]


@pytest.mark.parametrize("ssid", ["", "   ", None, "x" * 40])
def test_an_ssid_that_cannot_be_one_is_refused(ssid):
    with pytest.raises(NetworkError):
        netconf.wifi_plan(interface="wlan0", ssid=ssid, password="pw12345678")


@pytest.mark.parametrize("password", ["short", "x" * 200, 12345678])
def test_a_password_that_cannot_be_one_is_refused(password):
    with pytest.raises(NetworkError):
        netconf.wifi_plan(interface="wlan0", ssid="Home", password=password)


def test_an_open_network_needs_no_password():
    plan = yaml.safe_load(netconf.wifi_plan(interface="wlan0", ssid="Cafe", password=None))
    assert plan["network"]["wifis"]["wlan0"]["access-points"]["Cafe"] == {}


def test_a_wifi_plan_can_also_be_static():
    plan = yaml.safe_load(netconf.wifi_plan(
        interface="wlan0", ssid="Home", password="pw12345678",
        address="192.168.1.60", prefix=24, gateway="192.168.1.1", dns=["1.1.1.1"],
    ))
    wifi = plan["network"]["wifis"]["wlan0"]
    assert wifi["addresses"] == ["192.168.1.60/24"]
    assert wifi["dhcp4"] is False


# ==========================================================================
# Where the files go
# ==========================================================================
def test_wired_and_wifi_are_separate_files():
    # Additive, alongside whatever the installer already wrote, so changing
    # one cannot disturb the other - and neither touches the distro's own
    # 50-cloud-init.yaml.
    assert netconf.WIRED_FILE != netconf.WIFI_FILE
    for path in (netconf.WIRED_FILE, netconf.WIFI_FILE):
        assert path.startswith("/etc/netplan/")
        assert "retrobox" in path
        assert path.endswith(".yaml")


def test_the_files_sort_after_the_distros_own():
    # netplan merges in lexical order and later wins, so ours has to come last.
    for path in (netconf.WIRED_FILE, netconf.WIFI_FILE):
        number = path.rsplit("/", 1)[1].split("-", 1)[0]
        assert number.isdigit() and int(number) >= 90, path


# ==========================================================================
# Reading what the box has now
# ==========================================================================
def test_interfaces_are_read_from_ip(monkeypatch):
    monkeypatch.setattr(netconf, "_run", lambda cmd, **k: (0, IP_JSON, ""))
    interfaces = netconf.interfaces()
    by_name = {i["name"]: i for i in interfaces}

    assert by_name["eth0"]["up"] is True
    assert by_name["eth0"]["addresses"] == ["192.168.1.42/24"]
    assert by_name["wlan0"]["wireless"] is True
    assert "lo" not in by_name, "loopback is not an interface anyone configures"


def test_no_ip_command_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(netconf, "_run", lambda cmd, **k: (1, "", ""))
    assert netconf.interfaces() == []


def test_junk_from_ip_does_not_break_the_page(monkeypatch):
    monkeypatch.setattr(netconf, "_run", lambda cmd, **k: (0, "not json", ""))
    assert netconf.interfaces() == []


IP_JSON = """[
 {"ifname":"lo","operstate":"UNKNOWN","link_type":"loopback",
  "addr_info":[{"family":"inet","local":"127.0.0.1","prefixlen":8}]},
 {"ifname":"eth0","operstate":"UP","link_type":"ether",
  "addr_info":[{"family":"inet","local":"192.168.1.42","prefixlen":24}]},
 {"ifname":"wlan0","operstate":"DOWN","link_type":"ether","wireless":true,
  "addr_info":[]}
]"""


# ==========================================================================
# What a command printed, and what it warned about
#
# These are two different things and the box must never confuse them. A
# command that succeeds can still write to stderr - sudo does it on every
# invocation once the hostname no longer matches /etc/hosts - and if that
# warning is treated as output, it ends up recorded as the contents of a
# netplan file. Putting that back is how a box comes up with no network at
# all, on a customer's shelf, with no way in.
# ==========================================================================
def test_a_command_that_warns_is_not_confused_with_one_that_answers(monkeypatch):
    monkeypatch.setattr(netconf.subprocess, "run",
                        fake_command(0, "the answer\n", "a warning\n"))
    assert netconf._run(["true"]) == (0, "the answer\n", "a warning\n")


def test_reading_a_plan_back_gives_the_file_and_nothing_sudo_said(monkeypatch):
    on_disk = (
        "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n"
    )
    monkeypatch.setattr(
        netconf.subprocess, "run",
        fake_command(0, on_disk, SUDO_CANNOT_RESOLVE_HOST),
    )
    # Byte for byte: this string is written back to /etc/netplan when a change
    # is undone, so anything added to it is added to the file.
    assert netconf.read_plan(netconf.WIRED_FILE) == on_disk


def test_a_plan_read_back_while_sudo_is_grumbling_is_still_valid_netplan(monkeypatch):
    # The failure this stops: the warning is glued onto the end of the YAML,
    # the box puts that back when a change is not confirmed, and the next boot
    # rejects the file. That box has no network and nobody can reach it.
    on_disk = (
        "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n"
    )
    monkeypatch.setattr(
        netconf.subprocess, "run",
        fake_command(0, on_disk, SUDO_CANNOT_RESOLVE_HOST),
    )
    restored = netconf.read_plan(netconf.WIRED_FILE)
    assert yaml.safe_load(restored) == {
        "network": {"version": 2, "ethernets": {"eth0": {"dhcp4": True}}}
    }


def test_the_adapters_are_still_listed_when_a_command_writes_to_stderr(monkeypatch):
    # Same warning, different victim: the Network page showing no adapters at
    # all on a box whose network is working perfectly well.
    monkeypatch.setattr(
        netconf.subprocess, "run",
        fake_command(0, IP_JSON, SUDO_CANNOT_RESOLVE_HOST),
    )
    assert [i["name"] for i in netconf.interfaces()] == ["eth0", "wlan0"]


def test_a_scan_lists_what_the_radio_found_not_what_sudo_warned(monkeypatch):
    # A warning on stderr must not be able to invent a wireless network for
    # somebody to try to join.
    warning = (
        SUDO_CANNOT_RESOLVE_HOST
        + "BSS ff:ff:ff:ff:ff:ff(on wlan0)\n\tSSID: Not Really There\n"
    )
    monkeypatch.setattr(netconf.subprocess, "run", fake_command(0, IW_SCAN, warning))
    assert [n["ssid"] for n in netconf.scan("wlan0")] == ["Strong Net", "Weak Net"]


# ==========================================================================
# The three separate answers
# ==========================================================================
def test_connectivity_is_three_answers_not_one(monkeypatch):
    monkeypatch.setattr(netconf, "_has_link", lambda: True)
    monkeypatch.setattr(netconf, "_dns_resolves", lambda: False)
    monkeypatch.setattr(netconf, "_reaches_internet", lambda: False)

    result = netconf.connectivity()
    assert result["link"] is True
    assert result["dns"] is False
    assert result["internet"] is False


def test_dns_is_named_when_dns_is_the_actual_problem(monkeypatch):
    # The case that discriminates: the box can reach the internet by address
    # but names do not resolve. That is a different fix from "no internet",
    # and it is the one worth saying out loud.
    monkeypatch.setattr(netconf, "_has_link", lambda: True)
    monkeypatch.setattr(netconf, "_dns_resolves", lambda: False)
    monkeypatch.setattr(netconf, "_reaches_internet", lambda: True)

    result = netconf.connectivity()
    assert result["ok"] is False
    assert "dns" in result["summary"].lower()


def test_no_internet_is_named_when_that_is_the_problem(monkeypatch):
    monkeypatch.setattr(netconf, "_has_link", lambda: True)
    monkeypatch.setattr(netconf, "_dns_resolves", lambda: False)
    monkeypatch.setattr(netconf, "_reaches_internet", lambda: False)
    assert "internet" in netconf.connectivity()["summary"].lower()


def test_a_working_box_says_so(monkeypatch):
    monkeypatch.setattr(netconf, "_has_link", lambda: True)
    monkeypatch.setattr(netconf, "_dns_resolves", lambda: True)
    monkeypatch.setattr(netconf, "_reaches_internet", lambda: True)
    assert netconf.connectivity()["ok"] is True


def test_no_link_is_the_first_thing_it_says(monkeypatch):
    monkeypatch.setattr(netconf, "_has_link", lambda: False)
    monkeypatch.setattr(netconf, "_dns_resolves", lambda: False)
    monkeypatch.setattr(netconf, "_reaches_internet", lambda: False)
    summary = netconf.connectivity()["summary"].lower()
    assert "cable" in summary or "network" in summary


# ==========================================================================
# The wifi scan
# ==========================================================================
def test_a_scan_is_parsed_into_something_pickable(monkeypatch):
    monkeypatch.setattr(netconf, "_run", lambda cmd, **k: (0, IW_SCAN, ""))
    found = netconf.scan("wlan0")

    assert [n["ssid"] for n in found] == ["Strong Net", "Weak Net"]
    assert found[0]["secured"] is True
    assert found[1]["secured"] is False
    assert found[0]["signal"] > found[1]["signal"]


def test_a_scan_on_an_interface_that_is_not_wireless_is_refused():
    with pytest.raises(NetworkError):
        netconf.scan("eth0; rm -rf /")


def test_a_failed_scan_is_an_empty_list_not_an_error(monkeypatch):
    monkeypatch.setattr(netconf, "_run", lambda cmd, **k: (1, "", "no such device"))
    assert netconf.scan("wlan0") == []


IW_SCAN = """BSS aa:bb:cc:dd:ee:ff(on wlan0)
\tsignal: -45.00 dBm
\tSSID: Strong Net
\tRSN:\t * Version: 1
BSS 11:22:33:44:55:66(on wlan0)
\tsignal: -80.00 dBm
\tSSID: Weak Net
"""


# ==========================================================================
# The credential file
# ==========================================================================
def test_the_wifi_file_is_written_not_world_readable(tmp_path, monkeypatch):
    # It has the customer's home wifi password in it.
    written = {}

    def fake_install(path, content, mode):
        target = tmp_path / path.rsplit("/", 1)[1]
        target.write_text(content)
        target.chmod(mode)
        written["path"] = target
        return 0, ""

    monkeypatch.setattr(netconf, "_install_privileged", fake_install)
    netconf.write_plan(netconf.WIFI_FILE, "network: {}\n")

    mode = stat.S_IMODE(written["path"].stat().st_mode)
    assert mode & stat.S_IROTH == 0, f"world readable: {oct(mode)}"
    assert mode & stat.S_IRGRP == 0, f"group readable: {oct(mode)}"


# ==========================================================================
# The password, and the moment it lands on disk
#
# "0600 eventually" is not the same as "0600", and on a box with no
# authentication on the dashboard the difference is the customer's home wifi
# password sitting in a file every account on the machine can read.
# ==========================================================================
PASSWORD = "correct-horse-battery"
PLAN_WITH_A_PASSWORD = netconf.wifi_plan(
    interface="wlan0", ssid="Home", password=PASSWORD,
)
A_PLAN_ALREADY_THERE = "network:\n  version: 2\n  wifis: {}\n"


class FakeRoot:
    """The privileged half of the box, in a temporary directory.

    It runs the three commands netconf has a sudoers rule for - tee, chmod and
    cat - and gives files the permissions root would really give them: tee
    creates under root's umask, which is 0644, and nothing narrows that except
    chmod. What it records is the thing that matters: the mode each file had
    at the instant content went into it.
    """

    def __init__(self, folder, *, chmod_works=True, tee_works=True, grumbles=False):
        self.folder = Path(folder)
        self.chmod_works = chmod_works
        self.tee_works = tee_works
        # A box whose hostname was changed: every sudo warns, every time, and
        # every one of them still works.
        self.grumbles = grumbles
        self.commands = []
        self.writes = []          # (content, the mode it was written into)

    def local(self, path):
        return self.folder / path.rsplit("/", 1)[1]

    def run(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.commands.append(argv)
        assert argv[:2] == ["sudo", "-n"], f"not run through sudo -n: {argv}"
        assert not any(PASSWORD in part for part in argv), (
            f"the password reached the command line, where /proc shows it "
            f"to every account on the box: {argv}"
        )

        name, rest = argv[2], argv[3:]
        if name == "tee":
            return self._tee(rest[0], kwargs.get("input") or "")
        if name == "chmod":
            return self._chmod(rest[0], rest[1])
        if name == "cat":
            return self._cat(rest[0])
        raise AssertionError(
            f"netconf ran {name!r}, which no sudoers rule on the box permits"
        )

    def _done(self, argv, code, printed="", warned=""):
        if self.grumbles:
            warned = SUDO_CANNOT_RESOLVE_HOST + warned
        return subprocess.CompletedProcess(argv, code, printed, warned)

    def _tee(self, path, content):
        target = self.local(path)
        if not self.tee_works:
            # tee copies what it is given to stdout whether or not it managed
            # to open the file, so a failure still echoes the whole plan back.
            return self._done(["tee"], 1, content,
                              f"tee: {path}: Permission denied\n")
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        target.write_text(content)
        os.chmod(target, mode)
        self.writes.append((content, mode))
        return self._done(["tee"], 0, content, "")

    def _chmod(self, mode, path):
        if not self.chmod_works:
            return self._done(["chmod"], 1, "",
                              "sudo: a password is required\n")
        target = self.local(path)
        if not target.exists():
            return self._done(
                ["chmod"], 1, "",
                f"chmod: cannot access '{path}': No such file or directory\n")
        os.chmod(target, int(mode, 8))
        return self._done(["chmod"], 0, "", "")

    def _cat(self, path):
        target = self.local(path)
        if not target.exists():
            return self._done(
                ["cat"], 1, "",
                f"cat: {path}: No such file or directory\n")
        return self._done(["cat"], 0, target.read_text(), "")


def test_the_password_is_never_written_into_a_file_others_could_read(tmp_path, monkeypatch):
    box = FakeRoot(tmp_path)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)

    for content, mode in box.writes:
        if PASSWORD in content:
            assert mode & 0o077 == 0, (
                f"the password went into a file at {oct(mode)}. Every account "
                f"on the box could read it until chmod caught up."
            )
    landed = box.local(netconf.WIFI_FILE)
    assert PASSWORD in landed.read_text()
    assert stat.S_IMODE(landed.stat().st_mode) == netconf.PLAN_MODE


def test_a_file_that_could_not_be_made_private_never_gets_the_password(tmp_path, monkeypatch):
    # chmod failing is not hypothetical: install-service.sh skips the sudoers
    # file entirely if visudo refuses it, and then this is a box that can
    # write /etc/netplan but cannot narrow it.
    box = FakeRoot(tmp_path, chmod_works=False)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError):
        netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)

    left_behind = box.local(netconf.WIFI_FILE)
    on_disk = left_behind.read_text() if left_behind.exists() else ""
    assert PASSWORD not in on_disk, (
        "the write was reported as failed but left the plaintext wifi "
        "password in /etc/netplan for everyone to read"
    )


def test_refusing_to_write_leaves_the_configuration_already_on_the_box(tmp_path, monkeypatch):
    # If we will not write, we must also not have destroyed what was there.
    # An emptied netplan file is a box that comes up on a different network -
    # or on none - the next time somebody switches it on at the wall.
    already_there = tmp_path / netconf.WIFI_FILE.rsplit("/", 1)[1]
    already_there.write_text(A_PLAN_ALREADY_THERE)
    already_there.chmod(netconf.PLAN_MODE)

    box = FakeRoot(tmp_path, chmod_works=False)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError):
        netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)

    assert already_there.read_text() == A_PLAN_ALREADY_THERE


def test_a_renamed_box_can_still_put_its_old_configuration_back(tmp_path, monkeypatch):
    # The whole round trip a change on probation makes - write the new plan,
    # having read the old one first, then put the old one back when nobody
    # confirms - on a box whose hostname was changed and where every sudo
    # therefore warns. What goes back must be what was there, and it must
    # still be a file netplan will accept at the next boot.
    box = FakeRoot(tmp_path, grumbles=True)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    netconf.write_plan(netconf.WIRED_FILE, A_PLAN_ALREADY_THERE)
    previous = netconf.read_plan(netconf.WIRED_FILE)

    netconf.write_plan(netconf.WIRED_FILE, netconf.dhcp_plan(interface="eth0"))
    netconf.write_plan(netconf.WIRED_FILE, previous)

    put_back = box.local(netconf.WIRED_FILE).read_text()
    assert put_back == A_PLAN_ALREADY_THERE
    assert yaml.safe_load(put_back) == yaml.safe_load(A_PLAN_ALREADY_THERE), \
        "the box would come up with a netplan file it cannot read"


def test_a_write_that_failed_does_not_read_the_password_back_out(tmp_path, monkeypatch):
    # The failure message goes to the browser and into the journal. tee echoes
    # its input, so anything that treats tee's output as the reason publishes
    # the password to both.
    already_there = tmp_path / netconf.WIFI_FILE.rsplit("/", 1)[1]
    already_there.write_text(A_PLAN_ALREADY_THERE)
    already_there.chmod(netconf.PLAN_MODE)

    box = FakeRoot(tmp_path, tee_works=False)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError) as caught:
        netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)
    assert PASSWORD not in str(caught.value)
    assert "denied" in str(caught.value).lower(), "it did not say why either"
