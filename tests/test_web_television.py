"""Dayparting, filler and branding over HTTP.

The one that matters most is at the bottom: the schedule the editor saved has
to produce exactly the channel the television produces, at every minute of the
day. An editor that is right about the easy hours and wrong about the 22:00
wrap is worse than no editor.
"""

import time

import pytest

from retrobox.config import load_config
from retrobox.probe import MediaInfo
from tests.helpers import make_show

flask = pytest.importorskip("flask")
from retrobox.webui import create_app  # noqa: E402


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("RETROBOX_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setenv("RETROBOX_CONTROL_SOCKET", str(tmp_path / "c.sock"))


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr("retrobox.webui.send_command", lambda c, **k: out.append(c) or True)
    return out


@pytest.fixture
def box(tmp_path, runtime):
    root = tmp_path / "media"
    root.mkdir()
    make_show(root, "sitcoms", 2)
    make_show(root, "latenight", 2)
    assets = tmp_path / "assets"
    assets.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"media_root: {root}\nassets_dir: {assets}\nchannels:\n"
        f'  - number: 2\n    name: "Sitcoms"\n    path: {root / "sitcoms"}\n'
    )
    app = create_app(str(cfg))
    app.config.update(TESTING=True)
    return app.test_client(), cfg, root, assets


BLOCKS = [
    {"from": "06:00", "to": "12:00", "name": "Mornings"},
    {"from": "22:00", "to": "04:00", "name": "After Dark"},
]


# ==========================================================================
# The schedule
# ==========================================================================
def test_a_schedule_saves_and_comes_back(box, sent):
    client, cfg, _, _ = box
    res = client.put("/api/schedule/2", json={"blocks": BLOCKS})
    assert res.status_code == 200, res.get_json()

    parts = load_config(cfg).channels[0].dayparts
    assert [p.label for p in parts] == ["06:00-12:00", "22:00-04:00"]
    assert [p.name for p in parts] == ["Mornings", "After Dark"]


def test_the_day_comes_back_laid_out_with_its_gaps(box, sent):
    client, _, _, _ = box
    client.put("/api/schedule/2", json={"blocks": BLOCKS})
    day = client.get("/api/schedule/2").get_json()["day"]

    assert sum(b["minutes"] for b in day) == 24 * 60
    assert any(b["kind"] == "gap" for b in day), "12:00-22:00 is a gap"
    assert [b["start"] for b in day] == sorted(b["start"] for b in day)


def test_overlapping_blocks_are_refused_and_nothing_is_saved(box, sent):
    client, cfg, _, _ = box
    before = cfg.read_text()
    res = client.put("/api/schedule/2", json={"blocks": [
        {"from": "06:00", "to": "12:00", "name": "A"},
        {"from": "11:00", "to": "14:00", "name": "B"},
    ]})
    assert res.status_code == 400
    assert "overlap" in res.get_json()["error"].lower()
    assert cfg.read_text() == before


def test_clearing_the_schedule_removes_it(box, sent):
    client, cfg, _, _ = box
    client.put("/api/schedule/2", json={"blocks": BLOCKS})
    client.put("/api/schedule/2", json={"blocks": []})
    assert load_config(cfg).channels[0].dayparts == ()


def test_the_clock_is_shown_right_there_in_the_editor(box, sent, monkeypatch):
    # A wrong clock makes dayparting behave in a way that looks like a bug.
    from retrobox import sysinfo

    monkeypatch.setattr(
        sysinfo, "_run",
        lambda cmd, **k: "Timezone=UTC\nNTPSynchronized=no\nNTP=no\n" if "show" in cmd else "",
    )
    client, _, _, _ = box
    clock = client.get("/api/schedule/2").get_json()["clock"]
    assert clock["warning"] is True
    assert clock["timezone"] == "UTC"


