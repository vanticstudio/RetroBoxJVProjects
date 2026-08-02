"""The network page, which is the one that can cut you off from the box.

Nothing in this suite may reconfigure the machine running it: netplan, iw,
tee and hostnamectl are all stubbed, and tests/conftest.py refuses the
destructive ones against the real checkout regardless.
"""

import time

import pytest

from retrobox import netconf, netprobation
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app  # noqa: E402


IP_JSON = """[
 {"ifname":"eth0","operstate":"UP","link_type":"ether",
  "addr_info":[{"family":"inet","local":"192.168.1.42","prefixlen":24}]},
 {"ifname":"wlan0","operstate":"UP","link_type":"ether","wireless":true,
  "addr_info":[{"family":"inet","local":"192.168.1.77","prefixlen":24}]}
]"""

ONE_INTERFACE = """[
 {"ifname":"eth0","operstate":"UP","link_type":"ether",
  "addr_info":[{"family":"inet","local":"192.168.1.42","prefixlen":24}]}
]"""


class FakeTry:
    def __init__(self):
        self.started, self.confirmed, self.cancelled, self.alive = [], 0, 0, False

    def start(self, timeout):
        self.started.append(timeout)
        self.alive = True
        return "handle"

    def confirm(self, handle):
        self.confirmed += 1
        self.alive = False

    def cancel(self, handle):
        self.cancelled += 1
        self.alive = False

    def running(self, handle):
        return self.alive


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def box(tmp_path, runtime, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )

    # Everything privileged, stubbed. Nothing here touches a real network.
    files = {}
    ran = []
    monkeypatch.setattr(netconf, "write_plan", lambda p, c: files.__setitem__(p, c))
    monkeypatch.setattr(netconf, "read_plan", lambda p: files.get(p))
    monkeypatch.setattr(
        netconf, "_run",
        lambda cmd, **k: (ran.append(list(cmd)),
                         (0, IP_JSON if "addr" in cmd else "", ""))[1],
    )
    trier = FakeTry()
    monkeypatch.setattr(netprobation, "NetplanTry", lambda *a, **k: trier)

    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), files, trier, ran


# ==========================================================================
# Nothing is ever applied in one step
# ==========================================================================
def test_a_wired_change_goes_on_probation(box):
    client, files, trier, _ = box
    res = client.post("/api/network/wired", json={"interface": "eth0", "mode": "dhcp"})

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["phase"] == "testing"
    assert trier.started, "it did not go through netplan try"
    assert trier.confirmed == 0, "it committed without being asked"


def test_the_page_is_told_how_long_it_has(box):
    client, _, _, _ = box
    body = client.post("/api/network/wired",
                       json={"interface": "eth0", "mode": "dhcp"}).get_json()
    assert body["seconds_left"] > 0
    assert "put the old ones back" in body["message"]


def test_confirming_keeps_it(box):
    client, _, trier, _ = box
    client.post("/api/network/wired", json={"interface": "eth0", "mode": "dhcp"})
    res = client.post("/api/network/confirm")

    assert res.get_json()["phase"] == "kept"
    assert trier.confirmed == 1


def test_undoing_it_by_hand_works_too(box):
    client, _, trier, _ = box
    client.post("/api/network/wired", json={"interface": "eth0", "mode": "dhcp"})
    assert client.post("/api/network/revert").get_json()["phase"] == "reverted"
    assert trier.cancelled == 1


def test_the_change_is_visible_to_a_page_that_reconnected(box):
    # The browser has just spent 20 seconds finding the box at a new address.
    # It has to be able to pick the probation back up.
    client, _, _, _ = box
    client.post("/api/network/wired", json={"interface": "eth0", "mode": "dhcp"})
    change = client.get("/api/network").get_json()["change"]
    assert change["phase"] == "testing"
    assert change["seconds_left"] > 0


