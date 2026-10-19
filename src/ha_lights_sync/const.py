"""Config constants for the HA Lights Sync provider."""

from __future__ import annotations

from typing import Final

# -- Config entry keys --

CONF_LIGHT_ENTITIES: Final[str] = "light_entities"
CONF_COLOR_MODE: Final[str] = "color_mode"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_BEAT_MULTIPLIER: Final[str] = "beat_multiplier"
CONF_TRANSITION_STYLE: Final[str] = "transition_style"
CONF_HA_LATENCY_MS: Final[str] = "ha_latency_ms"
CONF_SENSITIVITY: Final[str] = "sensitivity"
CONF_HUE_LOCK_ENABLED: Final[str] = "hue_lock_enabled"
CONF_HUE_LOCK_DEG: Final[str] = "hue_lock_deg"
CONF_RESTORE_ON_STOP: Final[str] = "restore_on_stop"

# -- Visualization styles --
#
# Named to match Music Assistant's built-in Hue Lights Sync plugin so the
# concepts carry over for anyone who has used that one. First entry is the
# default. "pulse" is the odd one out - its whole point is brightness
# continuously tracking overall loudness and nothing else (no beat flash,
# no hue jump) - see analyzer.py's _ModePreset.use_overall_level.
#
# "energetic", "auto", and "strobe" existed here through 2026-10-19 but were
# removed after a real-hardware test session: the render loop died silently
# (no exception, no log activity of any kind) for all three of them for
# 10-20+ second stretches, root cause not found despite adding real
# heartbeat logging (see bridge.py's _render_tick/_start_render_loop/
# _cancel_render_loop) - "smooth" was in the same test run but inconclusive
# (never actually got a clean settings change fired at it) and was kept.
# Rather than ship modes confirmed to freeze on real hardware with no fix in
# hand, they were cut. If re-adding any of them: the heartbeat logging is
# still in place, so a fresh real-hardware run should localize this
# properly instead of inferring from log silence like the original
# investigation had to.
COLOR_MODES: Final[tuple[str, ...]] = (
    "smooth", "ambient", "flashing", "pulse",
)
DEFAULT_COLOR_MODE: Final[str] = COLOR_MODES[0]

# -- Hue lock --
#
# Overrides every mode's hue output (drift, beat jump, treble shift - all of
# it) with one fixed degree value, while brightness/saturation keep reacting
# normally. For someone who wants the lights to react to the music but stay
# a specific color (a team color, a holiday color, matching a room's decor)
# rather than roam the color wheel - a complaint dynamic hue has no other
# way to address short of picking "ambient" and hoping the drift is slow
# enough not to matter.
DEFAULT_HUE_LOCK_ENABLED: Final[bool] = False
DEFAULT_HUE_LOCK_DEG: Final[int] = 0
HUE_LOCK_RANGE: Final[tuple[int, int]] = (0, 359)

# -- Restore on stop --
#
# Whether to put each light back exactly how it was before the stream
# started (on/off, brightness, color) once the stream really ends, instead
# of leaving it frozen at whatever the last rendered frame happened to be.
# Default true: freezing mid-color/mid-brightness when music stops reads as
# broken even though it isn't. The one-time per-entity state snapshot this
# needs is captured in bridge.py's _on_stream_start, not here.
DEFAULT_RESTORE_ON_STOP: Final[bool] = True
# Transition (seconds) used for the restore call itself - deliberately
# gentle regardless of the configured transition_style, since "snap back to
# whatever it was before" reads as glitchy if done as a hard instant cut.
RESTORE_TRANSITION_S: Final[float] = 1.0

# -- Settings-change confirmation flash --
#
# A settings change (color_mode, brightness, Speed, ...) applies silently
# and instantly - there's nothing to actually SEE happen if music isn't
# playing at that moment, which makes "did my change actually take effect"
# a real question with no visual answer. Flashing every configured light
# white at full brightness right when a change is saved gives an
# unmistakable, immediate confirmation independent of whether anything is
# currently streaming - see bridge.py's update_settings/_flash_confirmation.
SETTINGS_FLASH_S: Final[float] = 2.0

DEFAULT_BRIGHTNESS: Final[int] = 100

# -- Sensitivity --
#
# A plain gain multiplier on the raw spectrum magnitude, applied before it's
# clamped into bass/treble/overall energy (see analyzer.py's apply_spectrum).
# Exists because the analyzer has no idea how loud the room actually is -
# Sendspin hands it magnitudes computed from whatever the track's own mix/
# mastering level and the player's current volume happen to be, so quieter
# listening (or a quietly-mastered track) can sit well under 1.0 on every
# bin, muting every mode's swell/flash contrast even though beats are still
# tracked correctly - beat timestamps come from Sendspin's own tempo
# tracking, not from this magnitude, so they're unaffected by this setting
# (see push_beats). Stored as a percent (matching this plugin's other
# integer config entries) and divided by 100 where it's actually applied.
DEFAULT_SENSITIVITY: Final[int] = 100
SENSITIVITY_RANGE: Final[tuple[int, int]] = (25, 400)

