"""
HA Lights Sync provider for Music Assistant.

Each configured provider instance drives one group of Home Assistant lights
(e.g. "all the garage strips") as a single virtual Sendspin player - group it
with a real player in the Music Assistant UI the same way you'd group a Hue
Lights Sync player, and the configured lights react to whatever that player
plays.

Which lights this group drives is picked interactively in setup_flow.py (a
dedicated form, prefilled from Home Assistant's own live entity list via
`mass.get_provider("hass")`) rather than here - that field has no sensible
default, so it needs the same kind of must-fill-this-in interactive step
Hue Entertainment's bridge pairing uses, not a regular (optional-feeling)
config entry. See setup_flow.py's docstring for why that distinction
matters in practice, not just in theory.

Add the provider again (manifest declares multi_instance: true) for a second
independently-configured room/group.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .bridge import HALightGroupBridge
from .const import (
    BEAT_MULTIPLIERS,
    CONF_BEAT_MULTIPLIER,
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_LIGHT_ENTITIES,
    CONF_SENSITIVITY,
    CONF_TRANSITION_STYLE,
    COLOR_MODES,
    DEFAULT_BEAT_MULTIPLIER,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
    DEFAULT_SENSITIVITY,
    DEFAULT_TRANSITION_STYLE,
    SENSITIVITY_RANGE,
    TRANSITION_STYLES,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


class HALightsSyncProvider(PluginProvider):
    """Provider that syncs a group of Home Assistant lights to music via Sendspin."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        super().__init__(mass, manifest, config, supported_features)
        self._bridge: HALightGroupBridge | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return the (options) config entries for this provider instance.

        light_entities is deliberately absent here - it's collected by
        setup_flow.py instead. These two are the settings that DO have a
        sensible default and stay editable any time after setup, same as
        Hue Entertainment's brightness/color_mode (its bridge pairing is
        likewise setup_flow-only, not repeated here).
        """
        return (
            ConfigEntry(
                key=CONF_COLOR_MODE,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_COLOR_MODE,
                options=[ConfigValueOption(mode, title=mode.capitalize()) for mode in COLOR_MODES],
                category="settings",
            ),
            ConfigEntry(
                key=CONF_BRIGHTNESS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_BRIGHTNESS,
                range=(1, 100),
                category="settings",
            ),
            ConfigEntry(
                key=CONF_BEAT_MULTIPLIER,
                type=ConfigEntryType.INTEGER,
                label="Speed",
                description=(
                    "Pulses per beat. 1x follows the track's real tempo exactly. 2x/4x "
                    "insert evenly-spaced pulses between beats (softer than the real "
                    "beat) for a faster feel - how crisp these look in practice depends "
                    "on your lights' own response time, see the README's Speed section."
                ),
                default_value=DEFAULT_BEAT_MULTIPLIER,
                options=[ConfigValueOption(n, title=f"{n}x") for n in BEAT_MULTIPLIERS],
                category="settings",
            ),
            ConfigEntry(
                key=CONF_TRANSITION_STYLE,
                type=ConfigEntryType.STRING,
                label="Transition style",
                description=(
                    "'Fade' eases between colors/brightness smoothly. 'Instant' snaps "
                    "to a hard, un-eased change on every update instead - a punchier, "
                    "more percussive feel, at the cost of visible stepping during the "
                    "non-beat color drift most modes also do."
                ),
                default_value=DEFAULT_TRANSITION_STYLE,
                options=[
                    ConfigValueOption("fade", title="Fade"),
                    ConfigValueOption("instant", title="Instant"),
                ],
                category="settings",
            ),
            ConfigEntry(
                key=CONF_SENSITIVITY,
                type=ConfigEntryType.INTEGER,
                label="Sensitivity",
                description=(
                    "Amplifies the detected audio signal itself, as a percent - distinct "
                    "from Speed (which only changes how often pulses fire, not how strong "
                    "they look). Raise this if quieter listening volumes make the lights "
                    "look washed-out/barely reactive; 100% is unmodified."
                ),
                default_value=DEFAULT_SENSITIVITY,
                range=SENSITIVITY_RANGE,
                category="settings",
            ),
        )

    def get_color_mode(self) -> str:
        value = self.config.get_value(CONF_COLOR_MODE)
        return str(value) if value in COLOR_MODES else DEFAULT_COLOR_MODE

    def get_brightness(self) -> int:
        value = self.config.get_value(CONF_BRIGHTNESS)
        try:
            return max(1, min(100, int(value)))
        except (TypeError, ValueError):
            return DEFAULT_BRIGHTNESS

    def get_beat_multiplier(self) -> int:
        value = self.config.get_value(CONF_BEAT_MULTIPLIER)
        try:
            value = int(value)
        except (TypeError, ValueError):
            return DEFAULT_BEAT_MULTIPLIER
        return value if value in BEAT_MULTIPLIERS else DEFAULT_BEAT_MULTIPLIER

    def get_transition_style(self) -> str:
        value = self.config.get_value(CONF_TRANSITION_STYLE)
        return str(value) if value in TRANSITION_STYLES else DEFAULT_TRANSITION_STYLE

    def get_sensitivity(self) -> int:
        value = self.config.get_value(CONF_SENSITIVITY)
        try:
            lo, hi = SENSITIVITY_RANGE
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return DEFAULT_SENSITIVITY

    def get_light_entity_ids(self) -> list[str]:
        """
        Return the light entities picked in setup_flow.py.

        Values collected via a setup flow's session.finish() land in the
        provider's setup_data, not its regular config values - read back
        via get_setup_value(), not config.get_value(). Mirrors Hue
        Entertainment's own provider.py reading CONF_BRIDGE_HOST the same
        way. Using config.get_value() here was the bug that let setup
        "succeed" (the flow completed, the provider got created) while
        silently persisting no light selection at all - see commit history
        for the first (wrong) version of this method.
        """
        value = self.get_setup_value(CONF_LIGHT_ENTITIES)
        if isinstance(value, list):
            return [str(v) for v in value if v]
        return [str(value)] if value else []

    async def loaded_in_mass(self) -> None:
        """Start the Sendspin bridge for the configured light entities."""
        entity_ids = self.get_light_entity_ids()
        if not entity_ids:
            self.logger.warning("No light entities configured, provider inactive")
            self.available = False
            return

        sendspin_server = self._get_sendspin_server()
        if sendspin_server is None:
            self.logger.error("Sendspin provider not available/loaded - cannot start")
            self.available = False
            return

        self._bridge = HALightGroupBridge(
            self, self.name or self.instance_id, entity_ids, sendspin_server
        )
        try:
            await self._bridge.start()
            self.available = True
        except Exception:
            self.logger.exception("Failed to start HA Lights bridge")
            self.available = False

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._bridge:
            await self._bridge.stop()
            self._bridge = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """
        Handle config changes.

        brightness/color_mode/beat_multiplier/transition_style can be applied
        to the running bridge in place; anything else (notably
        light_entities) falls through to the base implementation, which
        reloads the provider - matches HueEntertainmentProvider.
        update_config's split, see
        music_assistant/providers/hue_entertainment/provider.py.
        """
        settings_keys = {
            f"values/{key}"
            for key in (
                CONF_BRIGHTNESS,
                CONF_COLOR_MODE,
                CONF_BEAT_MULTIPLIER,
                CONF_TRANSITION_STYLE,
                CONF_SENSITIVITY,
            )
        }
        # Logged at INFO (not just debug) while this is still new: confirms,
        # for any given settings change, whether it actually took the live-
        # apply path below or fell through to a full provider reload - the
        # two are not visually distinguishable to a user just watching the
        # lights, so this is the fast way to tell which happened.
        self.logger.info(
            "update_config called with changed_keys=%s (live-applicable=%s)",
            changed_keys,
            bool(changed_keys) and changed_keys <= settings_keys,
        )
        if changed_keys and changed_keys <= settings_keys and self._bridge:
            self._bridge.update_settings(
                color_mode=self.get_color_mode(),
                brightness=self.get_brightness(),
                beat_multiplier=self.get_beat_multiplier(),
                transition_style=self.get_transition_style(),
                sensitivity=self.get_sensitivity(),
            )
            self.config = config
            return
        await super().update_config(config, changed_keys)

    def _get_sendspin_server(self):  # noqa: ANN202 - SendspinServer, kept untyped to avoid an import cycle
        sendspin_prov = self.mass.get_provider("sendspin")
        if sendspin_prov is None:
            return None
        # The running SendspinServer instance lives on the Sendspin provider as
        # `server_api` (see music_assistant/providers/sendspin/provider.py,
        # `self.server_api = SendspinServer(...)`). Confirmed against
        # HueEntertainmentBridgeManager.sendspin_server, which reads the same
        # attribute - not a guess.
        return getattr(sendspin_prov, "server_api", None)
