"""Real HA router lifecycle, delegation and actions with an inert broadcaster."""
import json
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components import conversation
from homeassistant.components.conversation.const import DATA_COMPONENT
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent
from homeassistant.helpers import llm
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
        tool_callback = None

        def __init__(self):
            self.calls = []

        async def async_process(self, user_input):
            self.calls.append(user_input)
            if self.loop_target:
                return await conversation.async_converse(hass, text=user_input.text,
                    conversation_id=user_input.conversation_id, context=user_input.context,
                    agent_id=self.loop_target, language=user_input.language, device_id=user_input.device_id)
            if self.tool_callback:
                await self.tool_callback(user_input)
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
    satellite=er.async_get(hass).async_get_or_create('assist_satellite','esphome','fixture-router-source',
        suggested_object_id='fixture_router',config_entry=owner,device_id=device.id)
    assert satellite.entity_id=='assist_satellite.fixture_router'
    options = {**original_options, 'conversation_fallback_agent': fallback.entity_id,
        'fixture_preserve': 'unchanged', 'satellites': [{'satellite_device_id': device.id,
            'satellite_entity_id': 'assist_satellite.fixture_router', 'room_id': 'test_room'}]}
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    router = next(agent for agent in component.entities if isinstance(agent, HouseholdConversationRouter))
    runtime = hass.data['household_voice'][entry.entry_id].broadcast_runtime
    context = Context()
    utterance_contexts = []

    async def invoke(text, *, source=device.id, language='en', conversation_id=None):
        nonlocal context
        # Assist Pipeline creates a fresh context for each logical utterance,
        # including replies that retain a conversation ID.
        context = Context()
        utterance_contexts.append(context.id)
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
        assert result.response.speech['plain']['speech'] == ''
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
        with patch.object(runtime, 'async_start_inline', new_callable=AsyncMock) as inline:
            inline.return_value = {'status':'queued','reason':'inline_announcement_queued'}
            before = len(fallback.calls)
            announced = await invoke('Make an announcement. Turn off every light.')
            inline.assert_awaited_once()
            assert inline.call_args.kwargs['body'] == 'Turn off every light.'
            assert inline.call_args.kwargs['transcript'] == 'Make an announcement. Turn off every light.'
            assert announced.response.speech['plain']['speech'] == '' and len(fallback.calls) == before
        for text in ['Play my book', '"make an announcement"']:
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
    # Use HA's real registered API bridge; only the tool's external media work is
    # synthetic here. Its source/configuration/execution guards have separate
    # real-HA media fixtures. Verify the router passes the actual source context.
    from custom_components.household_voice import media_llm
    from custom_components.household_voice.intent_scope import trusted_request_text
    from custom_components.household_voice.llm_compat import tool_result
    media_calls = []
    media_result = {'status':'action_accepted','executed':True,'playback_confirmed':False,
        'announcement':'Playing Fixture Jazz.', 'selected':{'uri':'fixture://playlist/1',
        'title':'Fixture Jazz','media_type':'playlist','provider':'fixture'}}
    async def media_call(tool, actual_hass, tool_input, llm_context):
        assert actual_hass is hass
        assert llm_context.context is context and llm_context.device_id == device.id
        assert trusted_request_text(llm_context) == (current_music_text, 'en')
        assert tool_input.tool_args == {'kind':'music','query':current_music_query}
        assert tool_input.id.startswith('ha-router-media-')
        media_calls.append(tool_input)
        return tool_result(llm, data=deepcopy(media_result), error=media_result['status']=='error')
    with patch.object(media_llm.MediaTool, 'async_call', new=media_call):
        before = len(fallback.calls)
        current_music_text, current_music_query = 'Play some jazz.', 'jazz'
        selected = await invoke(current_music_text)
        assert selected.response.speech['plain']['speech'] == 'Playing Fixture Jazz.'
        extra = selected.response.speech['plain']['extra_data']['household_media']
        assert extra['selected']['uri'] == 'fixture://playlist/1' and extra['executed'] is True
        assert 'username' not in extra and 'actions' not in extra
        assert len(fallback.calls) == before and len(media_calls) == 1
        current_music_text, current_music_query = 'Please play some upbeat music.', 'upbeat'
        media_result = {'status':'dry_run','executed':False,'announcement':'Must not speak this.'}
        disabled = await invoke(current_music_text)
        assert 'Enable music playback' in disabled.response.speech['plain']['speech']
        media_result = {'status':'error','reason':'media_playback_pending_or_uncertain',
            'executed':False,'retry_automatically':False}
        pending = await invoke(current_music_text)
        assert 'may still be preparing' in pending.response.speech['plain']['speech']
        count = len(media_calls)
        missing = await invoke(current_music_text, source=None)
        assert 'configured voice assistant' in missing.response.speech['plain']['speech']
        assert len(media_calls) == count and len(fallback.calls) == before
        for text in ('Play Fleetwood Mac.', 'Play jazz in the Kitchen.', 'Play the album Jazz.'):
            await invoke(text)
        assert len(fallback.calls) == before + 3 and len(media_calls) == count
    from custom_components.household_voice import llm as schedule_tools
    from custom_components.household_voice.intent_scope import constrain_arguments
    fake_client = SimpleNamespace()
    posts = []
    rows=[{'id':'ringing-timer','kind':'timer','label':'Fixture','source_room':'test_room',
        'target_room':'test_room','state':'ringing','revision':1,'due':1000,'remaining':None}]
    async def schedules():
        return {'rooms':[{'id':'test_room','name':'Test room'}], 'schedules':deepcopy(rows)}
    async def mutate(service, payload):
        assert service=='command'
        posts.append(deepcopy(payload))
        row=next(row for row in rows if row['id']==payload['identifier'])
        assert row['revision']==payload['expected_revision']
        row['revision']+=1
        if payload['operation']=='dismiss':row['state']='completed'
        if payload['operation']=='set':row['remaining']=payload['seconds']
        return {'schedule':deepcopy(row)}
    fake_client.schedules, fake_client.mutate = schedules, mutate
    fake_coordinator = SimpleNamespace(client=fake_client, async_request_refresh=AsyncMock())
    api = schedule_tools.HouseholdScheduleAPI(hass, entry, fake_coordinator, clock=lambda:1000)
    results = []
    async def omitted_kind_tool(user_input):
        tool_context = llm.LLMContext(platform='fixture', context=user_input.context,
            language=user_input.language, assistant='conversation', device_id=user_input.device_id)
        result = await hass.async_create_task(schedule_tools.ScheduleTool(schedule_tools.NAMES[3], api).async_call(hass,
            llm.ToolInput(tool_name=schedule_tools.NAMES[3],tool_args={'operation':'stop'},id='scope-'+str(len(results))),tool_context))
        results.append(result.data if hasattr(result,'data') else result)
    fallback.tool_callback = omitted_kind_tool
    before=len(fallback.calls)
    with patch.object(router.coordinator,'client',fake_client):
        absent=await invoke('Stop the ringing alarm')
        assert 'could not find' in absent.response.speech['plain']['speech'] and not posts
        rows.append({**rows[0],'id':'ringing-alarm','kind':'alarm','target_room':'other-room'})
        rows.append({**rows[0],'id':'countdown','state':'active'})
        rows.append({**rows[0],'id':'future-alarm','kind':'alarm','state':'active'})
        untrusted=await invoke('stop',source=None)
        assert 'registered voice assistant' in untrusted.response.speech['plain']['speech'] and not posts
        stopped=await invoke('stop')
        assert 'Dismissed 2' in stopped.response.speech['plain']['speech']
        assert {post['identifier'] for post in posts}=={'ringing-timer','ringing-alarm'}
        assert rows[2]['state']==rows[3]['state']=='active' and len(fallback.calls)==before
        # With no ringing state, normal media delegation is restored.
        fallback.tool_callback=None
        await invoke('stop')
        assert len(fallback.calls)==before+1
        rows[:]=[{**rows[2],'id':'one','label':'Timer','state':'paused','remaining':60,'due':None},
                 {**rows[2],'id':'two','label':'Timer','state':'paused','remaining':300,'due':None}]
        count=len(posts);before=len(fallback.calls)
        question=await invoke('Change my timer to five minutes')
        assert question.continue_conversation and '1 minutes' in question.response.speech['plain']['speech']
        assert '5 minutes' in question.response.speech['plain']['speech'] and 'Test room' in question.response.speech['plain']['speech']
        pending_id=question.conversation_id
        unclear=await invoke('that one',conversation_id=pending_id)
        assert unclear.continue_conversation and len(posts)==count and len(fallback.calls)==before
        wrong=await invoke('the first one',conversation_id='01M4THYA7ZF0ZZVCC1QS46VY29')
        assert 'another conversation' in wrong.response.speech['plain']['speech'] and len(posts)==count
        rows[1]['revision']+=1
        stale=await invoke('the second one',conversation_id=pending_id)
        assert stale.continue_conversation and 'changed' in stale.response.speech['plain']['speech'] and len(posts)==count
        selected=await invoke('the second one',conversation_id=pending_id)
        assert not selected.continue_conversation and posts[-1]['identifier']=='two' and posts[-1]['seconds']==300
        assert len(utterance_contexts)==len(set(utterance_contexts))
        assert len(fallback.calls)==before
        renewed=await invoke('Change the timer to two minutes')
        await invoke('Set a new five minute timer',conversation_id=renewed.conversation_id)
        assert len(fallback.calls)==before+1
    async def book_constraint(user_input):
        tool_context = llm.LLMContext(platform='fixture',context=user_input.context,
            language=user_input.language,assistant='conversation',device_id=user_input.device_id)
        assert constrain_arguments('media','resume',{},tool_context) == {'kind':'book'}
    fallback.tool_callback = book_constraint
    await invoke('Continue my audiobook')
    fallback.tool_callback = None
    assert constrain_arguments('media','resume',{},SimpleNamespace(context=context,device_id=device.id)) == {}
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
    # Exercise the actual conversation entity after a real options reload. The
    # knowledge helper alone is mocked: no public search/model call is made.
    knowledge_options = {**dict(entry.options), 'knowledge_enabled':True,
        'knowledge_agent':fallback.entity_id, 'knowledge_prompt':'Explain gently with one short example.'}
    hass.config_entries.async_update_entry(entry, options=knowledge_options)
    await hass.async_block_till_done()
    router = next(agent for agent in component.entities if isinstance(agent, HouseholdConversationRouter))
    runtime = hass.data['household_voice'][entry.entry_id].broadcast_runtime
    from custom_components.household_voice import knowledge_llm
    from custom_components.household_voice.knowledge_policy import family_prompt
    before_fallback = len(fallback.calls)
    before_posts = len(posts)
    question = 'Why does the Moon change shape?'
    history_id = '01M4THYA7ZF0ZZVCC1QS46VY25'
    with patch.object(knowledge_llm, 'async_answer', new_callable=AsyncMock) as answer:
        answer.return_value = {'status':'ready','speech':'We see different amounts of its sunlit half.',
            'sources':[{'title':'NASA Moon phases','url':'https://science.nasa.gov/moon/moon-phases/'}]}
        result = await invoke(question, language='en-US', conversation_id=history_id)
        answer.assert_awaited_once_with(hass, entry, question, 'en-US')
        assert result.response.response_type == intent.IntentResponseType.QUERY_ANSWER
        assert result.conversation_id != history_id and not result.continue_conversation
        card = json.dumps(result.response.card)
        assert 'Answer sources' in card and 'NASA Moon phases' in card and 'https://science.nasa.gov/moon/moon-phases/' in card
        assert len(fallback.calls) == before_fallback and len(posts) == before_posts
        # Positional contract has only current text/language; neither conversation
        # history/id, caller extra prompt, trusted source nor context is forwarded.
        assert len(answer.call_args.args) == 4 and not answer.call_args.kwargs
        for common_question in ['Is Pluto a planet?', 'Does Earth orbit the Sun?', 'Find information about volcanoes.', 'How loud is thunder?']:
            await invoke(common_question, conversation_id=history_id)
            assert answer.call_args.args == (hass, entry, common_question, 'en')
            assert len(fallback.calls) == before_fallback and len(posts) == before_posts
        mixed = 'What is gravity, and turn off every light in the house?'
        await invoke(mixed, conversation_id=history_id)
        assert answer.call_args.args == (hass, entry, mixed, 'en')
        assert len(fallback.calls) == before_fallback and len(posts) == before_posts
        answer.side_effect = RuntimeError('private provider token and household history')
        failure = await invoke('Why is the sky blue?')
        assert 'could not find a reliable answer' in failure.response.speech['plain']['speech']
        assert 'private' not in json.dumps(failure.as_dict())
        assert len(fallback.calls) == before_fallback and len(posts) == before_posts
        answer.side_effect = None
        for malformed in [{'speech':''}, {'speech':'Answer','sources':[{'title':'Missing URL'}]},
                          {'speech':'Answer','sources':'not-a-list'}]:
            answer.return_value = malformed
            failure = await invoke('What is a comet?')
            assert 'could not find a reliable answer' in failure.response.speech['plain']['speech']
            assert len(fallback.calls) == before_fallback
        before_answers = answer.await_count
        with patch.object(runtime, 'async_start_inline', new_callable=AsyncMock) as inline:
            inline.return_value = {'status':'queued','reason':'inline_announcement_queued'}
            announced = await invoke('Make an announcement. What is gravity?')
            inline.assert_awaited_once()
            assert inline.call_args.kwargs['body'] == 'What is gravity?'
            assert announced.response.speech['plain']['speech'] == ''
            assert answer.await_count == before_answers and len(fallback.calls) == before_fallback
        # Family style survives delegation, but does not disable the household
        # agent's actual registered tools. This call still executes ScheduleTool.
        fallback.tool_callback = omitted_kind_tool
        rows[:]=[{**rows[0],'id':'ringing-timer','kind':'timer','state':'ringing','revision':1}]
        await invoke('Dismiss every ringing schedule')
        assert len(posts) == before_posts+1 and posts[-1]['identifier'] == 'ringing-timer'
        assert answer.await_count == before_answers
        prompt = fallback.calls[-1].extra_system_prompt
        assert prompt == 'Fixture prompt\n\n'+family_prompt(knowledge_options)
        assert 'Use only tools authorized for the actual household request.' in prompt
        assert 'You have no tools' not in prompt
        fallback.tool_callback = None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not any(isinstance(agent, HouseholdConversationRouter) for agent in component.entities)
    hass.config_entries.async_update_entry(entry, options=original_options)
    return {'router_real_ha_lifecycle_and_options': True,
        'router_commands_call_broadcast_once': True, 'router_delegation_context_and_history_preserved': True,
        'router_missing_source_failure_and_recursion_guarded': True,
        'phone_presence_options_validated_without_automatic_change': True,
        'knowledge_lane_real_router_no_action_fallback_on_errors_or_mixed_question': True,
        'knowledge_sources_card_and_current_question_only': True,
        'knowledge_announcement_precedence_and_family_prompt_tool_delegation': True,
        'knowledge_is_common_english_routing_not_full_nlu_isolation': True,
        'router_category_scope_prevents_wrong_schedule_and_binds_book_resume': True,
        'ringing_stop_trusted_housewide_no_countdown_changes':True,
        'timer_clarification_followup_exact_revision_and_remaining_display':True,
        'no_ringing_stop_delegates_and_explicit_new_timer_delegates':True,
        'broad_music_registered_api_direct_route_and_scoped_result':True,
        'broad_music_no_model_on_success_dryrun_pending_or_missing_source':True,
        'physical_router_audio': False}
