"""
Turns Sendspin visualizer features into light targets.

Mirrors the shape of Music Assistant's built-in HueAudioAnalyzer
(music_assistant/providers/hue_entertainment/analyzer.py): accumulate the
periodic spectrum/loudness/peak frames and the scheduled beat timestamps
Sendspin's visualizer role delivers, then on each render tick turn the
current state into a light command.

The two real differences from the Hue version:

1. Output is a single flat LightCommand (hue/saturation/brightness/transition_ms)
   rather than a per-channel list - a Home Assistant light group is one or two
   color zones, not a multi-point Entertainment Area.
2. render() is called at RENDER_RATE_HZ (see const.py), not Hue's 30Hz, and
   leans on light.turn_on's own `transition` parameter to interpolate rather
   than repainting every frame - see bridge.py for why.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .const import (
    AUTO_MIN_DWELL_S,
    AUTO_MODE_CANDIDATES,
    BEAT_MULTIPLIERS,
    COLOR_MODES,
    DEFAULT_BEAT_MULTIPLIER,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
    DEFAULT_HUE_LOCK_DEG,
    DEFAULT_HUE_LOCK_ENABLED,
    DEFAULT_SENSITIVITY,
    DEFAULT_TRANSITION_STYLE,
    RENDER_PERIOD_S,
    TRANSITION_STYLES,
)

# Beat flashes normally decay over this long. Tightened automatically for
# closely-spaced beats (see push_beats) so a high speed multiplier produces
# distinct fast pulses instead of one flash blurring into the next.
_BEAT_FLASH_DECAY_S = 0.25
# A flash is never allowed to decay faster than this - below it a strobe just
# looks like flicker rather than a pulse.
_MIN_FLASH_DECAY_S = 0.05
# A beat landing within this window of "now" still counts as "just happened" -
# render() is not guaranteed to be called at the exact beat timestamp.
_BEAT_FIRE_WINDOW_S = 0.08
# Beats older than this are forgotten rather than fired late.
_BEAT_STALE_S = 1.5
# Synthetic sub-beats (speed > 1x) pulse softer than the real beat they're
# interpolated between, so the actual beat still reads as the strongest hit.
_SUBBEAT_STRENGTH = 0.55


@dataclass(frozen=True)
class LightCommand:
    """One target state for a light, handed to the bridge to send via light.turn_on/turn_off."""

    hue: int  # degrees, 0-360
    saturation: int  # percent, 0-100
    brightness: int  # percent, 0-100
    transition_ms: int
    # False only for "strobe" mode's off phase, where the bridge calls
    # light.turn_off instead of light.turn_on - every other mode always
    # dims towards a floor rather than truly switching off, so this is
    # always True for them. See _ModePreset.hard_strobe.
    on: bool = True


@dataclass(frozen=True)
class _ModePreset:
    """Tuning knobs for one COLOR_MODES entry."""

    # Resting brightness as a fraction of the configured ceiling (0-1).
    floor: float
    # How much a beat's flash adds on top of the resting brightness (0-1+).
    flash_strength: float
    # Hue degrees/second the base color drifts at, before any beat offset.
    hue_drift_deg_s: float
    # Extra hue jump applied on each beat (degrees); 0 disables beat-driven color.
    hue_beat_jump_deg: float
    # Saturation floor (0-1); bass energy pushes it up to 1.0 from here.
    saturation_floor: float
    # How much the continuous bass-energy level lifts brightness ABOVE floor,
    # separate from the on-beat flash (0-1). A sustained loud passage keeps
    # resting brightness elevated at 1.0 (smooth/ambient's "swell" character,
    # the original behavior), which also shrinks the contrast a beat flash
    # reads with. Lower values hold resting brightness close to floor
    # regardless of how loud the track currently is, so the flash itself -
    # not the ongoing bass level - is what makes the light move. flashing
    # uses this for genuine strobe-like contrast rather than "bright with a
    # bump on top".
    bass_weight: float = 1.0
    # When True, the continuous brightness driver above is the full-spectrum
    # overall loudness level (self._overall, all bins) instead of the
    # bass-only band (self._bass, bottom third of bins) - i.e. "track the
    # music's general volume", not "track the bass specifically". Only
    # "pulse" uses this; every other preset keeps the original bass-driven
    # behavior unchanged.
    use_overall_level: bool = False
    # When True, render() takes a completely different path for this preset:
    # a hard binary on/off toggle (real light.turn_off between flashes, full
    # brightness during them) instead of dimming towards a floor. Every
    # field above except saturation_floor/hue_drift_deg_s/hue_beat_jump_deg
    # is ignored for a hard_strobe preset - see render()'s branch. Only
    # "strobe" sets this.
    hard_strobe: bool = False


_PRESETS: dict[str, _ModePreset] = {
    "smooth": _ModePreset(
        floor=0.55, flash_strength=0.35, hue_drift_deg_s=6.0, hue_beat_jump_deg=15.0,
        saturation_floor=0.7, bass_weight=1.0,
    ),
    "ambient": _ModePreset(
        floor=0.45, flash_strength=0.25, hue_drift_deg_s=3.0, hue_beat_jump_deg=40.0,
        saturation_floor=0.4, bass_weight=1.0,
    ),
    "flashing": _ModePreset(
        floor=0.05, flash_strength=1.0, hue_drift_deg_s=0.0, hue_beat_jump_deg=0.0,
        saturation_floor=1.0, bass_weight=0.15,
    ),
    "energetic": _ModePreset(
        floor=0.15, flash_strength=0.85, hue_drift_deg_s=25.0, hue_beat_jump_deg=90.0,
        saturation_floor=0.9, bass_weight=0.5,
    ),
    # Brightness-first: no beat flash and no beat-driven hue jump at all
    # (flash_strength=0, hue_beat_jump_deg=0) so the ONLY thing that moves
    # brightness is the continuous overall-loudness level, at full weight
    # (bass_weight=1.0, use_overall_level=True) with a low floor so the
    # swing from quiet to loud passages is as visible as possible. A slow
    # hue drift keeps it from looking static without competing with the
    # brightness-tracks-music effect that's the whole point of this mode.
    "pulse": _ModePreset(
        floor=0.08, flash_strength=0.0, hue_drift_deg_s=8.0, hue_beat_jump_deg=0.0,
        saturation_floor=0.75, bass_weight=1.0, use_overall_level=True,
    ),
    # Hard on/off strobe: the light is fully OFF (a real light.turn_off, not
    # a dim floor) between beats, and snaps to full brightness for each
    # beat's flash-decay window - see render()'s hard_strobe branch, which
    # ignores floor/flash_strength/bass_weight/use_overall_level entirely
    # (kept at inert defaults here only because the dataclass requires
    # values). saturation_floor=1.0 keeps color fully saturated during each
    # flash; a modest hue_beat_jump_deg gives successive flashes some color
    # variety instead of being the exact same color every time.
    "strobe": _ModePreset(
        floor=0.0, flash_strength=1.0, hue_drift_deg_s=0.0, hue_beat_jump_deg=50.0,
        saturation_floor=1.0, bass_weight=0.0, hard_strobe=True,
    ),
}


class _ExpFilter:
    """Asymmetric exponential smoother: rises fast, decays slow (or vice versa)."""

    def __init__(self, alpha_rise: float, alpha_decay: float, initial: float = 0.0) -> None:
        self._alpha_rise = alpha_rise
        self._alpha_decay = alpha_decay
        self.value = initial

    def update(self, new_value: float) -> float:
        alpha = self._alpha_rise if new_value > self.value else self._alpha_decay
        self.value += alpha * (new_value - self.value)
        return self.value


@dataclass
class _ScheduledBeat:
    timestamp_s: float
    is_downbeat: bool
    # 1.0 for a real tracked beat; lower for a synthesized sub-beat (speed > 1x).
    strength: float = 1.0
    # How fast THIS flash should decay - tightened for closely-spaced beats so
    # a high speed multiplier reads as distinct pulses, not a blur. None means
    # "use the default" (set once the full expanded schedule is known).
    decay_s: float | None = None


class HALightsAudioAnalyzer:
    """
    Accumulates visualizer features for one Sendspin client and renders light targets.

    One instance per configured light group (see provider.py) - matches a
    HueAudioAnalyzer per Entertainment Area in the upstream plugin.
    """

    def __init__(
        self,
        color_mode: str = DEFAULT_COLOR_MODE,
        brightness: int = DEFAULT_BRIGHTNESS,
        beat_multiplier: int = DEFAULT_BEAT_MULTIPLIER,
        transition_style: str = DEFAULT_TRANSITION_STYLE,
        sensitivity: int = DEFAULT_SENSITIVITY,
        hue_lock_enabled: bool = DEFAULT_HUE_LOCK_ENABLED,
        hue_lock_deg: int = DEFAULT_HUE_LOCK_DEG,
    ) -> None:
        self.color_mode = color_mode if color_mode in COLOR_MODES else DEFAULT_COLOR_MODE
        self.brightness_ceiling = brightness
        self.beat_multiplier = beat_multiplier if beat_multiplier in BEAT_MULTIPLIERS else DEFAULT_BEAT_MULTIPLIER
        self.transition_style = (
            transition_style if transition_style in TRANSITION_STYLES else DEFAULT_TRANSITION_STYLE
        )
        # Stored as a plain multiplier (sensitivity is a percent at the
        # config/analyzer boundary - see const.py's DEFAULT_SENSITIVITY).
        self._sensitivity = max(0.0, sensitivity / 100.0)
        self.hue_lock_enabled = hue_lock_enabled
        self.hue_lock_deg = hue_lock_deg
        self._bass = _ExpFilter(alpha_rise=0.6, alpha_decay=0.08, initial=0.0)
        self._treble = _ExpFilter(alpha_rise=0.5, alpha_decay=0.1, initial=0.0)
        self._overall = _ExpFilter(alpha_rise=0.6, alpha_decay=0.08, initial=0.0)
        self._beats: list[_ScheduledBeat] = []
        self._last_beat_flash_s: float = -10.0
        self._last_flash_strength: float = 1.0
        self._last_flash_decay_s: float = _BEAT_FLASH_DECAY_S
        self._hue_base: float = 0.0
        self._last_render_s: float = time.monotonic()
        # "auto" mode's current pick and when it last changed - see
        # _select_auto_preset. _auto_mode_changed_s lives in render()'s
        # now_s clock domain (not time.monotonic()) like every other timing
        # field on this class. None means "never classified yet" - the
        # very first call always takes its classification immediately
        # (nothing to debounce against yet) and uses that call's own now_s
        # as the dwell baseline, rather than using a fixed sentinel time
        # like -1e9, which would make the FIRST switch look like it's long
        # overdue and bypass the dwell check it's supposed to enforce.
        self._auto_mode: str = AUTO_MODE_CANDIDATES[0][0]
        self._auto_mode_changed_s: float | None = None

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        beat_multiplier: int | None = None,
        transition_style: str | None = None,
        sensitivity: int | None = None,
        hue_lock_enabled: bool | None = None,
        hue_lock_deg: int | None = None,
    ) -> None:
        if color_mode is not None and color_mode in COLOR_MODES:
            self.color_mode = color_mode
        if brightness is not None:
            self.brightness_ceiling = brightness
        if beat_multiplier is not None and beat_multiplier in BEAT_MULTIPLIERS:
            self.beat_multiplier = beat_multiplier
        if transition_style is not None and transition_style in TRANSITION_STYLES:
            self.transition_style = transition_style
        if sensitivity is not None:
            self._sensitivity = max(0.0, sensitivity / 100.0)
        if hue_lock_enabled is not None:
            self.hue_lock_enabled = hue_lock_enabled
        if hue_lock_deg is not None:
            self.hue_lock_deg = hue_lock_deg

    def apply_spectrum(self, bins: list[float]) -> None:
        """
        Fold a binned spectrum frame into smoothed bass/treble/overall energy.

        :param bins: Magnitude per mel bin, low frequency first (see
            SPECTRUM_BINS/SPECTRUM_SCALE in const.py for the request shape).
        """
        if not bins:
            return
        split = max(1, len(bins) // 3)
        bass_raw = sum(bins[:split]) / split
        treble_raw = sum(bins[-split:]) / split
        overall_raw = sum(bins) / len(bins)
        # Sensitivity amplifies the raw magnitude BEFORE clamping to 0-1, so
        # quiet playback (every bin sitting well under 1.0) can still drive
        # the smoothed levels up near their ceiling instead of staying
        # perpetually muted - see const.py's DEFAULT_SENSITIVITY docstring.
        self._bass.update(_clamp01(bass_raw * self._sensitivity))
        self._treble.update(_clamp01(treble_raw * self._sensitivity))
        self._overall.update(_clamp01(overall_raw * self._sensitivity))

    def push_beats(self, beats_s: list[tuple[float, bool]]) -> None:
        """
        Add scheduled beats, expanding each gap into sub-beats at the current speed.

        At 1x this is just the real tracked beats. At 2x/4x, evenly-spaced
        synthetic sub-beats are inserted between each consecutive pair in this
        batch (weaker flash than a real beat - see _SUBBEAT_STRENGTH), and
        every resulting flash's decay time is tightened to a fraction of its
        local gap so fast pulses stay crisp instead of smearing together.

        :param beats_s: (timestamp, is_downbeat) pairs, as delivered by Sendspin's
            visualizer role (see bridge.py's ``_on_beats``). Seconds share render()'s clock.
        """
        real = sorted(
            (_ScheduledBeat(ts, down) for ts, down in beats_s), key=lambda b: b.timestamp_s
        )
        if not real:
            return
        expanded: list[_ScheduledBeat] = []
        multiplier = self.beat_multiplier
        for current, nxt in zip(real, real[1:] + [None]):
            expanded.append(current)
            if multiplier > 1 and nxt is not None:
                gap = nxt.timestamp_s - current.timestamp_s
                step = gap / multiplier
                for k in range(1, multiplier):
                    expanded.append(
                        _ScheduledBeat(
                            current.timestamp_s + step * k, is_downbeat=False, strength=_SUBBEAT_STRENGTH
                        )
                    )
        expanded.sort(key=lambda b: b.timestamp_s)
        for i, beat in enumerate(expanded):
            local_gap = (
                expanded[i + 1].timestamp_s - beat.timestamp_s
                if i + 1 < len(expanded)
                else _BEAT_FLASH_DECAY_S / 0.8
            )
            beat.decay_s = max(_MIN_FLASH_DECAY_S, min(_BEAT_FLASH_DECAY_S, local_gap * 0.8))
        self._beats.extend(expanded)
        self._beats.sort(key=lambda b: b.timestamp_s)

    def clear_beats(self) -> None:
        self._beats.clear()

    def render(self, now_s: float) -> LightCommand:
        """Compute the light command for this instant; called at RENDER_RATE_HZ."""
        preset = (
            _PRESETS[self._select_auto_preset(now_s)]
            if self.color_mode == "auto"
            else _PRESETS[self.color_mode]
        )
        dt = max(0.0, now_s - self._last_render_s)
        self._last_render_s = now_s

        fired = self._consume_due_beat(now_s)
        if fired is not None:
            self._last_beat_flash_s = now_s
            self._last_flash_strength = fired.strength
            self._last_flash_decay_s = fired.decay_s or _BEAT_FLASH_DECAY_S
            jump = preset.hue_beat_jump_deg * (1.6 if fired.is_downbeat else 1.0)
            self._hue_base = (self._hue_base + jump) % 360

        self._hue_base = (self._hue_base + preset.hue_drift_deg_s * dt) % 360
        flash_age = now_s - self._last_beat_flash_s

        saturation = round(
            100 * _clamp01(preset.saturation_floor + (1 - preset.saturation_floor) * self._treble.value)
        )

        if preset.hard_strobe:
            # Hard binary toggle: fully on for the flash-decay window after
            # each beat, fully off (a real light.turn_off, see bridge.py)
            # the rest of the time. None of floor/flash_strength/bass_weight/
            # use_overall_level apply here - see _ModePreset.hard_strobe.
            on = flash_age < self._last_flash_decay_s
            brightness_pct = self.brightness_ceiling if on else 1
            hue = round(self._hue_base % 360)
            # Always a hard cut, regardless of transition_style - an eased
            # strobe isn't a strobe.
            transition_ms = 0
        else:
            flash = (
                preset.flash_strength
                * self._last_flash_strength
                * max(0.0, 1.0 - flash_age / self._last_flash_decay_s)
            )
            level_source = self._overall.value if preset.use_overall_level else self._bass.value
            level = preset.floor + (1.0 - preset.floor) * level_source * preset.bass_weight + flash
            # brightness_ceiling is 0-100 (the configured cap); level is the
            # 0-1+ fraction of it this instant renders at, clamped before
            # scaling.
            brightness_pct = max(1, min(100, round(self.brightness_ceiling * _clamp01(level))))
            hue_shift = self._treble.value * 20.0  # treble brightens/cools the hue slightly
            hue = round((self._hue_base + hue_shift) % 360)
            on = True
            # "instant" sends transition=0 (a hard, un-eased cut on every
            # render - a punchier "solid change" on the beat); "fade" spans
            # the render period so continuous drift still looks smooth.
            transition_ms = 0 if self.transition_style == "instant" else round(RENDER_PERIOD_S * 1000)

        if self.hue_lock_enabled:
            # Overrides drift/beat-jump/treble-shift entirely - the whole
            # point is "stay this color no matter what", not "stay close to
            # this color".
            hue = self.hue_lock_deg

        return LightCommand(
            hue=hue,
            saturation=saturation,
            brightness=brightness_pct,
            transition_ms=transition_ms,
            on=on,
        )

    def _select_auto_preset(self, now_s: float) -> str:
        """
        Pick which non-auto preset "auto" mode should currently render as.

        Classifies the track's current overall-loudness level against
        AUTO_MODE_CANDIDATES (low to high energy), but only actually
        switches the active pick if it's been at least AUTO_MIN_DWELL_S
        since the last switch - otherwise a track hovering right at a
        boundary would flap between two presets every render tick.
        """
        level = self._overall.value
        candidate = AUTO_MODE_CANDIDATES[0][0]
        for name, threshold in AUTO_MODE_CANDIDATES:
            if level >= threshold:
                candidate = name
        if self._auto_mode_changed_s is None:
            # First classification ever - nothing to debounce against yet.
            self._auto_mode = candidate
            self._auto_mode_changed_s = now_s
        elif candidate != self._auto_mode and now_s - self._auto_mode_changed_s >= AUTO_MIN_DWELL_S:
            self._auto_mode = candidate
            self._auto_mode_changed_s = now_s
        return self._auto_mode

    def _consume_due_beat(self, now_s: float) -> _ScheduledBeat | None:
        """Pop and return the next due beat/sub-beat, or None if none is due."""
        while self._beats and self._beats[0].timestamp_s < now_s - _BEAT_STALE_S:
            self._beats.pop(0)
        if not self._beats:
            return None
        head = self._beats[0]
        if head.timestamp_s <= now_s + _BEAT_FIRE_WINDOW_S:
            self._beats.pop(0)
            return head
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
