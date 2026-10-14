"""Actual HA native LLM discovery; fixture options/registries only, no house I/O."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context
from homeassistant.helpers import area_registry as ar, device_registry as dr, llm


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
    context = llm.LLMContext(platform='fixture', context=Context(), language='en',
        assistant='conversation', device_id=device.id)
    try:
        for api in apis:
            api.entry = fixture_entry
        assert platform.async_get_tools(hass, context, 'assist') is None
        fixture_entry.options = {'assist_tools_enabled':True, 'knowledge_enabled':True}
        assert platform.async_get_tools(hass, context, 'other-api') is None
        with patch.object(knowledge_llm, 'async_answer', new_callable=AsyncMock) as answer, \
                patch.object(hass.services, 'async_call', new_callable=AsyncMock) as service:
            answer.return_value = {'status':'ready', 'speech':'Fixture sourced answer.',
                'sources':[], 'household_actions':False}
            contribution = platform.async_get_tools(hass, context, 'assist')
            # HA's own intent contribution includes its preferred area. Only our
            # capability guidance must remain stable across requesting devices.
            assert 'Native Assist fixture area' not in contribution.prompt
            assert device.id not in contribution.prompt and area.id not in contribution.prompt
            instance = await llm.async_get_api(hass, 'assist', context)
            tools = [tool for tool in instance.tools if tool.name.startswith('household_voice__')]
            assert len(tools) == 12  # Context, four schedule, six research, one knowledge.
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
            fixture_entry.options = {}
            result = await tools[0].async_call(hass, llm.ToolInput(tool_name=tools[0].name,
                tool_args={}, id='disabled-stale-tool'), context)
            data = result.data if hasattr(result, 'data') else result
            assert data['reason'] == 'assist_tools_disabled'
            service.assert_not_awaited()
    finally:
        for api, original in original_entries:
            api.entry = original
        dr.async_get(hass).async_remove_device(device.id)
        ar.async_get(hass).async_delete(area.id)
    return {'native_assist_opt_in_only':True, 'registered_api_objects_reused':True,
            'source_area_from_real_registry':True, 'knowledge_tool_mock_backend_only':True,
            'household_contacts':False, 'physical_audio':False}