# ==========================================================================
# The trial has to outlive the request that started it
#
# Every test above hands the dashboard one FakeTry and keeps hold of it, so
# they cannot see whether the box could find its own trial again. The
# dashboard builds a new Probation, and a new trier, for every single
# request - Save and Keep are different requests, and on a real box they are
# usually from a browser that has had to hunt the box down at a new address
# in between. If the running netplan try is not findable across that gap,
# nothing a customer changes here can ever be kept.
# ==========================================================================
@pytest.fixture
def real_trial(tmp_path, runtime, monkeypatch):
    """A box driving the real trier, over a stand-in for ``netplan try``.

    ``/bin/sh -c 'read line'`` behaves the way netplan try does in the two
    ways that matter: it waits on the terminal it was given, and it exits
    when somebody presses ENTER on it. Nothing here reconfigures anything.
    """
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )

    files = {}
    monkeypatch.setattr(netconf, "write_plan", lambda p, c: files.__setitem__(p, c))
    monkeypatch.setattr(netconf, "read_plan", lambda p: files.get(p))
    monkeypatch.setattr(
        netconf, "_run",
        lambda cmd, **k: (0, IP_JSON if "addr" in cmd else "", ""),
    )
    real = netprobation.NetplanTry
    monkeypatch.setattr(
        netprobation, "NetplanTry",
        lambda *a, **k: real(timeout_command=["/bin/sh", "-c", "read line"]),
    )

    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), files


def test_a_change_can_still_be_kept_by_a_later_request(real_trial):
    client, files = real_trial
    started = client.post("/api/network/wired",
                          json={"interface": "eth0", "mode": "dhcp"})
    assert started.get_json()["phase"] == "testing", started.get_json()

    # A separate request, with its own Probation and its own trier: the page
    # polling while the customer reads the panel.
    assert client.get("/api/network").get_json()["change"]["phase"] == "testing"

    kept = client.post("/api/network/confirm")
    assert kept.status_code == 200, kept.get_json()
    assert kept.get_json()["phase"] == "kept", (
        "the box could not find the trial it had started, so no network change "
        "made from this dashboard can ever be kept"
    )


def test_a_change_can_still_be_undone_by_a_later_request(real_trial):
    client, files = real_trial
    client.post("/api/network/wired", json={"interface": "eth0", "mode": "dhcp"})
    undone = client.post("/api/network/revert")
    assert undone.status_code == 200, undone.get_json()
    assert undone.get_json()["phase"] == "reverted"


# ==========================================================================
# Wifi
# ==========================================================================
def test_joining_a_network_writes_only_the_wifi_file(box):
    client, files, _, _ = box
    res = client.post("/api/network/wifi",
                      json={"ssid": "Home Network", "password": "hunter22"})
    assert res.status_code == 200, res.get_json()
    assert list(files) == [netconf.WIFI_FILE], "it disturbed the wired configuration"


@pytest.mark.parametrize(
    "ssid",
    ['net"quote', "net'quote", "net; reboot", "net`id`", "net$(id)", "net\nkey: x"],
)
def test_a_hostile_ssid_never_reaches_a_command_line(box, ssid):
    client, files, _, ran = box
    client.post("/api/network/wifi", json={"ssid": ssid, "password": "hunter22"})

    # It is in the document, exactly - checked by parsing rather than by
    # substring, because a newline inside an SSID is legitimately written as a
    # quoted multi-line scalar and will not appear literally in the text.
    import yaml

    plan = yaml.safe_load(files[netconf.WIFI_FILE])
    assert list(plan["network"]["wifis"]["wlan0"]["access-points"]) == [ssid]
    # ...and in no argv anywhere.
    for command in ran:
        for part in command:
            assert ssid not in part, f"{ssid!r} reached a command line: {command}"


