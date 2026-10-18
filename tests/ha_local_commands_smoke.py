"""Actual HA matcher, adapter and tool contracts with inert household backends."""
import asyncio
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant import loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components import conversation
from homeassistant.components.conversation.const import DATA_COMPONENT
from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import __version__
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent, llm
from homeassistant.setup import async_setup_component


async def verify(hass):
    from custom_components.household_voice.local_conversation import HouseholdLocalConversation
    from custom_components.household_voice.llm import HouseholdScheduleAPI, ScheduleTool
    from custom_components.household_voice.schedule_stop import HouseholdStopTool
    from custom_components.household_voice.local_commands import exclude_intent

    assert await async_setup_component(hass, 'homeassistant', {})
    assert await async_setup_component(hass, 'conversation', {})
    assert await async_setup_component(hass, 'light', {})
    class Lamp(LightEntity):
        _attr_name = 'Fixture lamp'
        _attr_unique_id = 'local-lamp'
        _attr_supported_color_modes = {ColorMode.ONOFF}
        _attr_color_mode = ColorMode.ONOFF
        _attr_is_on = False
        calls = 0
        async def async_turn_on(self, **kwargs):
            self.calls += 1
            self._attr_is_on = True
            self.async_write_ha_state()
    lamp = Lamp()
    await hass.data['light'].async_add_entities([lamp])
    owner = ConfigEntry(domain='esphome', title='Fixture', data={}, options={},
        source='user', version=1, minor_version=1, unique_id=None,
        discovery_keys=MappingProxyType({}), subentries_data=None)
    hass.config_entries._entries[owner.entry_id] = owner
    device = dr.async_get(hass).async_get_or_create(config_entry_id=owner.entry_id,
        identifiers={('esphome','local-fixture')})
    satellite = er.async_get(hass).async_get_or_create('assist_satellite','esphome','local-fixture',
        config_entry=owner, device_id=device.id)
    class Fallback(conversation.ConversationEntity):
        _attr_name = 'Fixture AI'
        _attr_unique_id = 'local-fallback'
        supported_languages = '*'
        calls = []
        async def async_process(self, request):
            self.calls.append(request)
            response = intent.IntentResponse(language=request.language)
            response.async_set_speech('Which one?')
            return conversation.ConversationResult(response=response,
                conversation_id='01TEST00000000000000000001', continue_conversation=True)
    fallback = Fallback()
    await hass.data[DATA_COMPONENT].async_add_entities([fallback])
    entry = SimpleNamespace(entry_id='fixture', disabled_by=None, options={
        'assist_tools_enabled': True, 'local_commands_enabled': True,
        'local_commands_agent': fallback.entity_id, 'announcements_enabled': True,
        'native_recording_enabled': True})
    coordinator = SimpleNamespace(broadcast_runtime=SimpleNamespace(async_start=AsyncMock(return_value={'status':'queued'})))
    hass.data['household_voice'] = {entry.entry_id: coordinator}
    router = HouseholdLocalConversation(entry, coordinator)
    assert router.supports_streaming
    await hass.data[DATA_COMPONENT].async_add_entities([router])
    api = HouseholdScheduleAPI(hass, entry, coordinator)
    async def ask(text, source=device.id, context=None):
        return await conversation.async_converse(hass, text=text,
            conversation_id='01TEST00000000000000000002', context=context or Context(),
            language='en', agent_id=router.entity_id, device_id=source,
            satellite_id=satellite.entity_id, extra_system_prompt='Keep this context')
    with patch.object(llm, 'async_get_apis', return_value=[api]), \
         patch.object(ScheduleTool, 'async_call', new_callable=AsyncMock) as create, \
         patch.object(HouseholdStopTool, 'async_call', new_callable=AsyncMock) as stop:
        create.return_value = {'status':'stored','audio_delivery':'not_verified'}
        context = Context()
        result = await ask('set a five minute timer', context=context)
        assert result.response.speech['plain']['speech'] == 'Timer set.'
        assert create.call_args.args[1].tool_args == {'seconds':300}
        assert create.call_args.args[2].device_id == device.id
        key = create.call_args.args[1].id
        await ask('set a five minute timer', context=context)
        assert create.call_args.args[1].id == key
        await ask('set a five minute timer')
        assert create.call_args.args[1].id != key
        assert not fallback.calls
        stop.return_value = {'status':'stored','reason':'ringing_schedules_dismissed'}
        await ask('stop')
        assert stop.call_args.args[1].tool_args == {}
        await ask('stop', source='unknown')
        assert stop.await_count == 1
        create.side_effect = RuntimeError('uncertain')
        result = await ask('set a five minute timer')
        assert 'could not confirm' in result.response.speech['plain']['speech']
        assert not fallback.calls
        create.side_effect = None
        create.return_value = {'status':'stored','audio_delivery':'dry_run'}
        result = await ask('set a five minute timer')
        assert 'will not ring' in result.response.speech['plain']['speech']
        await ask('broadcast')
        coordinator.broadcast_runtime.async_start.assert_awaited_once()
        await ask('turn on fixture lamp')
        assert lamp.calls == 1 and not fallback.calls
        for text in ['what time is it', 'what is the time', 'tell me the time']:
            result = await ask(text)
            assert not result.response.error_code
            assert result.response.speech['plain']['speech']
            assert not fallback.calls, 'Current time must never wait for the AI'
        for text in ['set an alarm for seven', 'set a timer for five minutes in the bedroom',
                     'play jazz', 'the second one', 'add five minutes to my timer']:
            result = await ask(text)
            assert result.continue_conversation
            assert result.conversation_id == '01TEST00000000000000000001'
            assert fallback.calls[-1].text == text
            assert fallback.calls[-1].device_id == device.id
            assert fallback.calls[-1].extra_system_prompt == 'Keep this context'
        # An execution error is terminal, never delegated for another action.
        response = intent.IntentResponse(language='en')
        response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, 'Failed')
        count = len(fallback.calls)
        with patch.object(conversation, 'async_handle_intents', return_value=response):
            result = await ask('turn on fixture lamp')
            assert result.response is response
        assert len(fallback.calls) == count
        for name in ['HassStartTimer','HassMediaSearchAndPlay','HassMediaStop','UnknownFutureIntent']:
            assert exclude_intent(SimpleNamespace(intent=SimpleNamespace(name=name)))
        entry.options['local_commands_enabled'] = False
        await ask('turn on fixture lamp')
        assert lamp.calls == 1 and len(fallback.calls) == count
    print(json.dumps({'ha':__version__, 'native_light_calls':lamp.calls,
        'native_time_without_ai':True,
        'timer_and_stop_backend_contracts':True,'fallback_context_and_continuation':True,
        'uncertainty_not_retried':True,'trusted_source_required':True,'physical_audio':False}))


async def main():
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        shutil.copytree(Path(__file__).resolve().parents[1]/'custom_components', Path(directory)/'custom_components')
        sys.path.insert(0, directory)
        hass = HomeAssistant(directory); hass.config.skip_pip = True
        loader.async_setup(hass); hass.config_entries = ConfigEntries(hass, {})
        try:
            assert await async_load_base_functionality(hass)
            hass.auth = await auth_manager_from_config(hass, [], [])
            await verify(hass)
        finally:
            await hass.async_stop(force=True)

if __name__ == '__main__':
    asyncio.run(main())
