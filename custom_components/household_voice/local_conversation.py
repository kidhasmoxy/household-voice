"""One selective front door to the existing shared agent and household tools.

No global intent replacement, separate scheduler, or media interpretation. The
pipeline must keep prefer_local_intents off so Core cannot intercept timers.
"""
import hashlib
import json
from contextvars import ContextVar
from dataclasses import replace

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import ChatLog
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent, llm
from homeassistant.util import ulid as ulid_util

from .local_commands import clock_request, exclude_intent, timer_request
from .schedule_language import bare_stop

_ACTIVE = ContextVar('household_voice_local_active', default=False)


class HouseholdLocalConversation(conversation.ConversationEntity):
    _attr_name = 'Household Voice fast commands'
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL
    # Fallback uses the pipeline's active chat-log delta listener. Advertising
    # streaming keeps Core's existing TTS stream available to the shared agent;
    # local replies with no deltas still use Core's normal final-speech path.
    _attr_supports_streaming = True
    supported_languages = '*'

    def __init__(self, entry, coordinator):
        self.entry, self.coordinator = entry, coordinator
        self._attr_unique_id = entry.entry_id + '_local_commands'

    def reply(self, request, speech, lane):
        response = intent.IntentResponse(language=request.language)
        response.async_set_speech(speech, extra_data={'household_voice_path': lane})
        return conversation.ConversationResult(response=response,
            conversation_id=request.conversation_id or ulid_util.ulid_now(),
            continue_conversation=False)

    def trusted(self, request):
        device = dr.async_get(self.hass).async_get(request.device_id) if request.device_id else None
        if device is None or device.disabled_by is not None or not getattr(request.context, 'id', None):
            return False
        rows = [row for row in er.async_get(self.hass).entities.values()
                if row.device_id == device.id and row.entity_id.startswith('assist_satellite.')
                and row.disabled_by is None]
        satellite = getattr(request, 'satellite_id', None)
        return any(row.entity_id == satellite for row in rows) if satellite else len(rows) == 1

    async def fallback(self, request):
        target = self.entry.options.get('local_commands_agent')
        row = er.async_get(self.hass).async_get(target) if target else None
        if not row or row.disabled_by is not None or row.platform == 'household_voice':
            return self.reply(request, 'The shared conversation agent is unavailable.', 'unavailable')
        # Return the entire result: preserve choices, cards, conversation ID and
        # follow-up listening. Never send failed/uncertain actions for a retry.
        return await conversation.async_converse(self.hass, text=request.text,
            conversation_id=request.conversation_id, context=request.context,
            language=request.language, agent_id=target, device_id=request.device_id,
            satellite_id=request.satellite_id, extra_system_prompt=request.extra_system_prompt)

    async def async_process(self, request):
        if _ACTIVE.get():
            return self.reply(request, 'The conversation configuration loops back to itself.', 'unavailable')
        token = _ACTIVE.set(True)
        try:
            if (self.entry.options.get('local_commands_enabled') is not True
                    or self.entry.options.get('assist_tools_enabled') is not True
                    or getattr(self.entry, 'disabled_by', None) is not None):
                return self.reply(request, 'Fast commands are disabled. Select the shared agent.', 'disabled')
            authorities = self.hass.data.get('household_voice', {})
            if len(authorities) != 1 or authorities.get(self.entry.entry_id) is not self.coordinator:
                return self.reply(request, 'Choose one Household Voice controller.', 'unavailable')
            timer = timer_request(request.text, request.language)
            stop = bare_stop(request.text, request.language)
            if timer is not None or stop:
                if not self.trusted(request):
                    return self.reply(request, 'Please use a configured voice assistant.', 'source_required')
                return await self.household_action(request, timer, stop)
            from .schedule_control import get_schedule_control
            selection = await get_schedule_control(self.hass).route(
                self.coordinator.client, self.entry.entry_id, request,
                trusted=self.trusted(request),
                conversation_id=request.conversation_id or ulid_util.ulid_now(), snooze_only=True)
            if selection is not None:
                result = self.reply(request, selection['speech'], 'snooze')
                result.conversation_id = selection.get('conversation_id') or result.conversation_id
                result.continue_conversation = selection['continue_conversation']
                return result
            # The existing sentence trigger owns announcement recording in the
            # pipeline. Also support direct conversation calls using that exact
            # callback, without reimplementing recording or acknowledgement.
            from .conversation_router import command
            if command(request.text, request.language) == 'start':
                from .broadcast_trigger import async_handle
                speech = await async_handle(self.hass, self.entry, self.coordinator, request)
                return self.reply(request, speech, 'announcement')
            # Native strict recognition retains HA exposure and authorization.
            # A reported action error stays local; no automatic second attempt.
            try:
                native_request = replace(request, text=text) if (text := clock_request(request.text, request.language)) else request
                response = await conversation.async_handle_intents(self.hass, native_request,
                    ChatLog(self.hass, request.conversation_id or ulid_util.ulid_now()),
                    intent_filter=exclude_intent)
            except Exception:
                return self.reply(request, 'I could not confirm that command completed.', 'uncertain')
            if response is not None:
                return conversation.ConversationResult(response=response,
                    conversation_id=request.conversation_id or ulid_util.ulid_now(),
                    continue_conversation=False)
            # Reuse the exact catalog and room guards, bypassing model inference
            # for complete playback requests. Unsupported constraints stay intact.
            from .conversation_router import music_request
            from .media_llm import _MUSIC_STYLES, _resolve
            parsed = music_request(request.text, request.language, _MUSIC_STYLES)
            if parsed is not None:
                if not self.trusted(request):
                    return self.reply(request, 'Please use a configured voice assistant.', 'source_required')
                if target := parsed.get('target_room'):
                    try:
                        _resolve(self.hass, self.entry, request.as_llm_context('household_voice'), target)
                    except Exception:
                        return await self.fallback(request)
                from .conversation import HouseholdConversationRouter
                router = HouseholdConversationRouter(self.entry, self.coordinator)
                router.hass = self.hass
                scoped = replace(request, conversation_id=request.conversation_id or ulid_util.ulid_now())
                result = await router._media_reply(scoped, parsed)
                # Saved catalog choices are bound to this same conversation.
                result.conversation_id = scoped.conversation_id
                return result
            return await self.fallback(request)
        finally:
            _ACTIVE.reset(token)

    async def household_action(self, request, timer, stop):
        from .llm import HouseholdScheduleAPI, ScheduleTool
        from .schedule_stop import HouseholdStopTool
        from .intent_scope import bind_request
        apis = [api for api in llm.async_get_apis(self.hass)
                if isinstance(api, HouseholdScheduleAPI) and api.entry.entry_id == self.entry.entry_id]
        if len(apis) != 1:
            return self.reply(request, 'Household commands are unavailable right now.', 'unavailable')
        tool = HouseholdStopTool(apis[0]) if stop else ScheduleTool('household_create_timer', apis[0])
        key = 'local-' + hashlib.sha256(json.dumps([self.entry.entry_id,
            request.context.id, request.device_id, tool.name, timer], sort_keys=True).encode()).hexdigest()
        try:
            with bind_request(request.text, request.language, request.context, request.device_id):
                result = await tool.async_call(self.hass,
                    llm.ToolInput(tool_name=tool.name, tool_args={} if stop else timer, id=key),
                    request.as_llm_context('household_voice'))
            data = result.data if hasattr(result, 'data') else result
        except Exception:
            return self.reply(request, 'I could not confirm that command completed.', 'uncertain')
        if data.get('status') == 'stored':
            if stop:
                speech = 'Stopped the ringing alarms and timers.'
            elif data.get('audio_delivery') == 'dry_run':
                speech = 'Timer saved for testing; it will not ring.'
            else:
                speech = 'Timer set.'
        elif stop and data.get('status') == 'action_accepted':
            speech = 'Stopped.'
        elif stop and data.get('status') == 'noop':
            speech = 'Nothing is ringing.'
        else:
            speech = data.get('question') or 'I could not complete that command. Check the assistant speaker settings.'
        return self.reply(request, speech, 'stop' if stop else 'timer')
