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
    BEAT_MULTIPLIERS,
    COLOR_MODES,
    DEFAULT_BEAT_MULTIPLIER,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
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
    """One target state for a light, handed to the bridge to send via light.turn_on."""

    hue: int  # degrees, 0-360
    saturation: int  # percent, 0-100
    brightness: int  # percent, 0-100
    transition_ms: int


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
    ) -> None:
        self.color_mode = color_mode if color_mode in COLOR_MODES else DEFAULT_COLOR_MODE
        self.brightness_ceiling = brightness
        self.beat_multiplier = beat_multiplier if beat_multiplier in BEAT_MULTIPLIERS else DEFAULT_BEAT_MULTIPLIER
        self.transition_style = (
            transition_style if transition_style in TRANSITION_STYLES else DEFAULT_TRANSITION_STYLE
        )
        self._bass = _ExpFilter(alpha_rise=0.6, alpha_decay=0.08, initial=0.0)
        self._treble = _ExpFilter(alpha_rise=0.5, alpha_decay=0.1, initial=0.0)
        self._beats: list[_ScheduledBeat] = []
        self._last_beat_flash_s: float = -10.0
        self._last_flash_strength: float = 1.0
        self._last_flash_decay_s: float = _BEAT_FLASH_DECAY_S
        self._hue_base: float = 0.0
        self._last_render_s: float = time.monotonic()

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        beat_multiplier: int | None = None,
        transition_style: str | None = None,
    ) -> None:
        if color_mode is not None and color_mode in COLOR_MODES:
            self.color_mode = color_mode
        if brightness is not None:
            self.brightness_ceiling = brightness
        if beat_multiplier is not None and beat_multiplier in BEAT_MULTIPLIERS:
            self.beat_multiplier = beat_multiplier
        if transition_style is not None and transition_style in TRANSITION_STYLES:
            self.transition_style = transition_style

    def apply_spectrum(self, bins: list[float]) -> None:
        """
        Fold a binned spectrum frame into smoothed bass/treble energy.

        :param bins: Magnitude per mel bin, low frequency first (see
            SPECTRUM_BINS/SPECTRUM_SCALE in const.py for the request shape).
        """
        if not bins:
            return
        split = max(1, len(bins) // 3)
        bass_raw = sum(bins[:split]) / split
        treble_raw = sum(bins[-split:]) / split
        self._bass.update(_clamp01(bass_raw))
        self._treble.update(_clamp01(treble_raw))

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
        preset = _PRESETS[self.color_mode]
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
        flash = (
            preset.flash_strength
            * self._last_flash_strength
            * max(0.0, 1.0 - flash_age / self._last_flash_decay_s)
        )

        level = preset.floor + (1.0 - preset.floor) * self._bass.value * preset.bass_weight + flash
        # brightness_ceiling is 0-100 (the configured cap); level is the 0-1+
        # fraction of it this instant renders at, clamped before scaling.
        brightness_pct = max(1, min(100, round(self.brightness_ceiling * _clamp01(level))))

        saturation = round(100 * _clamp01(preset.saturation_floor + (1 - preset.saturation_floor) * self._treble.value))

        hue_shift = self._treble.value * 20.0  # treble brightens/cools the hue slightly
        hue = round((self._hue_base + hue_shift) % 360)

        # "instant" sends transition=0 (a hard, un-eased cut on every render -
        # a punchier "solid change" on the beat); "fade" spans the render
        # period so continuous drift still looks smooth. See const.py.
        transition_ms = 0 if self.transition_style == "instant" else round(RENDER_PERIOD_S * 1000)

        return LightCommand(
            hue=hue,
            saturation=saturation,
            brightness=brightness_pct,
            transition_ms=transition_ms,
        )

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
