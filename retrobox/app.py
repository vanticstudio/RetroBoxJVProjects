"""The television itself: the state machine that ties everything together.

:class:`TVApp` owns the channel lineup, the player, the overlays and the input
queue, and turns remote-control actions into TV behaviour: changing channels
(with a burst of static and a channel banner), adjusting and muting the volume,
direct channel entry by number, an info banner, a browsable channel guide, a
"last channel" jump, a sleep timer, and a standby/off mode. When an episode ends
it automatically rolls into the next one on that channel's shuffle - optionally
by way of a station bumper - so the box never stops "broadcasting". It also
watches the clock, so a daypart window opening or closing takes effect straight
away rather than waiting for the current episode to finish.

The class is written to be testable without a display: pass it a
:class:`~retrobox.player.MockPlayer` and a fake clock and you can single-step
the whole thing (see ``step`` / ``handle_event`` / ``process_pending``).
"""

from __future__ import annotations

import logging
import queue
import random
import subprocess
import time
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import display as display_mod
from .actions import Action, CrtSettings, InputEvent
from .channel import Channel, ChannelLineup, PlayRequest, build_lineup, scan_episodes
from .config import POWER_OFF_COMMANDS, Config, ConfigError, CrtConfig, load_config
from .input.cec import CEC_ABSENT, CecPower, cec_display_power
from .input.manager import InputManager, create_backends
from .menu import MenuContext, MenuModel
from .overlay import CANVAS_H, CANVAS_W, GuideEntry, OverlayManager, menu_row_at
from .player import END_EOF, END_ERROR, MockPlayer, Player
from .status import display_summary, write_status
from .playlist import ShuffleBag
from .static_gen import (
    COLORBARS_FILENAME,
    DEFAULT_ASSETS_DIR,
    GLITCH_FILENAME,
    STATIC_FILENAME,
)

log = logging.getLogger(__name__)

# Exit status used after a clean, deliberate power-off. The systemd unit lists
# it in RestartPreventExitStatus so the box is not relaunched while the machine
# is on its way down.
EXIT_POWERED_OFF = 3

# While the guide is on screen these drive the guide, not the tuner.
_GUIDE_KEYS = (Action.CHANNEL_UP, Action.CHANNEL_DOWN, Action.ENTER)

# While the menu is on screen these drive the menu, not the tuner.
_MENU_KEYS = (
    Action.CHANNEL_UP,
    Action.CHANNEL_DOWN,
    Action.VOLUME_UP,
    Action.VOLUME_DOWN,
    Action.ENTER,
    Action.LAST_CHANNEL,
)

# Safety net for the boot splash: if the clip never reports end-of-file (a
# truncated or unplayable file), give up after this long and start the TV
# anyway rather than sitting on a dead screen forever.
_SPLASH_TIMEOUT_SECONDS = 30.0

# How often the status snapshot the web dashboard reads is refreshed.
_STATUS_INTERVAL_SECONDS = 2.0

#: The token used by the hold that a deliberate wake puts on the box - the
#: dashboard's Wake button, a button pressed on a box that had gone quiet,
#: and (when there is one) a browser watching the live stream, which sends the
#: same command over and over as a heartbeat. One token, so a heartbeat
#: extends the hold rather than stacking a new one up every few seconds.
WAKE_HOLD_TOKEN = "wake"

#: How long a deliberate wake keeps the box awake for.
#:
#: Long on purpose. Somebody pressing Wake is telling us detection got it
#: wrong, and a box that went straight back to sleep in front of them would be
#: worse than one that never slept. Half an hour of a fan running is the cheap
#: mistake; a dark screen somebody cannot fix is the expensive one. Anything
#: that wants finer control - a live viewer, say - holds the box awake itself
#: with its own token and its own interval.
WAKE_HOLD_SECONDS = 30 * 60.0


def make_display_watcher(config: Config, **overrides) -> "display_mod.DisplayWatcher":
    """Build the watcher that answers "is there a television out there?".

    The only place the config's timings meet :mod:`retrobox.display`. The
    keyword overrides exist for the tests, which hand it a sysfs tree of their
    own and an event source that only ever waits - this machine has no /sys,
    no DRM and no netlink, and the suite must never touch any of them.
    """
    settings = dict(sleep_after=config.display_sleep.sleep_after_seconds)
    settings.update(overrides)
    return display_mod.DisplayWatcher(**settings)


