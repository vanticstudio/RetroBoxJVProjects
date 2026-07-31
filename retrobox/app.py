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
from pathlib import Path
from typing import Callable, List, Optional

from .actions import Action, InputEvent
from .channel import Channel, ChannelLineup, PlayRequest, build_lineup, scan_episodes
from .config import Config
from .input.manager import InputManager, create_backends
from .menu import MenuContext, MenuModel
from .overlay import CANVAS_H, CANVAS_W, GuideEntry, OverlayManager, menu_row_at
from .player import END_EOF, END_ERROR, MockPlayer, Player
from .status import write_status
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


class TVApp:
    """The retro-TV application state machine."""

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
    ) -> None:
        self.config = config
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

        # Filler assets.
        self._assets_dir = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
        self._colorbars_path = self._resolve_asset(COLORBARS_FILENAME)
        # The channel-change transition clip depends on the configured effect.
        self._transition_path = self._resolve_transition_asset()

        # Boot splash: played once before the first channel is tuned.
        self._splash_path = self._resolve_splash()
        self._splash_active = False
        self._splash_deadline: Optional[float] = None

        # On-screen menu. `_menu` is None whenever the menu is closed.
        self._menu: Optional[MenuModel] = None
        self._audio_device = config.audio_device
        # Clicks arrive on mpv's thread, so they are queued like end-of-file.
        self._clicks: "queue.Queue[tuple]" = queue.Queue()
        self.player.on_click = self._clicks.put

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

        return cls(config, player, input_manager, assets_dir=assets_dir)

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
        self._update_sleep_indicator()
        self._maybe_retune_daypart()
        self._drain_clicks()
        self._track_pointer()
        self._maybe_write_status(now)
        self._drain_playback_events()

        event = self.input.get(timeout=timeout if block else 0.0)
        if event is not None:
            self.handle_event(event)

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
        command = list(self.config.power_off_command)
        if not command:
            return  # disabled / test mode
        try:
            subprocess.Popen(command)
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
        if self.standby or self._menu is not None or self._switch_deadline is not None:
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
            "sleep_minutes": (
                None if remaining is None
                else int(remaining // 60) + (1 if remaining % 60 else 0)
            ),
            "channel_count": len(self.lineup),
        }

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


def run_from_config(config: Config, *, dry_run: bool = False) -> int:
    """Convenience entry point used by the CLI. Returns the process exit code."""
    app = TVApp.from_config(config, dry_run=dry_run)
    app.run()
    # Tell systemd the difference between "crashed, restart me" and "the viewer
    # asked me to shut the machine down".
    return EXIT_POWERED_OFF if app.powered_off else 0


__all__ = ["TVApp", "run_from_config", "EXIT_POWERED_OFF"]
