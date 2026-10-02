"""
Kasa bridge — in-process Sendspin visualizer client.

Registers one virtual Sendspin player of type LIGHT per configured device
group (see provider.py). The user groups that virtual player with whatever
real Music Assistant player is playing to the room - same workflow as the
built-in Hue Lights Sync plugin ("join a Hue light player to any active
Sendspin player").

This file is the Kasa-specific half of the split described in
music_assistant/providers/hue_entertainment/bridge.py's own docstring: the
Sendspin registration, callback wiring and render-loop scheduling are the
same shape as the Hue plugin uses (deliberately - this is the pattern
Music Assistant expects a lighting bridge to follow). What's different:

- No DTLS session: a KasaCommand is sent as a plain local-network
  set_hsv()/set_brightness() call via python-kasa.
- The render loop runs at RENDER_RATE_HZ (const.py), much lower than Hue's
  30Hz, because Kasa's protocol is one request/response round trip per
  command rather than a continuous stream - see const.py's
  DEFAULT_KASA_LATENCY_MS comment for the reasoning. Each command carries a
  `transition_ms` spanning the render period so the strip eases between
  points instead of visibly stepping.
- Sends are fire-and-forget tasks, skipped (not queued) if the previous one
  for the same device hasn't finished - a slow/unreachable strip must never
  back up the render loop or send commands out of order.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
)
from kasa import Device, Discover, Module
from music_assistant_models.enums import PlayerType

from music_assistant.providers.sendspin.bridge_role import VISUALIZER_BRIDGE_ROLE_ID, BridgeVisualizerRole

from .analyzer import KasaAudioAnalyzer
from .const import (
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

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import KasaLightsSyncProvider

LOGGER = logging.getLogger(__name__)

# Debounce before actually tearing a stream down, so a track transition's
# brief gap doesn't drop and reopen device connections every song.
_STOP_DEBOUNCE_S = 2.0


class KasaLightGroupBridge:
    """Manages one configured group of Kasa lights as a Sendspin visualizer client."""

    def __init__(
        self,
        provider: KasaLightsSyncProvider,
        group_name: str,
        device_hosts: list[str],
        sendspin_server: SendspinServer,
    ) -> None:
        self.provider = provider
        self.mass = provider.mass
        self.group_name = group_name
        self.device_hosts = device_hosts
        self.sendspin_server = sendspin_server
        self.logger = LOGGER.getChild(f"bridge.{group_name}")

        self._analyzer: KasaAudioAnalyzer | None = None
        self._devices: dict[str, Device] = {}
        self._pending_send: dict[str, asyncio.Task[None]] = {}
        self._sendspin_client: SendspinClient | None = None
        self._is_streaming = False
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._render_handle: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        """Connect to the configured Kasa devices and register as a Sendspin visualizer client."""
        for host in self.device_hosts:
            try:
                device = await Discover.discover_single(host)
                await device.update()
                if Module.Light not in device.modules:
                    self.logger.warning("%s does not expose a Light module, skipping", host)
                    continue
                self._devices[host] = device
            except Exception:
                self.logger.exception("Could not connect to Kasa device at %s", host)

        self._analyzer = KasaAudioAnalyzer(
            color_mode=self.provider.get_color_mode(),
            brightness=self.provider.get_brightness(),
        )

        client_id = f"kasa-{self.group_name.lower().replace(' ', '-')[:24]}"

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
            name=f"Kasa: {self.group_name}",
            version=1,
            supported_roles=[VISUALIZER_BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(manufacturer="TP-Link", product_name="Kasa Light Strip"),
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
            "Kasa bridge started for '%s' (%d device(s) connected of %d configured)",
            self.group_name,
            len(self._devices),
            len(self.device_hosts),
        )

    async def stop(self) -> None:
        """Stop the bridge and release device connections."""
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

        for device in self._devices.values():
            with suppress(Exception):
                await device.disconnect()
        self._devices.clear()
        self._is_streaming = False
        self.logger.debug("Kasa bridge stopped for '%s'", self.group_name)

    def update_settings(self, color_mode: str | None = None, brightness: int | None = None) -> None:
        """Update analyzer settings without restarting the bridge."""
        if self._analyzer:
            self._analyzer.update_settings(color_mode=color_mode, brightness=brightness)

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
                for host in self._devices:
                    self._dispatch_send(host, command)
        except Exception:
            self.logger.exception("Kasa render tick failed for '%s'", self.group_name)
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(RENDER_PERIOD_S, self._render_tick)

    def _dispatch_send(self, host: str, command) -> None:  # noqa: ANN001 - KasaCommand, see analyzer.py
        """Fire the device command as a task, skipping if the previous send is still in flight."""
        pending = self._pending_send.get(host)
        if pending is not None and not pending.done():
            return
        self._pending_send[host] = self.mass.create_task(self._send_command(host, command))

    async def _send_command(self, host: str, command) -> None:  # noqa: ANN001
        device = self._devices.get(host)
        if device is None:
            return
        try:
            light = device.modules[Module.Light]
            await light.set_hsv(
                command.hue,
                command.saturation,
                command.brightness,
                transition=command.transition_ms,
            )
        except Exception:
            self.logger.debug("Kasa command to %s failed (will retry next tick)", host, exc_info=True)
