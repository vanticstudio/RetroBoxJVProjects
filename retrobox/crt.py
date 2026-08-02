"""Generate the CRT picture effect as an mpv GLSL user shader.

Everything from this era was shot for 4:3 tube TVs, so to sell the nostalgia we bend the
picture the way a real CRT did: a gentle barrel "bulge", rounded corners, a soft
vignette toward the edges, and faint scanlines. mpv applies this as a GLSL user
shader on the video plane (the 4:3 image stays pillar-boxed inside the frame).

The shader is written out with the numbers from :class:`~retrobox.config.CrtConfig`
baked in, so it is fully tunable from ``config.yaml`` without editing GLSL. If the
shader ever fails to compile on a given GPU, mpv simply logs it and keeps
playing - the effect is cosmetic and never blocks playback.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from .config import CrtConfig

log = logging.getLogger(__name__)

#: How many generated shader files are kept in the cache directory.
#:
#: See :func:`write_new_shader` for why each regeneration needs a new
#: filename. The consequence is that this box - which runs for months at a
#: time while somebody fiddles with the curvature slider - would otherwise
#: leave one small file behind per nudge. Three is enough to cover the one mpv
#: is using, the one it was using a moment ago, and one spare, and it caps the
#: directory at a few kilobytes no matter how long the box stays on.
KEEP_GENERATIONS = 3

#: The generated filenames, so old ones can be recognised and swept up. Only
#: files this module made itself are ever deleted.
_GENERATION_NAME = re.compile(r"crt-(\d+)\.glsl\Z")

# mpv/libplacebo user-shader ("hook") template. Hooks the MAIN video plane and
# remaps texture coordinates for the curvature, then masks/rounds/shades.
_SHADER_TEMPLATE = """//!HOOK MAIN
//!BIND HOOKED
//!DESC retrobox CRT (curvature + rounded corners + vignette + scanlines)

// Tunables baked in from config.yaml
#define CURVATURE {curvature:.5f}
#define CORNER_RADIUS {corner_radius:.5f}
#define VIGNETTE {vignette:.5f}
#define SCANLINES {scanlines}
#define SCAN_INTENSITY {scanline_intensity:.5f}

vec4 hook() {{
    vec2 uv = HOOKED_pos;
    vec2 cc = uv - 0.5;
    float dist2 = dot(cc, cc);

    // Barrel distortion: push pixels outward with distance from centre.
    vec2 warped = uv + cc * dist2 * CURVATURE;

    // Rounded-rectangle mask over the (warped) frame, anti-aliased at the edge.
    vec2 p = warped - 0.5;
    vec2 d = abs(p) - (vec2(0.5) - CORNER_RADIUS);
    float outside = length(max(d, vec2(0.0))) - CORNER_RADIUS;
    float aa = 1.5 / max(HOOKED_size.x, HOOKED_size.y);
    float mask = 1.0 - smoothstep(0.0, aa, outside);
    if (mask <= 0.0)
        return vec4(0.0, 0.0, 0.0, 1.0);

    vec4 col = HOOKED_tex(clamp(warped, 0.0, 1.0));

    // Vignette: darken toward the edges/corners.
    float vig = clamp(1.0 - VIGNETTE * dist2 * 4.0, 0.0, 1.0);
    col.rgb *= vig;

    // Scanlines: subtle horizontal darkening at the source line pitch.
    if (SCANLINES > 0) {{
        float s = 0.5 + 0.5 * cos(warped.y * HOOKED_size.y * 3.14159265);
        col.rgb *= 1.0 - SCAN_INTENSITY * s;
    }}

    col.rgb *= mask;
    return col;
}}
"""


def render_shader(crt: CrtConfig) -> str:
    return _SHADER_TEMPLATE.format(
        curvature=crt.curvature,
        corner_radius=crt.corner_radius,
        vignette=crt.vignette,
        scanlines=1 if crt.scanlines else 0,
        scanline_intensity=crt.scanline_intensity,
    )


def shader_dir() -> Path:
    """The writable directory the generated shaders are cached in."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(cache_home) / "retrobox"


