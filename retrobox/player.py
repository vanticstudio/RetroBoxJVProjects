"""The video player abstraction.

The application talks to an abstract :class:`Player`; two implementations exist:

* :class:`MpvPlayer` - the real thing, backed by libmpv (via the ``python-mpv``
  package). This is what runs on the box against the TV.
* :class:`MockPlayer` - a no-op player that records what it was asked to do and
  lets tests/dev drive "the episode ended" by hand. This lets the entire app be
  exercised on a laptop with no display, no libmpv, and no media files.

Keeping this boundary thin (load / stop / volume / a couple of OSD hooks) means
the interesting logic in ``app.py`` never has to know which one it is using.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import cheap
    from .config import CrtConfig

log = logging.getLogger(__name__)

# Reason strings passed to the "playback finished" callback.
END_EOF = "eof"        # the file played to its natural end -> roll next episode
END_ERROR = "error"    # the file failed to play -> skip to next episode
END_STOPPED = "stopped"  # we stopped it on purpose (channel change) -> ignore


class Player(ABC):
    """Minimal video-player interface used by the application."""

    #: Called when playback of the current item finishes. Receives one of the
    #: END_* reason strings. Set by the application before playing anything.
    on_end: Optional[Callable[[str], None]] = None

    #: Called on a left mouse click while the pointer is enabled. Receives the
    #: click position as (x, y) fractions of the video surface, 0.0-1.0, so the
    #: player never needs to know about the overlay's virtual canvas. May fire
    #: on a background thread - the application queues it like ``on_end``.
    on_click: Optional[Callable[[Tuple[float, float]], None]] = None

    def set_paused(self, paused: bool) -> None:
        """Pause or resume playback in place. Default: nothing to do."""

    def set_audio_device(self, device: str) -> None:
        """Switch audio output at runtime. Default: nothing to do."""

    def set_mouse_enabled(self, enabled: bool) -> None:
        """Start/stop reporting pointer activity. Default: nothing to do."""

    def set_crt(self, crt: "CrtConfig") -> bool:
        """Change the CRT picture effect on the picture that is showing now.

        Curvature is a taste setting with no correct value: the only way
        anybody sets it sensibly is by dragging the dashboard slider while
        watching the television, so it has to take effect there and then.

        ``crt.enabled`` False means NO shader - not a shader that does nothing.
        An identity pass still costs this box's two-core Celeron real GPU work
        every frame for a picture indistinguishable from having none.

        Returns True when the picture was changed. False means it was left
        exactly as it was (and said so in the log) - never an exception, and
        never a blank screen: the effect is cosmetic and outranked by the
        programme somebody is halfway through. Default: nothing to do.
        """
        return False

    def get_hwdec(self) -> Optional[str]:
        """The decoder actually in use, e.g. "vaapi", or None for software."""
        return None

    def get_audio_status(self) -> dict:
        """What the player's audio output is ACTUALLY doing, right now.

        The player made the choice, so it is the only thing on the box that
        knows the answer. An external probe can say what hardware exists and
        what is installed; it cannot say which device was opened, and when
        the two disagree the probe is the one that is wrong.

        Keys, all of which may be None when there is nothing to report:

        * ``device``  - the output that was opened, as the player names it.
        * ``ao``      - the audio backend in use ("alsa", "pulse", ...).
        * ``active``  - True when an output was genuinely opened and is
          taking samples, False when opening it failed, None when nothing is
          playing so there is nothing to have opened.
        * ``channels``- the layout actually being sent to the sink.
        * ``track``   - False when the file has no audio track at all, which
          is not a fault and must not be reported as one.
        """
        return {"device": None, "ao": None, "active": None,
                "channels": None, "track": None}

    def list_audio_devices(self) -> List[dict]:
        """Every output the player can see, as ``{name, description}``.

        Asked of the player rather than of ``aplay`` because the player runs
        as the service that actually holds the ``audio`` group.
        """
        return []

    def set_audio_channels(self, layout: str) -> bool:
        """Ask for a channel layout ("stereo", "5.1"). True when it took."""
        return False

    def play_test_tone(self, *, seconds: float = 2.0,
                       frequency: int = 440) -> bool:
        """Put a short tone through the selected output. True when it played.

        There is no way to answer "is it the box or is it the telly" from a
        living room without this, and telling the customer to open a terminal
        is not an option.
        """
        return False

    def get_mouse_position(self) -> Optional[Tuple[float, float]]:
        """Pointer position as (x, y) fractions of the video surface.

        Returns ``None`` when the pointer is disabled, off the window, or its
        position is not knowable.
        """
        return None

    @abstractmethod
    def play(self, path: Path, *, start: float = 0.0) -> None:
        """Begin playing ``path`` from ``start`` seconds in."""

    @abstractmethod
    def play_loop(self, path: Path) -> None:
        """Play ``path`` on an endless loop (used for the static/no-signal clip)."""

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        """Show a brief static burst, then the target episode.

        The default implementation just plays the target; players that can
        preload (see :class:`MpvPlayer`) override this to make the switch
        near-instant.
        """
        self.play(target_path, start=start)

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        """Begin loading ``target_path`` in the background while the CURRENT item
        keeps playing. Call :meth:`commit_switch` to cut over once it's ready.

        The default implementation has no way to preload, so it just plays the
        target immediately; :class:`MpvPlayer` overrides it.
        """
        self.play(target_path, start=start)

    def commit_switch(self) -> None:
        """Switch to the item queued by :meth:`preload_next` (no-op by default)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop playback and show a blank screen."""

    @abstractmethod
    def set_volume(self, volume: int) -> None:
        """Set the volume (0-100)."""

    @abstractmethod
    def set_mute(self, muted: bool) -> None: ...

    @abstractmethod
    def get_time_pos(self) -> Optional[float]:
        """Current playback position in seconds, or None if nothing is playing."""

    @abstractmethod
    def show_text(self, text: str, duration: float) -> None:
        """Show a plain OSD message for ``duration`` seconds."""

    @abstractmethod
    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        """Draw an ASS overlay with the given id (replacing any previous one)."""

    @abstractmethod
    def clear_overlay(self, overlay_id: int) -> None:
        """Remove a previously drawn overlay."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""


