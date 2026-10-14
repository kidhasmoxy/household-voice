"""Household Voice with local durable state or an optional external coordinator."""
from aiohttp import ClientError
import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .api import VoiceClient, CoordinatorError
from .const import CONF_ENTRY, CONF_TOKEN, CONF_URL, DOMAIN, SERVICES
from .contracts import validate_payload
from .coordinator import VoiceCoordinator
from .llm import async_register as async_register_llm
from .media_llm import async_register as async_register_media_llm
from .broadcast_llm import async_register as async_register_broadcast_llm
from .broadcast_runtime import BroadcastRuntime
from .runtime_config import async_configure
from .ring_worker import RingWorker
from .research_llm import async_register as async_register_research_llm


def reset_configured_audio(hass,entry,player_id,confirmed):
    """Explicit administrator recovery only; never sends or infers a stop."""
    from .satellite_transport import reset_uncertain_audio
    options=entry.options
    allowed=set(options.get('default_announcement_speakers',[]))
    allowed.update(options.get('ring_fallback_speakers',[]))
    allowed.update(row['tts_media_player'] for row in options.get('satellites',[]))
    allowed.update(row['music_player'] for row in options.get('media_rooms',[]))
    allowed.update(player for row in options.get('alarm_rooms',[]) for player in row['speakers'])
    if confirmed is not True or player_id not in allowed:
        raise HomeAssistantError('Select a saved speaker and confirm its pending audio has stopped')
    try:
        return reset_uncertain_audio(hass,player_id,operator_confirmed_stopped=True)
    except Exception:
        raise HomeAssistantError('Audio recovery unavailable; wait for active work and check the saved speaker') from None


def service_schema(name):
    def validate(value):
        try:
            return validate_payload(name, dict(value))
        except (TypeError, ValueError) as error:
            raise vol.Invalid(str(error)) from None
    return vol.Schema(validate)


async def async_setup(hass, config):
    hass.data.setdefault(DOMAIN, {})
    from .assist_inventory import async_register as async_register_inventory
    async_register_inventory(hass)
    async def handle(call):
        entries = hass.data[DOMAIN]
        requested = call.data.get(CONF_ENTRY)
        if requested:
            coordinator = entries.get(requested)
        else:
            coordinator = next(iter(entries.values())) if len(entries) == 1 else None
        if coordinator is None:
            raise HomeAssistantError("Select one loaded Household Voice config_entry_id")
        try:
            selected_entry=hass.config_entries.async_get_entry(coordinator.entry_id)
            if selected_entry is not None:
                await async_configure(coordinator.client,selected_entry.options,hass=hass)
            result = await coordinator.client.mutate(call.service, dict(call.data))
        except (CoordinatorError, ClientError, TimeoutError) as error:
            # Never forward response bodies, URL query strings, or credentials.
            message = str(error) if isinstance(error, CoordinatorError) else "Coordinator is unavailable"
            raise HomeAssistantError(message) from None
        await coordinator.async_request_refresh()
        return result
    for name in SERVICES:
        hass.services.async_register(DOMAIN, name, handle, schema=service_schema(name), supports_response=SupportsResponse.OPTIONAL)
    async def test_pairing(call):
        if call.context.user_id:
            user=await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise HomeAssistantError('Administrator access is required')
        coordinator=hass.data[DOMAIN].get(call.data['config_entry_id'])
        runtime=getattr(coordinator,'satellite_runtime',None)
        if runtime is None:
            raise HomeAssistantError('Enable the configured satellite audio feature first')
        try:
            result=await runtime.async_test_pairing(call.data['satellite_device_id'],
                test_url=coordinator.client.url+'/audio/alarm.wav')
            return {'status':'action_accepted' if result.get('service_accepted') else 'unavailable',**result}
        except Exception:
            raise HomeAssistantError('Pairing test unavailable; check the configured devices') from None
    hass.services.async_register(DOMAIN,'test_pairing',test_pairing,
        schema=vol.Schema({vol.Required('config_entry_id'):str,vol.Required('satellite_device_id'):str}),
        supports_response=SupportsResponse.ONLY)
    async def reset_audio(call):
        if call.context.user_id:
            user=await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise HomeAssistantError('Administrator access is required')
        coordinator=hass.data[DOMAIN].get(call.data['config_entry_id'])
        entry=hass.config_entries.async_get_entry(call.data['config_entry_id'])
        if coordinator is None or entry is None:
            raise HomeAssistantError('Select one loaded Household Voice config entry')
        return reset_configured_audio(hass,entry,call.data['media_player'],call.data['confirm_audio_stopped'])
    hass.services.async_register(DOMAIN,'reset_uncertain_audio',reset_audio,
        schema=vol.Schema({vol.Required('config_entry_id'):str,vol.Required('media_player'):str,
                           vol.Required('confirm_audio_stopped'):vol.In([True])}),
        supports_response=SupportsResponse.ONLY)
    async def reset_native_audio(call):
        if not call.context.user_id:
            raise HomeAssistantError('Administrator access is required')
        user=await hass.auth.async_get_user(call.context.user_id)
        if user is None or not user.is_admin:
            raise HomeAssistantError('Administrator access is required')
        coordinator=hass.data[DOMAIN].get(call.data['config_entry_id'])
        entry=hass.config_entries.async_get_entry(call.data['config_entry_id'])
        if coordinator is None or entry is None:
            raise HomeAssistantError('Select one loaded Household Voice config entry')
        from .native_alarm import async_reset_native_alarm
        try:
            return await async_reset_native_alarm(hass,entry,call.data['assistant_id'],
                confirm_output_stopped=call.data['confirm_audio_stopped'])
        except Exception:
            raise HomeAssistantError('Native audio recovery unavailable; confirm silence and wait for active work') from None
    hass.services.async_register(DOMAIN,'reset_native_alarm_audio',reset_native_audio,
        schema=vol.Schema({vol.Required('config_entry_id'):str,vol.Required('assistant_id'):str,
                           vol.Required('confirm_audio_stopped'):vol.In([True])}),
        supports_response=SupportsResponse.ONLY)
    return True


