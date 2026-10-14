"""Admin-only discovery of actual Assist tool schemas; never executes tools."""
from collections import Counter
from copy import deepcopy

import probatio
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.helpers import device_registry as dr, entity_registry as er, llm

from .assist_prompt import render_prompt


async def async_inventory(hass, *, entry_id, assistant_entity_id, api_ids, context):
    """Discover with authenticated request identity and a registered source device."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if (entry is None or entry.domain != 'household_voice' or entry.disabled_by is not None
            or getattr(entry.state, 'value', entry.state) != 'loaded'):
        raise ValueError('Household Voice entry is not loaded')
    source = er.async_get(hass).async_get(assistant_entity_id)
    if (source is None or not source.entity_id.startswith('assist_satellite.')
            or source.disabled_by is not None or not source.device_id):
        raise ValueError('An enabled registered voice satellite is required')
    device = dr.async_get(hass).async_get(source.device_id)
    if device is None or device.disabled_by is not None:
        raise ValueError('The source device is unavailable')
    if context is None or not context.user_id:
        raise ValueError('Authenticated request context is required')
    registered = {api.id for api in llm.async_get_apis(hass)}
    if (not isinstance(api_ids, list) or not 1 <= len(api_ids) <= 8
            or any(not isinstance(value, str) or value not in registered for value in api_ids)
            or len(set(api_ids)) != len(api_ids)):
        raise ValueError('Select one to eight distinct registered APIs')
    request = llm.LLMContext(platform='local_openai', context=context,
        language=hass.config.language, assistant='conversation', device_id=device.id)
    instance = await llm.async_get_api(hass, api_ids, request)
    tools = []
    for tool in instance.tools:
        # Match Local OpenAI 1.12.1 _format_tool, including its top-level cleanup.
        parameters = deepcopy(probatio.to_openapi(tool.parameters,
            custom_serializer=instance.custom_serializer))
        for key in ('allOf', 'anyOf', 'oneOf'):
            parameters.pop(key, None)
        tools.append({'type':'function', 'function':{'name':tool.name,
            'description':tool.description if tool.description and tool.description.strip()
                          else 'A callable function', 'parameters':parameters}})
    counts = Counter(tool['function']['name'] for tool in tools)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    # Render from the actual merged names, never from a private API prompt.
    guidance = render_prompt([name for name, count in counts.items() if count == 1])
    return {'api_ids':list(api_ids), 'tools':tools, 'tool_count':len(tools),
        'duplicate_names':duplicates,
        'warnings':['Duplicate tool names make dispatch ambiguous'] if duplicates else [],
        'household_guidance':guidance, 'tool_calls_performed':False,
        'read_only_enforcement_verified':False}


@websocket_api.websocket_command({vol.Required('type'):'household_voice/assist_inventory',
    vol.Required('entry_id'):str, vol.Required('assistant_entity_id'):str,
    vol.Required('api_ids'):vol.All([str], vol.Length(min=1, max=8))})
@websocket_api.async_response
async def websocket_inventory(hass, connection, msg):
    """Return schema discovery only to an authenticated administrator."""
    if connection.user is None or not connection.user.is_admin:
        connection.send_error(msg['id'], 'unauthorized', 'Administrator access required')
        return
    try:
        result = await async_inventory(hass, entry_id=msg['entry_id'],
            assistant_entity_id=msg['assistant_entity_id'], api_ids=msg['api_ids'],
            context=connection.context(msg))
    except Exception:
        # API failures can contain private templates, URLs or remote auth details.
        connection.send_error(msg['id'], 'unavailable',
            'Inventory unavailable; check the entry, source assistant and selected APIs')
        return
    connection.send_result(msg['id'], result)


def async_register(hass):
    """Register once at integration setup; parent owns lifecycle wiring."""
    websocket_api.async_register_command(hass, websocket_inventory)