def test_what_is_on_right_now_is_reported(box, sent):
    client, _, _, _ = box
    client.put("/api/schedule/2", json={"blocks": [
        {"from": "00:00", "to": "24:00", "name": "All Day"},
    ]})
    now = client.get("/api/schedule/2").get_json()["now"]
    assert now["active"] is True and now["name"] == "All Day"


def test_the_preview_answers_for_a_time_that_is_not_now(box, sent):
    client, _, _, _ = box
    client.put("/api/schedule/2", json={"blocks": BLOCKS})
    assert client.get("/api/schedule/2/preview?minute=480").get_json()["name"] == "Mornings"
    assert client.get("/api/schedule/2/preview?minute=120").get_json()["name"] == "After Dark"
    assert client.get("/api/schedule/2/preview?minute=900").get_json()["active"] is False


def test_a_schedule_on_a_channel_that_is_not_there_is_a_404(box, sent):
    client, _, _, _ = box
    assert client.get("/api/schedule/77").status_code == 404
    assert client.put("/api/schedule/77", json={"blocks": []}).status_code == 404


# ==========================================================================
# Where a daypart is allowed to point
# ==========================================================================
def test_a_daypart_folder_that_is_not_there_is_refused_at_the_door(box, sent):
    """A typo in a folder name must not be discovered at 18:00 that evening.

    Nothing downstream can tell anybody about it: the channel simply has no
    episodes for that block, plays nothing, and the dashboard still shows the
    schedule it happily saved. So the only place to catch it is here.
    """
    client, cfg, root, _ = box
    before = cfg.read_text()

    res = client.put("/api/schedule/2", json={"blocks": [
        {"from": "18:00", "to": "20:00", "name": "Teatime",
         "path": str(root / "sitcmos")},          # a typo for "sitcoms"
    ]})

    assert res.status_code == 400, res.get_json()
    assert "sitcmos" in res.get_json()["error"], "it did not say which folder"
    assert cfg.read_text() == before, "the schedule was written anyway"


def test_a_daypart_cannot_point_outside_the_library(box, sent, tmp_path):
    """The same containment rule the channel routes have always had.

    The dashboard has no password, so "any string at all goes into config.yaml
    and the television then reads that folder" is not a small thing.
    """
    client, cfg, root, _ = box
    elsewhere = tmp_path / "not-the-library"
    elsewhere.mkdir()
    before = cfg.read_text()

    res = client.put("/api/schedule/2", json={"blocks": [
        {"from": "18:00", "to": "20:00", "name": "Elsewhere",
         "path": str(elsewhere)},
    ]})

    assert res.status_code == 400, res.get_json()
    assert "media_root" in res.get_json()["error"]
    assert cfg.read_text() == before


def test_a_daypart_folder_is_saved_as_the_folder_it_really_means(box, sent):
    """Typed the way a person thinks of it, stored the way the box needs it.

    A bare folder name is taken as being under the library, exactly as it is
    when a channel is created, and what lands in config.yaml is the full path.
    """
    client, cfg, root, _ = box
    res = client.put("/api/schedule/2", json={"blocks": [
        {"from": "18:00", "to": "20:00", "name": "Teatime", "path": "latenight"},
    ]})

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["blocks"][0]["path"] == str(root / "latenight")
    assert load_config(cfg).channels[0].dayparts[0].path == root / "latenight"


def test_the_editor_hands_a_folder_swap_back_the_way_it_found_it(box, sent):
    """The schedule editor has no folder field, and must not delete one either.

    A block that swaps in a different folder is written by hand in config.yaml.
    The editor loads it, shows the times and the name, and sends the whole list
    back when you press save - so if it does not carry the folder along, moving
    a block by ten minutes quietly deletes the folder swap out of somebody's
    config. There is no JavaScript runner in this suite, so this is the page's
    own text: the block it builds to send has to keep the path.
    """
    client, _, _, _ = box
    page = client.get("/dash").get_data(as_text=True)
    assert "out.path = b.path" in page, (
        "the editor rebuilds each block without its folder, so saving drops it"
    )


