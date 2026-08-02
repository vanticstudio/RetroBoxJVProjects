import random

from retrobox.channel import (
    BroadcastSchedule,
    Channel,
    ChannelLineup,
    build_lineup,
    detect_season,
    scan_episodes,
)
from retrobox.config import config_from_dict
from tests.helpers import make_show


def _channel(tmp_path, name="latenight", episodes=4, **kw):
    folder = make_show(tmp_path, name, episodes)
    from retrobox.config import ChannelConfig

    cfg = ChannelConfig(number=kw.pop("number", 3), name=name, path=folder)
    eps = scan_episodes(folder, [".mp4"])
    return Channel(cfg, eps, rng=random.Random(0), **kw)


def test_scan_episodes_sorted_and_filtered(tmp_path):
    folder = make_show(tmp_path, "latenight", 3)
    (folder / "notes.txt").write_text("nope")
    (folder / ".DS_Store").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"])
    assert [p.name for p in eps] == [
        "latenight_ep01.mp4",
        "latenight_ep02.mp4",
        "latenight_ep03.mp4",
    ]


def test_detect_season():
    assert detect_season("Late Night S06E01.mp4") == 6
    assert detect_season("latenight.s6e12.mkv") == 6
    assert detect_season("Season 12/ep03.mp4") == 12
    assert detect_season("Late Night 6x05.mp4") == 6
    assert detect_season("The Late Show Reunion.mp4") is None