async def async_setup_entry(hass, entry):
    local = entry.data.get('mode') == 'embedded'
    if local:
        from .local_setup import async_create_local_client
        try:
            client = await async_create_local_client(hass, entry)
        except (OSError, ValueError, CoordinatorError):
            raise ConfigEntryNotReady('Local household voice storage or audio origin is unavailable') from None
    else:
        client = VoiceClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    coordinator = VoiceCoordinator(hass, entry, client)
    try:
        await async_configure(client,entry.options,hass=hass)
        await coordinator.async_config_entry_first_refresh()
    except BaseException as error:
        if local:
            await client.async_close()
        if isinstance(error, (CoordinatorError, ClientError, TimeoutError)):
            raise ConfigEntryNotReady('Coordinator runtime configuration is temporarily unavailable') from None
        raise
    coordinator.broadcast_runtime=BroadcastRuntime(hass,entry,client,execution_authorized=True)
    coordinator.ring_worker=RingWorker(hass,entry,client)
    coordinator.satellite_runtime=None
    coordinator.echomuse_runtime=None
    coordinator.native_duck_runtime=None
    coordinator.platforms=[Platform.SENSOR]
    if entry.options.get('conversation_fallback_agent'):
        coordinator.platforms.append(Platform.CONVERSATION)
    if entry.options.get('stt_capture_enabled') is True:
        coordinator.platforms.append(Platform.STT)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    from .satellite_runtime import async_setup_satellite_runtime, SatelliteDependencyNotReady
    try:
        await hass.config_entries.async_forward_entry_setups(entry, coordinator.platforms)
        from .echomuse_runtime import async_setup_echomuse_runtime
        coordinator.echomuse_runtime=await async_setup_echomuse_runtime(hass,entry)
        # Restore durable playback holds before any external-audio callback or
        # ring/broadcast worker can submit a new clip to the same MA output.
        coordinator.satellite_runtime=await async_setup_satellite_runtime(hass,entry)
        if (entry.options.get('live_actions_enabled') is True
                and entry.options.get('native_room_ducking_enabled') is True):
            from .native_duck import async_setup_native_duck
            coordinator.native_duck_runtime=await async_setup_native_duck(hass,entry)
        await coordinator.broadcast_runtime.async_start_worker()
        await coordinator.ring_worker.async_start()
        entry.async_on_unload(async_register_llm(hass, entry, coordinator))
        entry.async_on_unload(async_register_media_llm(hass, entry))
        entry.async_on_unload(async_register_broadcast_llm(hass, entry, coordinator))
        entry.async_on_unload(async_register_research_llm(hass, entry, client))
        entry.async_on_unload(entry.add_update_listener(async_reload_entry))
        if 'frontend' in getattr(getattr(hass, 'config', None), 'components', set()):
            from .frontend import async_setup_panel
            await async_setup_panel(hass, entry.entry_id)
        if local:
            from homeassistant.const import EVENT_HOMEASSISTANT_STOP
            async def stop_local_engine(event):
                if coordinator.native_duck_runtime:
                    await coordinator.native_duck_runtime.async_close()
                if coordinator.echomuse_runtime:
                    await coordinator.echomuse_runtime.async_close()
                await coordinator.broadcast_runtime.async_close()
                await coordinator.ring_worker.async_close()
                if coordinator.satellite_runtime:
                    await coordinator.satellite_runtime.stop()
                await client.async_close()
            entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_local_engine))
    except BaseException as error:
        if coordinator.native_duck_runtime:
            await coordinator.native_duck_runtime.async_close()
        if coordinator.echomuse_runtime:
            await coordinator.echomuse_runtime.async_close()
        await coordinator.broadcast_runtime.async_close()
        await coordinator.ring_worker.async_close()
        if coordinator.satellite_runtime:
            await coordinator.satellite_runtime.stop()
        await hass.config_entries.async_unload_platforms(entry,coordinator.platforms)
        hass.data[DOMAIN].pop(entry.entry_id,None)
        if 'frontend' in getattr(getattr(hass, 'config', None), 'components', set()):
            from .frontend import async_unload_panel
            await async_unload_panel(hass, entry.entry_id)
        if local:
            await client.async_close()
        if isinstance(error,SatelliteDependencyNotReady):
            raise ConfigEntryNotReady('Configured satellite audio dependency is temporarily unavailable') from None
        raise
    return True


async def async_unload_entry(hass, entry):
    coordinator=hass.data[DOMAIN].get(entry.entry_id)
    if coordinator is None:
        return True
    if getattr(coordinator,'native_duck_runtime',None):
        await coordinator.native_duck_runtime.async_close()
    if coordinator.echomuse_runtime:
        await coordinator.echomuse_runtime.async_close()
    await coordinator.broadcast_runtime.async_close()
    await coordinator.ring_worker.async_close()
    if coordinator.satellite_runtime:
        await coordinator.satellite_runtime.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, coordinator.platforms)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if 'frontend' in getattr(getattr(hass, 'config', None), 'components', set()):
            from .frontend import async_unload_panel
            await async_unload_panel(hass, entry.entry_id)
        if entry.data.get('mode') == 'embedded':
            await coordinator.client.async_close()
    return unloaded


async def async_reload_entry(hass,entry):
    await hass.config_entries.async_reload(entry.entry_id)