class TVApp:
    """The retro-TV application state machine."""

    #: The shortest gap between two changes to the CRT shader, in seconds.
    #:
    #: A range input fires per pixel of travel, and every change makes mpv
    #: write and compile a new GLSL shader - real work on this box's two-core
    #: Celeron. Five a second is plenty for a value with no correct answer:
    #: nobody's eye resolves the difference between the 0.31 and the 0.32 they
    #: swept through on the way to 0.33.
    #:
    #: This is a TRAILING throttle, not a dropping one. A value that arrives
    #: too soon is held, not discarded, and :meth:`step` puts it on the screen
    #: as soon as the gap has passed - so the value somebody let go of is
    #: always the value they end up looking at.
    CRT_THROTTLE_SECONDS = 0.2

    #: How long a preview survives with nothing further heard from whoever
    #: started it, before the last SAVED settings go back on the screen.
    #:
    #: A preview lives in this process and nowhere else, so the three ways
    #: somebody "walks away" land differently. Switching the box off at the
    #: wall is free: nothing was written, so the next start reads config.yaml
    #: and shows what was saved. The dashboard's Cancel button is explicit.
    #: But a browser tab that closes - or a phone that walks out of wifi range
    #: mid-drag - says nothing at all, and one line of socket traffic per
    #: connection means there is no dropped connection to notice either. So
    #: the box times the preview out for itself. The dashboard is expected to
    #: re-send the value it is showing every few seconds while its picture
    #: panel is open; an unchanged value costs nothing (see
    #: :meth:`_want_crt`), and when the heartbeats stop, so does the preview.
    CRT_PREVIEW_HOLD_SECONDS = 20.0

    def __init__(
        self,
        config: Config,
        player: Player,
        input_manager: InputManager,
        *,
        overlay: Optional[OverlayManager] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        assets_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
        display_factory: Optional[
            Callable[[Config], Optional["display_mod.DisplayWatcher"]]
        ] = None,
    ) -> None:
        self.config = config
        # Where the config came from, so a `reload` command can go and read it
        # again. None when the app was built straight from a Config object
        # (the tests, and anything embedding it), in which case reload is a
        # no-op rather than a guess at which file was meant.
        self._config_path = Path(config_path) if config_path else None
        self.player = player
        self.input = input_manager
        self.overlay = overlay or OverlayManager(player, config, clock=clock)
        # Two clocks on purpose: `clock` is monotonic and drives durations
        # (overlay expiry, the bridge, the sleep timer); `wall_clock` is real
        # calendar time and drives dayparting, which cares what hour it is.
        self._clock = clock
        self._wall_clock = wall_clock

        self.lineup: ChannelLineup = build_lineup(config, wall_clock=wall_clock)

        # Runtime state.
        self.volume = config.initial_volume
        self.muted = False
        self.standby = False
        self.powered_off = False
        self._playing_path: Optional[Path] = None
        self._last_channel_number: Optional[int] = None
        self._running = False

        # Direct channel entry ("type 1 then 2 -> channel 12").
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self._digit_entry_timeout = 2.0

        # Pending "bridge" switch: keep the old show playing until this deadline,
        # then cut to the channel that was preloaded. The channel banner is shown
        # at the moment of the cut-over, not when the button is pressed.
        self._switch_deadline: Optional[float] = None
        self._pending_banner: Optional[tuple[int, str]] = None

        # Sleep timer: `_sleep_index` is where we are in config.sleep_steps,
        # `_sleep_deadline` is the monotonic instant the box turns itself off,
        # and `_sleep_shown` is the minute currently drawn in the corner.
        self._sleep_index: Optional[int] = None
        self._sleep_deadline: Optional[float] = None
        self._sleep_shown: Optional[int] = None

        # Which row of the guide is highlighted while it is open.
        self._guide_index: Optional[int] = None

        # Last observed daypart state of the current channel, so a window
        # opening or closing under us is noticed rather than waited out.
        self._daypart_marker: Optional[tuple] = None

        # Status snapshot for the web dashboard.
        self._status_due = 0.0
        self._started_at = self._clock()

        # Station bumpers played between episodes.
        self._bumper_rng = random.Random(config.shuffle_seed)
        self._bumpers: Optional[ShuffleBag[Path]] = self._load_bumpers()

        # Playback-finished events from the player (may arrive on any thread).
        self._ended: "queue.Queue[str]" = queue.Queue()
        self.player.on_end = self._ended.put

        # Going quiet when there is no television watching.
        #
        # `display_factory` is how the watcher gets built, and None means this
        # process does not watch the display at all - which is what a test
        # harness wants, and exactly what `display_sleep.enabled: false`
        # produces. `from_config` supplies the real one, so the box itself
        # always has it and nothing about that decision is hidden in here.
        self._display_factory = display_factory
        self._display_watcher: Optional["display_mod.DisplayWatcher"] = None
        # The settings the watcher standing there was built from, so a reload
        # can tell a change that needs a new one from a change that does not.
        self._display_settings = config.display_sleep
        # State changes arrive on the WATCHER's thread, so they are queued
        # exactly like end-of-file and clicks are, and read on our own loop.
        self._display_events: "queue.Queue[display_mod.DisplaySnapshot]" = queue.Queue()
        self._display_asleep = False
        # Wall-clock instant the picture was paused, and where in the episode
        # it was paused - both only meaningful while asleep.
        self._display_since: Optional[float] = None
        self._display_position: Optional[float] = None
        # Set once detection has been given up on, so it is said exactly once.
        self._display_broken = False
        # An episode that reached its end while the box was quiet.
        self._display_ended = False
        # WHO IS HOLDING THE BOX AWAKE, AND WHEN EACH HOLD RUNS OUT (monotonic).
        #
        # Kept here rather than only in the watcher because the watcher is a
        # disposable thing: saving any setting throws it away and builds
        # another, and a hold that lived only in the old one would vanish -
        # putting the box back to sleep in front of somebody who had just
        # pressed Wake. This is the book of record; the watcher gets a copy so
        # its own snapshot tells the truth.
        self._display_hold_until: Dict[str, float] = {}

        # Filler assets.
        self._assets_dir = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
        self._colorbars_path = self._resolve_asset(COLORBARS_FILENAME)
        # The channel-change transition clip depends on the configured effect.
        self._transition_path = self._resolve_transition_asset()

        # Boot splash: played once before the first channel is tuned.
        self._splash_path = self._resolve_splash()
        self._splash_active = False
        self._splash_deadline: Optional[float] = None

        # The CRT picture effect, which is the one setting somebody adjusts
        # while looking at the television rather than at the dashboard.
        #
        # `_crt_live` is what the picture is actually showing. The player was
        # built with config.crt baked into its mpv options, so that is where
        # it starts. `_crt_wanted` is a value that has been asked for but not
        # yet put on the screen - held back by the throttle, and flushed by
        # step(). `_crt_preview_until` is the instant an unsaved preview gives
        # up on whoever started it; None means nothing is being previewed and
        # the screen is showing saved settings.
        self._crt_live: CrtConfig = config.crt
        self._crt_wanted: Optional[CrtConfig] = None
        self._crt_next_allowed = 0.0
        self._crt_preview_until: Optional[float] = None

        # On-screen menu. `_menu` is None whenever the menu is closed.
        self._menu: Optional[MenuModel] = None
        self._audio_device = config.audio_device
        # Clicks arrive on mpv's thread, so they are queued like end-of-file.
        self._clicks: "queue.Queue[tuple]" = queue.Queue()
        self.player.on_click = self._clicks.put
        #: Which output the television is on and how it got there. Decided
        #: at every start, never only at install - see set_up_audio.
        self._audio_setup = self.set_up_audio()

    # -- sound --------------------------------------------------------------
    def set_up_audio(self):
        """Choose an output, put the television on it, and unmute it.

        Run at **every** start, not once at install. A box built on a bench
        with no screen attached - because ethernet is faster for loading the
        library - and later carried to a living room has to find its sound on
        that boot, with nobody typing anything.

        Never raises and never refuses to start. A box with no working audio
        is still a box; it says so and plays the picture.
        """
        from . import audioout, eld

        try:
            sockets = eld.hdmi_outputs()
        except Exception:  # noqa: BLE001 - an odd SBC, no /proc/asound
            log.debug("could not read the HDMI sockets", exc_info=True)
            sockets = []
        try:
            devices = self.player.list_audio_devices()
        except Exception:  # noqa: BLE001
            log.debug("the player would not list its outputs", exc_info=True)
            devices = []

        decision = audioout.decide(
            self._audio_device, sockets=sockets, player_devices=devices)

        if decision.device and decision.device != self._audio_device:
            try:
                self.player.set_audio_device(decision.device)
                self._audio_device = decision.device
            except Exception:  # noqa: BLE001
                log.warning("could not switch audio output to %s",
                            decision.device, exc_info=True)
        try:
            self.player.set_audio_channels(decision.layout)
        except Exception:  # noqa: BLE001
            log.debug("the player would not take a channel layout",
                      exc_info=True)

        if decision.source == "eld":
            # Only when a set was genuinely found: HDMI controls are often
            # muted at zero by default, and everything above looks healthy
            # while producing silence.
            live = [s for s in sockets if s.usable]
            if live:
                try:
                    audioout.unmute(str(live[0].card_index))
                except Exception:  # noqa: BLE001
                    log.debug("could not unmute the HDMI output", exc_info=True)

        log.info("%s", decision.summary)
        return decision

    def play_test_tone(self, *, seconds: float = 2.0) -> bool:
        """Put a short tone through whatever output was chosen.

        Somebody setting a box up in a living room has to be able to answer
        "is it the box or is it the telly" without a terminal, and this is
        the only way to do it. Works with nothing playing and while something
        is playing: what was on is remembered and put back afterwards.
        """
        import threading

        was_playing = self._playing_path
        try:
            position = self.player.get_time_pos() or 0.0
        except Exception:  # noqa: BLE001
            position = 0.0

        if not self.player.play_test_tone(seconds=seconds):
            return False

        if was_playing is not None:
            # keep_open=yes leaves mpv paused on the tone's last frame, so
            # without this the picture stops on a black screen.
            timer = threading.Timer(
                seconds + 0.4, self._after_test_tone, (was_playing, position))
            timer.daemon = True
            timer.start()
        return True

    def _after_test_tone(self, path: Path, position: float) -> None:
        try:
            self.player.play(path, start=max(0.0, position))
        except Exception:  # noqa: BLE001 - never leave the screen on a tone
            log.warning("could not resume after the test tone", exc_info=True)

    # -- construction -------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        player: Optional[Player] = None,
        input_manager: Optional[InputManager] = None,
        dry_run: bool = False,
        assets_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ) -> "TVApp":
        """Build a fully wired app, creating real hardware backends by default.

        ``dry_run`` swaps in a :class:`MockPlayer` and disables all real input
        backends (a stdin backend is added if a TTY is available), which is how
        the box can be exercised on a development machine.
        """
        if player is None:
            if dry_run:
                player = MockPlayer(verbose=True)
            else:
                from .crt import write_shader
                from .player import MpvPlayer

                assets = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
                shader_path = write_shader(config.crt)
                player = MpvPlayer(
                    glsl_shaders=str(shader_path) if shader_path else None,
                    fonts_dir=assets / "fonts",
                    force_4_3=config.force_4_3,
                    audio_device=config.audio_device,
                )

        if input_manager is None:
            if dry_run:
                backends = create_backends({"keyboard": False, "cec": False, "stdin": True})
            else:
                backends = create_backends(config.input_options)
            input_manager = InputManager(backends)

        return cls(
            config, player, input_manager,
            assets_dir=assets_dir, config_path=config_path,
            # The real box always watches the display; whether it acts on what
            # it sees is `display_sleep.enabled`, checked in one place (see
            # _start_display_watch) rather than in two.
            display_factory=make_display_watcher,
        )

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Power on: set volume, start input, and tune to the first channel.

        When a boot splash is configured it plays once first; the normal
        start-channel behaviour happens when it finishes, is skipped by a
        keypress, or times out.
        """
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)
        # Channel mode is remote-only: the pointer is switched off and hidden
        # from the moment the box comes up, whether or not a mouse is plugged
        # in. Only the menu turns it on, and only for as long as it is open.
        self.player.set_mouse_enabled(False)
        self.input.start()
        self._select_start_channel()
        if not self._begin_splash():
            self.tune_current(show_static=False)
        # Last, and deliberately so: there is a picture on the screen by the
        # time anything goes looking for a television. A box that starts with
        # no display attached must come up exactly like any other and go quiet
        # afterwards, never sit dark waiting to be told what is out there.
        self._start_display_watch()

    # -- boot splash --------------------------------------------------------
    def _resolve_splash(self) -> Optional[Path]:
        """Locate the configured splash clip, or None to skip it entirely."""
        configured = self.config.boot_splash
        if configured is None:
            return None
        if configured.is_file():
            return configured
        # A bare filename resolves against the assets directory, so
        # `boot_splash: boot_splash.mp4` finds the bundled clip.
        bundled = self._assets_dir / configured.name
        if bundled.is_file():
            return bundled
        log.warning("boot splash not found, skipping it: %s", configured)
        return None

    def _begin_splash(self) -> bool:
        """Play the splash once. Returns False when there is nothing to play."""
        if self._splash_path is None:
            return False
        self._splash_active = True
        self._splash_deadline = self._clock() + _SPLASH_TIMEOUT_SECONDS
        # No channel banner over the splash: nothing is tuned yet, and
        # tune_current (which draws it) is deliberately not called until after.
        self.overlay.clear_all()
        self.player.play(self._splash_path)
        log.info("playing boot splash: %s", self._splash_path)
        return True

    def _finish_splash(self) -> None:
        """Leave the splash and fall through to the normal start-up path."""
        if not self._splash_active:
            return
        self._splash_active = False
        self._splash_deadline = None
        self.tune_current(show_static=False)

    def run(self) -> None:
        """Run the blocking main loop until a QUIT action is received."""
        self.start()
        self._running = True
        log.info("Retro Box is on the air. %d channels.", len(self.lineup))
        try:
            while self._running:
                self.step(block=True)
        except KeyboardInterrupt:  # pragma: no cover - interactive convenience
            log.info("interrupted; shutting down")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._running = False
        try:
            self.overlay.clear_all()
        except Exception:  # noqa: BLE001
            pass
        self._stop_display_watch()
        self.input.stop()
        self.player.close()

    # -- main-loop step (small and testable) --------------------------------
    def step(self, *, block: bool = False, timeout: float = 0.1) -> None:
        """Advance the state machine by one iteration.

        Handles overlay expiry, channel-entry timeouts, finished episodes, and
        at most one queued input event.
        """
        now = self._clock()
        self.overlay.tick()
        self._maybe_commit_switch(now)
        self._maybe_commit_digits(now)
        self._maybe_fire_sleep(now)
        self._maybe_timeout_splash(now)
        self._maybe_flush_crt(now)
        # Before the daypart watcher and before the status is written, so that
        # a box waking up spends no part of a tick half awake: everything
        # below this line sees a box that is playing again.
        self._maybe_display_sleep()
        self._update_sleep_indicator()
        self._maybe_retune_daypart()
        self._drain_clicks()
        self._track_pointer()
        self._maybe_write_status(now)
        self._drain_playback_events()

        event = self.input.get(timeout=timeout if block else 0.0)
        if event is not None:
            self.handle_event(event)
            # Publish straight away rather than waiting out the status
            # interval: the dashboard's input test is "press a button, see it
            # light up", and a two-second lag on that reads as broken.
            self._status_due = 0.0
            self._maybe_write_status(self._clock())

    def _maybe_commit_switch(self, now: float) -> None:
        """Cut over to the preloaded channel once the bridge window has elapsed."""
        if self._switch_deadline is not None and now >= self._switch_deadline:
            self._switch_deadline = None
            self.player.commit_switch()
            # Flash the channel banner right as the picture actually changes.
            if self._pending_banner is not None:
                self.overlay.show_channel_bug(*self._pending_banner)
                self._pending_banner = None

    # -- input handling -----------------------------------------------------
    def handle_event(self, event: InputEvent) -> None:
        action = event.action

        if action == Action.QUIT:
            self._running = False
            return
        if action == Action.SHUTDOWN:
            self.close_menu()
            self._power_off()
            return
        # Administrative, not a button on the remote: it comes from the
        # dashboard having just rewritten config.yaml. Handled up here so it
        # works during the splash, in standby, and with the menu open.
        if action == Action.RELOAD:
            self.reload_config()
            return
        # Also administrative, and also handled up here: somebody is dragging
        # the curvature slider with the television in front of them, and that
        # has to work with the menu up, during the splash and in standby -
        # exactly like a reload does, and for the same reason. Nothing here
        # touches config.yaml; a preview is only ever on the screen.
        if action == Action.CRT_PREVIEW:
            self._preview_crt(event.crt)
            return
        if action == Action.CRT_CANCEL:
            self._cancel_crt_preview()
            return
        # Administrative too: somebody in a living room pressed REPAIR or
        # TEST SOUND on the dashboard. Handled up here so they work while the
        # menu is open, during the splash and in standby - a box that is
        # silent is exactly the box somebody will be poking at.
        if action == Action.AUDIO_SETUP:
            self._audio_setup = self.set_up_audio()
            return
        if action == Action.TEST_TONE:
            self.play_test_tone()
            return
        # Also administrative: the dashboard's Wake button, for when display
        # detection got it wrong, and the door a live viewer will hold the box
        # awake through when there is one. It has to work during the splash,
        # in standby and with the menu open, like the two above.
        if action == Action.WAKE:
            self._manual_wake()
            return

        # Anything else means somebody is pressing buttons, and somebody
        # pressing buttons is somebody being there. A box that stayed paused
        # while its owner worked the remote would look broken, and "stuck
        # asleep" is the one failure this feature is not allowed to have. The
        # press is not consumed - it still does whatever it was going to do.
        if self._display_asleep:
            self._manual_wake()

        # Any other button cuts the boot splash short and starts the TV. The
        # press is consumed doing that - nobody wants their first channel-up to
        # silently land on channel 3.
        if self._splash_active:
            self._finish_splash()
            return

        if action == Action.MENU:
            self._toggle_menu()
            return

        # With the menu up, the D-pad drives the menu rather than the tuner.
        if self._menu is not None and action in _MENU_KEYS:
            self._handle_menu_key(action)
            return

        if action == Action.POWER:
            self._toggle_standby()
            return

        # While in standby, ignore everything except POWER/QUIT (handled above).
        if self.standby:
            return

        # With the guide up, the D-pad drives the guide rather than the tuner.
        if self.overlay.guide_visible and action in _GUIDE_KEYS:
            self._handle_guide_key(action)
            return

        handlers = {
            Action.CHANNEL_UP: self._channel_up,
            Action.CHANNEL_DOWN: self._channel_down,
            Action.VOLUME_UP: self._volume_up,
            Action.VOLUME_DOWN: self._volume_down,
            Action.MUTE: self._toggle_mute,
            Action.INFO: self._show_info,
            Action.GUIDE: self._toggle_guide,
            Action.SLEEP: self._cycle_sleep,
            Action.LAST_CHANNEL: self._jump_last_channel,
            Action.ENTER: self._confirm_digits,
        }
        if action == Action.DIGIT:
            self._push_digit(event.value or 0)
        else:
            handler = handlers.get(action)
            if handler is not None:
                handler()

    # -- the CRT picture effect, live -------------------------------------
    def _preview_crt(self, settings: Optional[CrtSettings]) -> None:
        """Put unsaved picture settings on the television that is playing.

        Merged onto whatever is being previewed already, or onto the saved
        settings when nothing is: the dashboard sends the control that moved,
        not the whole panel, so a curvature drag must not quietly undo the
        scanlines somebody switched off a moment before.
        """
        if settings is None:
            return
        changes = settings.changes()
        if not changes:
            return
        base = self._crt_wanted if self._crt_wanted is not None else self._crt_live
        now = self._clock()
        # Pushed out on every nudge. While somebody is dragging, or while the
        # dashboard is saying it is still there, the preview never expires.
        self._crt_preview_until = now + self.CRT_PREVIEW_HOLD_SECONDS
        self._want_crt(dataclass_replace(base, **changes), now=now)

    def _cancel_crt_preview(self) -> None:
        """Throw the preview away and put the last SAVED settings back.

        This is the dashboard's Cancel button, and it is also what the box
        does for itself when a dashboard stops talking to it. It restores the
        picture, not just the browser: somebody who dragged the slider and
        changed their mind must not have to save to undo it.
        """
        if self._crt_preview_until is None:
            return                       # nothing was being previewed
        self._crt_preview_until = None
        self._want_crt(self.config.crt, now=self._clock())

    def _want_crt(self, crt: CrtConfig, *, now: float) -> None:
        """Ask for ``crt`` on the screen, subject to the throttle."""
        self._crt_wanted = crt
        self._flush_crt(now)

    def _flush_crt(self, now: float) -> None:
        """Put the wanted settings on the screen if the throttle allows it.

        THIS IS WHERE THE THROTTLE LIVES, and it lives here rather than in the
        dashboard or in the browser on purpose. This is the last gate before
        the expensive part - writing a shader file and making mpv compile it -
        and it is inside the process that owns the player, which is the only
        way to reach that player at all. A limit in the page's JavaScript
        protects the box right up until somebody writes to the socket by hand;
        a limit here holds for every caller there will ever be.
        """
        wanted = self._crt_wanted
        if wanted is None:
            return
        if wanted == self._crt_live:
            # Already on the screen. The dashboard's heartbeat lands here, as
            # does a slider dragged back to where it started, and neither one
            # should cost a shader compile.
            self._crt_wanted = None
            return
        if now < self._crt_next_allowed:
            return                       # too soon; step() will come back for it
        self._crt_wanted = None
        # Bumped whether or not the change takes, so a player that is refusing
        # them (a full cache disk) is asked five times a second at worst.
        self._crt_next_allowed = now + self.CRT_THROTTLE_SECONDS
        try:
            changed = self.player.set_crt(wanted)
        except Exception:  # noqa: BLE001 - the programme outranks the effect
            log.warning("could not change the CRT picture effect", exc_info=True)
            return
        if changed:
            self._crt_live = wanted
        # A player that says no left the picture as it was, so `_crt_live`
        # stays as it was too - and the next nudge of the slider tries again.

    def _maybe_flush_crt(self, now: float) -> None:
        """Main-loop half of the throttle, and the preview's own dead-man's switch."""
        if (self._crt_preview_until is not None
                and now >= self._crt_preview_until):
            # Whoever was previewing has gone quiet: a closed tab, a phone out
            # of range, a socket that dropped mid-drag. None of those tell us
            # anything, so silence is the signal.
            log.info("nobody is watching the CRT preview any more; "
                     "putting the saved picture settings back")
            self._cancel_crt_preview()
            return
        self._flush_crt(now)

    # -- going quiet when there is no television watching -------------------
    #
    # WHY THIS EXISTS, and it is not the money. It saves eight or ten watts,
    # which is about fifteen pounds a year and irrelevant. It exists because a
    # box in a cabinet behind a television that roars whenever the room is
    # empty is a thing people complain about, because heat shortens the life of
    # hardware that is already secondhand, and because silence when the telly
    # is off is a quality signal somebody notices the first evening they own it.
    #
    # THE RULE THAT OUTRANKS ALL OF IT: nothing here may take the picture away
    # from somebody who is watching. Every unknown, every failure and every
    # question this cannot answer ends in "stay awake", which is the same
    # behaviour as the feature being switched off. Being wrong costs a fan
    # spinning; being wrong the other way costs a customer a dark television
    # in a box they cannot log into and can only switch off at the wall.
    @property
    def display_asleep(self) -> bool:
        """True when the picture is paused because nothing is watching."""
        return self._display_asleep

    def _start_display_watch(self) -> None:
        """Begin watching the video output, if this box does that at all."""
        if self._display_watcher is not None:
            return
        if self._display_factory is None or not self.config.display_sleep.enabled:
            return
        watcher = None
        try:
            watcher = self._display_factory(self.config)
            if watcher is None:
                self._display_gave_up("there is no way to detect a display on this box")
                return
            # The callback runs on the watcher's thread, so it does exactly
            # what the player's end-of-file callback does: puts the snapshot
            # on a queue and gets out of the way.
            watcher.on_change = self._display_events.put
            watcher.start()
        except Exception:  # noqa: BLE001 - video first, always
            self._display_gave_up("display detection could not be started", exc=True)
            if watcher is not None:
                try:
                    watcher.stop()
                except Exception:  # noqa: BLE001
                    log.debug("stopping the failed display watcher failed", exc_info=True)
            return
        self._display_settings = self.config.display_sleep
        self._display_watcher = watcher
        # Hand the new watcher the holds that are still live. Without this a
        # saved setting silently cancels somebody's Wake, because the watcher
        # it was taken on has just been thrown away.
        self._replay_holds(watcher)

    def _replay_holds(self, watcher) -> None:
        now = self._clock()
        for token, until in self._live_hold_deadlines(now):
            try:
                watcher.hold_awake(token, until - now)
            except Exception:  # noqa: BLE001 - a hold we cannot copy is still ours
                log.debug("could not carry a hold over to the new watcher", exc_info=True)

    def _live_hold_deadlines(self, now: float) -> tuple:
        """The holds that have not run out, dropping the ones that have."""
        for token in [t for t, until in self._display_hold_until.items() if until <= now]:
            del self._display_hold_until[token]
        return tuple(sorted(self._display_hold_until.items()))

    def _stop_display_watch(self) -> None:
        watcher = self._display_watcher
        self._display_watcher = None
        if watcher is None:
            return
        try:
            watcher.stop()
        except Exception:  # noqa: BLE001 - never hold up a shutdown
            log.debug("stopping the display watcher failed", exc_info=True)

    def _display_gave_up(self, why: str, *, exc: bool = False) -> None:
        """Switch the feature off for this run, and say so exactly once.

        Once is the point for the LOG LINE. This runs on a two-core box that
        logs to eMMC, and a failure that repeats every tick would fill the
        journal and wear the disk to tell somebody the same thing several
        thousand times.

        THE WAKE IS NOT ONCE, AND COMES FIRST. Giving up drops the watcher,
        and _maybe_display_sleep returns on its first line without one - so
        anything left paused at this moment would stay paused for ever, in
        front of a working television, with a dashboard reporting no fault.
        The log line already promises the box will stay awake; this is the
        line that makes that true rather than a claim.
        """
        self._display_watcher = None
        self._wake_display(why)
        if self._display_broken:
            return
        self._display_broken = True
        log.warning(
            "%s - the box will stay awake and will not go quiet on its own", why,
            exc_info=exc,
        )

    def apply_display_sleep_setting(self) -> None:
        """Take up a change to the ``display_sleep`` block, mid-programme.

        Switching it OFF happens at once and in the safe direction: the
        watcher stops and anything it paused is put back on the screen.
        Switching it on, or changing how long the display has to have been
        gone, needs a new watcher - the debounce is baked in when one is built.
        """
        settings = self.config.display_sleep
        if not settings.enabled:
            self._display_settings = settings
            self._stop_display_watch()
            self._wake_display("going quiet was switched off")
            return
        if self._display_watcher is not None and settings == self._display_settings:
            return                       # nothing that needs a new watcher
        self._stop_display_watch()
        self._display_settings = settings
        self._start_display_watch()

    # -- the seam for anything else that is watching ------------------------
    # There is no live stream viewer yet - that work stopped at a hardware
    # gate - so nothing in this process calls these. They exist so that when
    # there is one, a browser watching counts as watching: it holds the box
    # awake with its own token, repeated as a heartbeat, and a tab closed on a
    # train stops holding it of its own accord. The dashboard's Wake button
    # goes through exactly the same door (see WAKE_HOLD_TOKEN).
    def hold_awake(self, token: str, seconds: Optional[float] = None) -> bool:
        """Keep the box awake under ``token`` for ``seconds``, and wake it now.

        Returns False only when this process is not in the display-sleep
        business at all - the feature switched off, or no factory to build a
        watcher with - because then there is nothing a hold could be holding
        back. A watcher that is momentarily absent is NOT that case: the hold
        is written down here either way, so it outlives a rebuild and so it
        still counts while detection is being given up on.

        Calling again with the same token extends that one hold rather than
        stacking another up, so a heartbeat is just the same call again.
        """
        if self._display_factory is None or not self.config.display_sleep.enabled:
            return False
        span = (
            display_mod.HOLD_SECONDS if seconds is None else max(0.0, float(seconds))
        )
        self._display_hold_until[token] = self._clock() + span
        watcher = self._display_watcher
        if watcher is not None:
            try:
                watcher.hold_awake(token, span)
            except Exception:  # noqa: BLE001 - our own record is the one that counts
                log.debug("could not take a hold on the display watcher", exc_info=True)
        return True

    def release_hold(self, token: str) -> None:
        """Give up a hold early. Harmless if there was never one."""
        self._display_hold_until.pop(token, None)
        watcher = self._display_watcher
        if watcher is None:
            return
        try:
            watcher.release_hold(token)
        except Exception:  # noqa: BLE001
            log.debug("could not release a hold on the display watcher", exc_info=True)

    # -- deciding, once a tick ---------------------------------------------
    def _maybe_display_sleep(self) -> None:
        """Should this box be awake at all?

        Asked every tick, not only when the watcher pushes a change. A hold
        running out is edge-free - nobody is there to announce it - and the
        television's own word about its power arrives over CEC on a different
        thread entirely, so the polled question is the authoritative one and
        the pushed events are only how the journal gets the detail.
        """
        watcher = self._display_watcher
        if watcher is None:
            return
        self._drain_display_events()
        try:
            power = self._cec_power()
            awake = self._wants_awake(watcher, power)
        except Exception:  # noqa: BLE001 - the programme outranks the feature
            # Giving up wakes the box itself - it has to, because it is also
            # reached from a rebuild that failed, where nothing else would.
            self._display_gave_up("display detection stopped working", exc=True)
            return

        if awake:
            self._wake_display("something is watching again")
            return

        # From here on this tick intends to leave the box quiet, so this is
        # where the second opinion belongs.
        #
        # BELT AND BRACES, AND THE BRACES ARE THE POINT. Stuck asleep in front
        # of a working television is strictly worse than never sleeping at
        # all, and it is the one outcome nobody can recover from without
        # switching the box off at the wall. So it is checked for with a
        # different question than the one that decided to sleep, rather than
        # trusted to fall out of the state machine being right. A set in
        # standby commonly keeps hotplug asserted, so a live cable only counts
        # as a television watching while CEC is not saying otherwise.
        if (self._display_asleep and watcher.state == display_mod.PRESENT
                and not power.says_off):
            log.error(
                "the box was asleep with a display connected - waking it. That "
                "should not be reachable, so something above this is wrong."
            )
            self._wake_display("a display is connected after all")
            return
        if self._display_asleep or not self._may_go_quiet():
            return
        self._sleep_display(watcher, power)

    def _wants_awake(self, watcher, power: CecPower) -> bool:
        """Is anything at all asking this box to stay awake?

        THIS DOES NOT DECIDE ANYTHING ITSELF. It gathers the three inputs and
        hands them to :func:`retrobox.display.wants_awake`, which is the one
        and only place the precedence between them is written down - the same
        function the watcher's own ``should_be_awake`` goes through. Two
        implementations of one question drift apart, and the drift shows up as
        a box that goes quiet in front of somebody. So there is one.

        What this layer adds over the watcher is the television's own word:
        HDMI-CEC lives in the input backends, which the watcher cannot see.
        """
        return display_mod.wants_awake(
            holds=bool(self._display_holds(watcher)),
            wire_awake=watcher.should_be_awake(),
            set_is_off=power.says_off if power.is_known else None,
        )

    def _display_holds(self, watcher) -> tuple:
        """Everything currently holding the box awake, from both books.

        Ours is the book of record - it is the one that survives a watcher
        being rebuilt, and the one that still exists when there is no watcher.
        The watcher's is folded in as well because a hold this process never
        heard about is still a reason not to take somebody's picture away.
        """
        holds = set(dict(self._live_hold_deadlines(self._clock())))
        try:
            holds.update(watcher.snapshot().holds)
        except Exception:  # noqa: BLE001 - a watcher that cannot say is not a veto
            log.debug("could not read the watcher's holds", exc_info=True)
        return tuple(sorted(holds))

    def _cec_power(self) -> CecPower:
        """What HDMI-CEC says about the television, if this box has any."""
        try:
            return cec_display_power(self.input.backends)
        except Exception:  # noqa: BLE001
            log.debug("could not read the CEC power state", exc_info=True)
            return CecPower(CEC_ABSENT, None, "the CEC backend could not be read")

    def _may_go_quiet(self) -> bool:
        """Whether now is a sensible moment to pause - not whether to."""
        if self.standby or self.powered_off:
            return False    # the picture is already off; there is nothing to save
        if self._splash_active:
            return False    # never in front of the first video
        if self._menu is not None:
            return False    # the menu already paused playback for its own reasons
        if self._switch_deadline is not None:
            return False    # mid channel-change; let it land first
        return True

    def _drain_display_events(self) -> None:
        """Write down what the watcher pushed while we were busy elsewhere."""
        while True:
            try:
                snapshot = self._display_events.get_nowait()
            except queue.Empty:
                return
            log.info(
                "the video output is now %s (%s)", snapshot.state, snapshot.detail
            )

    # -- going quiet, and coming back --------------------------------------
    def _sleep_display(self, watcher, power: CecPower) -> None:
        """PAUSE. Never stop, and never tear the player down.

        Pausing drops decoding to nothing, which is all of the heat and all of
        the fan, while mpv stays alive holding the file it had open. The
        dashboard stays accurate, the file share keeps running, and coming
        back is instant. Stopping and reloading mpv would be slow, would lose
        everything about where we were, and would spend one of the restarts
        systemd is willing to allow before it gives up on the unit entirely.
        """
        self._display_position = self.player.get_time_pos()
        try:
            self.player.set_paused(True)
        except Exception:  # noqa: BLE001 - a player that will not pause keeps playing
            log.warning("could not pause the picture; staying awake", exc_info=True)
            self._display_position = None
            return
        self._display_asleep = True
        self._display_since = self._wall_clock()
        if power.says_off:
            why = power.detail
        else:
            try:
                why = watcher.snapshot().detail
            except Exception:  # noqa: BLE001
                why = "nothing is watching"
        log.info("nothing is watching (%s); pausing the picture", why)

    def _wake_display(self, why: str) -> None:
        """Undo exactly what going quiet did, and put the right thing back on."""
        if not self._display_asleep:
            return
        since = self._display_since
        position = self._display_position
        self._display_asleep = False
        self._display_since = None
        self._display_position = None
        slept = max(0.0, self._wall_clock() - since) if since is not None else 0.0
        # Unconditionally, and before anything else can go wrong: whatever the
        # rest of this decides, the picture must never be left paused.
        try:
            self.player.set_paused(False)
        except Exception:  # noqa: BLE001
            log.warning("could not un-pause the picture", exc_info=True)
        log.info("waking up: %s (quiet for %d seconds)", why, int(slept))
        self._resume_after_sleep(slept, position)

    def _resume_after_sleep(self, slept: float, position: Optional[float]) -> None:
        """Put back what should be on the screen now - rarely the paused frame.

        THIS IS THE PART THAT MATTERS MOST, and it is one line away from being
        wrong. A broadcast channel is a notional station that never stopped
        transmitting: :meth:`BroadcastSchedule.at` works out what is airing
        from the wall clock, so RE-TUNING lands exactly where the broadcast
        has got to. Calling un-pause and stopping there would resume three
        hours behind, and the illusion that the station kept going while the
        room was empty - which is the entire product - would be gone.

        A channel in `random` or `resume` mode has no schedule to recompute
        from, so the choice is the owner's and lives in the config. See
        :class:`~retrobox.config.DisplaySleepConfig` for the two answers and
        why `resume` is the default.
        """
        ended = self._display_ended
        self._display_ended = False
        if self.standby or self._splash_active or self._menu is not None:
            return
        if self.config.tune_in == "broadcast":
            self.tune_current(show_static=False)
            return
        if ended:
            # The episode ran out while nobody was watching, whatever the
            # setting says - there is no paused frame left to carry on from.
            self._advance_current()
            return
        if self.config.display_sleep.non_broadcast != "advance":
            return                     # `resume`: the paused frame is the right one
        self._advance_by(slept, position)

    def _advance_by(self, slept: float, position: Optional[float]) -> None:
        """Skip forward by however long the box was quiet, for `advance` mode."""
        path = self._playing_path
        if path is None or slept <= 0:
            return
        target = (position or 0.0) + slept
        duration = self._known_duration()
        if duration is not None and target >= duration:
            # It ran out while nobody was watching, so roll on exactly as an
            # ended episode does - but with no station bumper, because a
            # bumper is for a gap somebody is sitting through.
            request = self.lineup.current.advance()
            if request is None:
                self._show_no_signal(self.lineup.current)
            else:
                self._play_request(request)
            return
        # A duration we never learned is not a reason to refuse: mpv asked to
        # start past the end of a file reports end-of-file, and that rolls the
        # channel on by the ordinary path.
        self._play_request(PlayRequest(path=path, start=target))

    def _manual_wake(self) -> None:
        """Wake because somebody said so, and keep it awake for a while.

        The hold matters as much as the wake. Without it the very next tick
        would look at a television still reporting itself absent and put the
        box straight back to sleep, which is exactly the experience somebody
        pressing Wake is complaining about.
        """
        self.hold_awake(WAKE_HOLD_TOKEN, seconds=WAKE_HOLD_SECONDS)
        self._wake_display("somebody asked for it")

    # -- reloading ----------------------------------------------------------
    def reload_config(self) -> bool:
        """Re-read the config file and rebuild the lineup, mid-programme.

        Returns True if the new config was taken up. A config that will not
        load is logged and ignored on purpose: a box that goes dark because
        somebody typo'd a channel name is far worse than one running slightly
        stale settings, and the dashboard is the only way back in.

        Whatever is on screen keeps playing unless the channel it belongs to
        actually changed - being on a phone renaming channel 9 must not restart
        the film someone else is halfway through.

        Some of the config is welded in when the process starts and cannot be
        picked up here: the 4:3 letterboxing and which input backends exist are
        handed to mpv and to the input manager once. The dashboard knows this
        and says so rather than pretending. The CRT effect used to be on that
        list; it is not any more - see the shader block below.
        """
        if self._config_path is None:
            log.info("reload asked for, but this app was not built from a config file")
            return False

        try:
            config = load_config(self._config_path)
        except (ConfigError, OSError):
            log.warning(
                "reload: %s would not load, keeping the running config",
                self._config_path, exc_info=True,
            )
            self.overlay.show_message("CONFIG ERROR  -  KEEPING CURRENT SETUP")
            return False

        was_number = self.lineup.current.number
        was_path = self.lineup.current.config.path

        self.config = config
        self.overlay.use_config(config)
        self.lineup = build_lineup(config, wall_clock=self._wall_clock)
        self._bumper_rng = random.Random(config.shuffle_seed)
        self._bumpers = self._load_bumpers()
        self._transition_path = self._resolve_transition_asset()

        # A save ends any preview: what was just written to config.yaml is the
        # truth now, whatever a slider happened to be showing. This has to run
        # even when the crt block itself did not change - somebody renaming a
        # channel while a preview is up must not have that preview quietly
        # promoted to permanent.
        self._crt_preview_until = None
        self._crt_wanted = None
        if config.crt != self._crt_live:
            # Curvature is a taste setting with no correct value: the only way
            # anyone sets it sensibly is by dragging the dashboard slider while
            # watching the television. So it goes onto the live picture here
            # rather than waiting for a restart nobody is going to do twice.
            #
            # Compared against what the SCREEN is showing, not against the
            # config we were running: saving the value that was already being
            # previewed leaves the picture exactly right, and re-applying it
            # would make mpv recompile the same shader for no visible change.
            #
            # A save goes on immediately, throttle or no throttle. It is one
            # deliberate press of a button, not a slider firing, and it is the
            # moment the customer is told "saved" - so it has to be true by
            # the time they look up.
            try:
                if self.player.set_crt(config.crt):
                    self._crt_live = config.crt
            except Exception:  # noqa: BLE001 - the programme outranks the effect
                log.warning("could not change the CRT picture effect", exc_info=True)

        if config.audio_device and config.audio_device != self._audio_device:
            self._audio_device = config.audio_device
            try:
                self.player.set_audio_device(config.audio_device)
            except Exception:  # noqa: BLE001 - a bad device name must not kill the TV
                log.warning(
                    "could not switch audio output to %s", config.audio_device,
                    exc_info=True,
                )

        if self.lineup.has_number(was_number):
            self.lineup.select_number(was_number)
            # Same channel, different folder: what is on screen is no longer
            # what that channel is, so it has to be retuned.
            retune = self.lineup.current.config.path != was_path
        else:
            # The channel being watched was deleted. Land somewhere sensible
            # rather than pointing at nothing.
            wanted = config.start_channel
            if wanted is not None and self.lineup.has_number(wanted):
                self.lineup.select_number(wanted)
            else:
                self.lineup.select_index(0)
            retune = True

        self._daypart_marker = self._daypart_snapshot(self.lineup.current)
        if retune and not self.standby and not self._splash_active:
            self.tune_current(show_static=False)

        if self._menu is not None:
            self._menu.context = self._menu_context()
            self._draw_menu()

        # Last, so that switching going-quiet off - which puts the picture
        # back - does not fight the retune above.
        self.apply_display_sleep_setting()

        log.info(
            "config reloaded from %s: %d channels", self._config_path, len(self.lineup)
        )
        return True

    # -- channel changing ---------------------------------------------------
    def _channel_up(self) -> None:
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.up()
        self.tune_current()

    def _channel_down(self) -> None:
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.down()
        self.tune_current()

    def _jump_last_channel(self) -> None:
        if self._last_channel_number is None:
            return
        target = self._last_channel_number
        if not self.lineup.has_number(target):
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(target)
        self.tune_current()

    def select_channel_number(self, number: int) -> bool:
        """Tune directly to a channel number. Returns False if it doesn't exist."""
        if not self.lineup.has_number(number):
            self.overlay.show_message(f"CH {number:02d}  -  NO CHANNEL")
            return False
        if number == self.lineup.current.number:
            self._show_info()
            return True
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(number)
        self.tune_current()
        return True

    def tune_current(self, *, show_static: bool = True) -> None:
        """Tune into the currently selected channel."""
        channel = self.lineup.current
        self.overlay.clear_standby()
        # Changing channel dismisses the guide, so it never fights the banner.
        self.overlay.clear_guide()
        self._guide_index = None
        # Re-baseline the daypart watcher against whatever we are now tuned to.
        self._daypart_marker = self._daypart_snapshot(channel)

        request = channel.tune_in()
        self._pending_banner = None

        if request is None:
            # No episodes on this channel: show the "no signal" screen.
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._show_no_signal(channel)
            return

        if not show_static:
            # Not a channel change (first tune / waking from standby): play now.
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._play_request(request)
        elif self._transition_path is not None:
            # Transition clip (glitch/static) + preloaded episode.
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._playing_path = request.path
            self.player.play_transition(
                self._transition_path,
                request.path,
                start=request.start,
                static_seconds=self.config.transition_duration,
            )
        elif self.config.bridge_seconds > 0 and self._playing_path is not None:
            # No transition effect: keep the current show playing while the next
            # channel preloads, then cut over (no frozen frame). The banner is
            # shown at the cut-over (see _maybe_commit_switch), not right now.
            self._playing_path = request.path
            self.player.preload_next(request.path, start=request.start)
            self._switch_deadline = self._clock() + self.config.bridge_seconds
            self._pending_banner = (channel.number, channel.name)
        else:
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._play_request(request)

    def _play_request(self, request: PlayRequest) -> None:
        self._playing_path = request.path
        self.player.play(request.path, start=request.start)

    def _show_no_signal(self, channel: Channel) -> None:
        self._switch_deadline = None
        self._pending_banner = None
        self._playing_path = None
        if self._colorbars_path is not None:
            self.player.play_loop(self._colorbars_path)
        else:
            self.player.stop()
        self.overlay.show_message(
            f"CH {channel.number:02d}  {channel.name}  -  NO SIGNAL", duration=6.0
        )

    # -- volume -------------------------------------------------------------
    def _volume_up(self) -> None:
        self._set_volume(self.volume + self.config.volume_step, unmute=True)

    def _volume_down(self) -> None:
        # One press below zero cleanly powers off the box (safe to unplug).
        if self.config.power_off_on_min_volume and not self.muted and self.volume <= 0:
            self._power_off()
            return
        self._set_volume(self.volume - self.config.volume_step, unmute=True)

    def _set_volume(self, value: int, *, unmute: bool = False) -> None:
        self.volume = max(0, min(100, value))
        if unmute and self.muted:
            self.muted = False
            self.player.set_mute(False)
        self.player.set_volume(self.volume)
        self.overlay.show_volume(self.volume, self.muted)

    def _power_off(self) -> None:
        """Cleanly shut the machine down so it's safe to cut power."""
        log.info("powering off (volume floor)")
        self.powered_off = True
        self._switch_deadline = None
        self._pending_banner = None
        try:
            self.overlay.clear_all()
            self.overlay.show_message("GOODBYE", duration=0)
            self.player.stop()
        except Exception:  # noqa: BLE001
            pass
        self._run_power_off_command()
        self._running = False  # exit the main loop

    def _run_power_off_command(self) -> None:
        command = tuple(self.config.power_off_command)
        if not command:
            return  # disabled / test mode
        # The last thing between a config value and a real exec on this box.
        # config.py already refuses to hand out anything but a plain shutdown,
        # so reaching this is a bug rather than an attack - but this is the
        # line that actually launches the process, and the config behind it can
        # be replaced from a dashboard with no password, so it checks for
        # itself instead of trusting whoever built the Config.
        if command not in POWER_OFF_COMMANDS:
            log.error(
                "refusing to run a power-off command that is not a shutdown: %s",
                " ".join(command),
            )
            return
        try:
            subprocess.Popen(list(command))
        except Exception:  # noqa: BLE001
            log.exception("power-off command failed: %s", command)

    def _toggle_mute(self) -> None:
        self.muted = not self.muted
        self.player.set_mute(self.muted)
        self.overlay.show_volume(self.volume, self.muted)

    # -- info / standby -----------------------------------------------------
    def _show_info(self) -> None:
        channel = self.lineup.current
        self.overlay.show_channel_bug(channel.number, channel.name)

    def _toggle_standby(self) -> None:
        # Going to standby with the menu up would wipe it off screen (clear_all)
        # while leaving it "open" in state - close it properly first.
        self.close_menu()
        self.standby = not self.standby
        if self.standby:
            self._remember_position()
            self._switch_deadline = None
            self._pending_banner = None
            self._guide_index = None
            # clear_all() wipes the sleep readout too; forget what was drawn so
            # it is put back when the box wakes.
            self._sleep_shown = None
            self.player.stop()
            self.overlay.clear_all()
            self.overlay.show_standby()
        else:
            self.overlay.clear_standby()
            self.tune_current(show_static=False)

    # -- direct channel entry ----------------------------------------------
    def _push_digit(self, digit: int) -> None:
        self._digit_buffer = (self._digit_buffer + str(digit))[-3:]
        self._digit_deadline = self._clock() + self._digit_entry_timeout
        self.overlay.show_message(f"CH {self._digit_buffer}_", duration=self._digit_entry_timeout)

    def _confirm_digits(self) -> None:
        if not self._digit_buffer:
            return
        number = int(self._digit_buffer)
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self.select_channel_number(number)

    def _maybe_commit_digits(self, now: float) -> None:
        if self._digit_buffer and now >= self._digit_deadline:
            self._confirm_digits()

    # -- playback-finished handling ----------------------------------------
    def _maybe_timeout_splash(self, now: float) -> None:
        """Don't strand the box on a splash that never reports end-of-file."""
        if not self._splash_active or self._splash_deadline is None:
            return
        if now >= self._splash_deadline:
            log.warning("boot splash did not finish in time; starting the TV")
            self._finish_splash()

    def _drain_playback_events(self) -> None:
        advanced = False
        while True:
            try:
                reason = self._ended.get_nowait()
            except queue.Empty:
                break
            if reason not in (END_EOF, END_ERROR):
                continue
            if self._splash_active:
                # The splash played out on its own; start the TV proper.
                self._finish_splash()
                advanced = True
            elif self._display_asleep:
                # An end-of-file that was already in flight when the picture
                # was paused. Rolling the next episode now would start
                # decoding for nobody, which is the whole thing we just
                # stopped doing - so it is remembered and dealt with on the
                # way back (see _resume_after_sleep).
                self._display_ended = True
            # Coalesce: only advance once even if several events queued up.
            elif not advanced and not self.standby:
                self._advance_current()
                advanced = True

    def _advance_current(self) -> None:
        channel = self.lineup.current
        request = channel.advance()
        if request is None:
            # The channel signed off mid-episode (a daypart went off air), or
            # it simply has nothing left to play.
            self._show_no_signal(channel)
            return

        bumper = self._next_bumper()
        if bumper is None:
            self._play_request(request)
            return
        # Reuse the transition primitive: play the bumper, then roll straight
        # into the next episode with no gap (mpv prefetches the second entry).
        self._playing_path = request.path
        self.player.play_transition(
            bumper,
            request.path,
            start=request.start,
            static_seconds=self.config.bumper_max_seconds,
        )

    # -- station bumpers ----------------------------------------------------
    def _load_bumpers(self) -> Optional[ShuffleBag[Path]]:
        """Scan the bumper folder into a shuffle bag (None when not configured)."""
        folder = self.config.bumpers_dir
        if folder is None:
            return None
        clips = scan_episodes(
            folder,
            self.config.video_extensions,
            recursive=self.config.scan_recursive,
        )
        if not clips:
            log.warning("bumpers folder has no playable clips: %s", folder)
            return None
        log.info("loaded %d station bumpers from %s", len(clips), folder)
        return ShuffleBag(clips, self._bumper_rng)

    def _next_bumper(self) -> Optional[Path]:
        """The next bumper to air, honouring ``bumper_chance``."""
        if self._bumpers is None or self._bumpers.is_empty:
            return None
        # A channel may opt out entirely: station idents between items on a
        # news or music channel are wrong rather than nostalgic.
        if not self.lineup.current.config.bumpers:
            return None
        chance = self.config.bumper_chance
        if chance < 1.0 and self._bumper_rng.random() >= chance:
            return None
        return self._bumpers.next()

    # -- sleep timer --------------------------------------------------------
    def sleep_remaining(self) -> Optional[float]:
        """Seconds left on the sleep timer, or None when it isn't running."""
        if self._sleep_deadline is None:
            return None
        return max(0.0, self._sleep_deadline - self._clock())

    def _cycle_sleep(self) -> None:
        """Step through the configured sleep durations, then back to off."""
        steps = self.config.sleep_steps
        if not steps:
            self.overlay.show_message("SLEEP TIMER OFF")
            return

        self._sleep_index = 0 if self._sleep_index is None else self._sleep_index + 1
        if self._sleep_index >= len(steps):
            self._cancel_sleep()
            self.overlay.show_message("SLEEP  OFF")
            return

        minutes = steps[self._sleep_index]
        self._sleep_deadline = self._clock() + minutes * 60.0
        self.overlay.show_message(f"SLEEP  {minutes} MIN")

    def _cancel_sleep(self) -> None:
        self._sleep_index = None
        self._sleep_deadline = None

    def _maybe_fire_sleep(self, now: float) -> None:
        """Turn the box off once the sleep timer runs out."""
        if self._sleep_deadline is None or now < self._sleep_deadline:
            return
        self._cancel_sleep()
        log.info("sleep timer expired -> %s", self.config.sleep_action)
        if self.config.sleep_action == "off":
            self._power_off()
        elif not self.standby:
            self._toggle_standby()

    def _update_sleep_indicator(self) -> None:
        """Keep the corner 'SLEEP 29m' readout in step with the timer."""
        if self.standby:
            return
        remaining = self.sleep_remaining()
        if remaining is None:
            if self._sleep_shown is not None:
                self.overlay.clear_sleep()
                self._sleep_shown = None
            return
        # Round up, so a timer set to 30 reads "30m" rather than "29m".
        minutes = int(remaining // 60) + (1 if remaining % 60 else 0)
        if minutes != self._sleep_shown:
            self._sleep_shown = minutes
            self.overlay.show_sleep(minutes)

    # -- dayparting ---------------------------------------------------------
    def _daypart_snapshot(self, channel: Channel) -> tuple:
        """What the current channel looks like right now, for change detection."""
        now = self._wall_clock()
        return (
            channel.number,
            channel.pool_key(now),
            channel.is_off_air(now),
            channel.name_at(now),
        )

    def _maybe_retune_daypart(self) -> None:
        """React to a daypart window opening or closing under the current show."""
        # Not while the menu is up: retuning would resume playback behind it.
        # The marker is left untouched, so the change is picked up on close.
        # Nor while the box has gone quiet - a daypart opening at ten o'clock
        # in an empty room must not start the box decoding for nobody. Same
        # reasoning, same fix: the marker is left alone and the change is
        # picked up the moment somebody is watching again.
        if (self.standby or self._display_asleep
                or self._menu is not None or self._switch_deadline is not None):
            return
        channel = self.lineup.current
        if not channel.config.dayparts:
            return

        marker = self._daypart_snapshot(channel)
        previous = self._daypart_marker
        if marker == previous:
            return
        self._daypart_marker = marker
        if previous is None:
            return

        _, pool, off_air, name = marker
        _, prev_pool, prev_off_air, _prev_name = previous
        if off_air != prev_off_air or pool != prev_pool:
            # The programming genuinely changed (or the station signed off), so
            # cut to it now instead of waiting for the episode to finish.
            log.info("CH %s crossed a daypart boundary -> %s", channel.number, name)
            self.tune_current(show_static=False)
        else:
            # Only the station ident changed - keep playing, reflash the bug.
            self.overlay.show_channel_bug(channel.number, name)

    # -- what the player is actually doing ----------------------------------
    def _audio_report(self) -> dict:
        """The output mpv opened, not the one something hoped it opened."""
        try:
            live = self.player.get_audio_status() or {}
        except Exception:  # noqa: BLE001 - a status snapshot is not worth one
            log.debug("the player would not report its audio", exc_info=True)
            live = {}
        setup = self._audio_setup
        return {
            "device": live.get("device") or self._audio_device,
            "ao": live.get("ao"),
            # True/False once something with a soundtrack is playing, None
            # when nothing is - which is an answer, not a failure.
            "working": live.get("active"),
            "channels": live.get("channels") or (setup.layout if setup else None),
            "has_track": live.get("track"),
            "source": setup.source if setup else None,
            "summary": setup.summary if setup else None,
            "monitor": setup.monitor_name if setup else None,
        }

    def _decode_report(self) -> dict:
        """Which decoder is in use, and whether that is even knowable yet.

        An idle television has not decided anything. Reporting "software
        decode" for it - which is what a probe does - is how the System page
        came to contradict the Watch tab on the same box at the same moment.
        """
        try:
            hwdec = self.player.get_hwdec()
        except Exception:  # noqa: BLE001
            hwdec = None
        playing = bool(self._playing_path)
        return {
            "hwdec": hwdec,
            "playing": playing,
            "working": bool(hwdec) if playing else None,
        }

    # -- status snapshot ----------------------------------------------------
    def build_status(self) -> dict:
        """A cheap snapshot of what the box is doing, for the dashboard."""
        channel = self.lineup.current
        now = self._wall_clock()
        remaining = self.sleep_remaining()
        return {
            "version": _version(),
            "uptime_seconds": round(self._clock() - self._started_at, 1),
            "channel": {"number": channel.number, "name": channel.name_at(now)},
            "now_playing": (
                _episode_title(self._playing_path) if self._playing_path else ""
            ),
            "off_air": channel.is_off_air(now),
            "volume": self.volume,
            "muted": self.muted,
            "standby": self.standby,
            "menu_open": self._menu is not None,
            "audio_device": self._audio_device,
            "hwdec": self.player.get_hwdec(),
            # What the player is ACTUALLY doing, as opposed to what an
            # external probe infers. The player made both choices, so no
            # probe run from another process can be more correct than this.
            "audio": self._audio_report(),
            "decode": self._decode_report(),
            "display": self._display_status(),
            "sleep_minutes": (
                None if remaining is None
                else int(remaining // 60) + (1 if remaining % 60 else 0)
            ),
            "channel_count": len(self.lineup),
            # For the now-playing page. Position comes free from the player;
            # the duration is only reported when it is already in the probe
            # cache, because this runs every couple of seconds and forking
            # ffprobe on a timer would be a real cost on a small box.
            "position": self.player.get_time_pos(),
            "duration": self._known_duration(),
            "lineup": self._lineup_snapshot(now),
            # For the dashboard's input test: which sources are live, and the
            # last few presses with the action each one mapped to.
            "input": {
                "backends": self.input.backend_names(),
                "recent": self.input.recent(),
            },
        }

    def _display_status(self) -> dict:
        """Whether the box is playing or has gone quiet, and why.

        Everything the dashboard needs to say so without inventing wording,
        including the finished sentence itself (see
        :func:`retrobox.status.display_summary`). A box that has gone quiet is
        WORKING: nothing in here may be rendered as a fault.
        """
        settings = self.config.display_sleep
        watcher = self._display_watcher
        power = self._cec_power()
        block = None
        if watcher is not None:
            try:
                block = watcher.snapshot().as_dict()
            except Exception:  # noqa: BLE001 - the dashboard never outranks the video
                # A watcher that cannot describe itself must not take the
                # television down with it: this runs inside step().
                log.debug("the display watcher could not describe itself", exc_info=True)
        if block is None:
            # Not watching, or not able to say: either switched off, detection
            # that could not be made to work here, or a watcher that blew up.
            # All of them mean the box simply stays awake.
            block = {
                "state": display_mod.UNKNOWN,
                "awake": True,
                "description": display_mod.describe(display_mod.UNKNOWN),
                "caveat": display_mod.CAVEAT,
                "mode": display_mod.MODE_STOPPED,
                "mode_detail": "the display is not being watched",
                "detail": "",
                "connectors": [],
                "holds": [],
                "changed_at": 0.0,
            }
        # Both books, exactly as the arbitration reads them - so what the
        # dashboard shows is what the box is actually acting on, including the
        # holds that survived a watcher being rebuilt or that outlived one.
        mine = dict(self._live_hold_deadlines(self._clock()))
        block["holds"] = sorted(set(block["holds"]) | set(mine))
        block.update({
            # Whether this box is watching at all, which is the setting AND
            # detection having worked. `configured` is what config.yaml says,
            # so a dashboard can tell "switched off" from "could not".
            "enabled": watcher is not None,
            "configured": settings.enabled,
            "sleeping": self._display_asleep,
            "asleep_seconds": (
                round(max(0.0, self._wall_clock() - self._display_since), 1)
                if self._display_since is not None else 0.0
            ),
            # WHETHER TO OFFER THE WAKE BUTTON, and it must never be tied to
            # the watcher alone. A paused picture is always wakeable - waking
            # is un-pausing, which needs no watcher at all - and the moment
            # the button matters most is exactly the moment detection has
            # fallen over. Hiding the fix precisely when the fix is needed
            # leaves a physical remote as the only way back into a box nobody
            # can log into.
            "can_wake": watcher is not None or self._display_asleep,
            "sleep_after_seconds": settings.sleep_after_seconds,
            "non_broadcast": settings.non_broadcast,
            "cec": {
                "state": power.state,
                "detail": power.detail,
                "says_off": power.says_off,
            },
        })
        block["summary"] = display_summary(
            sleeping=self._display_asleep,
            enabled=watcher is not None,
            state=str(block["state"]),
            cec=power.state,
            holds=list(block["holds"]),
        )
        return block

    def _known_duration(self) -> Optional[float]:
        if self._playing_path is None:
            return None
        from .probe import cached_media

        info = cached_media(self._playing_path)
        return info.duration if info is not None else None

    def _lineup_snapshot(self, now: float) -> List[dict]:
        """What every channel is showing, for the viewer page.

        ``peek_now`` is deliberately cheap: it answers only for channels whose
        schedule already exists (broadcast channels somebody has tuned to), and
        returns nothing rather than building one. A guide that ffprobed every
        file on every channel to draw itself would stall the television.
        """
        rows = []
        for channel in self.lineup:
            playing = channel.peek_now(now)
            rows.append({
                "number": channel.number,
                "name": channel.name_at(now),
                "off_air": channel.is_off_air(now),
                "now_playing": _episode_title(playing) if playing else "",
                "current": channel.number == self.lineup.current.number,
            })
        return rows

    def _maybe_write_status(self, now: float) -> None:
        if now < self._status_due:
            return
        self._status_due = now + _STATUS_INTERVAL_SECONDS
        write_status(self.build_status())

    # -- on-screen menu -----------------------------------------------------
    def _menu_context(self) -> MenuContext:
        """Snapshot the state the menu renders from."""
        from . import __version__

        try:
            from .hwdetect import detect_audio

            devices = detect_audio()
        except Exception:  # noqa: BLE001 - detection must never break the menu
            log.debug("audio detection failed for the menu", exc_info=True)
            devices = []

        now = self._wall_clock()
        return MenuContext(
            channels=[(c.number, c.name_at(now)) for c in self.lineup],
            current_channel=self.lineup.current.number,
            volume=self.volume,
            muted=self.muted,
            audio_devices=devices,
            current_audio=self._audio_device,
            version=__version__,
        )

    def open_menu(self) -> None:
        """Pause playback and put the menu up."""
        if self._menu is not None:
            return
        self._menu = MenuModel(self._menu_context())
        self.player.set_paused(True)
        self.player.set_mouse_enabled(True)
        self._draw_menu()

    def close_menu(self) -> None:
        """Take the menu down and resume exactly where playback left off."""
        if self._menu is None:
            return
        self._menu = None
        self.overlay.clear_menu()
        self.player.set_mouse_enabled(False)
        self.player.set_paused(False)

    def _toggle_menu(self) -> None:
        if self._menu is None:
            self.open_menu()
        else:
            self.close_menu()

    def _draw_menu(self) -> None:
        if self._menu is None:
            return
        self.overlay.show_menu(self._menu.title, self._menu.rows(), self._menu.index)

    def _handle_menu_key(self, action: Action) -> None:
        menu = self._menu
        if menu is None:
            return
        if action == Action.CHANNEL_UP:
            menu.move(-1)            # up the list, i.e. towards the top row
        elif action == Action.CHANNEL_DOWN:
            menu.move(1)
        elif action in (Action.VOLUME_UP, Action.VOLUME_DOWN):
            # Volume works from anywhere in the menu, not just its own row -
            # it is a TV, and the volume buttons should never feel dead. The
            # deliberate difference from normal playback is that holding
            # volume-down here cannot trip the power-off-at-zero gesture.
            step = 1 if action == Action.VOLUME_UP else -1
            self._set_volume(self.volume + self.config.volume_step * step, unmute=True)
            menu.context = self._menu_context()
        elif action == Action.LAST_CHANNEL:
            if not menu.back():
                self.close_menu()
                return
        elif action == Action.ENTER:
            self._run_menu_command(menu.activate())
            if self._menu is None:
                return
        self._draw_menu()

    def _run_menu_command(self, command) -> None:
        if command.kind == "tune":
            self.close_menu()
            self.select_channel_number(int(command.value))
        elif command.kind == "audio":
            device = str(command.value)
            self._audio_device = device
            self.player.set_audio_device(device)
            log.info("audio output switched to %s (this session)", device)
            if self._menu is not None:
                self._menu.context = self._menu_context()
                self._menu.back()
        elif command.kind == "shutdown":
            self.close_menu()
            self._power_off()

    # -- pointer ------------------------------------------------------------
    def _track_pointer(self) -> None:
        """Move the highlight to whatever row the pointer is hovering over."""
        if self._menu is None:
            return
        position = self.player.get_mouse_position()
        if position is None:
            return
        row = self._row_under(position)
        if row is not None and row != self._menu.index:
            if self._menu.set_index(row):
                self._draw_menu()

    def _drain_clicks(self) -> None:
        while True:
            try:
                position = self._clicks.get_nowait()
            except queue.Empty:
                break
            if self._menu is None:
                continue
            row = self._row_under(position)
            if row is None or not self._menu.set_index(row):
                continue
            self._run_menu_command(self._menu.activate())
            if self._menu is not None:
                self._draw_menu()

    def _row_under(self, position) -> Optional[int]:
        """Map a normalised pointer position onto a menu row, or None."""
        if self._menu is None:
            return None
        x, y = position
        return menu_row_at(
            self._menu.rows(), self._menu.index, x * CANVAS_W, y * CANVAS_H
        )

    # -- channel guide ------------------------------------------------------
    def _toggle_guide(self) -> None:
        if self.overlay.guide_visible:
            self.overlay.clear_guide()
            self._guide_index = None
            return
        self._guide_index = self.lineup.index_of(self.lineup.current.number)
        self._draw_guide()

    def _handle_guide_key(self, action: Action) -> None:
        """Channel up/down move the highlight; OK tunes to it."""
        if action == Action.ENTER:
            self._guide_select()
        else:
            self._guide_move(1 if action == Action.CHANNEL_UP else -1)

    def _guide_move(self, delta: int) -> None:
        if self._guide_index is None:
            self._guide_index = self.lineup.index_of(self.lineup.current.number) or 0
        self._guide_index = (self._guide_index + delta) % len(self.lineup)
        self._draw_guide()  # also re-arms the guide's expiry while you browse

    def _guide_select(self) -> None:
        if self._guide_index is None:
            return
        number = self.lineup.numbers[self._guide_index]
        self.overlay.clear_guide()
        self._guide_index = None
        self.select_channel_number(number)

    def _draw_guide(self) -> None:
        selected = (
            self.lineup.numbers[self._guide_index]
            if self._guide_index is not None
            else None
        )
        self.overlay.show_guide(
            self.build_guide(),
            current_number=self.lineup.current.number,
            selected_number=selected,
            header=self._guide_header(),
        )

    def build_guide(self) -> List[GuideEntry]:
        """Snapshot the whole lineup for the on-screen guide."""
        now = self._wall_clock()
        return [
            GuideEntry(
                number=channel.number,
                name=channel.name_at(now),
                now_playing=self._now_playing(channel, now),
                off_air=channel.is_off_air(now),
            )
            for channel in self.lineup
        ]

    def _now_playing(self, channel: Channel, now: float) -> str:
        """What to print in the guide's right-hand column for one channel."""
        if channel.is_off_air(now):
            return ""
        if channel.number == self.lineup.current.number and self._playing_path is not None:
            return _episode_title(self._playing_path)
        # Only ask channels that already built a broadcast schedule - working it
        # out from scratch would ffprobe every file on every guide press.
        airing = channel.peek_now(now)
        if airing is not None:
            return _episode_title(airing)
        count = len(channel.episodes_at(now))
        return f"{count} episodes" if count else "NO SIGNAL"

    def _guide_header(self) -> str:
        clock_text = time.strftime("%H:%M", time.localtime(self._wall_clock()))
        remaining = self.sleep_remaining()
        if remaining is None:
            return f"GUIDE    {clock_text}"
        minutes = int(remaining // 60) + (1 if remaining % 60 else 0)
        return f"GUIDE    {clock_text}    SLEEP {minutes}m"

    # -- helpers ------------------------------------------------------------
    def _remember_position(self) -> None:
        if self.config.tune_in != "resume" or self._playing_path is None:
            return
        pos = self.player.get_time_pos()
        if pos is not None:
            self.lineup.current.remember(self._playing_path, pos)

    def _select_start_channel(self) -> None:
        if self.config.start_channel is not None and self.lineup.has_number(
            self.config.start_channel
        ):
            self.lineup.select_number(self.config.start_channel)

    def _resolve_asset(self, filename: str) -> Optional[Path]:
        path = self._assets_dir / filename
        return path if path.is_file() else None

    def _resolve_transition_asset(self) -> Optional[Path]:
        effect = self.config.transition_effect
        if effect == "none":
            return None
        filename = GLITCH_FILENAME if effect == "glitch" else STATIC_FILENAME
        return self._resolve_asset(filename)


def _version() -> str:
    from . import __version__

    return __version__


def _episode_title(path: Path) -> str:
    """Turn ``the.late.show_s02e04.mkv`` into something fit for the guide."""
    return " ".join(path.stem.replace("_", " ").replace(".", " ").split())


def run_from_config(
    config: Config, *, dry_run: bool = False, config_path: Optional[Path] = None
) -> int:
    """Convenience entry point used by the CLI. Returns the process exit code."""
    app = TVApp.from_config(config, dry_run=dry_run, config_path=config_path)
    app.run()
    # Tell systemd the difference between "crashed, restart me" and "the viewer
    # asked me to shut the machine down".
    return EXIT_POWERED_OFF if app.powered_off else 0


__all__ = ["TVApp", "run_from_config", "EXIT_POWERED_OFF"]
