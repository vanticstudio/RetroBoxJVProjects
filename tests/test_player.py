"""The video player, and in particular changing the picture while it is on.

libmpv is not installed on a development machine, so these tests hand
:class:`MpvPlayer` a stand-in ``mpv`` module that records what it was asked to
do. That is enough to pin down the part this box gets wrong on its own - WHICH
mpv property is written, with WHAT value, and what happens when mpv says no -
which is exactly the part that used to be untested.
"""

import sys
import types

import pytest

from retrobox.config import CrtConfig
from retrobox.player import MockPlayer, MpvPlayer


class FakeMPV:
    """Stands in for ``mpv.MPV``: records writes, and can refuse them."""

    def __init__(self, **options):
        self.options = dict(options)
        # Property writes, in order, as (name, value) - so a test can tell a
        # changed value from a value that was written twice.
        self.property_writes = []
        self.properties = {}
        self.commands = []
        self.loadfiles = []
        self.terminated = False
        #: Property names whose assignment raises, standing in for a shader
        #: mpv will not take.
        self.refuse = set()

    # python-mpv exposes mpv properties both as attributes and by name.
    def __setitem__(self, name, value):
        if name in self.refuse:
            raise RuntimeError(f"mpv refused {name}")
        self.properties[name] = value
        self.property_writes.append((name, value))

    def __getitem__(self, name):
        return self.properties[name]

    def property_observer(self, _name):
        return lambda fn: fn

    def event_callback(self, _name):
        return lambda fn: fn

    def register_key_binding(self, *_args):
        pass

    def loadfile(self, *args, **kwargs):
        self.loadfiles.append((args, kwargs))

    def command(self, *args):
        self.commands.append(args)

    def terminate(self):
        self.terminated = True

    def shaders_written(self):
        """Every value glsl-shaders was set to, in order."""
        return [value for name, value in self.property_writes
                if name == "glsl-shaders"]


@pytest.fixture
def fake_mpv(monkeypatch, tmp_path):
    """Make ``import mpv`` inside MpvPlayer find the stand-in above."""
    module = types.ModuleType("mpv")
    module.MPV = FakeMPV
    monkeypatch.setitem(sys.modules, "mpv", module)
    # Generated shaders are cached under XDG_CACHE_HOME; keep them in tmp.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return module


@pytest.fixture
def player(fake_mpv):
    p = MpvPlayer(fullscreen=False, force_4_3=False)
    yield p
    p.close()


def test_the_running_player_can_be_told_to_change_the_crt_effect(player):
    """The whole point: curvature is judged by eye, so it must apply live."""
    applied = player.set_crt(CrtConfig(enabled=True, curvature=0.30))

    assert applied is True
    written = player._mpv.shaders_written()
    assert len(written) == 1
    assert "0.30000" in open(written[0], encoding="utf-8").read()


def test_changing_the_crt_effect_twice_points_mpv_at_a_file_it_has_not_read_before(player):
    """mpv may not re-read a shader from a path it already holds.

    Dragging the slider must therefore land on a new filename every time, or
    the second nudge silently does nothing.
    """
    player.set_crt(CrtConfig(enabled=True, curvature=0.10))
    player.set_crt(CrtConfig(enabled=True, curvature=0.20))
    player.set_crt(CrtConfig(enabled=True, curvature=0.30))

    written = player._mpv.shaders_written()
    assert len(written) == 3
    assert len(set(written)) == 3, "mpv was pointed at the same file twice"
    assert "0.30000" in open(written[-1], encoding="utf-8").read()


def test_turning_the_crt_effect_off_clears_the_shader_instead_of_loading_a_blank_one(player):
    """An identity shader still costs a two-core Celeron real GPU work."""
    player.set_crt(CrtConfig(enabled=True))
    assert player.set_crt(CrtConfig(enabled=False)) is True

    assert player._mpv.shaders_written()[-1] == ""


def test_changing_the_crt_effect_does_not_disturb_what_is_playing(player):
    """Nobody should see the programme restart because they moved a slider."""
    player.set_crt(CrtConfig(enabled=True, curvature=0.2))

    assert player._mpv.loadfiles == []
    assert player._mpv.commands == []


def test_a_shader_mpv_will_not_take_leaves_the_television_playing(player):
    """crt.py promises a bad shader is logged and the picture keeps working.

    That promise has to hold mid-programme too, not only at start-up.
    """
    player._mpv.refuse.add("glsl-shaders")

    assert player.set_crt(CrtConfig(enabled=True, curvature=0.2)) is False

    # And the player is still a working player afterwards.
    player.set_volume(50)
    assert player._mpv.volume == 50


def test_a_shader_that_cannot_be_written_leaves_the_picture_exactly_as_it_was(
    player, monkeypatch
):
    """A full or read-only cache disk must not blank the effect that is on."""
    monkeypatch.setattr("retrobox.crt.write_new_shader", lambda *a, **k: None)

    assert player.set_crt(CrtConfig(enabled=True, curvature=0.2)) is False
    assert player._mpv.shaders_written() == []


def test_the_mock_player_records_the_crt_effect_it_was_given():
    """So the app's own logic can be tested with no television attached."""
    mock = MockPlayer()
    wanted = CrtConfig(enabled=True, curvature=0.42)

    assert mock.set_crt(wanted) is True
    assert mock.crt == wanted
    assert mock.crt_applied == [wanted]


def test_the_mock_player_records_the_crt_effect_being_switched_off():
    mock = MockPlayer()
    mock.set_crt(CrtConfig(enabled=True))
    mock.set_crt(CrtConfig(enabled=False))

    assert mock.crt is None, "an off effect should read as nothing on the picture"
    assert mock.crt_applied[-1] is None
