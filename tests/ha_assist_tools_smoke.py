"""Actual HA native LLM discovery; fixture options/registries only, no house I/O."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, llm


def verify_native_continuation(hass):
    from homeassistant.components.conversation.chat_log import ChatLog, AssistantContent, UserContent
    log=ChatLog(hass,'native-continuation-fixture')
    log.content.append(AssistantContent(agent_id='fixture',content='Which timer: Tea or Pasta?'))
    assert log.continue_conversation is True
    log.content.append(UserContent(content='Tea'))
    assert log.continue_conversation is False
    log.content.append(AssistantContent(agent_id='fixture',content='Added thirty seconds to Tea.'))
    assert log.continue_conversation is False


async def verify_schedule_tools(hass, instance, context, apis):
    """Actual native tool contracts with a fixture authority, never house state."""
    from custom_components.household_voice.llm import HouseholdScheduleAPI
    api=next(api for api in apis if isinstance(api,HouseholdScheduleAPI))
    rows=[{'id':name,'kind':'timer','label':name,'source_room':'fixture-area','target_room':'fixture-area',
           'state':'ringing','revision':3,'due':None,'remaining':None} for name in ('Tea','Pasta')]
    client=SimpleNamespace(schedules=AsyncMock(return_value={'schedules':rows,'events':[],
        'rooms':[{'id':'fixture-area','name':'Fixture area'}],'assistants':[]}),
        dismiss_ringing=AsyncMock(return_value={'ringing_decision':{'dismissed_count':2,
            'dismissed_ids':['Tea','Pasta'],'no_ringing_at_decision':False,'audible_stop_confirmed':False}}),
        mutate=AsyncMock(return_value={'schedule':dict(rows[0],revision=4),'mode':'dry_run'}))
    coordinator=SimpleNamespace(client=client,async_request_refresh=AsyncMock())
    def data(result):return result.data if hasattr(result,'data') else result
    with patch.object(api,'coordinator',coordinator):
        stop=llm.ToolInput(tool_name='household_voice__stop',tool_args={},id='native-stop-fixture')
        stopped=data(await instance.async_call_tool(stop))
        assert stopped['dismissed_count']==2 and stopped['audible_stop_confirmed'] is False
        assert stopped['media_stop_dispatched'] is False
        await instance.async_call_tool(stop)
        assert client.dismiss_ringing.await_count==1
        assert client.dismiss_ringing.call_args.args[1]=={'Tea':3,'Pasta':3}
        for row in rows:row.update(state='active',due=api.clock()+60)
        args={'operation':'add','kind':'timer','seconds':30}
        result=data(await instance.async_call_tool(llm.ToolInput(
            tool_name='household_voice__change_schedule',tool_args=args,id='native-choice-fixture')))
        assert result['reason']=='ambiguous_schedule' and result['choice_token']
        selected=dict(args,schedule_id='Tea',choice_token=result['choice_token'])
        same=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__change_schedule',
            tool_args=selected,id='native-choice-same-request')))
        assert same['reason']=='schedule_choice_invalid_or_expired'
        client.mutate.assert_not_awaited()
        followup=llm.LLMContext(platform='fixture',context=Context(user_id=context.context.user_id),
            language='en',assistant='conversation',device_id=context.device_id)
        later=await llm.async_get_api(hass,'assist',followup)
        result=data(await later.async_call_tool(llm.ToolInput(tool_name='household_voice__change_schedule',
            tool_args=selected,id='native-choice-later-request')))
        assert result['status']=='stored'
        assert client.mutate.call_args.args[1]['expected_revision']==3
        assert client.mutate.await_count==1


async def verify_assist_tools(hass, entry):
    from custom_components.household_voice import knowledge_llm
    from custom_components.household_voice import llm as platform

    apis = [api for api in llm.async_get_apis(hass)
            if getattr(getattr(api, 'entry', None), 'entry_id', None) == entry.entry_id]
    assert len(apis) == 4
    original_entries = [(api, api.entry) for api in apis]
    # Keep the registered API objects, caches and locks. A fixture options view
    # avoids invoking the integration's config-entry reload listener.
    fixture_entry = SimpleNamespace(entry_id=entry.entry_id, disabled_by=None, options={})
    area = ar.async_get(hass).async_create('Native Assist fixture area')
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={('household_voice','native-assist-fixture')})
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    satellite=er.async_get(hass).async_get_or_create('assist_satellite','household_voice','native-assist-fixture',
        config_entry=entry,device_id=device.id)
    context = llm.LLMContext(platform='fixture', context=Context(), language='en',
        assistant='conversation', device_id=device.id)
    try:
        verify_native_continuation(hass)
        for api in apis:
            api.entry = fixture_entry
        assert platform.async_get_tools(hass, context, 'assist') is None
        fixture_entry.options = {'assist_tools_enabled':True, 'knowledge_enabled':True}
        assert platform.async_get_tools(hass, context, 'other-api') is None
        with patch.object(knowledge_llm, 'async_answer', new_callable=AsyncMock) as answer, \
                patch.object(type(hass.services), 'async_call', new_callable=AsyncMock) as service:
            answer.return_value = {'status':'ready', 'speech':'Fixture sourced answer.',
                'sources':[], 'household_actions':False}
            contribution = platform.async_get_tools(hass, context, 'assist')
            # HA's own intent contribution includes its preferred area. Only our
            # capability guidance must remain stable across requesting devices.
            assert 'Native Assist fixture area' not in contribution.prompt
            assert device.id not in contribution.prompt and area.id not in contribution.prompt
            instance = await llm.async_get_api(hass, 'assist', context)
            tools = [tool for tool in instance.tools if tool.name.startswith('household_voice__')]
            assert len(tools) == 13  # Context, four schedule, stop, six research, knowledge.
            assert len({tool.name for tool in tools}) == len(tools)
            assert not any(tool.name.startswith('household_voice__media_') for tool in tools)
            for tool in tools:
                if hasattr(tool, 'tool'):
                    assert tool.tool.api in apis
            answer.assert_not_awaited()
            context_result = await instance.async_call_tool(llm.ToolInput(
                tool_name='household_voice__current_context', tool_args={}, id='native-context'))
            context_data = context_result.data if hasattr(context_result, 'data') else context_result
            assert context_data['area'] == 'Native Assist fixture area'
            assert device.id not in str(context_data) and area.id not in str(context_data)
            service.assert_not_awaited()
            result = await instance.async_call_tool(llm.ToolInput(
                tool_name='household_voice__family_knowledge', tool_args={'question':'Why is the sky blue?'}, id='native-knowledge'))
            data = result.data if hasattr(result, 'data') else result
            assert data['status'] == 'ready' and data['household_actions'] is False
            answer.assert_awaited_once_with(hass, fixture_entry, 'Why is the sky blue?', 'en')
            service.assert_not_awaited()
            await verify_schedule_tools(hass,instance,context,apis)
            service.assert_not_awaited()
            # Enabled media is part of the same builtin Assist API, not a
            # separate conversation agent. This fixture intentionally has no MA
            # profile/output: the actual schema accepts semantics, then the
            # trusted backend asks for configuration without calling a service.
            fixture_entry.options['media_enabled'] = True
            media_instance = await llm.async_get_api(hass, 'assist', context)
            media_names = {tool.name for tool in media_instance.tools
                           if tool.name.startswith('household_voice__media_')}
            assert media_names == {'household_voice__media_play','household_voice__media_resume',
                                   'household_voice__media_pause','household_voice__media_stop',
                                   'household_voice__media_status','household_voice__media_queue_options'}
            assert 'media_type=album' in media_instance.api_prompt
            assert 'shuffle=true' in media_instance.api_prompt
            for number,args in enumerate(({'query':'jazz','selection_mode':'broad_music'},
                    {'query':'Matilda soundtrack','media_type':'album','shuffle':True,'selection_mode':'exact'})):
                result = await media_instance.async_call_tool(llm.ToolInput(
                    tool_name='household_voice__media_play',tool_args=args,id=f'native-media-schema-{number}'))
                data = result.data if hasattr(result,'data') else result
                assert data['reason'] == 'verified_source_and_media_configuration_required'
            service.assert_not_awaited()
            from ha_media_controls_smoke import verify_media_controls
            await verify_media_controls(hass,fixture_entry,context,satellite)
            fixture_entry.options = {}
            result = await tools[0].async_call(hass, llm.ToolInput(tool_name=tools[0].name,
                tool_args={}, id='disabled-stale-tool'), context)
            data = result.data if hasattr(result, 'data') else result
            assert data['reason'] == 'assist_tools_disabled'
            service.assert_not_awaited()
    finally:
        for api, original in original_entries:
            api.entry = original
        er.async_get(hass).async_remove(satellite.entity_id)
        dr.async_get(hass).async_remove_device(device.id)
        ar.async_get(hass).async_delete(area.id)
    return {'native_assist_opt_in_only':True, 'registered_api_objects_reused':True,
            'source_area_from_real_registry':True, 'knowledge_tool_mock_backend_only':True,
            'ringing_stop_fixture_authority_only':True,'choice_requires_later_request':True,
            'native_question_continues_acknowledgement_ends':True,
            'builtin_assist_exposes_enabled_typed_music_and_shuffle':True,
            'household_contacts':False, 'physical_audio':False}