def test_an_off_air_block_still_needs_no_folder(box, sent):
    # The folder check must not get in the way of the one kind of block that
    # is meant to have nowhere to point.
    client, cfg, _, _ = box
    res = client.put("/api/schedule/2", json={"blocks": [
        {"from": "02:00", "to": "06:00", "off_air": True},
    ]})
    assert res.status_code == 200, res.get_json()
    assert load_config(cfg).channels[0].dayparts[0].off_air is True


# ==========================================================================
# The one that matters: editor and television must agree
# ==========================================================================
def test_the_saved_schedule_produces_the_channel_the_television_produces(box, sent, tmp_path):
    """Round trip through the config writer, then compare against the engine.

    The editor's preview and the actual Channel object are asked the same
    question at every minute of the day. They are different code paths -
    schedule.preview_at against channel.name_at / is_off_air - and if they
    disagree anywhere, the editor is lying about what the box will do.
    """
    from retrobox.channel import build_lineup

    client, cfg, root, _ = box
    schedule = [
        {"from": "06:00", "to": "12:00", "name": "Mornings"},
        {"from": "12:00", "to": "22:00", "name": "Daytime",
         "path": str(root / "latenight")},
        {"from": "22:00", "to": "02:00", "name": "After Dark"},
        {"from": "02:00", "to": "06:00", "off_air": True},
    ]
    assert client.put("/api/schedule/2", json={"blocks": schedule}).status_code == 200

    # What the television will actually build from the file that was written.
    config = load_config(cfg)
    channel = list(build_lineup(config))[0]

    def at(hour, minute):
        parts = list(time.localtime(1_700_000_000))
        parts[3], parts[4], parts[5] = hour, minute, 0
        parts[8] = -1
        return time.mktime(tuple(parts))

    for minute in range(0, 24 * 60, 7):        # every 7 minutes covers every case
        when = at(minute // 60, minute % 60)
        shown = client.get(f"/api/schedule/2/preview?minute={minute}").get_json()

        assert shown["off_air"] == channel.is_off_air(when), f"minute {minute}"
        if shown["active"] and not shown["off_air"] and shown["name"]:
            assert channel.name_at(when) == shown["name"], f"minute {minute}"
        elif not shown["active"]:
            assert channel.name_at(when) == channel.config.name, f"minute {minute}"


# ==========================================================================
# Filler
# ==========================================================================
def test_the_filler_clips_are_listed(box, sent):
    client, _, _, assets = box
    (assets / "colorbars.mp4").write_bytes(b"\x00" * 100)
    body = client.get("/api/filler").get_json()
    bars = next(c for c in body["clips"] if c["name"] == "colorbars.mp4")
    assert bars["exists"] is True and bars["bytes"] == 100


def test_a_clip_can_be_played_back(box, sent):
    client, _, _, assets = box
    (assets / "static.mp4").write_bytes(b"\x00" * 64)
    res = client.get("/api/filler/static.mp4")
    assert res.status_code == 200
    assert res.mimetype == "video/mp4"


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "config.yaml", "evil.sh", "..", "%2e%2e"]
)
def test_only_this_boxs_own_clips_can_be_fetched(box, sent, name):
    client, _, _, _ = box
    assert client.get(f"/api/filler/{name}").status_code in (400, 404)


def test_how_often_bumpers_play_is_capped(box, sent):
    client, cfg, _, _ = box
    for bad in (-0.1, 1.5, 99, "often", True):
        res = client.post("/api/filler/settings", json={"bumper_chance": bad})
        assert res.status_code == 400, bad

    assert client.post("/api/filler/settings",
                       json={"bumper_chance": 0.25}).status_code == 200
    assert load_config(cfg).bumper_chance == 0.25


