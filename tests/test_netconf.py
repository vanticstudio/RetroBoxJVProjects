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
# Putting a configuration into effect
#
# Writing the file is not the same as changing the network. netplan reads
# /etc/netplan at boot and when it is told to, and at no other time - so a box
# that has had a good configuration put back for it goes on running the bad
# one, with no dashboard to fix it from if the bad one is what took the
# dashboard away, until somebody switches it off at the wall.
# ==========================================================================
def test_putting_the_configuration_into_effect_asks_netplan_to_apply_it(monkeypatch):
    ran = []
    monkeypatch.setattr(
        netconf, "_run", lambda cmd, **k: (ran.append(list(cmd)), (0, "", ""))[1]
    )
    netconf.apply_plan()
    assert ran == [["sudo", "-n", "netplan", "apply"]]


def test_an_apply_that_failed_is_said_out_loud_rather_than_shrugged_off(monkeypatch):
    monkeypatch.setattr(netconf, "_run",
                        lambda cmd, **k: (1, "", "netplan: cannot parse yaml\n"))
    with pytest.raises(NetworkError) as caught:
        netconf.apply_plan()
    assert "cannot parse" in str(caught.value)


def test_an_apply_is_never_allowed_to_wedge_the_dashboard_for_ever(monkeypatch):
    # This runs while the dashboard is starting up, before it serves its first
    # page, so a netplan that has hung must not take the dashboard with it.
    seen = {}
    monkeypatch.setattr(
        netconf, "_run", lambda cmd, **k: (seen.update(k), (0, "", ""))[1]
    )
    netconf.apply_plan()
    assert 0 < seen.get("timeout", 0) <= 60, seen


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


class PowerCut(Exception):
    """The wall switch, half way through a write. The documented way off."""


class FakeRoot:
    """The privileged half of the box, in a temporary directory.

    It runs the commands netconf has a sudoers rule for - tee, chmod, cat and
    the rename that puts a staged file in place - and gives files the
    permissions root would really give them: tee creates under root's umask,
    which is 0644, and nothing narrows that except chmod. What it records is
    the thing that matters: the mode each file had at the instant content went
    into it.
    """

    def __init__(self, folder, *, chmod_works=True, tee_works=True, grumbles=False,
                 mv_works=True, power_cut=False, tee_dies_on_the_plan=False,
                 old_sudoers=False):
        self.folder = Path(folder)
        self.chmod_works = chmod_works
        self.tee_works = tee_works
        # tee gets as far as the real document and is then killed - the OOM
        # killer on a box with 512 MB, or a SIGPIPE. The shell reports a
        # non-zero status, tee itself never gets to say anything, and stderr
        # comes back empty. The only thing left to describe the failure with is
        # tee's own copy of its input, which is the wifi password.
        self.tee_dies_on_the_plan = tee_dies_on_the_plan
        # A box installed before the rename was part of the sudoers table.
        self.mv_works = mv_works
        # A box installed before the staging file was part of it either. Its
        # sudoers rule names the live netplan file and nothing else, so sudo
        # itself refuses every command that mentions the staging name - the
        # command never runs and sudo's own words are all anybody sees.
        self.old_sudoers = old_sudoers
        # The power goes as the real document is being written - tee has
        # truncated the file it was given and never gets to write the rest.
        self.power_cut = power_cut
        # A box whose hostname was changed: every sudo warns, every time, and
        # every one of them still works.
        self.grumbles = grumbles
        self.commands = []
        self.writes = []          # (content, the mode it was written into)
        self.tee_inputs = []      # everything tee was handed, landed or not

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

        if self.old_sudoers and any(
            part.endswith(netconf.STAGING_SUFFIX) for part in argv
        ):
            # sudo -n with no rule matching this command line does not run
            # anything at all. It falls back to asking for a password, -n
            # forbids that, and this one sentence is the whole of what the
            # dashboard has to explain itself with.
            return self._done(argv, 1, "", "sudo: a password is required\n")

        name, rest = argv[2], argv[3:]
        if name == "tee":
            return self._tee(rest[0], kwargs.get("input") or "")
        if name == "chmod":
            return self._chmod(rest[0], rest[1])
        if name == "cat":
            return self._cat(rest[0])
        if name == "mv":
            return self._mv(rest)
        raise AssertionError(
            f"netconf ran {name!r}, which no sudoers rule on the box permits"
        )

    def _done(self, argv, code, printed="", warned=""):
        if self.grumbles:
            warned = SUDO_CANNOT_RESOLVE_HOST + warned
        return subprocess.CompletedProcess(argv, code, printed, warned)

    def _tee(self, path, content):
        target = self.local(path)
        self.tee_inputs.append(content)
        if self.tee_dies_on_the_plan and content != netconf.EMPTY_DOCUMENT:
            # Killed, not refused: it echoed what it had been handed and said
            # nothing on stderr, because it never got the chance to.
            return self._done(["tee"], 137, content, "")
        if not self.tee_works:
            # tee copies what it is given to stdout whether or not it managed
            # to open the file, so a failure still echoes the whole plan back.
            return self._done(["tee"], 1, content,
                              f"tee: {path}: Permission denied\n")
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        if self.power_cut and content != netconf.EMPTY_DOCUMENT:
            # tee opens with O_TRUNC: the file is empty from that instant, and
            # stays empty if nothing ever writes the rest of it.
            target.write_text("")
            os.chmod(target, mode)
            raise PowerCut(f"the box went off at the wall while writing {path}")
        target.write_text(content)
        os.chmod(target, mode)
        self.writes.append((content, mode))
        return self._done(["tee"], 0, content, "")

    def _mv(self, rest):
        source, destination = rest[-2], rest[-1]
        if not self.mv_works:
            return self._done(
                ["mv"], 1, "",
                f"sudo: a password is required\n")
        origin = self.local(source)
        if not origin.exists():
            return self._done(
                ["mv"], 1, "",
                f"mv: cannot stat '{source}': No such file or directory\n")
        # rename(2): the destination goes from being one whole file to being
        # the other, with nothing in between, and the mode travels with it.
        os.replace(origin, self.local(destination))
        return self._done(["mv"], 0, "", "")

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


