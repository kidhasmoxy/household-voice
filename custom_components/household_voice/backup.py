"""Quiesce embedded SQLite operations while HA copies configuration files."""
import asyncio
from .const import DOMAIN

BACKENDS = '_household_voice_backup_backends'


async def _resume(backends):
    results = await asyncio.gather(*(item.async_resume_after_backup()
        for item in reversed(backends)), return_exceptions=True)
    return any(isinstance(result, BaseException) for result in results)


async def async_pre_backup(hass):
    if hass.data.get('_household_voice_knowledge_backup_paused'):
        raise RuntimeError('Household Voice backup preparation is already active')
    paused = []
    hass.data['_household_voice_knowledge_backup_paused'] = True
    try:
        async with asyncio.timeout(90):
            while (hass.data.get('_household_voice_knowledge_busy')
                    or hass.data.get('_household_voice_knowledge_workers')):
                await asyncio.sleep(.05)
        from .native_alarm import get_native_alarm_backend
        native = get_native_alarm_backend(hass)
        paused.append(native)
        await native.async_pause_for_backup()
        from .schedule_control import get_schedule_control
        controls = get_schedule_control(hass)
        paused.append(controls)
        await controls.async_pause_for_backup()
        for coordinator in list(hass.data.get(DOMAIN, {}).values()):
            for client in (getattr(coordinator, 'echomuse_runtime', None), coordinator.client):
                pause = getattr(client, 'async_pause_for_backup', None)
                if pause is not None:
                    paused.append(client)
                    await pause()
        hass.data[BACKENDS] = paused
    except BaseException:
        hass.data['_household_voice_knowledge_backup_paused'] = False
        await _resume(paused)
        raise


async def async_post_backup(hass):
    try:
        if await _resume(hass.data.pop(BACKENDS, [])):
            raise RuntimeError('A Household Voice backend could not resume after backup')
    finally:
        hass.data['_household_voice_knowledge_backup_paused'] = False