@pytest.mark.parametrize(
    "password", ["pass; reboot", "pass`id`", "pass$(whoami)", 'pass"quote'],
)
def test_a_hostile_password_never_reaches_a_command_line(box, password):
    client, files, _, ran = box
    client.post("/api/network/wifi", json={"ssid": "Home", "password": password})
    for command in ran:
        for part in command:
            assert password not in part, f"the password reached {command}"


def test_a_bad_wifi_password_is_refused_before_anything_is_written(box):
    client, files, trier, _ = box
    res = client.post("/api/network/wifi", json={"ssid": "Home", "password": "short"})
    assert res.status_code == 400
    assert files == {}
    assert trier.started == []


# ==========================================================================
# The wifi password does not come back out
#
# There is no login on this dashboard, by design. So the record of how to
# undo a wireless change - which is the previous netplan file, and therefore
# the household's wifi password in plain text - must never leave this box in
# an answer to anybody who can reach port 80.
# ==========================================================================
OLD_WIFI = (
    "network:\n  wifis:\n    wlan0:\n"
    '      access-points:\n        "TheOldNetwork":\n'
    "          password: correct-horse-battery-staple\n"
)


def test_no_answer_about_the_network_carries_the_wifi_password(box):
    client, files, _, _ = box
    # This box is already on a wireless network, so the file about to be
    # replaced has the password in it - and the way back to that file is
    # written down for as long as the trial lasts.
    files[netconf.WIFI_FILE] = OLD_WIFI
    secrets = ["correct-horse-battery-staple", "brand-new-password"]

    answers = [
        client.post("/api/network/wifi",
                    json={"ssid": "TheNewNetwork", "password": "brand-new-password"}),
        client.get("/api/network"),
        client.post("/api/network/revert"),
        client.get("/api/network"),
    ]
    for res in answers:
        body = res.get_data(as_text=True)
        for secret in secrets:
            assert secret not in body, (
                f"a wifi password came back out of {res.request.path} on a "
                f"dashboard that has no login"
            )


def test_the_password_stays_in_even_when_the_box_cannot_put_the_old_file_back(box):
    """The one case where the record really does still hold the password.

    A restore that fails keeps the way back on the disk, because it is the
    only copy of what that file said. That is the right call - and it is
    exactly when the answer must not repeat it.
    """
    client, files, _, _ = box
    files[netconf.WIFI_FILE] = OLD_WIFI
    client.post("/api/network/wifi",
                json={"ssid": "TheNewNetwork", "password": "brand-new-password"})

    def refuse(path, content):
        raise netconf.NetworkError("read-only filesystem")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(netconf, "write_plan", refuse)
        undone = client.post("/api/network/revert")
        assert undone.status_code == 200, undone.get_json()
        answers = [undone, client.get("/api/network")]

    for res in answers:
        body = res.get_data(as_text=True)
        assert "correct-horse-battery-staple" not in body
        assert "brand-new-password" not in body


def test_a_scan_lists_what_is_in_range(box, monkeypatch):
    client, _, _, _ = box
    monkeypatch.setattr(netconf, "scan", lambda i: [
        {"ssid": "Home", "signal": -40.0, "secured": True},
    ])
    body = client.get("/api/network/scan").get_json()
    assert body["networks"][0]["ssid"] == "Home"


def test_a_scan_on_something_that_is_not_wireless_is_refused(box):
    client, _, _, _ = box
    assert client.get("/api/network/scan?interface=eth0").status_code == 400


# ==========================================================================
# Static addressing
# ==========================================================================
def test_a_valid_static_configuration_is_accepted(box):
    client, files, _, _ = box
    res = client.post("/api/network/wired", json={
        "interface": "eth0", "mode": "static", "address": "192.168.1.50",
        "prefix": 24, "gateway": "192.168.1.1", "dns": ["1.1.1.1"],
    })
    assert res.status_code == 200, res.get_json()
    assert "192.168.1.50/24" in files[netconf.WIRED_FILE]