class MpvPlayer(Player):
    """A :class:`Player` backed by libmpv, tuned for a headless box + TV."""

    def __init__(
        self,
        *,
        fullscreen: bool = True,
        hwdec: str = "auto-safe",
        glsl_shaders: Optional[str] = None,
        fonts_dir: Optional[Path] = None,
        force_4_3: bool = True,
        audio_device: Optional[str] = None,
        audio_channels: Optional[str] = None,
        extra_options: Optional[dict] = None,
    ) -> None:
        try:
            import mpv  # type: ignore
        except ImportError as exc:  # pragma: no cover - only on machines w/o libmpv
            raise RuntimeError(
                "python-mpv/libmpv is not installed. On the box run "
                "`scripts/install.sh` or `pip install -e '.[hardware]'`, and "
                "ensure libmpv is present (`sudo apt install mpv libmpv2`, or "
                "libmpv1 on older Ubuntu)."
            ) from exc

        # Make our bundled retro font discoverable by libass (used for the OSD
        # overlays) by dropping it into mpv's config "fonts" directory.
        if fonts_dir is not None:
            _install_fonts_for_mpv(fonts_dir)

        options = dict(
            # We drive the OSD ourselves, so disable mpv's own on-screen
            # controller and default keybindings.
            osc=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            # Keep a window alive even with nothing playing so the screen never
            # drops to a console/desktop between episodes or on an empty channel.
            idle="yes",
            force_window="yes",
            # keep-open=yes means a file that reaches its end PAUSES on the last
            # frame and sets the "eof-reached" property instead of silently
            # unloading. We watch that property to roll the next episode. This
            # avoids a nasty race: replacing a file (on a channel change) also
            # fires an "end-file" event for the outgoing file, and its reason is
            # unreliable across mpv versions - reacting to it caused episodes to
            # be skipped or the picture to hang. "eof-reached" only ever trips on
            # a genuine end-of-file, so it is the robust signal.
            keep_open="yes",
            # Preload the next playlist entry while the current one plays. This
            # is what makes channel changes near-instant: during the ~0.5s of
            # static, mpv is already opening/decoding the episode, so it appears
            # the moment the static ends (see play_transition).
            prefetch_playlist="yes",
            fullscreen=fullscreen,
            # Hardware decode (VA-API/Quick Sync on Intel) plus a sensible video
            # output. With no display server libmpv picks the gpu/drm context and
            # renders straight to the console; it falls back sanely elsewhere.
            hwdec=hwdec,
            # 4:3 shows should be pillarboxed (not stretched) inside the frame.
            keepaspect="yes",
            video_unscaled="no",
            # Hide the mouse cursor - this is a TV, not a computer.
            cursor_autohide="always",
            # A pleasant, readable OSD font size relative to the window.
            osd_font_size=40,
            # A 1998 cable box had no subtitles, so an embedded track must not
            # switch itself on. sub-auto=no also stops mpv picking up a .srt
            # sitting next to the episode.
            sid="no",
            sub_auto="no",
            # Everything is decoded to PCM. A television is not an AV
            # receiver: bitstreaming AC3/DTS/TrueHD to a set that cannot
            # decode it is silence, and silence is the bug this exists to
            # prevent. Somebody with a receiver can turn it back on.
            audio_spdif="",
        )
        if audio_channels:
            # What the sink said it accepts, from its ELD. Sending 5.1 to a
            # stereo-only set is not an error - it is silent.
            options["audio_channels"] = audio_channels
        if audio_device:
            # Force audio to a specific output (e.g. HDMI) instead of mpv's
            # default (which can pick the wrong sink on a multi-output box).
            options["audio_device"] = audio_device
        if glsl_shaders:
            # CRT curvature/rounding/vignette/scanlines. Applied globally (always
            # on) so a newly-loaded episode is never shown for a frame or two
            # without the effect on a channel change. This is only the STARTING
            # value - see set_crt, which changes it while the television is on.
            options["glsl_shaders"] = glsl_shaders
        if force_4_3:
            # Fit ANY source into a 4:3 raster (letterboxing 16:9 with black
            # bars), so every show - and the static/colour-bar clips - appears in
            # the same 4:3 tube-TV frame. mpv then pillarboxes that 4:3 image on
            # a 16:9 TV, and the CRT shader curves it.
            options["vf"] = (
                "lavfi=[scale=960:720:force_original_aspect_ratio=decrease,"
                "pad=960:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1]"
            )
        if extra_options:
            options.update(extra_options)

        self._mpv = mpv.MPV(**options)
        self._closed = False
        self._mouse_bound = False
        # The shader file mpv is currently reading from, so set_crt can tell
        # what it would be replacing. None means the effect is off.
        self._crt_shader: Optional[str] = glsl_shaders or None
        # True while a looping filler clip (static / colour bars) is showing, so
        # its (non-)ending never advances the channel.
        self._suppress = True

        @self._mpv.property_observer("eof-reached")
        def _on_eof(_name, value):  # pragma: no cover - needs libmpv + media
            if value and not self._suppress and self.on_end is not None:
                try:
                    self.on_end(END_EOF)
                except Exception:  # noqa: BLE001 - never let a callback kill mpv
                    log.exception("error in on_end (eof) callback")

        @self._mpv.event_callback("end-file")
        def _on_end_file(event):  # pragma: no cover - needs libmpv + media
            # We only care about *errors* here (e.g. a corrupt/missing file) so
            # we can skip to the next episode. Natural ends are handled by the
            # eof-reached observer above; intentional stops/replacements are
            # ignored.
            if self._suppress:
                return
            if _extract_end_reason(event) == END_ERROR and self.on_end is not None:
                try:
                    self.on_end(END_ERROR)
                except Exception:  # noqa: BLE001
                    log.exception("error in on_end (error) callback")

    # -- playback -----------------------------------------------------------
    def play(self, path: Path, *, start: float = 0.0) -> None:
        # Enable end detection only for real content.
        self._suppress = False
        try:
            self._mpv.loop_file = "no"
            if start and start > 0:
                # start is an mpv per-file option; +N seeks N seconds in.
                self._mpv.loadfile(str(path), "replace", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(path), "replace")
            self._mpv.pause = False  # keep-open can leave us paused; force play
        except Exception:  # noqa: BLE001
            log.exception("failed to play %s", path)
            if self.on_end is not None:
                self.on_end(END_ERROR)

    def play_loop(self, path: Path) -> None:
        self._suppress = True  # a looping clip should never trigger "next"
        try:
            self._mpv.loop_file = "inf"
            self._mpv.loadfile(str(path), "replace")
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.exception("failed to loop %s", path)

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        # Build a 2-entry playlist: [static (cut to static_seconds), episode].
        # mpv plays the static burst and, thanks to prefetch-playlist, has the
        # episode ready to show the instant the static ends. keep-open=yes only
        # holds the LAST entry, so eof-reached (which advances the channel) only
        # ever trips for the episode - never the static.
        self._suppress = False
        try:
            self._mpv.loop_file = "no"
            self._mpv.loadfile(
                str(static_path), "replace", end=f"{max(0.05, static_seconds):.3f}"
            )
            if start and start > 0:
                self._mpv.loadfile(str(target_path), "append", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(target_path), "append")
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.exception("failed transition to %s", target_path)
            self.play(target_path, start=start)

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        # Keep the currently-playing item on screen and append the target as a
        # second playlist entry. With prefetch-playlist=yes, mpv opens/decodes it
        # in the background while the current show keeps playing, so commit_switch
        # can cut over near-instantly (no frozen frame).
        self._suppress = True  # ignore the outgoing show's own eof during the bridge
        try:
            self._mpv.command("playlist-clear")  # drop any earlier pending append
            if start and start > 0:
                self._mpv.loadfile(str(target_path), "append", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(target_path), "append")
        except Exception:  # noqa: BLE001
            log.exception("failed to preload %s", target_path)
            self.play(target_path, start=start)

    def commit_switch(self) -> None:
        self._suppress = False
        try:
            self._mpv.command("playlist-next", "force")  # jump to the prefetched item
            self._mpv.command("playlist-clear")          # keep only the new current
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.debug("commit_switch failed", exc_info=True)

    def stop(self) -> None:
        self._suppress = True
        try:
            self._mpv.command("stop")
        except Exception:  # noqa: BLE001 - stopping should never crash us
            log.debug("mpv stop failed", exc_info=True)

    # -- audio --------------------------------------------------------------
    def set_volume(self, volume: int) -> None:
        try:
            self._mpv.volume = max(0, min(100, int(volume)))
        except Exception:  # noqa: BLE001
            log.debug("could not set volume", exc_info=True)

    def set_mute(self, muted: bool) -> None:
        try:
            self._mpv.mute = bool(muted)
        except Exception:  # noqa: BLE001
            log.debug("could not set mute", exc_info=True)

    def get_time_pos(self) -> Optional[float]:
        try:
            pos = self._mpv.time_pos
            return float(pos) if pos is not None else None
        except Exception:  # noqa: BLE001
            return None

    # -- OSD ----------------------------------------------------------------
    def show_text(self, text: str, duration: float) -> None:
        try:
            self._mpv.command("show-text", text, int(duration * 1000))
        except Exception:  # noqa: BLE001
            log.debug("show-text failed", exc_info=True)

    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        try:
            # osd-overlay positional args: id, format, data, res_x, res_y.
            # (Trailing z/hidden/compute_bounds use their defaults.)
            self._mpv.command(
                "osd-overlay", overlay_id, "ass-events", ass, res_x, res_y
            )
        except Exception:  # noqa: BLE001
            # Fall back to a plain message so the viewer still gets feedback.
            log.debug("osd-overlay failed, falling back to show-text", exc_info=True)
            self.show_text(_strip_ass(ass), 3.0)

    def clear_overlay(self, overlay_id: int) -> None:
        try:
            self._mpv.command("osd-overlay", overlay_id, "none", "")
        except Exception:  # noqa: BLE001
            log.debug("clearing overlay failed", exc_info=True)

    # -- pause / audio / pointer -------------------------------------------
    def set_paused(self, paused: bool) -> None:
        try:
            self._mpv.pause = bool(paused)
        except Exception:  # noqa: BLE001
            log.debug("setting pause failed", exc_info=True)

    def set_audio_device(self, device: str) -> None:
        """Switch output live. mpv reopens the audio chain on the next frame."""
        try:
            self._mpv["audio-device"] = device
        except Exception:  # noqa: BLE001
            log.warning("could not switch audio device to %s", device, exc_info=True)

    def set_mouse_enabled(self, enabled: bool) -> None:
        """Let mpv track the pointer, and bind left-click, only while needed.

        The box normally runs with mpv's own input switched off and the cursor
        hidden, which is right for a TV. The menu turns this on for as long as
        it is open and back off afterwards, so a stray desk bump never does
        anything while you're just watching.
        """
        try:
            self._mpv["input-cursor"] = "yes" if enabled else "no"
            self._mpv["cursor-autohide"] = "no" if enabled else "always"
            if enabled and not self._mouse_bound:
                # Registered once and left in place; the input-cursor flag above
                # is what actually gates whether clicks can arrive.
                self._mpv.register_key_binding("MBTN_LEFT", self._handle_click)
                self._mouse_bound = True
        except Exception:  # noqa: BLE001
            log.debug("toggling mouse support failed", exc_info=True)

    def set_crt(self, crt: "CrtConfig") -> bool:
        """Regenerate the CRT shader and put it on the live picture.

        mpv takes glsl-shaders at runtime perfectly well; the trap is that it
        keeps the shader it compiled from a path, so it has to be handed a
        filename it has not read before. :func:`crt.write_new_shader` is what
        guarantees that (and sweeps the old ones up) - the reasoning, and the
        clear-and-set alternative that was rejected, are written down there.

        Nothing here touches what is playing: no loadfile, no seek, no pause.
        The frame on screen keeps going and simply comes out looking different.
        """
        from .crt import write_new_shader

        if not crt.enabled:
            # OFF clears the property outright. Writing an identity shader
            # instead would keep a GLSL pass running every frame on a two-core
            # Celeron to produce the picture it was already producing.
            if self._crt_shader is None:
                return True                      # already off; nothing to do
            try:
                self._mpv["glsl-shaders"] = ""
            except Exception:  # noqa: BLE001 - the programme outranks the effect
                log.warning("could not clear the CRT shader", exc_info=True)
                return False
            self._crt_shader = None
            return True

        path = write_new_shader(crt)
        if path is None:
            # A full or read-only cache disk. Say so and leave the effect that
            # is already on the picture exactly where it is.
            log.warning("could not write a new CRT shader; picture left as it was")
            return False

        try:
            self._mpv["glsl-shaders"] = str(path)
        except Exception:  # noqa: BLE001
            # crt.py's promise, kept at runtime: a shader mpv will not take is
            # logged and the television carries on playing. mpv itself does the
            # same for one that fails to COMPILE - it logs and renders the
            # frame without it - so neither case can take the picture down.
            log.warning(
                "mpv would not take the new CRT shader; picture left as it was",
                exc_info=True,
            )
            return False
        self._crt_shader = str(path)
        return True

    def _handle_click(self, key_state=None, *_args) -> None:  # pragma: no cover - needs libmpv
        """mpv key-binding callback. Fires on any thread, so keep it tiny."""
        # key_state looks like "d-" (down) / "u-" ; ignore the release half.
        if key_state and not str(key_state).startswith("d"):
            return
        position = self.get_mouse_position()
        if position is not None and self.on_click is not None:
            self.on_click(position)

    def get_hwdec(self) -> Optional[str]:
        try:
            current = self._mpv.hwdec_current
        except Exception:  # noqa: BLE001
            return None
        # mpv reports the string 'no' when it fell back to software.
        if not current or str(current) in ('no', 'none'):
            return None
        return str(current)

    def _property(self, name: str, default=None):
        """One mpv property, or ``default``. Never raises.

        Properties are unavailable at perfectly ordinary moments - nothing
        loaded, output not yet initialised - and python-mpv signals that by
        raising. A status snapshot is not worth an exception.
        """
        try:
            value = getattr(self._mpv, name)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def get_audio_status(self) -> dict:
        # audio-out-params is only populated once an output has actually been
        # opened and is taking samples, which is exactly the question. An
        # empty dict means mpv tried and did not get an output.
        out = self._property("audio_out_params", {}) or {}
        track = self._property("aid", None)
        has_track = None if track is None else str(track) not in ("no", "False")
        idle = bool(self._property("idle_active", False))

        ao = self._property("current_ao", None)
        if idle or not has_track:
            # Nothing playing, or a file with no sound in it. Neither is a
            # fault, and neither is evidence about the output.
            active = None if idle else False
        elif ao and str(ao) == "null":
            # mpv's audio-fallback-to-null is on by default and it is what
            # makes a broken output look healthy: the file plays, the clock
            # runs, and the samples go nowhere. Left on deliberately, because
            # refusing to play a picture over it would be worse - but it is
            # reported as what it is, which is no sound.
            active = False
        else:
            active = bool(out)

        channels = out.get("hr-channels") or out.get("channels")
        return {
            "device": self._property("audio_device", None),
            "ao": ao,
            "active": active,
            "channels": str(channels) if channels else None,
            "track": has_track,
        }

    def list_audio_devices(self) -> List[dict]:
        found = self._property("audio_device_list", []) or []
        devices = []
        for entry in found:
            try:
                devices.append({"name": str(entry.get("name", "")),
                                "description": str(entry.get("description", ""))})
            except AttributeError:
                continue
        return [d for d in devices if d["name"]]

    def set_audio_channels(self, layout: str) -> bool:
        try:
            self._mpv.audio_channels = layout
        except Exception:  # noqa: BLE001
            log.debug("mpv would not take audio-channels=%s", layout,
                      exc_info=True)
            return False
        return True

    def play_test_tone(self, *, seconds: float = 2.0,
                       frequency: int = 440) -> bool:
        # lavfi is built into the ffmpeg mpv already links, so this needs no
        # tone file on disk and no new dependency.
        source = f"av://lavfi:sine=frequency={int(frequency)}:duration={seconds:g}"
        try:
            self._mpv.play(source)
        except Exception:  # noqa: BLE001
            log.warning("could not play the test tone", exc_info=True)
            return False
        return True

    def get_mouse_position(self) -> Optional[Tuple[float, float]]:
        try:
            pos = self._mpv.mouse_pos
            if not pos or not pos.get("hover", False):
                return None
            width = self._mpv.osd_width or 0
            height = self._mpv.osd_height or 0
            if not width or not height:
                return None
            # Normalised, so the caller maps it onto whatever canvas it drew on.
            return (pos["x"] / width, pos["y"] / height)
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._mpv.terminate()
        except Exception:  # noqa: BLE001
            log.debug("mpv terminate failed", exc_info=True)