# ==========================================================================
# The write itself, against the wall switch
#
# This box is switched off at the wall. That is not misuse, it is how a
# television works, so every write has to assume the power can go at any
# instruction - and `tee` truncates the file it is given before it writes a
# byte of the replacement. A cut inside that window leaves a truncated
# document in /etc/netplan, and netplan generate then fails for the WHOLE
# directory: the box comes up with no network on ANY interface, not just the
# one somebody was changing. Every other write in this codebase stages and
# renames for exactly this reason (retrobox/configwrite.py).
# ==========================================================================
def test_a_power_cut_mid_write_never_truncates_the_file_netplan_reads(
    tmp_path, monkeypatch
):
    already_there = tmp_path / netconf.WIRED_FILE.rsplit("/", 1)[1]
    already_there.write_text(A_PLAN_ALREADY_THERE)
    already_there.chmod(netconf.PLAN_MODE)

    box = FakeRoot(tmp_path, power_cut=True)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(PowerCut):
        netconf.write_plan(netconf.WIRED_FILE, netconf.dhcp_plan(interface="eth0"))

    on_disk = already_there.read_text()
    assert on_disk, (
        "the file netplan reads was left empty, so netplan generate fails for "
        "the whole of /etc/netplan and the box comes up with no network at all"
    )
    assert yaml.safe_load(on_disk) == yaml.safe_load(A_PLAN_ALREADY_THERE), \
        "the box would come up on a netplan file it cannot read"


def test_the_live_netplan_file_is_never_the_one_being_written_into(
    tmp_path, monkeypatch
):
    box = FakeRoot(tmp_path)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)
    netconf.write_plan(netconf.WIRED_FILE, netconf.dhcp_plan(interface="eth0"))

    staged = set()
    for argv in box.commands:
        if argv[2] == "tee":
            assert argv[3] != netconf.WIRED_FILE, (
                "tee truncates before it writes, so the live file spends the "
                "length of a write being an empty one"
            )
            staged.add(argv[3])

    assert len(staged) == 1, f"more than one staging file: {staged}"
    staging = staged.pop()
    assert staging.rsplit("/", 1)[0] == netconf.WIRED_FILE.rsplit("/", 1)[0], (
        "staged outside /etc/netplan, and a rename is only atomic within one "
        "filesystem - across two it is a copy, which is what we are avoiding"
    )
    assert not staging.endswith(".yaml"), (
        "netplan reads /etc/netplan/*.yaml, so it would read the half-written "
        "one too"
    )
    assert ["sudo", "-n", "mv", "-f", staging, netconf.WIRED_FILE] in box.commands, (
        "nothing renamed the finished document into place"
    )
    assert box.local(netconf.WIRED_FILE).read_text() == \
        netconf.dhcp_plan(interface="eth0")


