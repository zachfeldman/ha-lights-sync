"""
HA Lights bridge — in-process Sendspin visualizer client.

Registers one virtual Sendspin player of type LIGHT per configured light
group (see provider.py). The user groups that virtual player with whatever
real Music Assistant player is playing to the room - same workflow as the
built-in Hue Lights Sync plugin ("join a Hue light player to any active
Sendspin player").

This file is the transport-specific half of the split described in
music_assistant/providers/hue_entertainment/bridge.py's own docstring: the
Sendspin registration, callback wiring and render-loop scheduling are the
same shape as the Hue plugin uses (deliberately - this is the pattern
Music Assistant expects a lighting bridge to follow). What's different:

- No DTLS session, no device-specific protocol client at all: a LightCommand
  is sent as a plain `light.turn_on` service call through Music Assistant's
  own "Home Assistant" plugin (`mass.get_provider("hass").hass`, a
  `hass_client.HomeAssistantClient` - already connected/authenticated by that
  plugin, no separate config needed here). This is what makes the plugin
  work with *any* light Home Assistant controls - Kasa, Hue, LIFX, Zigbee,
  whatever - rather than needing its own device-specific transport per brand.
- The render loop runs at RENDER_RATE_HZ (const.py), much lower than Hue's
  30Hz, because a service call is one request/response round trip (through
  HA's websocket API, then whatever HA does internally) rather than a
  continuous stream - see const.py's DEFAULT_HA_LATENCY_MS comment. Each
  command carries a `transition` spanning the render period so the light
  eases between points instead of visibly stepping.
- Sends are fire-and-forget tasks, skipped (not queued) if the previous one
  for the same entity hasn't finished - a slow/unavailable light must never
  back up the render loop or send commands out of order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
)
from music_assistant_models.enums import PlayerType

from music_assistant.providers.sendspin.bridge_role import VISUALIZER_BRIDGE_ROLE_ID, BridgeVisualizerRole

from .analyzer import HALightsAudioAnalyzer
from .const import (
    CALL_SERVICE_TIMEOUT_S,
    RENDER_PERIOD_S,
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
    VISUALIZER_RATE_HZ,
)

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame
    from aiosendspin.models.visualizer import BeatTiming
    from hass_client import HomeAssistantClient

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .analyzer import LightCommand
    from .provider import HALightsSyncProvider

LOGGER = logging.getLogger(__name__)

# Debounce before actually tearing a stream down, so a track transition's
# brief gap doesn't flap the Sendspin client every song.
_STOP_DEBOUNCE_S = 2.0


class HALightGroupBridge:
    """Manages one configured group of Home Assistant lights as a Sendspin visualizer client."""

    def __init__(
        self,
        provider: HALightsSyncProvider,
        group_name: str,
        entity_ids: list[str],
        sendspin_server: SendspinServer,
    ) -> None:
        self.provider = provider
        self.mass = provider.mass
        self.group_name = group_name
        self.entity_ids = entity_ids
        self.sendspin_server = sendspin_server
        self.logger = LOGGER.getChild(f"bridge.{group_name}")

        self._analyzer: HALightsAudioAnalyzer | None = None
        self._hass: HomeAssistantClient | None = None
        self._pending_send: dict[str, asyncio.Task[None]] = {}
        self._sendspin_client: SendspinClient | None = None
        self._is_streaming = False
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._render_handle: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        """Grab the Home Assistant connection and register as a Sendspin visualizer client."""
        hass_provider = self.mass.get_provider("hass")
        if hass_provider is None or not getattr(hass_provider, "hass", None):
            raise RuntimeError(
                "The 'Home Assistant' plugin provider must be loaded and connected first "
                "(Settings -> Add Provider -> Home Assistant)"
            )
        self._hass = hass_provider.hass

        self._analyzer = HALightsAudioAnalyzer(
            color_mode=self.provider.get_color_mode(),
            brightness=self.provider.get_brightness(),
            beat_multiplier=self.provider.get_beat_multiplier(),
        )

        client_id = f"ha-lights-{self.group_name.lower().replace(' ', '-')[:24]}"

        sendspin_prov: SendspinProvider | None = self.mass.get_provider("sendspin")  # type: ignore[assignment]
        if sendspin_prov:
            sendspin_prov.register_bridge_player_type(client_id, PlayerType.LIGHT)

        support = ClientHelloVisualizerSupport(
            buffer_capacity=1024,
            rate_max=VISUALIZER_RATE_HZ,
            types=["beat", "spectrum"],
            spectrum=ClientHelloVisualizerSpectrum(
                n_disp_bins=SPECTRUM_BINS,
                scale=SPECTRUM_SCALE,
                f_min=SPECTRUM_F_MIN,
                f_max=SPECTRUM_F_MAX,
            ),
        )
        hello = ClientHelloPayload(
            client_id=client_id,
            name=f"HA Lights: {self.group_name}",
            version=1,
            supported_roles=[VISUALIZER_BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(manufacturer="Home Assistant", product_name="Light Group"),
            visualizer_support=support,
        )
        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_external_stream_start
        )

        if viz_roles := self._sendspin_client.roles_by_family("visualizer"):
            viz_role = cast("BridgeVisualizerRole", viz_roles[0])
            viz_role.set_callbacks(
                on_frame=self._on_visualizer_frame,
                on_beats=self._on_beats,
                on_beats_clear=self._on_beats_clear,
                on_stream_start=self._on_stream_start,
                on_stream_clear=self._on_stream_clear,
                on_stream_end=self._on_stream_end,
            )
            viz_role.setup_visualizer(support)
        self._sendspin_client.attach_preinitialized_roles()

        self.logger.info(
            "HA Lights bridge started for '%s' (%d entit%s configured)",
            self.group_name,
            len(self.entity_ids),
            "y" if len(self.entity_ids) == 1 else "ies",
        )

    async def stop(self) -> None:
        """Stop the bridge."""
        self._cancel_render_loop()
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stop_debounce_task
        self._stop_debounce_task = None

        if self._sendspin_client:
            await self.sendspin_server.remove_client(self._sendspin_client.client_id)
            self._sendspin_client = None

        for task in self._pending_send.values():
            task.cancel()
        self._pending_send.clear()

        self._hass = None
        self._is_streaming = False
        self.logger.debug("HA Lights bridge stopped for '%s'", self.group_name)

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        beat_multiplier: int | None = None,
    ) -> None:
        """Update analyzer settings without restarting the bridge."""
        if self._analyzer:
            self._analyzer.update_settings(
                color_mode=color_mode, brightness=brightness, beat_multiplier=beat_multiplier
            )

    # -- Sendspin callbacks --

    def _on_external_stream_start(self, request: ExternalStreamStartRequest) -> None:
        self.logger.debug("Sendspin stream start request for '%s'", self.group_name)

    def _on_stream_start(self) -> None:
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            self._stop_debounce_task = None
        if not self._is_streaming:
            self._is_streaming = True
            self.logger.info("Stream starting for '%s'", self.group_name)
            self._start_render_loop()

    def _on_stream_end(self) -> None:
        if self._is_streaming:
            if self._stop_debounce_task and not self._stop_debounce_task.done():
                self._stop_debounce_task.cancel()
            self._stop_debounce_task = self.mass.create_task(self._debounced_stop())

    def _on_stream_clear(self) -> None:
        if self._analyzer is not None:
            self._analyzer.clear_beats()

    async def _debounced_stop(self) -> None:
        await asyncio.sleep(_STOP_DEBOUNCE_S)
        if self._is_streaming:
            self.logger.info("Visualizer stream ended for '%s'", self.group_name)
            self._is_streaming = False
            self._cancel_render_loop()

    def _on_visualizer_frame(self, frame: ExtractedFrame) -> None:
        if self._analyzer is None or not self._is_streaming:
            return
        if frame.spectrum is not None:
            self._analyzer.apply_spectrum(frame.spectrum.tolist())

    def _on_beats(self, beats: list[BeatTiming]) -> None:
        if self._analyzer is not None:
            self._analyzer.push_beats([(b.timestamp_us / 1_000_000, b.is_downbeat) for b in beats])

    def _on_beats_clear(self) -> None:
        if self._analyzer is not None:
            self._analyzer.clear_beats()

    # -- Render loop --

    def _start_render_loop(self) -> None:
        if self._render_handle is not None:
            return
        self._render_handle = self.mass.loop.call_later(RENDER_PERIOD_S, self._render_tick)

    def _cancel_render_loop(self) -> None:
        if self._render_handle is not None:
            self._render_handle.cancel()
            self._render_handle = None

    def _render_tick(self) -> None:
        self._render_handle = None
        if not self._is_streaming:
            return
        try:
            if self._analyzer is not None:
                now_s = self.sendspin_server.clock.now_us() / 1_000_000
                command = self._analyzer.render(now_s)
                for entity_id in self.entity_ids:
                    self._dispatch_send(entity_id, command)
        except Exception:
            self.logger.exception("Render tick failed for '%s'", self.group_name)
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(RENDER_PERIOD_S, self._render_tick)

    def _dispatch_send(self, entity_id: str, command: LightCommand) -> None:
        """Fire the service call as a task, skipping if the previous send is still in flight."""
        pending = self._pending_send.get(entity_id)
        if pending is not None and not pending.done():
            # Logged at WARNING (not just debug) on purpose: a user tuning
            # Speed up to 2x/4x needs an easy way to tell "my lights/network
            # can't keep up with this setting" apart from "it's just not
            # doing anything" - frequent skips here is the former, see
            # README's Speed section.
            self.logger.warning(
                "Skipped render for %s - previous light.turn_on still in flight", entity_id
            )
            return
        self._pending_send[entity_id] = self.mass.create_task(
            self._send_command(entity_id, command)
        )

    async def _send_command(self, entity_id: str, command: LightCommand) -> None:
        if self._hass is None:
            return
        # Round-trip time matters here, not just for curiosity: if it
        # regularly exceeds RENDER_PERIOD_S, _dispatch_send's skip-if-pending
        # guard above starts dropping renders - logged at DEBUG since this is
        # the expected-success path (every render tick hits it), unlike the
        # skip case above which is the thing worth a user's attention.
        start = time.monotonic()
        try:
            # wait_for matters, not just a nicety: a call_service() that never
            # returns (seen in practice when the underlying device/integration
            # wedges - e.g. a Cast-group protocol switch disrupting the LAN
            # the light sits on) would otherwise leave _pending_send[entity_id]
            # "in flight" forever, permanently skipping every future render
            # for that one entity until the bridge restarts. A stuck call is
            # abandoned after CALL_SERVICE_TIMEOUT_S so the next render tick
            # gets a clean shot at it instead.
            await asyncio.wait_for(
                self._hass.call_service(
                    "light",
                    "turn_on",
                    service_data={
                        "hs_color": [command.hue, command.saturation],
                        "brightness_pct": command.brightness,
                        # light.turn_on's transition is in seconds, unlike the
                        # millisecond unit used everywhere else in this plugin
                        # (matching python-kasa's/Hue's convention) - convert here,
                        # at the one spot that actually calls the HA service.
                        "transition": command.transition_ms / 1000.0,
                    },
                    target={"entity_id": entity_id},
                ),
                timeout=CALL_SERVICE_TIMEOUT_S,
            )
            elapsed_ms = round((time.monotonic() - start) * 1000)
            self.logger.debug("light.turn_on for %s took %dms", entity_id, elapsed_ms)
        except TimeoutError:
            self.logger.warning(
                "light.turn_on for %s did not respond within %.0fs - abandoning this send "
                "so future renders aren't permanently blocked (the light or its integration "
                "may be stuck)",
                entity_id,
                CALL_SERVICE_TIMEOUT_S,
            )
        except Exception:
            self.logger.debug(
                "light.turn_on failed for %s (will retry next tick)", entity_id, exc_info=True
            )
