"""Real HA router lifecycle, delegation and actions with an inert broadcaster."""
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.components import conversation
from homeassistant.components.conversation.const import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr, intent
from homeassistant.setup import async_setup_component


async def verify_router(hass, entry):
    """Called with the embedded config entry unloaded; restore it when finished."""
    from custom_components.household_voice.conversation import HouseholdConversationRouter
    original_options = dict(entry.options)
    assert not any(isinstance(agent, HouseholdConversationRouter)
                   for agent in hass.data.get(DATA_COMPONENT, ()).entities) if DATA_COMPONENT in hass.data else True
    assert await async_setup_component(hass, 'conversation', {})
    component = hass.data[DATA_COMPONENT]

    class Fallback(conversation.ConversationEntity):
        _attr_name = 'Fixture fallback'
        _attr_unique_id = 'fixture-fallback'
        supported_languages = '*'
        loop_target = None

        def __init__(self):
            self.calls = []

        async def async_process(self, user_input):
            self.calls.append(user_input)
            if self.loop_target:
                return await conversation.async_converse(hass, text=user_input.text,
                    conversation_id=user_input.conversation_id, context=user_input.context,
                    agent_id=self.loop_target, language=user_input.language, device_id=user_input.device_id)
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_speech('Fixture delegated answer')
            return conversation.ConversationResult(response=response,
                conversation_id='01M4THYA7ZF0ZZVCC1QS46VY23', continue_conversation=True)

    fallback = Fallback()
    await component.async_add_entities([fallback])
    owner = ConfigEntry(domain='esphome', title='Router fixture source', data={}, options={},
        source='user', version=1, minor_version=1, unique_id=None,
        discovery_keys=MappingProxyType({}), subentries_data=None)
    hass.config_entries._entries[owner.entry_id] = owner
    device = dr.async_get(hass).async_get_or_create(config_entry_id=owner.entry_id,
        identifiers={('esphome', 'fixture-router-source')})
    options = {**original_options, 'conversation_fallback_agent': fallback.entity_id,
        'fixture_preserve': 'unchanged', 'satellites': [{'satellite_device_id': device.id,
            'satellite_entity_id': 'assist_satellite.fixture_router', 'room_id': 'test_room'}]}
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    router = next(agent for agent in component.entities if isinstance(agent, HouseholdConversationRouter))
    runtime = hass.data['household_voice'][entry.entry_id].broadcast_runtime
    context = Context()

    async def invoke(text, *, source=device.id, language='en', conversation_id=None):
        return await conversation.async_converse(hass, text=text, conversation_id=conversation_id,
            context=context, language=language, agent_id=router.entity_id, device_id=source,
            satellite_id='assist_satellite.fixture_router' if source else None,
            extra_system_prompt='Fixture prompt')

    with patch.object(runtime, 'async_start', new_callable=AsyncMock) as start, \
            patch.object(runtime, 'async_cancel', new_callable=AsyncMock) as cancel, \
            patch.object(runtime, 'async_status', new_callable=AsyncMock) as status:
        start.return_value = {'status': 'queued', 'reason': 'wait_for_prompt_then_speak_announcement'}
        cancel.return_value = {'status': 'cancelled'}
        status.return_value = {'status': 'no_change'}
        result = await invoke('Please make an announcement.', conversation_id='01M4THYA7ZF0ZZVCC1QS46VY22')
        start.assert_awaited_once()
        assert start.call_args.args == (device.id, context)
        assert result.conversation_id != '01M4THYA7ZF0ZZVCC1QS46VY22'
        assert not result.continue_conversation and not fallback.calls
        assert result.response.speech['plain']['speech'] == 'Wait for the announcement prompt, then say your message.'
        await invoke('cancel the announcement')
        await invoke('announcement status')
        cancel.assert_awaited_once_with(device.id)
        status.assert_awaited_once_with(device.id)
        missing = await invoke('make an announcement', source=None)
        assert 'configured voice satellite' in missing.response.speech['plain']['speech']
        assert start.await_count == 1
        start.side_effect = RuntimeError('private runtime details')
        failed = await invoke('make an announcement')
        assert 'could not start' in failed.response.speech['plain']['speech']
        assert 'private' not in failed.response.speech['plain']['speech']
        for text in ['Play my book', '"make an announcement"', 'Make an announcement. Dinner is ready.']:
            delegated = await invoke(text, language='en-US', conversation_id='01M4THYA7ZF0ZZVCC1QS46VY24')
            actual = fallback.calls[-1]
            assert actual.text == text and actual.context is context and actual.device_id == device.id
            assert actual.satellite_id == 'assist_satellite.fixture_router' and actual.language == 'en-US'
            assert actual.conversation_id == '01M4THYA7ZF0ZZVCC1QS46VY24'
            assert actual.extra_system_prompt == 'Fixture prompt'
            assert delegated.conversation_id == '01M4THYA7ZF0ZZVCC1QS46VY23' and delegated.continue_conversation
        assert start.await_count == 2
        fallback.loop_target = router.entity_id
        looping = await invoke('Ordinary request')
        assert 'loops back' in looping.response.speech['plain']['speech']
        fallback.loop_target = None
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow['flow_id'], {'next_step_id': 'conversation_routing'})
    assert flow['step_id'] == 'conversation_routing'
    rejected = await hass.config_entries.options.async_configure(flow['flow_id'], {'conversation_fallback_agent': router.entity_id})
    assert rejected['errors']['base'] == 'invalid_conversation_fallback'
    accepted = await hass.config_entries.options.async_configure(flow['flow_id'], {'conversation_fallback_agent': fallback.entity_id})
    assert accepted['type'] == 'menu'
    assert entry.options['fixture_preserve'] == 'unchanged'
    announcement_form = await hass.config_entries.options.async_configure(flow['flow_id'], {'next_step_id': 'announcements'})
    assert announcement_form['step_id'] == 'announcements'
    assert any(str(key) == 'phone_presence_policy' for key in announcement_form['data_schema'].schema)
    from homeassistant.data_entry_flow import InvalidData
    try:
        await hass.config_entries.options.async_configure(flow['flow_id'], {'phone_presence_policy': 'always'})
    except InvalidData:
        pass
    else:
        raise AssertionError('Native selector accepted an invalid phone presence policy')
    accepted_presence = await hass.config_entries.options.async_configure(flow['flow_id'], {'phone_presence_policy': 'current_state'})
    assert accepted_presence['type'] == 'menu'
    assert entry.options.get('phone_presence_policy', 'recent_five_minutes') == 'recent_five_minutes'
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not any(isinstance(agent, HouseholdConversationRouter) for agent in component.entities)
    hass.config_entries.async_update_entry(entry, options=original_options)
    return {'router_real_ha_lifecycle_and_options': True,
        'router_commands_call_broadcast_once': True, 'router_delegation_context_and_history_preserved': True,
        'router_missing_source_failure_and_recursion_guarded': True,
        'phone_presence_options_validated_without_automatic_change': True, 'physical_router_audio': False}