# -- Speed --
#
# Sendspin/smart_fades only ever gives us the real, tracked beat (1x). 2x/4x
# are synthesized: evenly-spaced sub-beats inserted between each pair of real
# beats (see analyzer.py's push_beats), pulsing softer than the real beat so
# it still reads as the strongest hit. How crisp these look in practice is
# capped by RENDER_RATE_HZ below and by real round-trip time to the physical
# light - 4x on a fast track can ask for pulses closer together than either
# can reliably keep up with; see README's Speed section.
BEAT_MULTIPLIERS: Final[tuple[int, ...]] = (1, 2, 4)
DEFAULT_BEAT_MULTIPLIER: Final[int] = BEAT_MULTIPLIERS[0]

# -- Transition style --
#
# "fade" (default) sends a transition spanning the render period, so the
# light eases between points - necessary at RENDER_RATE_HZ's modest update
# rate to avoid visible stepping on continuous brightness/hue drift.
# "instant" sends transition=0 instead: every render ties to a hard, un-eased
# cut, which can read as a punchier/more percussive "solid change" on the
# beat rather than a morph - at the cost of that same stepping becoming
# visible during non-beat drift (hue_drift_deg_s, bass-driven brightness).
TRANSITION_STYLES: Final[tuple[str, ...]] = ("fade", "instant")
DEFAULT_TRANSITION_STYLE: Final[str] = TRANSITION_STYLES[0]

# How far ahead of "now" a render target is scheduled, to absorb command
# round-trip time through Home Assistant's light.turn_on service (our own
# call to HA's websocket API, then HA's own call to the light's integration,
# e.g. Hue/Zigbee/Wi-Fi - two network hops, not Hue Entertainment's dedicated
# low-latency DTLS stream). Deliberately larger than Hue's own default (20ms)
# - see README for the round trip this was tuned against.
DEFAULT_HA_LATENCY_MS: Final[int] = 150

# -- Visualizer feature request (passed to Sendspin's ClientHelloVisualizerSupport) --
#
# light.turn_on is a request/response service call, not a streamed protocol -
# sending a fresh command once per extracted frame at Hue's 20Hz would flood
# both our websocket link to HA and whatever integration/protocol HA then
# uses to reach the physical light. Instead we request frames at a modest
# rate and rely on the `transition` parameter (seconds) to interpolate
# between them, so the light's own transition bridges each render period.
#
# 8Hz (125ms) rather than a more conservative rate because 2x/4x speed needs
# the resolution to tell consecutive sub-beats apart - measured round trip
# to real smart-light-via-HA hardware during early development (TP-Link
# Kasa strips, specifically) was 13-220ms (mostly under 100ms), which mostly
# clears this budget but not always; a render
# tick landing on top of a still-in-flight one is skipped rather than
# queued (see bridge.py's _dispatch_send), so very fast songs at 4x can
# still visibly skip a pulse here and there - that's a real hardware/
# network ceiling, not a bug. Lower this if your logs show frequent skips.
VISUALIZER_RATE_HZ: Final[int] = 8
RENDER_RATE_HZ: Final[int] = 8
RENDER_PERIOD_S: Final[float] = 1.0 / RENDER_RATE_HZ

# How long a light.turn_on call may sit "in flight" before _dispatch_send
# considers it stuck and sends a new one anyway. Well above the worst real
# round trip measured during development (~220ms) - this is purely a
# backstop against a call that never returns at all (confirmed in practice:
# a Cast-group protocol switch left one wedged for 4+ minutes straight).
# This is NOT an asyncio.wait_for/cancellation timeout - see bridge.py's
# _dispatch_send docstring for why that approach doesn't reliably work here
# and wall-clock elapsed time is used instead.
CALL_SERVICE_TIMEOUT_S: Final[float] = 3.0

# 12 mel bins is plenty for a 2-zone bass/treble split; keeps the requested
# payload small. See music_assistant/providers/hue_entertainment/constants.py
# for the prior art this mirrors (it uses 17 bins across its multi-channel
# Entertainment Areas - a HA light group is usually just one or two zones).
SPECTRUM_BINS: Final[int] = 12
SPECTRUM_SCALE: Final = "mel"
SPECTRUM_F_MIN: Final[int] = 20
SPECTRUM_F_MAX: Final[int] = 20000

# -- Per-entity adaptive pacing --
#
# Not every light in a group answers light.turn_on at the same speed - a
# slow/laggy device (e.g. a cheap Wi-Fi LED strip a few hops from the HA
# host) sharing a group with a fast one (e.g. a local Zigbee bulb) used to
# get hammered at the same RENDER_RATE_HZ as the fast one, producing
# constant "previous light.turn_on still in flight" skips and arguably
# making the slow light look worse than if it were just paced to what it
# can actually keep up with. Rather than requiring the user to manually
# mark a light as "slow" (a new per-entity setup step), each entity's own
# recent round-trip time is tracked (see bridge.py's _entity_latency_ms)
# and used to automatically widen its minimum inter-send gap beyond the
# shared RENDER_PERIOD_S once it's shown it needs that - so it still reacts
# to every beat, just at whatever pace it can actually sustain, without
# starving faster lights in the same group of their full render rate.
#
# Multiplier applied to an entity's own observed typical round-trip before
# using it as that entity's minimum gap between sends - gives it headroom
# to finish comfortably rather than pacing it right at the ragged edge of
# its last measured time.
ENTITY_LATENCY_SAFETY_FACTOR: Final[float] = 1.3
# How fast the per-entity round-trip estimate adapts to new samples.
ENTITY_LATENCY_EMA_ALPHA: Final[float] = 0.3
