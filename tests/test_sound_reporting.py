"""The player is the authority on what the player is doing.

The System page and the Watch tab once contradicted each other about the same
mpv, at the same moment, on the same box: "hardware decode: not active" beside
"hw decode: vaapi". They were answering from two different places - one asked
the television, the other forked ``vainfo`` from a process that was not
allowed to open the GPU and reported the refusal as a fact about the hardware.

So there is now a rule with a test behind it: **the player wins**, the
external probe is demoted to what the box is CAPABLE of, and a disagreement
between them is logged as the bug it is.

Nothing here opens a real audio device.
"""

from types import SimpleNamespace

import pytest

from retrobox import status as status_mod
from retrobox import sysinfo
from retrobox.player import MockPlayer
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app  # noqa: E402


PROBE_SAYS_NOTHING_WORKS = {
    "gpu_vendor": "intel",
    "gpu_description": "Intel GeminiLake [UHD Graphics 600] [8086:3185]",
    "audio_devices": [],
    "decode": {"working": False, "summary": "Hardware decode: not active",
               "profiles": []},
    "audio": {"working": False, "summary": "Sound: no HDMI audio output on this box",
              "advice": ""},
}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"media_root: {root}\nchannels:\n"
                   f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n')
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, **k: "")
    monkeypatch.setattr(
        sysinfo.shutil, "disk_usage",
        lambda p: SimpleNamespace(total=500 * 1024**3, used=100 * 1024**3,
                                  free=400 * 1024**3))
    monkeypatch.setattr(sysinfo, "hardware", lambda: dict(PROBE_SAYS_NOTHING_WORKS))
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client()


# ==========================================================================
# The bug, as an assertion
# ==========================================================================
def test_the_player_beats_the_probe_about_decoding(box, caplog):
    """The exact contradiction from the bench box: the probe says software,
    the television says vaapi. The television is right - it made the choice."""
    status_mod.write_status({
        "decode": {"hwdec": "vaapi", "playing": True, "working": True},
    })
    hardware = box.get("/api/system").get_json()["hardware"]
    assert hardware["decode"]["working"] is True
    assert "vaapi" in hardware["decode"]["summary"]
    assert hardware["decode"]["source"] == "player"
    assert hardware["decode"]["probe_working"] is False, "the probe's answer is kept"
    assert hardware["decode"]["disagreed"] is True


