"""Config constants for the HA Lights Sync provider."""

from __future__ import annotations

from typing import Final

# -- Config entry keys --

CONF_LIGHT_ENTITIES: Final[str] = "light_entities"
CONF_COLOR_MODE: Final[str] = "color_mode"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_HA_LATENCY_MS: Final[str] = "ha_latency_ms"

# -- Visualization styles --
#
# Named to match Music Assistant's built-in Hue Lights Sync plugin so the
# concepts carry over for anyone who has used that one. First entry is the
# default.
COLOR_MODES: Final[tuple[str, ...]] = ("smooth", "ambient", "flashing", "energetic")
DEFAULT_COLOR_MODE: Final[str] = COLOR_MODES[0]

DEFAULT_BRIGHTNESS: Final[int] = 100

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
VISUALIZER_RATE_HZ: Final[int] = 8
RENDER_RATE_HZ: Final[int] = 4
RENDER_PERIOD_S: Final[float] = 1.0 / RENDER_RATE_HZ

# 12 mel bins is plenty for a 2-zone bass/treble split; keeps the requested
# payload small. See music_assistant/providers/hue_entertainment/constants.py
# for the prior art this mirrors (it uses 17 bins across its multi-channel
# Entertainment Areas - a HA light group is usually just one or two zones).
SPECTRUM_BINS: Final[int] = 12
SPECTRUM_SCALE: Final = "mel"
SPECTRUM_F_MIN: Final[int] = 20
SPECTRUM_F_MAX: Final[int] = 20000