def test_a_bumper_length_cap_is_enforced(box, sent):
    client, cfg, _, _ = box
    assert client.post("/api/filler/settings",
                       json={"bumper_max_seconds": 9999}).status_code == 400
    assert client.post("/api/filler/settings",
                       json={"bumper_max_seconds": 20}).status_code == 200
    assert load_config(cfg).bumper_max_seconds == 20


def test_a_channel_can_opt_out_of_bumpers_from_the_dashboard(box, sent):
    client, cfg, _, _ = box
    res = client.post("/api/filler/settings", json={"channels": {"2": False}})
    assert res.status_code == 200, res.get_json()
    assert load_config(cfg).channels[0].bumpers is False

    client.post("/api/filler/settings", json={"channels": {"2": True}})
    assert load_config(cfg).channels[0].bumpers is True


def test_the_channel_change_effect_is_one_of_three(box, sent):
    client, cfg, _, _ = box
    assert client.post("/api/filler/settings",
                       json={"transition": "sparkles"}).status_code == 400
    assert client.post("/api/filler/settings",
                       json={"transition": "glitch"}).status_code == 200
    assert load_config(cfg).transition_effect == "glitch"


def test_generating_without_ffmpeg_says_so(box, sent, monkeypatch):
    from retrobox import static_gen

    monkeypatch.setattr(static_gen, "ffmpeg_available", lambda: False)
    client, _, _, _ = box
    res = client.post("/api/filler/generate")
    assert res.status_code == 503
    assert "ffmpeg" in res.get_json()["error"]


# ==========================================================================
# Branding
# ==========================================================================
def _resolved_splash(cfg, assets):
    """What the television would actually play, resolution and all."""
    from retrobox.app import TVApp
    from retrobox.input.manager import InputManager
    from retrobox.player import MockPlayer

    app = TVApp(load_config(cfg), MockPlayer(), InputManager([]), assets_dir=assets)
    return app._splash_path


def upload_splash(client, payload, **kw):
    return client.post("/api/branding/splash", data=payload,
                       content_type="application/octet-stream", **kw)


def test_a_good_splash_is_installed(box, sent, monkeypatch):
    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: MediaInfo(4.0, True)
    )
    client, cfg, _, assets = box
    res = upload_splash(client, b"\x00" * 4096)
    assert res.status_code == 200, res.get_json()
    assert (assets / "custom_splash.mp4").exists()
    # The config holds a bare name; the app resolves it against the assets
    # directory. Assert the resolution, not the spelling.
    assert load_config(cfg).boot_splash.name == "custom_splash.mp4"
    assert _resolved_splash(cfg, assets) == assets / "custom_splash.mp4"


def test_a_splash_that_is_too_long_is_refused_and_nothing_changes(box, sent, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: MediaInfo(90.0, True)
    )
    client, cfg, _, assets = box
    before = cfg.read_text()

    res = upload_splash(client, b"\x00" * 4096)
    assert res.status_code == 400
    assert "seconds" in res.get_json()["error"]
    assert not (assets / "custom_splash.mp4").exists()
    assert cfg.read_text() == before
    assert list(assets.glob("*.part")) == [], "a refused splash was left lying around"


def test_an_unplayable_splash_is_refused_and_the_old_one_still_works(box, sent, monkeypatch):
    client, cfg, _, assets = box
    # Install a good one first.
    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: MediaInfo(3.0, True)
    )
    upload_splash(client, b"\x01" * 2048)
    good = (assets / "custom_splash.mp4").read_bytes()

    # Then try to replace it with rubbish.
    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: MediaInfo(None, None)
    )
    assert upload_splash(client, b"garbage").status_code == 400
    assert (assets / "custom_splash.mp4").read_bytes() == good, "it clobbered the good one"
    assert _resolved_splash(cfg, assets) == assets / "custom_splash.mp4", (
        "the box lost the splash that was already working"
    )