class MockPlayer(Player):
    """A headless stand-in that records commands - for tests and dev mode."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.current: Optional[Path] = None
        self.looping: Optional[Path] = None
        self.volume: int = 0
        self.muted: bool = False
        self.time_pos: float = 0.0
        self.closed = False
        # Recorded history, handy for assertions in tests.
        self.played: List[Tuple[Path, float]] = []
        self.transitions: List[Tuple[Path, Path, float]] = []
        self.preloaded: Optional[Tuple[Path, float]] = None
        self.messages: List[Tuple[str, float]] = []
        self.overlays: dict[int, str] = {}
        self.stops = 0
        self.paused = False
        self.audio_device: Optional[str] = None
        self.mouse_enabled = False
        #: The CRT effect currently on the picture, or None when it is off.
        self.crt: Optional["CrtConfig"] = None
        #: Every effect applied, in order, so a test can prove the dashboard's
        #: slider reached the picture without a television being attached.
        self.crt_applied: List[Optional["CrtConfig"]] = []
        self.hwdec: Optional[str] = None
        #: Tests set this to a normalised (x, y) to simulate a pointer.
        self.mouse_position: Optional[Tuple[float, float]] = None
        #: The channel layout asked for, and every layout ever asked for, so
        #: a test can prove a 5.1 file was downmixed without a sound card.
        self.audio_channels: Optional[str] = None
        self.channel_layouts: List[str] = []
        #: What a real player would report its output was doing. Tests set
        #: this to stand in for mpv; the shape mirrors get_audio_status().
        self.audio_status: dict = {"device": None, "ao": None, "active": None,
                                   "channels": None, "track": None}
        #: What the player can see. Empty is the honest default: a mock has
        #: no sound card, exactly like a box with none.
        self.audio_device_list: List[dict] = []
        #: Every test tone played, as (seconds, frequency).
        self.test_tones: List[Tuple[float, int]] = []

    # -- pause / audio / pointer -------------------------------------------
    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self._log(f"{'PAUSE' if paused else 'RESUME'}")

    def set_audio_device(self, device: str) -> None:
        self.audio_device = device
        self._log(f"AUDIO DEVICE {device}")

    def set_mouse_enabled(self, enabled: bool) -> None:
        self.mouse_enabled = bool(enabled)
        self._log(f"MOUSE {'ON' if enabled else 'OFF'}")

    def set_crt(self, crt: "CrtConfig") -> bool:
        # No shader is written here: there is no mpv to read one. What is
        # recorded is the state a real player would have put on the picture -
        # the settings, or None for "no shader at all".
        self.crt = crt if crt.enabled else None
        self.crt_applied.append(self.crt)
        self._log(f"CRT {'OFF' if self.crt is None else f'curvature {crt.curvature}'}")
        return True

    def get_hwdec(self) -> Optional[str]:
        return self.hwdec

    def get_audio_status(self) -> dict:
        # The device the mock was actually told to use outranks whatever a
        # test left in audio_status, so set_audio_device stays meaningful.
        status = dict(self.audio_status)
        if self.audio_device is not None and status.get("device") is None:
            status["device"] = self.audio_device
        if self.audio_channels is not None and status.get("channels") is None:
            status["channels"] = self.audio_channels
        return status

    def list_audio_devices(self) -> List[dict]:
        return list(self.audio_device_list)

    def set_audio_channels(self, layout: str) -> bool:
        self.audio_channels = layout
        self.channel_layouts.append(layout)
        self._log(f"AUDIO CHANNELS {layout}")
        return True

    def play_test_tone(self, *, seconds: float = 2.0,
                       frequency: int = 440) -> bool:
        self.test_tones.append((seconds, int(frequency)))
        self._log(f"TEST TONE {frequency}Hz for {seconds}s")
        return True

    def get_mouse_position(self) -> Optional[Tuple[float, float]]:
        return self.mouse_position if self.mouse_enabled else None

    def click_at(self, x: float, y: float) -> None:
        """Test/dev helper: simulate a left click at normalised (x, y)."""
        self.mouse_position = (x, y)
        if self.mouse_enabled and self.on_click is not None:
            self.on_click((x, y))

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[player] {msg}")

    def play(self, path: Path, *, start: float = 0.0) -> None:
        self.current = path
        self.looping = None
        self.time_pos = start
        self.played.append((path, start))
        self._log(f"PLAY {path} @ {start:.1f}s")

    def play_loop(self, path: Path) -> None:
        self.looping = path
        self.current = path
        self._log(f"LOOP {path}")

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        self.transitions.append((static_path, target_path, start))
        # The episode is what ends up playing (static is momentary).
        self.current = target_path
        self.looping = None
        self.time_pos = start
        self.played.append((target_path, start))
        self._log(f"TRANSITION static={static_path} -> {target_path} @ {start:.1f}s")

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        # The current item keeps "playing"; the target is queued, not shown yet.
        self.preloaded = (target_path, start)
        self._log(f"PRELOAD {target_path} @ {start:.1f}s (current keeps playing)")

    def commit_switch(self) -> None:
        if self.preloaded is None:
            return
        target, start = self.preloaded
        self.preloaded = None
        self.current = target
        self.looping = None
        self.time_pos = start
        self.played.append((target, start))
        self._log(f"COMMIT SWITCH -> {target} @ {start:.1f}s")

    def stop(self) -> None:
        self.current = None
        self.looping = None
        self.preloaded = None
        self.stops += 1
        self._log("STOP")

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        self._log(f"VOLUME {self.volume}")

    def set_mute(self, muted: bool) -> None:
        self.muted = bool(muted)
        self._log(f"MUTE {self.muted}")

    def get_time_pos(self) -> Optional[float]:
        return self.time_pos if self.current is not None else None

    def show_text(self, text: str, duration: float) -> None:
        self.messages.append((text, duration))
        self._log(f"TEXT {text!r} ({duration}s)")

    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        self.overlays[overlay_id] = ass
        self._log(f"OVERLAY {overlay_id}")

    def clear_overlay(self, overlay_id: int) -> None:
        self.overlays.pop(overlay_id, None)
        self._log(f"CLEAR OVERLAY {overlay_id}")

    def close(self) -> None:
        self.closed = True
        self._log("CLOSE")

    # -- test/dev helper ----------------------------------------------------
    def finish_current(self, reason: str = END_EOF) -> None:
        """Simulate the current episode ending, triggering ``on_end``."""
        self.current = None
        if self.on_end is not None:
            self.on_end(reason)


def _extract_end_reason(event) -> str:  # pragma: no cover - libmpv specific
    """Normalise the many shapes of a python-mpv end-file event into a reason."""
    reason = None
    try:
        data = getattr(event, "data", event)
        if isinstance(data, dict):
            reason = data.get("reason")
        else:
            reason = getattr(data, "reason", None)
    except Exception:  # noqa: BLE001
        reason = None
    reason = str(reason).lower() if reason is not None else ""
    if "eof" in reason:
        return END_EOF
    if "error" in reason:
        return END_ERROR
    if "stop" in reason or "quit" in reason:
        return END_STOPPED
    # Unknown/redirect reasons: treat as a natural end so the channel keeps going.
    return END_EOF


def _install_fonts_for_mpv(fonts_dir: Path) -> None:
    """Copy bundled .ttf fonts into mpv's config 'fonts' dir so libass finds them.

    mpv automatically loads any fonts placed in ``<mpv config dir>/fonts``, which
    is the most reliable way to make our retro OSD font available to the ASS
    overlays without touching the system-wide fontconfig setup.
    """
    import os
    import shutil

    if not fonts_dir.is_dir():
        return
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    dest = Path(config_home) / "mpv" / "fonts"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for ttf in fonts_dir.glob("*.ttf"):
            target = dest / ttf.name
            if not target.exists():
                shutil.copy2(ttf, target)
    except OSError:
        log.debug("could not install bundled fonts for mpv", exc_info=True)


def _strip_ass(ass: str) -> str:  # pragma: no cover - trivial
    """Very small ASS-tag stripper for the show-text fallback path."""
    import re

    text = re.sub(r"\{[^}]*\}", "", ass)
    text = text.replace("\\N", " ").replace("\\n", " ")
    return text.strip()


__all__ = [
    "Player",
    "MpvPlayer",
    "MockPlayer",
    "END_EOF",
    "END_ERROR",
    "END_STOPPED",
]