@pytest.mark.parametrize(
    "payload",
    [
        {"address": "192.168.1.50", "prefix": 24, "gateway": "10.0.0.1"},
        {"address": "nonsense", "prefix": 24, "gateway": "192.168.1.1"},
        {"address": "192.168.1.50", "prefix": 99, "gateway": "192.168.1.1"},
        {"address": "192.168.1.0", "prefix": 24, "gateway": "192.168.1.1"},
        {"address": "192.168.1.1", "prefix": 24, "gateway": "192.168.1.1"},
        {"address": "", "prefix": 24, "gateway": "192.168.1.1"},
        {"address": "192.168.1.50", "prefix": 24, "gateway": "192.168.1.1",
         "dns": ["not-a-server"]},
    ],
)
def test_a_configuration_that_would_lose_the_box_is_refused(box, payload):
    client, files, trier, _ = box
    res = client.post("/api/network/wired",
                      json={"interface": "eth0", "mode": "static", **payload})
    assert res.status_code == 400, payload
    assert files == {}, "it wrote a configuration that would strand the box"
    assert trier.started == []


# ==========================================================================
# The last way in
# ==========================================================================
def test_changing_the_only_interface_warns_first(box, monkeypatch):
    client, files, trier, _ = box
    monkeypatch.setattr(
        netconf, "_run", lambda cmd, **k: (0, ONE_INTERFACE if "addr" in cmd else "", "")
    )
    res = client.post("/api/network/wired", json={"interface": "eth0", "mode": "dhcp"})

    assert res.status_code == 409
    assert "only interface" in res.get_json()["error"]
    assert files == {}, "it changed the last way in without being told to"


def test_the_warning_can_be_accepted_explicitly(box, monkeypatch):
    client, files, _, _ = box
    monkeypatch.setattr(
        netconf, "_run", lambda cmd, **k: (0, ONE_INTERFACE if "addr" in cmd else "", "")
    )
    res = client.post("/api/network/wired?understood=yes",
                      json={"interface": "eth0", "mode": "dhcp"})
    assert res.status_code == 200
    assert files


def test_with_two_interfaces_up_no_warning_is_needed(box):
    # Changing one from the other is the safe way round, and the UI says so.
    client, _, _, _ = box
    assert client.post("/api/network/wired",
                       json={"interface": "eth0", "mode": "dhcp"}).status_code == 200


# ==========================================================================
# The three separate answers
# ==========================================================================
def test_the_connectivity_test_gives_three_answers(box, monkeypatch):
    client, _, _, _ = box
    monkeypatch.setattr(netconf, "_has_link", lambda: True)
    monkeypatch.setattr(netconf, "_dns_resolves", lambda: False)
    monkeypatch.setattr(netconf, "_reaches_internet", lambda: True)

    body = client.get("/api/network/test").get_json()
    assert (body["link"], body["internet"], body["dns"]) == (True, True, False)
    assert "dns" in body["summary"].lower()


# ==========================================================================
# The hostname, which moves the address
# ==========================================================================
def test_changing_the_hostname_says_the_new_address(box, monkeypatch):
    client, _, _, _ = box
    monkeypatch.setattr("retrobox.servicectl._run", lambda cmd, **k: (0, ""))
    body = client.post("/api/network/hostname", json={"hostname": "loungetv"}).get_json()
    assert "loungetv.local" in body["message"]
    assert "stop working" in body["message"]


@pytest.mark.parametrize(
    "name",
    ["", "-bad", "bad-", "has space", "has_underscore", "x" * 40, None,
     "semi;colon", "../etc"],
)
def test_a_hostname_that_is_not_one_never_reaches_sudo(box, monkeypatch, name):
    client, _, _, _ = box
    ran = []
    monkeypatch.setattr(
        "retrobox.servicectl._run", lambda cmd, **k: (ran.append(cmd), (0, ""))[1]
    )
    assert client.post("/api/network/hostname", json={"hostname": name}).status_code == 400
    assert ran == []


