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

# -- Visualization styles --
#
# Named to match Music Assistant's built-in Hue Lights Sync plugin so the
# concepts carry over for anyone who has used that one. First entry is the
# default.
COLOR_MODES: Final[tuple[str, ...]] = ("smooth", "ambient", "flashing", "energetic")
DEFAULT_COLOR_MODE: Final[str] = COLOR_MODES[0]

DEFAULT_BRIGHTNESS: Final[int] = 100

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
# e.g. Kasa/Hue/Zigbee - two network hops, not Hue Entertainment's dedicated
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
# to real Kasa-via-HA hardware during development was 13-220ms (mostly
# under 100ms), which mostly clears this budget but not always; a render
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