def test_the_staged_file_carries_its_own_private_mode_across_the_rename(
    tmp_path, monkeypatch
):
    # The rename moves the inode, permissions and all, so the file netplan
    # reads is 0600 from the instant it exists - there is no window at all,
    # not even the short one a chmod-afterwards would leave.
    box = FakeRoot(tmp_path)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)
    netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)

    landed = box.local(netconf.WIFI_FILE)
    assert stat.S_IMODE(landed.stat().st_mode) & 0o077 == 0, oct(
        stat.S_IMODE(landed.stat().st_mode))


def test_a_box_that_cannot_rename_refuses_rather_than_writing_over_the_live_file(
    tmp_path, monkeypatch
):
    """The upgrade case, and it must fail safe.

    A box installed before this version has a sudoers file that does not name
    the rename, so sudo refuses it. What must not happen is a fallback that
    writes over the live file anyway - that is the truncation this change
    exists to remove. The change is refused, the box keeps the network it has,
    and the message says what to do about it.
    """
    already_there = tmp_path / netconf.WIFI_FILE.rsplit("/", 1)[1]
    already_there.write_text(A_PLAN_ALREADY_THERE)
    already_there.chmod(netconf.PLAN_MODE)

    box = FakeRoot(tmp_path, mv_works=False)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError) as caught:
        netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)

    assert already_there.read_text() == A_PLAN_ALREADY_THERE, \
        "it left the box on a configuration nobody asked for"
    assert PASSWORD not in str(caught.value)
    assert "install-service" in str(caught.value), (
        "the one thing that fixes this is not in the only message anybody "
        "will ever see"
    )


def test_a_box_whose_sudo_rules_predate_the_staging_file_is_told_what_to_do(
    tmp_path, monkeypatch
):
    """The upgrade case as it actually goes, which is not where it was caught.

    A box installed before the staging file existed has a sudoers rule naming
    the live netplan file and nothing else. So the write does not get as far
    as the rename that has the helpful message on it: the very first command,
    the chmod on the staging file, is refused, and so is the tee that follows
    it. What sudo says about a command it has no rule for is "sudo: a password
    is required", which sends the owner of a box that has no password to type
    looking for one - while the one thing that would fix it, running the
    installer once more, is said nowhere at all.
    """
    already_there = tmp_path / netconf.WIFI_FILE.rsplit("/", 1)[1]
    already_there.write_text(A_PLAN_ALREADY_THERE)
    already_there.chmod(netconf.PLAN_MODE)

    box = FakeRoot(tmp_path, old_sudoers=True)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError) as caught:
        netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)

    said = str(caught.value)
    assert already_there.read_text() == A_PLAN_ALREADY_THERE, \
        "it left the box on a configuration nobody asked for"
    assert PASSWORD not in said
    assert "install-service" in said, (
        f"the only message this box's owner will ever see does not name the "
        f"one thing that fixes it: {said!r}"
    )


def test_a_write_that_failed_does_not_read_the_password_back_out(tmp_path, monkeypatch):
    # The failure message goes to the browser and into the journal. tee echoes
    # its input, so anything that treats tee's output as the reason publishes
    # the password to both.
    #
    # The staging file is put there first on purpose, and it is the whole
    # point of this test. Without it the first tee that fails is the one
    # carrying an empty document, the write stops there, and the tee that
    # holds the password - the only one that can leak anything - never runs:
    # the test passes while exercising nothing it is named after. A staging
    # file left over from an interrupted write is what a real box has, and it
    # takes this straight to the write that matters.
    already_there = tmp_path / netconf.WIFI_FILE.rsplit("/", 1)[1]
    already_there.write_text(A_PLAN_ALREADY_THERE)
    already_there.chmod(netconf.PLAN_MODE)
    left_over = tmp_path / netconf.staging_for(netconf.WIFI_FILE).rsplit("/", 1)[1]
    left_over.write_text(netconf.EMPTY_DOCUMENT)
    left_over.chmod(netconf.PLAN_MODE)

    box = FakeRoot(tmp_path, tee_works=False)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError) as caught:
        netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)
    assert any(PASSWORD in handed for handed in box.tee_inputs), (
        "the write carrying the password never happened, so nothing here "
        "tested the failure that can publish it"
    )
    assert PASSWORD not in str(caught.value)
    assert "denied" in str(caught.value).lower(), "it did not say why either"