def default_shader_path() -> Path:
    """A stable, writable location to cache the generated shader."""
    return shader_dir() / "crt.glsl"


def write_shader(crt: CrtConfig, out_path: Path | None = None) -> Path | None:
    """Write the shader for ``crt`` and return its path (or None if disabled)."""
    if not crt.enabled:
        return None
    out_path = out_path or default_shader_path()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_shader(crt), encoding="utf-8")
        return out_path
    except OSError:
        log.warning("could not write CRT shader; continuing without it", exc_info=True)
        return None


def write_new_shader(
    crt: CrtConfig, directory: Optional[Path] = None
) -> Optional[Path]:
    """Write the shader for ``crt`` somewhere mpv has never read before.

    Returns the new path, or None when the effect is off or the file could not
    be written.

    WHY A NEW FILENAME EVERY TIME, rather than rewriting one file.

    mpv holds the shader it compiled from a given path. Assigning
    ``glsl-shaders`` the string it already has is, at the option layer, not a
    change at all - so rewriting ``crt.glsl`` and setting the same path again
    can leave the old curvature on the screen, which is the exact bug this
    whole feature exists to fix, only harder to see.

    The other way out is to clear the property and set it again. That was
    rejected: clearing is a real change that mpv acts on, so for however many
    frames sit between the two writes the picture has NO shader on it. Somebody
    dragging the curvature slider would watch the effect flicker off and back
    on with every step - which is precisely the "save, restart, judge" misery
    this replaces, just faster. Pointing at a new filename is one property
    write, so the picture goes straight from the old effect to the new one.

    The cost of that choice is litter, and it is paid here: old generations are
    swept up on the way past, so the directory stays at :data:`KEEP_GENERATIONS`
    files however long the box runs. Numbering carries on from whatever is
    already in the directory, so a shader left behind by an earlier run (the
    box is switched off at the wall, mid-write is always possible) is never
    handed back to mpv as if it were new.
    """
    if not crt.enabled:
        return None
    directory = directory or shader_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        generation = _next_generation(directory)
        out_path = directory / f"crt-{generation}.glsl"
        out_path.write_text(render_shader(crt), encoding="utf-8")
    except OSError:
        log.warning("could not write CRT shader; leaving the picture as it is",
                    exc_info=True)
        return None
    _sweep_old_generations(directory, newest=generation)
    return out_path


def _next_generation(directory: Path) -> int:
    """One past the highest generation number already in ``directory``."""
    highest = 0
    try:
        entries = list(directory.iterdir())
    except OSError:                       # pragma: no cover - raced with a sweep
        return 1
    for entry in entries:
        match = _GENERATION_NAME.match(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _sweep_old_generations(directory: Path, *, newest: int) -> None:
    """Delete generations older than the last :data:`KEEP_GENERATIONS`.

    The one just written is obviously kept, and so are the couple before it:
    setting ``glsl-shaders`` tells mpv to go and read the file, and it does
    that on its own render thread. Deleting the outgoing shader in the same
    breath would be a race with that read, and losing it means a frame with no
    effect on it. Keeping a couple of generations costs a few kilobytes.
    """
    oldest_kept = newest - KEEP_GENERATIONS + 1
    try:
        entries = list(directory.iterdir())
    except OSError:                       # pragma: no cover - hostile fs
        return
    for entry in entries:
        match = _GENERATION_NAME.match(entry.name)
        if match and int(match.group(1)) < oldest_kept:
            try:
                entry.unlink()
            except OSError:               # pragma: no cover - already gone
                log.debug("could not remove old CRT shader %s", entry, exc_info=True)


__all__ = [
    "KEEP_GENERATIONS",
    "render_shader",
    "write_shader",
    "write_new_shader",
    "default_shader_path",
    "shader_dir",
]
