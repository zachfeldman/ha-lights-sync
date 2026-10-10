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
    DEFAULT_RESTORE_ON_STOP,
    ENTITY_LATENCY_EMA_ALPHA,
    ENTITY_LATENCY_SAFETY_FACTOR,
    RENDER_PERIOD_S,
    RESTORE_TRANSITION_S,
    SETTINGS_FLASH_S,
    SPECTRUM_BINS,
    SPECTRUM_F_MAX,
    SPECTRUM_F_MIN,
    SPECTRUM_SCALE,
    VISUALIZER_RATE_HZ,
)

# Color attributes a light state can carry, in the order we'll prefer them
# when restoring (light.turn_on accepts only one color spec at a time - see
# _build_restore_service_data). hs_color first since it's what we send
# ourselves during sync, so it's the most common case to restore exactly.
_RESTORE_COLOR_ATTRS: tuple[str, ...] = ("hs_color", "rgb_color", "xy_color", "color_temp_kelvin")

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

# Max wall-clock time _begin_streaming will let the pre-sync snapshot delay
# the render loop's start - see _begin_streaming's docstring for why this
# is a race against a timeout rather than an await with one, same lesson
# as _dispatch_send's stuck-call handling.
_PRE_SYNC_SNAPSHOT_BUDGET_S = 0.5


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
        # Wall-clock dispatch time per entity, independent of the task's own
        # notion of being "done" - see _dispatch_send for why this is the
        # thing that actually guarantees forward progress, not task.cancel().
        self._pending_send_started_at: dict[str, float] = {}
        # Per-entity round-trip EMA (ms), used to pace each light to its own
        # realistic speed rather than the shared render rate - see
        # _dispatch_send and const.py's "Per-entity adaptive pacing" note.
        # Seeded lazily (first real sample replaces the implicit "not slower
        # than the base render rate" assumption) rather than pre-filled, so
        # a light that's never been measured isn't throttled on a guess.
        self._entity_latency_ms: dict[str, float] = {}
        self._sendspin_client: SendspinClient | None = None
        self._is_streaming = False
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._render_handle: asyncio.TimerHandle | None = None
        # Whether to put each light back how it was before the stream
        # started once it really ends - see _capture_pre_sync_state/
        # _restore_pre_sync_state. Set from the provider's own setting in
        # start(); the literal default here never actually applies (always
        # overwritten before the bridge does anything), just avoids an
        # Optional type for a brief window.
        self.restore_on_stop: bool = DEFAULT_RESTORE_ON_STOP
        # entity_id -> {"state": "on"/"off", "attributes": {...}}, captured
        # fresh on every stream start, consumed (and cleared) by the next
        # restore. Empty whenever there's nothing to restore - either
        # restore_on_stop is off, capture failed, or a restore already ran.
        self._pre_sync_state: dict[str, dict] = {}
        # Tracks the in-progress settings-change confirmation flash, if
        # any - see update_settings/_flash_confirmation. Cancelled and
        # replaced (not left to run alongside) if another settings change
        # arrives before the current flash finishes, so dragging a slider
        # doesn't stack up several overlapping flashes.
        self._flash_task: asyncio.Task[None] | None = None

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
            transition_style=self.provider.get_transition_style(),
            sensitivity=self.provider.get_sensitivity(),
            hue_lock_enabled=self.provider.get_hue_lock_enabled(),
            hue_lock_deg=self.provider.get_hue_lock_deg(),
        )
        self.restore_on_stop = self.provider.get_restore_on_stop()

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

        if self._flash_task and not self._flash_task.done():
            self._flash_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flash_task
        self._flash_task = None

        if self._sendspin_client:
            await self.sendspin_server.remove_client(self._sendspin_client.client_id)
            self._sendspin_client = None

        for task in self._pending_send.values():
            task.cancel()
        self._pending_send.clear()

        # Best-effort: if the provider is being unloaded/reloaded mid-play,
        # the stream-end path (_debounced_stop) never gets a chance to run -
        # restore here instead, before _hass goes away, so a config change
        # (e.g. editing the light list) doesn't leave lights stuck mid-sync.
        await self._restore_pre_sync_state()

        self._hass = None
        self._is_streaming = False
        self.logger.debug("HA Lights bridge stopped for '%s'", self.group_name)

    def update_settings(
        self,
        color_mode: str | None = None,
        brightness: int | None = None,
        beat_multiplier: int | None = None,
        transition_style: str | None = None,
        sensitivity: int | None = None,
        hue_lock_enabled: bool | None = None,
        hue_lock_deg: int | None = None,
        restore_on_stop: bool | None = None,
    ) -> None:
        """Update analyzer settings without restarting the bridge."""
        if self._analyzer:
            self._analyzer.update_settings(
                color_mode=color_mode,
                brightness=brightness,
                beat_multiplier=beat_multiplier,
                transition_style=transition_style,
                sensitivity=sensitivity,
                hue_lock_enabled=hue_lock_enabled,
                hue_lock_deg=hue_lock_deg,
            )
        # Not an analyzer concern - this is purely a bridge-level behavior
        # (what to do at stream-end), not anything rendered per-tick.
        if restore_on_stop is not None:
            self.restore_on_stop = restore_on_stop
        self._trigger_settings_flash()

    def _trigger_settings_flash(self) -> None:
        """Start (or restart) the settings-change confirmation flash - see _flash_confirmation."""
        if self._hass is None or not self.entity_ids:
            return
        if self._flash_task and not self._flash_task.done():
            self._flash_task.cancel()
        self._flash_task = self.mass.create_task(self._flash_confirmation())

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
            self.mass.create_task(self._begin_streaming())

    async def _begin_streaming(self) -> None:
        """
        Capture pre-sync state (bounded by wall-clock time, not cancellation), then start rendering.

        An earlier version of this method directly ``await``ed
        ``_capture_pre_sync_state()`` before starting the render loop (to
        avoid a render tick overwriting a light's state before the
        snapshot was taken). That's correct in the common case, but it
        means a single hung ``hass.get_states()`` call - the exact class
        of wedged-coroutine failure ``_dispatch_send``'s docstring already
        documents for ``call_service()`` - silently and permanently
        blocked the render loop from ever starting, with nothing ever
        logged about it. Confirmed happening in practice: a stream started
        and (minutes later) ended cleanly in the logs with zero render
        activity in between and the snapshot task still pending.

        Wrapping the await in ``asyncio.wait_for`` would not reliably fix
        this either: if the hung call never yields even to being
        cancelled, ``wait_for``'s own await on the cancelled task can hang
        right alongside it - no different from the plain await it's
        replacing. Racing the snapshot against a short timeout via
        ``asyncio.wait`` (which does **not** cancel the loser) sidesteps
        that: the render loop starts on schedule regardless, a snapshot
        that completes within the budget (the normal case - get_states()
        has measured in the tens of ms in practice) is still used, and a
        genuinely wedged call is simply abandoned - orphaned and harmless,
        not blocking anything - rather than wedging the whole bridge.
        """
        snapshot_task = self.mass.create_task(self._capture_pre_sync_state())
        await asyncio.wait({snapshot_task}, timeout=_PRE_SYNC_SNAPSHOT_BUDGET_S)
        if not snapshot_task.done():
            # Logged at WARNING (not just debug) on purpose - this is the
            # one visible signal that get_states() is currently wedged,
            # which otherwise produces no log output at all. Starting the
            # render loop anyway regardless of this - that's the whole
            # point of racing it instead of awaiting it directly.
            self.logger.warning(
                "Pre-sync state snapshot for '%s' didn't complete within %.1fs - "
                "starting playback sync without it (restore-on-stop won't have "
                "anything to restore to this time; the snapshot call may still "
                "complete later in the background, harmlessly)",
                self.group_name,
                _PRE_SYNC_SNAPSHOT_BUDGET_S,
            )
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
            await self._restore_pre_sync_state()

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

    # -- Light state snapshot/restore (generic - used by both pre-sync and the settings flash) --

    async def _snapshot_entity_states(self) -> dict[str, dict]:
        """
        Capture each configured light's current state.

        Best-effort: if this fails (hass not connected, a transient
        get_states() error) the caller just has nothing to restore later -
        for pre-sync state that means skipping the stream-end restore; for
        the settings flash it means the light stays however the flash left
        it. Neither is worth blocking anything else on.
        """
        if self._hass is None:
            return {}
        try:
            states = await self._hass.get_states()
        except Exception:
            self.logger.debug(
                "Failed to snapshot light state for '%s'", self.group_name, exc_info=True
            )
            return {}
        by_entity = {s["entity_id"]: s for s in states}
        return {
            entity_id: {
                "state": by_entity[entity_id].get("state"),
                "attributes": dict(by_entity[entity_id].get("attributes") or {}),
            }
            for entity_id in self.entity_ids
            if entity_id in by_entity
        }

    async def _restore_snapshot(self, snapshot: dict[str, dict]) -> None:
        """Put each light in ``snapshot`` back to its captured state."""
        if self._hass is None or not snapshot:
            return
        for entity_id, captured in snapshot.items():
            try:
                if captured["state"] == "off":
                    await self._hass.call_service(
                        "light",
                        "turn_off",
                        service_data={"transition": RESTORE_TRANSITION_S},
                        target={"entity_id": entity_id},
                    )
                else:
                    await self._hass.call_service(
                        "light",
                        "turn_on",
                        service_data=_build_restore_service_data(captured["attributes"]),
                        target={"entity_id": entity_id},
                    )
            except Exception:
                self.logger.debug(
                    "Failed to restore light state for %s", entity_id, exc_info=True
                )

    # -- Pre-sync state capture/restore --

    async def _capture_pre_sync_state(self) -> None:
        """Snapshot each configured light's current state before the first render overwrites it."""
        self._pre_sync_state = {}
        if not self.restore_on_stop:
            return
        self._pre_sync_state = await self._snapshot_entity_states()

    async def _restore_pre_sync_state(self) -> None:
        """Put each light back how it was captured in _capture_pre_sync_state, if enabled."""
        if not self.restore_on_stop or not self._pre_sync_state:
            return
        snapshot, self._pre_sync_state = self._pre_sync_state, {}
        await self._restore_snapshot(snapshot)
        self.logger.info(
            "Restored %d light%s to their pre-sync state for '%s'",
            len(snapshot),
            "" if len(snapshot) == 1 else "s",
            self.group_name,
        )

    # -- Settings-change confirmation flash --

    async def _flash_confirmation(self) -> None:
        """
        Flash every configured light white at full brightness for a couple seconds.

        Fired on every live-applied settings change (see update_settings)
        so a change is visibly confirmed instead of only showing up in a
        log line - useful whether or not anything is currently streaming.

        If music is currently streaming, the render loop is paused for the
        flash and simply resumed afterward - its next tick naturally
        repaints the correct color/brightness, no snapshot needed. If
        nothing is streaming, the pre-flash state is captured and restored
        afterward instead, same mechanism as _capture_pre_sync_state/
        _restore_pre_sync_state but independent of the restore_on_stop
        setting - this flash always cleans up after itself regardless.

        Uses ``self._is_streaming`` (not ``self._render_handle``) to decide
        which path to take - the render loop's timer handle is itself
        toggled by this method, so checking it directly would misread a
        flash that's already paused the loop (e.g. a second settings
        change arriving while an earlier flash is still sleeping) as "not
        streaming" even though the stream is very much still active.
        """
        if self._hass is None or not self.entity_ids:
            return
        was_streaming = self._is_streaming
        snapshot: dict[str, dict] = {}
        if was_streaming:
            self._cancel_render_loop()
        else:
            # Same wall-clock race as _begin_streaming, same reason: a
            # hung get_states() must not block the flash (or anything
            # after it) forever - see that method's docstring.
            snapshot_task = self.mass.create_task(self._snapshot_entity_states())
            await asyncio.wait({snapshot_task}, timeout=_PRE_SYNC_SNAPSHOT_BUDGET_S)
            snapshot = snapshot_task.result() if snapshot_task.done() else {}

        self.logger.info(
            "Flashing %d light%s white to confirm a settings change for '%s'",
            len(self.entity_ids),
            "" if len(self.entity_ids) == 1 else "s",
            self.group_name,
        )
        for entity_id in self.entity_ids:
            self.mass.create_task(self._send_flash_white(entity_id))

        await asyncio.sleep(SETTINGS_FLASH_S)

        if was_streaming:
            if self._is_streaming:
                self._start_render_loop()
            else:
                # The stream ended naturally while we were mid-flash -
                # nothing to resume into. _debounced_stop's own restore may
                # already have run (or may run right after this); fall
                # back to it rather than leaving the light stuck white.
                await self._restore_pre_sync_state()
        else:
            await self._restore_snapshot(snapshot)

    async def _send_flash_white(self, entity_id: str) -> None:
        """One-shot white/full-brightness command for the settings-change flash."""
        if self._hass is None:
            return
        try:
            await self._hass.call_service(
                "light",
                "turn_on",
                # hs_color saturation=0 is white regardless of hue; transition=0
                # for an unmistakable hard snap rather than a fade-in.
                service_data={"hs_color": [0, 0], "brightness_pct": 100, "transition": 0},
                target={"entity_id": entity_id},
            )
        except Exception:
            self.logger.debug(
                "Failed to send settings-change flash to %s", entity_id, exc_info=True
            )

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
        """
        Fire the service call as a task, pacing each entity to its own realistic speed.

        Two independent reasons a render tick might NOT send a fresh command
        to this entity right now:

        1. The previous send for it hasn't finished yet (whether "finished"
           counts is decided by wall-clock time since dispatch, NOT by
           asyncio.Task.cancel()/done() - see the stuck-call note below).
        2. It HAS finished, but finished recently enough that sending again
           now would just repeat the "slow light getting hammered" problem
           in a different guise - see _entity_latency_ms/
           ENTITY_LATENCY_SAFETY_FACTOR. This is what lets a light with a
           genuinely slower round trip (e.g. a laggy Wi-Fi strip sharing a
           group with a fast Zigbee bulb) settle into whatever cadence it
           can actually sustain - still reacting to every beat, just not at
           the full shared render rate - instead of either starving faster
           lights in the same group to match its pace, or constantly
           skipping sends for it and logging warnings about something that
           isn't actually a problem.

        Stuck-call note: whether a previous send counts as "still in
        flight" is decided by wall-clock time since it was dispatched, NOT
        by asyncio.Task.cancel()/done(). Confirmed the hard way: a
        call_service() that wedges deep inside hass_client/aiohttp can sit
        "pending" indefinitely even after being cancelled - cancellation
        only takes effect where the coroutine actually yields, and a
        wedged one may never yield again. A timeout wrapped around the
        await (an earlier version of this method used asyncio.wait_for)
        inherits that same failure mode: it was observed sitting stuck for
        4+ minutes with zero timeout ever firing. Elapsed wall-clock time
        can't be defeated this way, so it - not task state - is what
        decides whether a new send goes out.
        """
        pending = self._pending_send.get(entity_id)
        started_at = self._pending_send_started_at.get(entity_id)
        age_s = None if started_at is None else time.monotonic() - started_at
        if pending is not None and not pending.done():
            if age_s is not None and age_s > CALL_SERVICE_TIMEOUT_S:
                self.logger.warning(
                    "light.turn_on for %s has been stuck for over %.0fs - sending a new "
                    "command anyway rather than waiting on it forever (the stuck call may "
                    "still complete later; harmless if so)",
                    entity_id,
                    CALL_SERVICE_TIMEOUT_S,
                )
                pending.cancel()  # best-effort; we don't wait to see if it takes
            else:
                # Logged at WARNING (not just debug) on purpose: a user tuning
                # Speed up to 2x/4x needs an easy way to tell "my lights/
                # network can't keep up with this setting" apart from "it's
                # just not doing anything" - frequent skips here is the
                # former, see README's Speed section. A single stuck call
                # (handled above) should not look identical to this.
                self.logger.warning(
                    "Skipped render for %s - previous light.turn_on still in flight", entity_id
                )
                return
        else:
            # Previous send (if any) already completed - but if THIS entity
            # has shown it typically takes meaningfully longer than the
            # shared render period, don't immediately re-fire just because
            # the task object is technically done; give it the breathing
            # room its own measured pace calls for. Unmeasured entities (no
            # EMA sample yet) fall through unthrottled here - RENDER_PERIOD_S
            # is already enforced by the render loop's own tick interval.
            typical_ms = self._entity_latency_ms.get(entity_id)
            if typical_ms is not None and age_s is not None:
                min_gap_s = max(RENDER_PERIOD_S, (typical_ms / 1000.0) * ENTITY_LATENCY_SAFETY_FACTOR)
                if age_s < min_gap_s:
                    return
        self._pending_send_started_at[entity_id] = time.monotonic()
        self._pending_send[entity_id] = self.mass.create_task(
            self._send_command(entity_id, command)
        )

    async def _send_command(self, entity_id: str, command: LightCommand) -> None:
        if self._hass is None:
            return
        # Timing logged at DEBUG since this is the expected-success path
        # (every render tick hits it); _dispatch_send's wall-clock check is
        # what actually guards against this never returning at all.
        start = time.monotonic()
        try:
            if command.on:
                await self._hass.call_service(
                    "light",
                    "turn_on",
                    service_data={
                        "hs_color": [command.hue, command.saturation],
                        "brightness_pct": command.brightness,
                        # light.turn_on's transition is in seconds, unlike the
                        # millisecond unit used everywhere else in this plugin
                        # (matching python-kasa's/Hue's convention) - convert
                        # here, at the one spot that actually calls the HA
                        # service.
                        "transition": command.transition_ms / 1000.0,
                    },
                    target={"entity_id": entity_id},
                )
            else:
                # "strobe" mode's off phase - a real turn_off, not a dim
                # floor (see analyzer.py's _ModePreset.hard_strobe). Same
                # transition handling as turn_on, for consistency.
                await self._hass.call_service(
                    "light",
                    "turn_off",
                    service_data={"transition": command.transition_ms / 1000.0},
                    target={"entity_id": entity_id},
                )
            elapsed_ms = round((time.monotonic() - start) * 1000)
            self.logger.debug("light.turn_on for %s took %dms", entity_id, elapsed_ms)
            # Only successful, non-stuck calls feed the pacing estimate - a
            # single stuck/failed call (handled separately above/below)
            # shouldn't permanently convince us this entity is slower than
            # it normally is off one bad sample.
            prev = self._entity_latency_ms.get(entity_id)
            self._entity_latency_ms[entity_id] = (
                elapsed_ms if prev is None
                else prev + ENTITY_LATENCY_EMA_ALPHA * (elapsed_ms - prev)
            )
        except asyncio.CancelledError:
            # Raised into us by _dispatch_send's best-effort pending.cancel()
            # above, potentially long after this coroutine actually wedged -
            # not a real cancellation of the request in flight, just us
            # giving up on waiting for it. Swallowed on purpose: the stuck
            # call is already being treated as abandoned by the time this
            # fires, so there is nothing left to report.
            pass
        except Exception:
            self.logger.debug(
                "light.turn_on failed for %s (will retry next tick)", entity_id, exc_info=True
            )


def _build_restore_service_data(attributes: dict) -> dict:
    """
    Build light.turn_on service_data that puts a light back to a captured state.

    light.turn_on accepts only one color specification at a time, so this
    picks the first one present in _RESTORE_COLOR_ATTRS (hs_color is tried
    first since it's what we send ourselves during sync - the common case
    is restoring exactly that). brightness is passed through as the
    absolute 0-255 HA uses in state attributes, not the 0-100 percent this
    plugin's own commands use elsewhere - straight passthrough of whatever
    was captured, not a re-derived value.
    """
    service_data: dict = {"transition": RESTORE_TRANSITION_S}
    if attributes.get("brightness") is not None:
        service_data["brightness"] = attributes["brightness"]
    for key in _RESTORE_COLOR_ATTRS:
        if attributes.get(key) is not None:
            service_data[key] = attributes[key]
            break
    return service_data
