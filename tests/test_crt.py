import pytest

from retrobox.config import CrtConfig
from retrobox.crt import (
    KEEP_GENERATIONS,
    render_shader,
    write_new_shader,
    write_shader,
)


def test_shader_contains_baked_constants():
    crt = CrtConfig(curvature=0.15, corner_radius=0.05, vignette=0.3, scanlines=True)
    shader = render_shader(crt)
    assert "//!HOOK MAIN" in shader
    assert "0.15000" in shader          # curvature baked in
    assert "#define SCANLINES 1" in shader


def test_shader_scanlines_off():
    shader = render_shader(CrtConfig(scanlines=False))
    assert "#define SCANLINES 0" in shader


def test_write_shader_disabled_returns_none(tmp_path):
    assert write_shader(CrtConfig(enabled=False), tmp_path / "crt.glsl") is None


def test_write_shader_writes_file(tmp_path):
    out = tmp_path / "crt.glsl"
    result = write_shader(CrtConfig(enabled=True), out)
    assert result == out
    assert out.is_file()
    assert "HOOK MAIN" in out.read_text()


# -- changing the effect while the television is on -------------------------

def test_every_new_shader_gets_a_filename_mpv_has_not_already_read(tmp_path):
    """mpv keeps the compiled shader it loaded from a given path.

    Handing it the same filename again can be a no-op, so each regeneration
    has to land somewhere new for the picture to actually change.
    """
    first = write_new_shader(CrtConfig(enabled=True, curvature=0.10), tmp_path)
    second = write_new_shader(CrtConfig(enabled=True, curvature=0.20), tmp_path)

    assert first is not None and second is not None
    assert first != second
    assert "0.10000" in first.read_text()
    assert "0.20000" in second.read_text()


def test_dragging_the_slider_for_months_cannot_fill_the_cache_directory(tmp_path):
    """A fresh filename each time is only safe if the old ones are swept up."""
    latest = None
    for step in range(40):
        latest = write_new_shader(
            CrtConfig(enabled=True, curvature=step / 100.0), tmp_path
        )

    left = sorted(tmp_path.glob("crt-*.glsl"))
    assert len(left) <= KEEP_GENERATIONS
    assert latest in left, "the shader mpv is using now must not be swept away"


def test_the_shader_before_the_current_one_is_kept_a_little_while(tmp_path):
    """mpv reads the file when the property is set, not necessarily at once.

    Deleting the outgoing shader the instant a new one is written would be a
    race against mpv's own loader, so a couple of generations are left behind.
    """
    previous = write_new_shader(CrtConfig(enabled=True, curvature=0.10), tmp_path)
    write_new_shader(CrtConfig(enabled=True, curvature=0.20), tmp_path)

    assert previous.is_file()


def test_a_disabled_crt_effect_writes_no_shader_at_all(tmp_path):
    """Off means no shader - not an identity shader the GPU still runs."""
    assert write_new_shader(CrtConfig(enabled=False), tmp_path) is None
    assert list(tmp_path.glob("*.glsl")) == []


def test_a_cache_directory_that_cannot_be_written_is_reported_not_raised(tmp_path):
    """The picture is cosmetic; a full or read-only disk must not end the show."""
    blocked = tmp_path / "nope"
    blocked.write_text("this is a file, not a directory")

    assert write_new_shader(CrtConfig(enabled=True), blocked) is None


def test_a_leftover_shader_from_an_earlier_run_is_never_handed_back(tmp_path):
    """A box runs for months. Numbering has to carry on from what is there."""
    stale = tmp_path / "crt-7.glsl"
    stale.write_text("// left behind by a previous run\n")

    fresh = write_new_shader(CrtConfig(enabled=True), tmp_path)

    assert fresh != stale
    assert "HOOK MAIN" in fresh.read_text()