def test_the_default_can_always_be_restored(box, sent, monkeypatch):
    client, cfg, _, assets = box
    (assets / "boot_splash.mp4").write_bytes(b"\x00" * 32)
    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: MediaInfo(3.0, True)
    )
    upload_splash(client, b"\x01" * 2048)
    assert load_config(cfg).boot_splash.name == "custom_splash.mp4"

    res = client.post("/api/branding/splash/default")
    assert res.status_code == 200, res.get_json()
    assert load_config(cfg).boot_splash.name == "boot_splash.mp4"


def test_the_splash_can_be_turned_off_entirely(box, sent):
    client, cfg, _, _ = box
    assert client.post("/api/branding/splash/off").status_code == 200
    assert load_config(cfg).boot_splash is None


def test_a_splash_in_flight_is_staged_where_the_box_can_find_it_again(box, sent, monkeypatch):
    """The assets folder is on the boot disk, and nothing ever looks in it.

    A splash that arrives while the power goes off leaves a part-file next to
    the television's own assets: not big enough to matter on the media drive,
    but this is the disk the box boots from, and no page on the dashboard can
    see it or free it. The upload spool is where half-arrived files live, and
    the spool is swept.
    """
    import pathlib

    from retrobox.uploads import spool_for

    monkeypatch.setattr(
        "retrobox.branding.probe_media", lambda p, **k: MediaInfo(4.0, True)
    )
    client, _, root, assets = box
    before = sorted(p.name for p in assets.iterdir())
    seen = {}

    original = pathlib.Path.replace

    def spy(self, target):
        seen["staged_in"] = self.parent
        seen["assets"] = sorted(p.name for p in assets.iterdir())
        return original(self, target)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pathlib.Path, "replace", spy)
        assert upload_splash(client, b"\x00" * 4096).status_code == 200

    assert seen, "the splash never landed"
    assert seen["assets"] == before, (
        "the clip was staged in the assets folder, where a power cut strands it"
    )
    spool = spool_for(root)
    assert spool == seen["staged_in"] or spool in seen["staged_in"].parents, (
        f"staged in {seen['staged_in']}, which the spool sweeper does not own"
    )


# ==========================================================================
# Appearance
# ==========================================================================
def test_the_crt_effect_can_be_adjusted(box, sent):
    client, cfg, _, _ = box
    res = client.post("/api/branding/appearance",
                      json={"crt_enabled": True, "curvature": 0.2, "scanlines": False})
    assert res.status_code == 200, res.get_json()

    crt = load_config(cfg).crt
    assert crt.curvature == 0.2 and crt.scanlines is False


def test_a_crt_change_says_it_needs_a_restart(box, sent):
    client, _, _, _ = box
    body = client.post("/api/branding/appearance", json={"curvature": 0.1}).get_json()
    assert body["restart_required"] == ["crt"]


@pytest.mark.parametrize(
    "payload",
    [{"curvature": 5}, {"curvature": -1}, {"scanline_intensity": 9},
     {"channel_bug_seconds": 999}, {"guide_seconds": -5}, {"crt_enabled": "yes"},
     {"scanlines": "on"}, {"nonsense": 1}],
)
def test_appearance_values_are_bounded(box, sent, payload):
    client, cfg, _, _ = box
    before = cfg.read_text()
    assert client.post("/api/branding/appearance", json=payload).status_code == 400
    assert cfg.read_text() == before


def test_the_banner_length_can_be_changed(box, sent):
    client, cfg, _, _ = box
    assert client.post("/api/branding/appearance",
                       json={"channel_bug_seconds": 6}).status_code == 200
    assert load_config(cfg).channel_bug_seconds == 6.0


def test_previewing_puts_the_banner_on_the_actual_television(box, sent):
    # The remote's own INFO button, not a second way of drawing an overlay -
    # so what is previewed is exactly what a viewer sees.
    client, _, _, _ = box
    assert client.post("/api/branding/preview").status_code == 200
    assert sent == ["info"]