def test_a_tee_that_dies_without_saying_why_still_does_not_publish_the_password(
    tmp_path, monkeypatch
):
    """The case the test above cannot catch, and the one that actually leaks.

    Up there tee fails and writes "Permission denied" to stderr, so any code
    that reaches for stderr first has something to say and never gets as far as
    tee's copy of its input. The dangerous failure is the one with nothing on
    stderr at all - tee killed rather than refused, which is what the OOM
    killer on a 512 MB box does - because then the only stream with anything in
    it is the one holding the whole netplan document, password included. That
    message goes to a browser on an unauthenticated dashboard and into the
    journal, so it must stay a description of the failure and never become a
    copy of the file.
    """
    already_there = tmp_path / netconf.WIFI_FILE.rsplit("/", 1)[1]
    already_there.write_text(A_PLAN_ALREADY_THERE)
    already_there.chmod(netconf.PLAN_MODE)

    box = FakeRoot(tmp_path, tee_dies_on_the_plan=True)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError) as caught:
        netconf.write_plan(netconf.WIFI_FILE, PLAN_WITH_A_PASSWORD)

    said = str(caught.value)
    assert PASSWORD not in said, (
        "the household's wifi password was handed to whoever asked for the "
        "network page, and written into the journal as well"
    )
    assert "password:" not in said and "access-points" not in said, (
        f"the netplan document was echoed back as the reason: {said!r}"
    )
    assert said.strip() and said.rstrip(":").strip() != \
        "could not write the network configuration", \
        f"it did not say anything about why either: {said!r}"
    assert already_there.read_text() == A_PLAN_ALREADY_THERE, \
        "the live file was touched by a write that failed"


# ==========================================================================
# Which file the privileged write is allowed to be aimed at
#
# write_plan and read_plan are the only things in this box that run tee, chmod,
# mv and cat as root, and the path they are given is the whole of what decides
# which file that lands on. Today every caller passes one of two module
# constants - but "no caller passes anything else" is a fact about today's
# callers, not a property of this module, and the dashboard those callers sit
# behind has no authentication at all. The refusal has to live here, next to
# the privilege, so that a route which one day passes a path through from a
# request body finds a closed door rather than a root-owned write of its
# choosing. sudoers naming the two files in full is the second lock, not a
# reason to leave this one off: an old box, a hand-edited rule or a service
# that ends up running as root are all ways it comes unlatched, and none of
# them announce themselves.
# ==========================================================================
SOMEWHERE_THAT_IS_NOT_OURS = [
    "/etc/sudoers.d/retrobox-system",
    "/etc/netplan/50-cloud-init.yaml",     # the distro's own, not ours
    "/root/.ssh/authorized_keys",
    "/etc/systemd/system/evil.service",
    "/etc/netplan/../shadow",
    "",
]


@pytest.mark.parametrize("path", SOMEWHERE_THAT_IS_NOT_OURS)
def test_writing_a_plan_refuses_any_file_but_this_boxs_own_two(
    tmp_path, monkeypatch, path
):
    box = FakeRoot(tmp_path)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError) as caught:
        netconf.write_plan(path, "network: {}\n")

    assert "not a file this box writes" in str(caught.value)
    assert box.commands == [], (
        f"a root-owned write was aimed at {path!r} and got as far as running "
        f"{box.commands}. Nothing below this point asks again whose file it is."
    )


@pytest.mark.parametrize("path", SOMEWHERE_THAT_IS_NOT_OURS)
def test_reading_a_plan_refuses_any_file_but_this_boxs_own_two(
    tmp_path, monkeypatch, path
):
    # Reading is not the harmless half. read_plan is `sudo cat` with the path
    # as its argument, and what it hands back is shown on the network page, so
    # a path that got through here is any root-only file on the box read out
    # to whoever asked for it.
    box = FakeRoot(tmp_path)
    for secret in ("authorized_keys", "shadow", "retrobox-system", "evil.service",
                   "50-cloud-init.yaml"):
        (tmp_path / secret).write_text("root:$6$the private half\n")
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    with pytest.raises(NetworkError) as caught:
        netconf.read_plan(path)

    assert "not a file this box writes" in str(caught.value)
    assert box.commands == [], (
        f"reading {path!r} ran {box.commands} as root instead of refusing"
    )


def test_the_two_files_this_box_does_own_are_still_written_and_read(tmp_path, monkeypatch):
    # The other half of the whitelist: refusing everything is not the goal.
    box = FakeRoot(tmp_path)
    monkeypatch.setattr(netconf.subprocess, "run", box.run)

    for ours in (netconf.WIRED_FILE, netconf.WIFI_FILE):
        netconf.write_plan(ours, A_PLAN_ALREADY_THERE)
        assert netconf.read_plan(ours) == A_PLAN_ALREADY_THERE