# ==========================================================================
# The page after a Keep that could not be kept
# ==========================================================================
def test_the_panel_is_redrawn_even_when_keeping_the_change_failed(box):
    """Keep can now come back as a refusal, and the page has to redraw anyway.

    When the box can no longer confirm a trial, the settings go back and the
    request answers 400. If the page only reloads itself when Keep succeeded,
    the customer is left looking at "testing the new settings" and a counting
    clock for a change that has already been undone - the one thing this panel
    must never show. There is no JavaScript runner in this suite, so this is
    the page's own text: the redraw has to sit after the catch, not inside the
    try with the toast.
    """
    client, _, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    handler = page.split("keep.onclick")[1].split("const undo")[0]
    assert "loadNetwork()" in handler
    assert handler.index("catch") < handler.index("loadNetwork()"), (
        "a Keep that failed leaves the trial panel up with its countdown"
    )


def test_a_box_whose_sudo_rules_are_out_of_date_says_so_in_the_browser(
    tmp_path, runtime, monkeypatch
):
    """The whole chain, to the sentence the customer actually reads.

    A box installed before the netplan staging file existed has sudo rules
    naming the live file and nothing else, so the very first privileged step
    of a save is refused - and sudo's own words for a command it has no rule
    for are "a password is required", on a box that has no password to type.
    Re-running the installer is the entire fix, so it has to be in what comes
    back, not in a message on a later step this never reaches.
    """
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )

    def old_sudoers(cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        if "addr" in cmd:
            return 0, IP_JSON, ""
        if any(part.endswith(netconf.STAGING_SUFFIX) for part in cmd):
            return 1, "", "sudo: a password is required\n"
        return 0, "", ""

    monkeypatch.setattr(netconf, "_run", old_sudoers)
    trier = FakeTry()
    monkeypatch.setattr(netprobation, "NetplanTry", lambda *a, **k: trier)
    app = create_app(str(cfg))
    app.config.update(TESTING=True)

    res = app.test_client().post(
        "/api/network/wired", json={"interface": "eth0", "mode": "dhcp"}
    )
    assert res.status_code == 400, res.get_json()
    said = res.get_json()["error"]
    assert "install-service" in said, (
        f"all the box's owner is told is to type a password it does not "
        f"have: {said!r}"
    )
    assert trier.started == [], \
        "it put a change on trial that was never written to the disk"


def test_a_keep_the_box_cannot_start_using_is_shown_rather_than_only_recorded(box):
    """A message with no branch to render it is a message nobody reads.

    When the box finishes a keep for itself and then cannot make netplan use
    those settings, it records that - `in_effect` false, and a sentence saying
    to switch the box off and on again. Switching it off and on again is the
    entire fix, and this panel is the only place anybody would ever be told.
    There is no JavaScript runner in this suite, so this is the page's own
    text: drawProbation has to have a branch for it.
    """
    client, _, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    drawer = page.split("function drawProbation")[1].split("function findTheBoxAgain")[0]

    assert "in_effect" in drawer, (
        "the box marks a keep it is not running yet and the page has no "
        "branch that shows it, so the customer is never told to restart it"
    )
    assert "'kept'" in drawer, (
        "nothing in the panel distinguishes a kept change from any other"
    )


# ==========================================================================
# A change that was still on trial when the box lost power
#
# This is the one that ends in a truck roll. netplan try holds the far end of
# a terminal this process owns, so when the box goes off at the wall netplan
# puts its own configuration back - but OUR netplan files still hold the
# untested one, and the next boot applies it for good. If those settings are
# the reason the box cannot be reached, nobody can open the network page, so
# "put it back the next time somebody looks at the page" never happens.
# Start-up is the only moment that is guaranteed to come round again.
# ==========================================================================
def _interrupted_change(tmp_path, previous):
    """The record a dashboard leaves behind when it dies mid-trial."""
    import json
    import os

    (tmp_path / netprobation.STATE_NAME).write_text(json.dumps({
        "phase": "testing",
        "note": "eth0: 192.168.1.99/24",
        "handle": "netplan-try-from-a-dashboard-that-has-gone",
        # A different process entirely: the one that started this is gone.
        "owner_pid": os.getpid() + 1,
        "previous": previous,
        "started_at": time.time(),          # well inside its window
        "timeout": 120,
    }))


def _box_that_starts(tmp_path, monkeypatch, files, writer=None, ran=None):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "S"\n    path: {root / "sitcoms"}\n'
    )
    monkeypatch.setattr(
        netconf, "write_plan",
        writer if writer is not None else (lambda p, c: files.__setitem__(p, c)),
    )
    monkeypatch.setattr(netconf, "read_plan", lambda p: files.get(p))
    recorded = ran if ran is not None else []
    monkeypatch.setattr(
        netconf, "_run",
        lambda cmd, **k: (recorded.append(list(cmd)),
                          (0, IP_JSON if "addr" in cmd else "", ""))[1],
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app


def test_a_change_interrupted_by_the_wall_switch_is_put_back_at_start_up(
    tmp_path, runtime, monkeypatch
):
    files = {}
    _interrupted_change(tmp_path, {netconf.WIRED_FILE: "network: {the old one}\n"})

    _box_that_starts(tmp_path, monkeypatch, files)

    assert files.get(netconf.WIRED_FILE) == "network: {the old one}\n", (
        "the box came up holding a configuration nothing ever confirmed, and "
        "will go on doing so until somebody opens the network page - which "
        "they cannot do if that configuration is why it is unreachable"
    )


def test_the_settings_put_back_at_start_up_are_also_put_into_effect(
    tmp_path, runtime, monkeypatch
):
    """Rewriting the file is not what gets the box back on the network.

    This is the box a customer typed a wrong static address into: it moved,
    the browser lost it, nobody could press Undo, and it has been switched off
    and on again. netplan applied the bad file at boot, so the bad
    configuration is the live one. Putting the good file back and stopping
    there leaves the box exactly as unreachable as it was, for another whole
    session, with the good settings sitting on the disk unused.
    """
    files = {}
    ran = []
    _interrupted_change(tmp_path, {netconf.WIRED_FILE: "network: {the old one}\n"})

    _box_that_starts(tmp_path, monkeypatch, files, ran=ran)

    assert ["sudo", "-n", "netplan", "apply"] in ran, (
        "the box put the old settings back on disk and went on running the "
        "ones that took it off the network"
    )


def test_a_keep_the_box_never_finished_is_put_into_effect_at_start_up(
    tmp_path, runtime, monkeypatch
):
    """The other half of the interrupted change, and the quieter failure.

    The record says "keeping", which is written down before the newline that
    commits the change - so the box stopped in that window. Whatever stopped
    it took `netplan try`'s terminal with it, and netplan then put its own
    configuration back. The netplan file on the disk still holds what the
    customer kept, so the box is marked kept while running something else,
    and nothing changes that until it is switched off and on again.
    """
    import json
    import os

    files = {netconf.WIRED_FILE: "network: {the kept one}\n"}
    ran = []
    (tmp_path / netprobation.STATE_NAME).write_text(json.dumps({
        "phase": "keeping",
        "note": "eth0: 192.168.1.99/24",
        "handle": "netplan-try-from-a-dashboard-that-has-gone",
        "owner_pid": os.getpid() + 1,
        "previous": {netconf.WIRED_FILE: "network: {the old one}\n"},
        "started_at": time.time(),
        "timeout": 120,
    }))

    app = _box_that_starts(tmp_path, monkeypatch, files, ran=ran)

    assert files[netconf.WIRED_FILE] == "network: {the kept one}\n", \
        "it took away a change somebody kept"
    assert ["sudo", "-n", "netplan", "apply"] in ran, (
        "the box says those settings are kept and goes on running the ones "
        "netplan put back when the dashboard died"
    )
    change = app.test_client().get("/api/network").get_json()["change"]
    assert change["phase"] == "kept", change


def test_a_box_with_nothing_in_flight_writes_no_network_files_at_start_up(
    tmp_path, runtime, monkeypatch
):
    # The common case, every boot: one small file read that finds nothing.
    files = {}
    ran = []
    _box_that_starts(tmp_path, monkeypatch, files, ran=ran)
    assert files == {}
    assert ["sudo", "-n", "netplan", "apply"] not in ran, (
        "every boot bounces the network of a box with nothing wrong with it"
    )


def test_the_dashboard_still_comes_up_when_it_cannot_put_the_change_back(
    tmp_path, runtime, monkeypatch
):
    """Start-up housekeeping may not cost the customer the dashboard.

    The dashboard is the only thing somebody with a sick box can reach. If
    putting an interrupted change back fails - a read-only root, sudo not
    installed, netplan gone - it is logged and the pages still come up.
    """
    _interrupted_change(tmp_path, {netconf.WIRED_FILE: "network: {the old one}\n"})

    def refuse(path, content):
        raise netconf.NetworkError("read-only filesystem")

    app = _box_that_starts(tmp_path, monkeypatch, {}, writer=refuse)
    client = app.test_client()
    assert client.get("/api/network").status_code == 200
    assert client.get("/dash").status_code == 200


# ==========================================================================
# What a refused command is allowed to say
#
# sudo's own words for a command it has no rule for are "a password is
# required", on a box whose owner has no password, no keyboard and no way to
# read an install log. That sentence reaching the browser is what made one
# real unit's failure incomprehensible. It is translated once, in
# servicectl.explain_failure, and nothing here repeats the original.
# ==========================================================================
def test_a_refused_hostname_change_does_not_quote_sudo_at_the_customer(
    box, monkeypatch
):
    client, _, _, _ = box
    monkeypatch.setattr(
        "retrobox.servicectl._run",
        lambda cmd, **k: (1, "sudo: a password is required"),
    )
    res = client.post("/api/network/hostname", json={"hostname": "loungetv"})
    assert res.status_code == 503
    said = res.get_json()["error"]
    assert "sudo" not in said
    assert "password" not in said
    assert "install-service.sh" in said, "it does not say what would fix it"


def test_a_hostname_change_that_failed_for_another_reason_keeps_its_reason(
    box, monkeypatch
):
    """Translation is for sudo's words, not a blanket for everything."""
    client, _, _, _ = box
    monkeypatch.setattr(
        "retrobox.servicectl._run",
        lambda cmd, **k: (1, "Could not set static hostname: Read-only file system"),
    )
    said = client.post(
        "/api/network/hostname", json={"hostname": "loungetv"}
    ).get_json()["error"]
    assert "Read-only file system" in said


def test_a_refused_network_save_does_not_quote_sudo_at_the_customer(
    box, monkeypatch
):
    client, _, _, _ = box

    def refuse(path, content):
        raise netconf.NetworkError(
            "could not write the network configuration: sudo: a password is "
            "required" + netconf.NEEDS_THE_INSTALLER_AGAIN
        )

    monkeypatch.setattr(netconf, "write_plan", refuse)
    res = client.post("/api/network/wired", json={"interface": "eth0", "mode": "dhcp"})
    said = res.get_json()["error"]
    assert "sudo" not in said
    assert "password" not in said
    assert "install-service.sh" in said


def test_a_network_change_refused_for_a_real_reason_still_says_what_it_was(
    box, monkeypatch
):
    client, _, _, _ = box
    res = client.post(
        "/api/network/wired",
        json={"interface": "eth0", "mode": "static", "address": "not an address"},
    )
    assert res.status_code == 400
    assert "not an address" in res.get_json()["error"]
