"""
Turns Sendspin visualizer features into Kasa light targets.

Mirrors the shape of Music Assistant's built-in HueAudioAnalyzer
(music_assistant/providers/hue_entertainment/analyzer.py): accumulate the
periodic spectrum/loudness/peak frames and the scheduled beat timestamps
Sendspin's visualizer role delivers, then on each render tick turn the
current state into a light command.

The two real differences from the Hue version:

1. Output is a single flat KasaCommand (hue/saturation/brightness/transition_ms)
   rather than a per-channel list - these strips are one color zone each, not
   a multi-point Entertainment Area.
2. render() is called at RENDER_RATE_HZ (see const.py), not Hue's 30Hz, and
   leans on the strip's own `transition` parameter to interpolate rather than
   repainting every frame - see bridge.py for why.
"""

from __future__ import annotations

import colorsys
import time
from dataclasses import dataclass, field

from .const import COLOR_MODES, DEFAULT_BRIGHTNESS, DEFAULT_COLOR_MODE, RENDER_PERIOD_S

# How long a beat's flash takes to decay back to the resting brightness.
_BEAT_FLASH_DECAY_S = 0.25
# A beat landing within this window of "now" still counts as "just happened" -
# render() is not guaranteed to be called at the exact beat timestamp.
_BEAT_FIRE_WINDOW_S = 0.08
# Beats older than this are forgotten rather than fired late.
_BEAT_STALE_S = 1.5


@dataclass(frozen=True)
class KasaCommand:
    """One target state for a Kasa light, handed to the bridge to send."""

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


_PRESETS: dict[str, _ModePreset] = {
    "smooth": _ModePreset(
        floor=0.55, flash_strength=0.35, hue_drift_deg_s=6.0, hue_beat_jump_deg=15.0,
        saturation_floor=0.7,
    ),
    "ambient": _ModePreset(
        floor=0.45, flash_strength=0.25, hue_drift_deg_s=3.0, hue_beat_jump_deg=40.0,
        saturation_floor=0.4,
    ),
    "flashing": _ModePreset(
        floor=0.2, flash_strength=1.0, hue_drift_deg_s=0.0, hue_beat_jump_deg=0.0,
        saturation_floor=1.0,
    ),
    "energetic": _ModePreset(
        floor=0.3, flash_strength=0.8, hue_drift_deg_s=25.0, hue_beat_jump_deg=90.0,
        saturation_floor=0.9,
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


class KasaAudioAnalyzer:
    """
    Accumulates visualizer features for one Sendspin client and renders Kasa targets.

    One instance per configured light group (see provider.py) - matches a
    HueAudioAnalyzer per Entertainment Area in the upstream plugin.
    """

    def __init__(self, color_mode: str = DEFAULT_COLOR_MODE, brightness: int = DEFAULT_BRIGHTNESS) -> None:
        self.color_mode = color_mode if color_mode in COLOR_MODES else DEFAULT_COLOR_MODE
        self.brightness_ceiling = brightness
        self._bass = _ExpFilter(alpha_rise=0.6, alpha_decay=0.08, initial=0.0)
        self._treble = _ExpFilter(alpha_rise=0.5, alpha_decay=0.1, initial=0.0)
        self._beats: list[_ScheduledBeat] = []
        self._last_fired_beat_s: float = 0.0
        self._last_beat_flash_s: float = -10.0
        self._hue_base: float = 0.0
        self._last_render_s: float = time.monotonic()

    def update_settings(self, color_mode: str | None = None, brightness: int | None = None) -> None:
        if color_mode is not None and color_mode in COLOR_MODES:
            self.color_mode = color_mode
        if brightness is not None:
            self.brightness_ceiling = brightness

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
        Add scheduled beats (seconds since an epoch shared with render()'s clock, is_downbeat).

        :param beats_s: (timestamp, is_downbeat) pairs, as delivered by Sendspin's
            visualizer role (see bridge.py's ``_on_beats``).
        """
        self._beats.extend(_ScheduledBeat(ts, down) for ts, down in beats_s)
        self._beats.sort(key=lambda b: b.timestamp_s)

    def clear_beats(self) -> None:
        self._beats.clear()

    def render(self, now_s: float) -> KasaCommand:
        """Compute the light command for this instant; called at RENDER_RATE_HZ."""
        preset = _PRESETS[self.color_mode]
        dt = max(0.0, now_s - self._last_render_s)
        self._last_render_s = now_s

        fired = self._consume_due_beat(now_s)
        if fired is not None:
            self._last_beat_flash_s = now_s
            self._hue_base = (self._hue_base + preset.hue_beat_jump_deg * (1.6 if fired else 1.0)) % 360

        self._hue_base = (self._hue_base + preset.hue_drift_deg_s * dt) % 360

        flash_age = now_s - self._last_beat_flash_s
        flash = preset.flash_strength * max(0.0, 1.0 - flash_age / _BEAT_FLASH_DECAY_S)

        level = preset.floor + (1.0 - preset.floor) * self._bass.value + flash
        # brightness_ceiling is 0-100 (the configured cap); level is the 0-1+
        # fraction of it this instant renders at, clamped before scaling.
        brightness_pct = max(1, min(100, round(self.brightness_ceiling * _clamp01(level))))

        saturation = round(100 * _clamp01(preset.saturation_floor + (1 - preset.saturation_floor) * self._treble.value))

        hue_shift = self._treble.value * 20.0  # treble brightens/cools the hue slightly
        hue = round((self._hue_base + hue_shift) % 360)

        return KasaCommand(
            hue=hue,
            saturation=saturation,
            brightness=brightness_pct,
            transition_ms=round(RENDER_PERIOD_S * 1000),
        )

    def _consume_due_beat(self, now_s: float) -> bool | None:
        """Pop and return whether the next due beat is a downbeat, or None if none is due."""
        while self._beats and self._beats[0].timestamp_s < now_s - _BEAT_STALE_S:
            self._beats.pop(0)
        if not self._beats:
            return None
        head = self._beats[0]
        if head.timestamp_s <= now_s + _BEAT_FIRE_WINDOW_S:
            self._beats.pop(0)
            return head.is_downbeat
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def hsv_to_kasa(hue: int, saturation: int, brightness: int) -> tuple[int, int, int]:
    """
    Pass-through today - python-kasa's set_hsv already takes hue/saturation/value in
    the same ranges this module computes in. Kept as a seam in case a future device
    needs RGB instead (e.g. via colorsys.hsv_to_rgb) without touching the analyzer.
    """
    return hue, saturation, brightness
