"""
Kasa Lights Sync - Music Assistant plugin.

Syncs TP-Link Kasa smart lights/light strips to music in real time, reusing
the same Sendspin visualizer pipeline (live spectrum + beat schedule) that
Music Assistant's built-in Hue Lights Sync plugin uses - see README.md for
the full architecture writeup and why this exists as its own package rather
than a fork of music-assistant/server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    from .provider import KasaLightsSyncProvider

    return cast(
        "ProviderInstanceType",
        KasaLightsSyncProvider(mass, manifest, config, SUPPORTED_FEATURES),
    )
