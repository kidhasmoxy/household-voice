"""Bundled sidebar UI; only static application code is served without auth.

State and mutations use Home Assistant's authenticated frontend connection.
No configuration, recordings, coordinator credentials or snapshots are served.
"""
import asyncio
from hashlib import sha256
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig

DATA_KEY = "household_voice_frontend"
PANEL_PATH = "household-voice"
ASSET_URL = "/household_voice_static/household-voice-panel.js"
ASSET_PATH = Path(__file__).parent / "www" / "household-voice-panel.js"


def _asset_version():
    return sha256(ASSET_PATH.read_bytes()).hexdigest()[:16]


async def async_setup_panel(hass, entry_id):
    """Register once for all loaded entries; safe on options reload/retry."""
    state = hass.data.setdefault(DATA_KEY, {
        "lock": asyncio.Lock(), "entries": set(), "static_registered": False,
        "panel_registered": False,
    })
    async with state["lock"]:
        if not state["static_registered"]:
            await hass.http.async_register_static_paths([
                StaticPathConfig(ASSET_URL, str(ASSET_PATH), False),
            ])
            state["static_registered"] = True
        if not state["panel_registered"]:
            version = await hass.async_add_executor_job(_asset_version)
            await panel_custom.async_register_panel(
                hass, frontend_url_path=PANEL_PATH,
                webcomponent_name="household-voice-panel",
                sidebar_title="Household Voice", sidebar_icon="mdi:timer-music-outline",
                module_url=f"{ASSET_URL}?v={version}", require_admin=False,
                config={},
            )
            state["panel_registered"] = True
        state["entries"].add(entry_id)


async def async_unload_panel(hass, entry_id):
    """Remove the sidebar after the last entry; retain the static route.

    HTTP static routes have no public unregister API. Keeping the code-only
    route avoids duplicate route registration when an entry is reloaded.
    """
    if (state := hass.data.get(DATA_KEY)) is None:
        return
    async with state["lock"]:
        state["entries"].discard(entry_id)
        if not state["entries"] and state["panel_registered"]:
            frontend.async_remove_panel(hass, PANEL_PATH)
            state["panel_registered"] = False
