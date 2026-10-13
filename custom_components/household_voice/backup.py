"""Quiesce embedded SQLite operations while HA copies configuration files."""
from .const import DOMAIN


async def async_pre_backup(hass):
    paused = []
    try:
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            client = coordinator.client
            pause = getattr(client, 'async_pause_for_backup', None)
            if pause is not None:
                await pause()
                paused.append(client)
    except BaseException:
        for client in paused:
            await client.async_resume_after_backup()
        raise


async def async_post_backup(hass):
    for coordinator in list(hass.data.get(DOMAIN, {}).values()):
        resume = getattr(coordinator.client, 'async_resume_after_backup', None)
        if resume is not None:
            await resume()