def test_scan_exclude_globs(tmp_path):
    folder = tmp_path / "latenight"
    (folder / "Season 1").mkdir(parents=True)
    (folder / "Specials").mkdir(parents=True)
    (folder / "Season 1" / "S01E01.mp4").write_bytes(b"")
    (folder / "Specials" / "Late Night Special.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude=["*special*"])
    names = [p.name for p in eps]
    assert names == ["S01E01.mp4"]


def test_scan_exclude_seasons(tmp_path):
    folder = tmp_path / "latenight"
    folder.mkdir()
    for s in (1, 5, 6, 7, 25):
        (folder / f"Late Night S{s:02d}E01.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude_seasons=set(range(6, 26)))
    seasons = sorted(detect_season(p.name) for p in eps)
    assert seasons == [1, 5]  # 6..25 removed


def test_build_lineup_applies_channel_excludes(tmp_path):
    folder = tmp_path / "latenight"
    folder.mkdir()
    (folder / "Late Night S01E01.mp4").write_bytes(b"")
    (folder / "Late Night S06E01.mp4").write_bytes(b"")
    (folder / "Late Night Special.mp4").write_bytes(b"")
    cfg = config_from_dict(
        {
            "channels": [
                {
                    "number": 3,
                    "name": "Late Night",
                    "path": str(folder),
                    "exclude": ["*special*"],
                    "exclude_seasons": ["6-25"],
                }
            ]
        }
    )
    lineup = build_lineup(cfg)
    eps = list(lineup)[0].episodes
    assert [p.name for p in eps] == ["Late Night S01E01.mp4"]


def test_scan_recursive(tmp_path):
    base = tmp_path / "show"
    (base / "season1").mkdir(parents=True)
    (base / "season2").mkdir(parents=True)
    (base / "season1" / "a.mp4").write_bytes(b"")
    (base / "season2" / "b.mp4").write_bytes(b"")
    assert len(scan_episodes(base, [".mp4"], recursive=True)) == 2
    assert len(scan_episodes(base, [".mp4"], recursive=False)) == 0


def test_tune_in_random_plays_from_start(tmp_path):
    ch = _channel(tmp_path, tune_in="random")
    req = ch.tune_in()
    assert req is not None
    assert req.start == 0.0
    assert req.path in ch.episodes


def test_advance_continues_shuffle(tmp_path):
    ch = _channel(tmp_path, episodes=4, tune_in="random")
    seen = {ch.tune_in().path}
    for _ in range(3):
        seen.add(ch.advance().path)
    assert len(seen) == 4  # every episode shown before repeats


def test_start_offset_fixed(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=5.0, start_offset_max=5.0)
    assert ch.tune_in().start == 5.0
    assert ch.advance().start == 5.0


def test_start_offset_range(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=6.0, start_offset_max=10.0)
    starts = [ch.tune_in().start for _ in range(20)] + [ch.advance().start for _ in range(20)]
    assert all(6.0 <= s <= 10.0 for s in starts)
    assert len(set(round(s, 3) for s in starts)) > 1  # actually varies


def test_resume_mode_remembers_position(tmp_path):
    ch = _channel(tmp_path, tune_in="resume")
    first = ch.tune_in()
    ch.remember(first.path, 123.5)
    again = ch.tune_in()
    assert again.path == first.path
    assert again.start == 123.5


def test_empty_channel_returns_none(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    from retrobox.config import ChannelConfig

    ch = Channel(ChannelConfig(number=9, name="Empty", path=folder), [])
    assert ch.is_empty
    assert ch.tune_in() is None
    assert ch.advance() is None


def test_broadcast_schedule_positions():
    from pathlib import Path

    eps = [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")]
    durs = [100.0, 200.0, 300.0]
    sched = BroadcastSchedule(eps, durs, epoch=0.0, rng=random.Random(0))
    # At t=0 we are at the start of the first item in the (shuffled) order.
    first = sched.at(0.0)
    assert first.start == 0.0
    # The schedule is a loop of total length 600s; t=600 == t=0.
    assert sched.at(600.0).path == first.path
    # 50s into the cycle we should still be within the first item, offset 50.
    assert sched.at(50.0).start == 50.0


def test_broadcast_tune_in_uses_real_time(tmp_path, monkeypatch):
    # Force probe_duration to a known value so we don't need ffprobe/real media.
    import retrobox.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 60.0)
    ch = _channel(tmp_path, episodes=3, tune_in="broadcast")
    # Two tune-ins at different times should generally land at different offsets.
    r1 = ch.tune_in(now=0.0)
    r2 = ch.tune_in(now=30.0)
    assert r1.start == 0.0
    assert r2.start == 30.0


def test_lineup_navigation(tmp_path):
    for n in ("a", "b", "c"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [
                {"number": 2, "name": "A", "path": str(tmp_path / "a")},
                {"number": 4, "name": "B", "path": str(tmp_path / "b")},
                {"number": 7, "name": "C", "path": str(tmp_path / "c")},
            ],
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [2, 4, 7]
    assert lineup.current.number == 2
    assert lineup.up().number == 4
    assert lineup.up().number == 7
    assert lineup.up().number == 2  # wraps
    assert lineup.down().number == 7  # wraps back
    assert lineup.select_number(4).number == 4
    assert lineup.select_number(99) is None
    assert lineup.has_number(7)


def test_lineup_sorted_by_number(tmp_path):
    for n in ("a", "b"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "channels": [
                {"number": 9, "name": "Nine", "path": str(tmp_path / "a")},
                {"number": 3, "name": "Three", "path": str(tmp_path / "b")},
            ]
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [3, 9]


# --------------------------------------------------------------------------
# shuffle: false  (sequential channels)
# --------------------------------------------------------------------------
def test_shuffle_false_plays_in_order_and_loops(tmp_path):
    from retrobox.channel import build_lineup
    from retrobox.config import config_from_dict

    make_show(tmp_path, "serial", 4)
    cfg = config_from_dict(
        {
            "start_offset": 0,
            "channels": [
                {"number": 2, "name": "Serial", "path": str(tmp_path / "serial"),
                 "shuffle": False}
            ],
        }
    )
    channel = build_lineup(cfg).current
    order = [channel.tune_in().path.name for _ in range(6)]
    expected = [p.name for p in channel.episodes]
    assert order == expected + expected[:2]   # in order, then wraps


def test_shuffle_true_is_still_the_default(tmp_path):
    from retrobox.channel import build_lineup
    from retrobox.config import config_from_dict

    make_show(tmp_path, "mixed", 6)
    cfg = config_from_dict(
        {
            "shuffle_seed": 5,
            "start_offset": 0,
            "channels": [{"number": 2, "name": "Mixed", "path": str(tmp_path / "mixed")}],
        }
    )
    channel = build_lineup(cfg).current
    assert channel.shuffle is True
    order = [channel.tune_in().path.name for _ in range(6)]
    assert sorted(order) == sorted(p.name for p in channel.episodes)  # all of them...
    assert order != [p.name for p in channel.episodes]                # ...but shuffled


def test_shuffle_false_keeps_broadcast_order(tmp_path):
    from retrobox.channel import BroadcastSchedule

    paths = [tmp_path / f"{i}.mp4" for i in range(4)]
    schedule = BroadcastSchedule(
        paths, [100.0] * 4, epoch=0.0, rng=random.Random(1), shuffle=False
    )
    assert [schedule.at(t).path for t in (0, 100, 200, 300)] == paths


# ==========================================================================
# Hidden FOLDERS, not just hidden files
# ==========================================================================
def test_a_channel_pointed_at_the_library_root_does_not_air_the_trash(tmp_path):
    """Deleting an episode must not put it back on the air.

    The dashboard's delete moves a file into `.retrobox-trash` inside the media
    library rather than unlinking it, so an owner has a fortnight to change
    their mind. That only works if the trash is invisible to scanning.

    The dot-prefix was assumed to cover it. It did not: the check was on the
    FILE name, and `rglob` walks straight into a dot-folder, so a channel whose
    path is the library root itself picked the trashed episode back up and
    aired it. Nothing the box creates makes such a channel, but a person can
    write one by hand - and a customer watching an episode they deleted last
    week would have no idea why.
    """
    root = tmp_path / "library"
    (root / "shows").mkdir(parents=True)
    (root / "shows" / "ep1.mp4").write_bytes(b"x")
    trashed = root / ".retrobox-trash" / "abc" / "payload"
    trashed.mkdir(parents=True)
    (trashed / "deleted.mp4").write_bytes(b"x")

    found = scan_episodes(root, (".mp4",))

    assert (root / "shows" / "ep1.mp4") in found
    assert not [p for p in found if ".retrobox-trash" in p.parts], (
        "a trashed episode is still on the air: " + str(found)
    )


def test_the_shipped_welcome_clip_still_plays(tmp_path):
    """The guard above must not break a fresh box.

    The installer seeds `<media>/.welcome` with the boot splash so a box with no
    library yet still has something to show, and points a channel straight at
    that folder (installer/provision.sh). The folder is dot-prefixed on purpose,
    to keep it out of auto-discovery - so the hidden-folder rule has to be
    relative to the folder being scanned, not absolute, or the placeholder stops
    playing on every new box.
    """
    welcome = tmp_path / "media" / ".welcome"
    welcome.mkdir(parents=True)
    (welcome / "boot_splash.mp4").write_bytes(b"x")

    assert scan_episodes(welcome, (".mp4",)) == [welcome / "boot_splash.mp4"]
