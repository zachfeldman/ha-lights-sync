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
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_LIGHT_ENTITIES,
    COLOR_MODES,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
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

    def get_light_entity_ids(self) -> list[str]:
        value = self.config.get_value(CONF_LIGHT_ENTITIES)
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

        brightness/color_mode can be applied to the running bridge in place;
        anything else (notably light_entities) falls through to the base
        implementation, which reloads the provider - matches
        HueEntertainmentProvider.update_config's split, see
        music_assistant/providers/hue_entertainment/provider.py.
        """
        settings_keys = {f"values/{key}" for key in (CONF_BRIGHTNESS, CONF_COLOR_MODE)}
        if changed_keys and changed_keys <= settings_keys and self._bridge:
            self._bridge.update_settings(
                color_mode=self.get_color_mode(), brightness=self.get_brightness()
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