def test_the_disagreement_is_logged_as_a_bug(box, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        status_mod.write_status({
            "decode": {"hwdec": "vaapi", "playing": True, "working": True}})
        box.get("/api/system")
    assert "trusting the player" in caplog.text
    assert "render" in caplog.text, "the log names what to actually check"


def test_the_player_beats_the_probe_about_sound(box):
    status_mod.write_status({
        "audio": {"device": "alsa/hdmi:CARD=PCH,DEV=1", "ao": "alsa",
                  "working": True, "channels": "stereo", "has_track": True,
                  "summary": "Sound: HDMI 1 - SAMSUNG"},
    })
    sound = box.get("/api/system").get_json()["hardware"]["sound"]
    assert sound["working"] is True
    assert sound["device"] == "alsa/hdmi:CARD=PCH,DEV=1"
    assert sound["source"] == "player"
    assert sound["disagreed"] is True


def test_the_device_shown_comes_from_the_player_not_the_probe(box):
    """The probe found no outputs at all. The television is playing through
    one. The page must show the one that exists."""
    status_mod.write_status({
        "audio": {"device": "alsa/hdmi:CARD=PCH,DEV=2", "ao": "alsa",
                  "working": True, "has_track": True},
    })
    sound = box.get("/api/system").get_json()["hardware"]["sound"]
    assert sound["device"] == "alsa/hdmi:CARD=PCH,DEV=2"
    assert box.get("/api/system").get_json()["hardware"]["audio_devices"] == []


# ==========================================================================
# An idle television has not decided anything
# ==========================================================================
def test_nothing_playing_is_said_rather_than_guessed_at(box):
    status_mod.write_status({"decode": {"hwdec": None, "playing": False,
                                        "working": None}})
    decode = box.get("/api/system").get_json()["hardware"]["decode"]
    assert decode["working"] is None
    assert decode["source"] == "idle"
    assert "nothing is playing" in decode["summary"].lower()


def test_capability_is_still_reported_while_idle(box, monkeypatch):
    """Demoted, not deleted: with nothing playing, what the box CAN do is
    exactly the useful thing to say."""
    probe = dict(PROBE_SAYS_NOTHING_WORKS)
    probe["decode"] = {"working": True, "summary": "ok",
                       "profiles": ["VAProfileHEVCMain10", "VAProfileH264High"]}
    monkeypatch.setattr(sysinfo, "hardware", lambda: probe)
    status_mod.write_status({"decode": {"hwdec": None, "playing": False,
                                        "working": None}})
    summary = box.get("/api/system").get_json()["hardware"]["decode"]["summary"]
    assert "HEVCMain10" in summary


def test_the_sound_sentence_does_not_say_sound_twice(box):
    """The setup line is already a whole sentence starting "Sound:", and
    stacking a second prefix on it reads like a stutter on the page."""
    status_mod.write_status({
        "audio": {"device": None, "working": False, "has_track": True,
                  "summary": "Sound: no display advertising audio is attached"},
    })
    summary = box.get("/api/system").get_json()["hardware"]["sound"]["summary"]
    assert summary.count("Sound:") == 1, summary
    assert "no display advertising audio" in summary


def test_a_file_with_no_soundtrack_is_not_a_fault(box):
    status_mod.write_status({
        "audio": {"device": "alsa/hdmi:CARD=PCH,DEV=0", "working": False,
                  "has_track": False},
    })
    sound = box.get("/api/system").get_json()["hardware"]["sound"]
    assert "no soundtrack" in sound["summary"]
    assert "nothing is wrong" in sound["summary"]


# ==========================================================================
# Test sound
# ==========================================================================
def test_the_test_tone_says_so_when_the_television_is_not_running(box):
    answer = box.post("/api/system/sound/test").get_json()
    assert answer["ok"] is False
    assert "not running" in answer["error"]
    assert "terminal" not in answer["error"].lower()
    assert "ssh" not in answer["error"].lower()


def test_repair_never_sends_anybody_to_a_terminal(box, monkeypatch):
    monkeypatch.setattr("retrobox.webui.send_command", lambda *a, **k: False)
    answer = box.post("/api/system/hardware/repair").get_json()
    words = " ".join(answer.get("changed", []) + [answer.get("advice") or ""]).lower()
    assert "terminal" not in words and "ssh" not in words and "sudo" not in words


def test_repair_is_safe_to_press_twice(box, monkeypatch):
    monkeypatch.setattr("retrobox.webui.send_command", lambda *a, **k: True)
    monkeypatch.setattr("retrobox.audioout.unmute", lambda *a, **k: ["Master"])
    first = box.post("/api/system/hardware/repair").get_json()
    second = box.post("/api/system/hardware/repair").get_json()
    assert first["ok"] is True and second["ok"] is True
    assert first["changed"] == second["changed"]


# ==========================================================================
# The television end of the tone
# ==========================================================================
def _app(tmp_path):
    from retrobox.app import TVApp
    from retrobox.config import load_config

    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    make_show(root, "sitcoms", 2)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"media_root: {root}\nchannels:\n"
                   f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n')
    return TVApp.from_config(load_config(cfg), dry_run=True)


def test_a_tone_plays_with_nothing_on(tmp_path):
    app = _app(tmp_path)
    assert app.play_test_tone(seconds=0.01) is True
    assert app.player.test_tones == [(0.01, 440)]


def test_a_tone_plays_while_something_is_on_and_the_programme_comes_back(tmp_path):
    app = _app(tmp_path)
    episode = next(iter(app.lineup)).episodes[0]
    app.player.play(episode)
    app._playing_path = episode

    assert app.play_test_tone(seconds=0.01) is True
    assert app.player.test_tones

    # The restore is on a short timer so the picture never stops on a tone.
    import time
    time.sleep(0.6)
    assert app.player.current == episode, "the programme was put back"


def test_a_box_with_no_audio_hardware_at_all_still_starts(tmp_path, monkeypatch):
    """Requirement seven: never fatal. No sockets, no player devices, and the
    television still comes up and plays a picture."""
    monkeypatch.setattr("retrobox.eld.hdmi_outputs", lambda **k: [])
    app = _app(tmp_path)
    assert app._audio_setup.device is None
    assert app._audio_setup.fatal is False
    assert len(app.lineup) >= 1


def test_the_status_snapshot_carries_what_the_player_is_doing(tmp_path):
    app = _app(tmp_path)
    app.player.audio_status = {"device": "alsa/hdmi:CARD=PCH,DEV=1",
                               "ao": "alsa", "active": True,
                               "channels": "stereo", "track": True}
    app.player.hwdec = "vaapi"
    snapshot = app.build_status()
    assert snapshot["audio"]["device"] == "alsa/hdmi:CARD=PCH,DEV=1"
    assert snapshot["audio"]["working"] is True
    assert "decode" in snapshot and "playing" in snapshot["decode"]
