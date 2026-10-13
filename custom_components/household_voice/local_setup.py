"""Local installation helpers; no external coordinator or account required."""
from urllib.parse import urlsplit

from .contracts import normalize_url


def audio_base_url(hass, configured=None):
    """Use an HA origin reachable by the media server; never a credential URL."""
    if not configured:
        from homeassistant.helpers.network import get_url, NoURLAvailableError
        try:
            configured = get_url(hass, allow_external=False)
        except NoURLAvailableError:
            raise ValueError("Set a Home Assistant internal URL") from None
    url = normalize_url(configured)
    if urlsplit(url).path:
        raise ValueError("Use the Home Assistant origin without a path")
    return url


async def async_create_local_client(hass, entry):
    from .embedded import EmbeddedVoiceClient
    return await EmbeddedVoiceClient.async_create(
        hass, entry.entry_id, audio_base_url(hass, entry.data.get("audio_base_url")))


async def async_close_local_client(client):
    close = getattr(client, "async_close", None)
    if close is not None:
        await close()
