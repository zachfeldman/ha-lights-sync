"""
Setup flow for the HA Lights Sync provider.

Picks which Home Assistant light entities this group drives. This is the
one setting with no sensible default - unlike color_mode/brightness (see
provider.py's get_config_entries, editable any time after setup) - so it
needs an interactive form the user must actually fill in before the
provider can be created, the same reason Hue Entertainment's bridge
pairing lives in its own setup_flow.py rather than in get_config_entries:
see music_assistant/providers/hue_entertainment/setup_flow.py's docstring.

Confirmed necessary the hard way: without this, Music Assistant's regular
provider-add flow attempted to create the provider immediately with
whatever defaults existed, which instantly failed validation on the one
required field with no default - the config screen never got a chance to
render before the user saw "Setup failed."

Runs for both initial setup and reconfigure (session.context.kind) - same
form either way, prefilled from the existing selection on reconfigure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .const import CONF_LIGHT_ENTITIES

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Run the HA Lights Sync setup/reconfigure flow."""
    prefill = session.context.values.get(CONF_LIGHT_ENTITIES)
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_LIGHT_ENTITIES,
                    type=ConfigEntryType.STRING,
                    label="Light(s)",
                    description=(
                        "The Home Assistant light entities this group should drive. "
                        "Pulled live from Home Assistant - if a light is already set "
                        "up there (any brand/integration), it's selectable here, no "
                        "extra setup needed."
                    ),
                    required=True,
                    multi_value=True,
                    options=await _light_entity_options(session.mass),
                    value=prefill,
                ),
            ],
            step_id="user",
            last_step=True,
            errors=errors,
        )
        try:
            await session.finish(dict(values))
            return
        except SetupFlowError as err:
            errors = {"base": str(err)}


async def _light_entity_options(mass: MusicAssistant) -> list[ConfigValueOption]:
    """
    Return every light.* entity Home Assistant currently knows about.

    Empty (rather than raising) when the Home Assistant plugin isn't
    loaded/connected yet, so the form still renders - with a clear "nothing
    to pick" state - instead of failing to open at all.
    """
    hass_provider = mass.get_provider("hass")
    hass = getattr(hass_provider, "hass", None) if hass_provider else None
    if hass is None:
        return []
    try:
        states = await hass.get_states()
    except Exception:
        return []
    options = [
        ConfigValueOption(
            state["entity_id"],
            title=state["attributes"].get("friendly_name", state["entity_id"]),
        )
        for state in states
        if state["entity_id"].startswith("light.")
    ]
    options.sort(key=lambda opt: (opt.title or "").casefold())
    return options
