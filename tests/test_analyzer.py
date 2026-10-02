"""Tests for the HA Lights audio analyzer - pure logic, no Sendspin/hardware needed."""

from __future__ import annotations

import pytest

from ha_lights_sync.analyzer import HALightsAudioAnalyzer, _clamp01


def test_clamp01() -> None:
    assert _clamp01(-1.0) == 0.0
    assert _clamp01(0.5) == 0.5
    assert _clamp01(2.0) == 1.0


def test_render_without_input_stays_within_range() -> None:
    analyzer = HALightsAudioAnalyzer(color_mode="smooth", brightness=100)
    command = analyzer.render(now_s=0.0)
    assert 1 <= command.brightness <= 100
    assert 0 <= command.saturation <= 100
    assert 0 <= command.hue < 360
    assert command.transition_ms > 0


def test_loud_bass_raises_brightness_over_silence() -> None:
    quiet = HALightsAudioAnalyzer(color_mode="smooth", brightness=100)
    loud = HALightsAudioAnalyzer(color_mode="smooth", brightness=100)

    # Feed enough frames for the asymmetric EMA to actually rise.
    for t in range(10):
        quiet.apply_spectrum([0.0] * 12)
        loud.apply_spectrum([1.0] * 12)

    quiet_cmd = quiet.render(now_s=1.0)
    loud_cmd = loud.render(now_s=1.0)
    assert loud_cmd.brightness > quiet_cmd.brightness


def test_beat_triggers_a_brightness_flash() -> None:
    analyzer = HALightsAudioAnalyzer(color_mode="flashing", brightness=100)
    analyzer.apply_spectrum([0.1] * 12)
    baseline = analyzer.render(now_s=0.0).brightness

    analyzer.push_beats([(1.0, False)])
    at_beat = analyzer.render(now_s=1.0).brightness
    assert at_beat > baseline


def test_stale_beats_are_dropped_not_fired_late() -> None:
    analyzer = HALightsAudioAnalyzer(color_mode="flashing", brightness=100)
    analyzer.push_beats([(0.0, False)])
    # Jump far enough ahead that the beat is stale (see _BEAT_STALE_S).
    analyzer.render(now_s=10.0)
    assert analyzer._beats == []  # noqa: SLF001 - internal check is the point of this test


def test_brightness_respects_configured_ceiling() -> None:
    capped = HALightsAudioAnalyzer(color_mode="smooth", brightness=10)
    for _ in range(10):
        capped.apply_spectrum([1.0] * 12)
    command = capped.render(now_s=1.0)
    assert command.brightness <= 10


def test_update_settings_changes_active_mode() -> None:
    analyzer = HALightsAudioAnalyzer(color_mode="smooth", brightness=100)
    analyzer.update_settings(color_mode="flashing", brightness=50)
    assert analyzer.color_mode == "flashing"
    assert analyzer.brightness_ceiling == 50


def test_unknown_color_mode_falls_back_to_default() -> None:
    analyzer = HALightsAudioAnalyzer(color_mode="not-a-real-mode", brightness=100)
    assert analyzer.color_mode == "smooth"


@pytest.mark.parametrize("mode", ["smooth", "ambient", "flashing", "energetic"])
def test_every_preset_renders_without_error(mode: str) -> None:
    analyzer = HALightsAudioAnalyzer(color_mode=mode, brightness=100)
    analyzer.apply_spectrum([0.3] * 12)
    analyzer.push_beats([(0.5, True)])
    command = analyzer.render(now_s=0.5)
    assert 1 <= command.brightness <= 100
