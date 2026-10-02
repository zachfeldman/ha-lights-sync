"""
Kasa Lights Sync provider for Music Assistant.

Each configured provider instance drives one group of Kasa lights (e.g. "all
the garage strips") as a single virtual Sendspin player - group it with a
real player in the Music Assistant UI the same way you'd group a Hue Lights
Sync player, and the configured lights react to whatever that player plays.

Add the provider again (manifest declares multi_instance: true) for a second
independently-configured room/group.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .bridge import KasaLightGroupBridge
from .const import (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_DEVICE_HOSTS,
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


class KasaLightsSyncProvider(PluginProvider):
    """Provider that syncs a group of Kasa lights to music via Sendspin."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        super().__init__(mass, manifest, config, supported_features)
        self._bridge: KasaLightGroupBridge | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the (options) config entries for this provider instance."""
        return (
            ConfigEntry(
                key=CONF_DEVICE_HOSTS,
                type=ConfigEntryType.STRING,
                label="Kasa device IP address(es)",
                description=(
                    "Comma-separated local IPs of the Kasa light strips/bulbs this "
                    "group should drive, e.g. 192.168.1.40,192.168.1.41. Use a DHCP "
                    "reservation on each device - python-kasa connects by IP, not by "
                    "the Kasa app's device name."
                ),
                required=True,
                category="settings",
            ),
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

    def get_device_hosts(self) -> list[str]:
        raw = str(self.config.get_value(CONF_DEVICE_HOSTS) or "")
        return [host.strip() for host in raw.split(",") if host.strip()]

    async def loaded_in_mass(self) -> None:
        """Connect to the configured Kasa devices and start the Sendspin bridge."""
        hosts = self.get_device_hosts()
        if not hosts:
            self.logger.warning("No Kasa device IPs configured, provider inactive")
            self.available = False
            return

        sendspin_server = self._get_sendspin_server()
        if sendspin_server is None:
            self.logger.error("Sendspin provider not available/loaded - cannot start")
            self.available = False
            return

        self._bridge = KasaLightGroupBridge(
            self, self.name or self.instance_id, hosts, sendspin_server
        )
        try:
            await self._bridge.start()
            self.available = True
        except Exception:
            self.logger.exception("Failed to start Kasa bridge")
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
        anything else (notably device_hosts) falls through to the base
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
