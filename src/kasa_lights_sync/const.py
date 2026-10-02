"""Config constants for the Kasa Lights Sync provider."""

from __future__ import annotations

from typing import Final

# -- Config entry keys --

CONF_DEVICE_HOSTS: Final[str] = "device_hosts"
CONF_COLOR_MODE: Final[str] = "color_mode"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_KASA_LATENCY_MS: Final[str] = "kasa_latency_ms"

# -- Visualization styles --
#
# Named to match Music Assistant's built-in Hue Lights Sync plugin so the
# concepts carry over for anyone who has used that one. First entry is the
# default.
COLOR_MODES: Final[tuple[str, ...]] = ("smooth", "ambient", "flashing", "energetic")
DEFAULT_COLOR_MODE: Final[str] = COLOR_MODES[0]

DEFAULT_BRIGHTNESS: Final[int] = 100

# How far ahead of "now" a render target is scheduled, to absorb command
# round-trip time to the strip. Kasa's local protocol is request/response over
# plain TCP, not a continuous low-latency stream like Hue Entertainment's DTLS,
# so this is deliberately larger than Hue's default (20ms) - see README for the
# measured round trip this was tuned against.
DEFAULT_KASA_LATENCY_MS: Final[int] = 120

# -- Visualizer feature request (passed to Sendspin's ClientHelloVisualizerSupport) --
#
# Kasa's local LAN protocol is a request/response TCP call per update, not a
# streamed protocol - sending a fresh command once per extracted frame at
# Hue's 20Hz would flood the strip's (fairly weak) Wi-Fi MCU with overlapping
# in-flight requests. Instead we request frames at a modest rate and rely on
# set_hsv's own `transition` parameter to interpolate between them, so a
# strip-side transition bridges each render period smoothly.
VISUALIZER_RATE_HZ: Final[int] = 8
RENDER_RATE_HZ: Final[int] = 4
RENDER_PERIOD_S: Final[float] = 1.0 / RENDER_RATE_HZ

# 12 mel bins is plenty for a 2-zone bass/treble split; keeps the requested
# payload small. See music_assistant/providers/hue_entertainment/constants.py
# for the prior art this mirrors (it uses 17 bins across its multi-channel
# Entertainment Areas - we only drive two single-color-zone strips).
SPECTRUM_BINS: Final[int] = 12
SPECTRUM_SCALE: Final = "mel"
SPECTRUM_F_MIN: Final[int] = 20
SPECTRUM_F_MAX: Final[int] = 20000
