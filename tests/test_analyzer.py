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


# -- Speed (beat_multiplier) --


def test_1x_speed_inserts_no_subbeats() -> None:
    analyzer = HALightsAudioAnalyzer(beat_multiplier=1)
    analyzer.push_beats([(0.0, False), (1.0, False), (2.0, False)])
    assert [b.timestamp_s for b in analyzer._beats] == [0.0, 1.0, 2.0]  # noqa: SLF001


def test_2x_speed_inserts_one_subbeat_per_gap() -> None:
    analyzer = HALightsAudioAnalyzer(beat_multiplier=2)
    analyzer.push_beats([(0.0, False), (1.0, False)])
    timestamps = [b.timestamp_s for b in analyzer._beats]  # noqa: SLF001
    assert timestamps == pytest.approx([0.0, 0.5, 1.0])


def test_4x_speed_inserts_three_subbeats_per_gap() -> None:
    analyzer = HALightsAudioAnalyzer(beat_multiplier=4)
    analyzer.push_beats([(0.0, False), (1.0, False)])
    timestamps = [b.timestamp_s for b in analyzer._beats]  # noqa: SLF001
    assert timestamps == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])


def test_subbeats_are_weaker_than_the_real_beat_they_came_from() -> None:
    analyzer = HALightsAudioAnalyzer(beat_multiplier=2)
    analyzer.push_beats([(0.0, False), (1.0, False)])
    real, sub, _next_real = analyzer._beats  # noqa: SLF001
    assert sub.strength < real.strength


def test_a_single_beat_with_no_following_beat_still_schedules_cleanly() -> None:
    # No "next" beat to interpolate toward - must not crash, and the lone
    # beat keeps full strength (nothing to subdivide into).
    analyzer = HALightsAudioAnalyzer(beat_multiplier=4)
    analyzer.push_beats([(0.0, False)])
    assert len(analyzer._beats) == 1  # noqa: SLF001
    assert analyzer._beats[0].strength == 1.0  # noqa: SLF001


def test_fast_subbeats_get_a_tighter_flash_decay_than_the_default() -> None:
    from ha_lights_sync.analyzer import _BEAT_FLASH_DECAY_S, _MIN_FLASH_DECAY_S

    analyzer = HALightsAudioAnalyzer(beat_multiplier=4)
    # 0.2s apart overall -> 0.05s between each of the 4 sub-intervals, well
    # under the default decay, so every beat here should be tightened.
    analyzer.push_beats([(0.0, False), (0.2, False)])
    for beat in analyzer._beats[:-1]:  # noqa: SLF001
        assert _MIN_FLASH_DECAY_S <= beat.decay_s <= _BEAT_FLASH_DECAY_S


def test_invalid_beat_multiplier_falls_back_to_default() -> None:
    analyzer = HALightsAudioAnalyzer(beat_multiplier=3)
    assert analyzer.beat_multiplier == 1


def test_update_settings_changes_beat_multiplier() -> None:
    analyzer = HALightsAudioAnalyzer(beat_multiplier=1)
    analyzer.update_settings(beat_multiplier=4)
    assert analyzer.beat_multiplier == 4


def test_update_settings_ignores_invalid_beat_multiplier() -> None:
    analyzer = HALightsAudioAnalyzer(beat_multiplier=2)
    analyzer.update_settings(beat_multiplier=3)
    assert analyzer.beat_multiplier == 2


@pytest.mark.parametrize("multiplier", [1, 2, 4])
def test_every_speed_renders_without_error(multiplier: int) -> None:
    analyzer = HALightsAudioAnalyzer(color_mode="flashing", beat_multiplier=multiplier)
    analyzer.apply_spectrum([0.3] * 12)
    analyzer.push_beats([(0.5, True), (1.5, False)])
    command = analyzer.render(now_s=0.5)
    assert 1 <= command.brightness <= 100
