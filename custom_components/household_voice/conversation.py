"""Optional exact-command announcement router with an existing agent fallback."""
from contextvars import ContextVar
import hashlib
import json

from homeassistant.components import conversation
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent, llm
from homeassistant.util import ulid as ulid_util

from .const import DOMAIN
from .conversation_router import (acknowledgement, command, inline_announcement,
                                  music_acknowledgement, media_action_request, music_request)
from .intent_scope import bind_request
from .knowledge_router import knowledge_question
from .knowledge_policy import family_prompt
from . import knowledge_llm
from .media_llm import _MUSIC_STYLES, _resolve as resolve_media_room
from .schedule_control import get_schedule_control

_ACTIVE_ROUTERS = ContextVar('household_voice_active_conversation_routers', default=frozenset())


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.options.get('local_commands_enabled') is True:
        from .local_conversation import HouseholdLocalConversation
        async_add_entities([HouseholdLocalConversation(entry, hass.data[DOMAIN][entry.entry_id])])
    if entry.options.get('conversation_fallback_agent'):
        async_add_entities([HouseholdConversationRouter(entry, hass.data[DOMAIN][entry.entry_id])])


class HouseholdConversationRouter(conversation.ConversationEntity):
    _attr_name = 'Household Voice router'
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, entry, coordinator):
        self.entry, self.coordinator = entry, coordinator
        self._attr_unique_id = entry.entry_id + '_conversation_router'

    @property
    def supported_languages(self):
        # Only English phrases are intercepted; the fallback owns other languages.
        return '*'

    def _reply(self, user_input, speech):
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(speech)
        # A deterministic command starts a clean session instead of retaining a
        # prior model's false acknowledgement as future conversation history.
        return conversation.ConversationResult(response=response,
            conversation_id=ulid_util.ulid_now(), continue_conversation=False)

    def _trusted_source(self, user_input):
        if not user_input.context or not getattr(user_input.context, 'id', None):
            return False
        device = dr.async_get(self.hass).async_get(user_input.device_id) if user_input.device_id else None
        mappings = [row for row in self.entry.options.get('satellites', [])
                    if row.get('satellite_device_id') == user_input.device_id]
        if device is None or device.disabled_by is not None or len(mappings) != 1:
            return False
        if user_input.satellite_id and user_input.satellite_id != mappings[0].get('satellite_entity_id'):
            return False
        return True

    def _trusted_schedule_source(self,user_input):
        """Registered assistant identity, independent of its current output pairing."""
        if not getattr(user_input.context,'id',None) or not user_input.device_id:
            return False
        device=dr.async_get(self.hass).async_get(user_input.device_id)
        if device is None or device.disabled_by is not None:return False
        registry=er.async_get(self.hass)
        assistants=[row for row in er.async_entries_for_device(registry,device.id)
            if row.entity_id.startswith('assist_satellite.') and row.disabled_by is None]
        if user_input.satellite_id:
            return any(row.entity_id==user_input.satellite_id for row in assistants)
        return len(assistants)==1

    async def _knowledge_reply(self, user_input):
        # No history, household context or caller prompt crosses this boundary.
        # Failure stays in this lane; never delegate it to a tool-enabled agent.
        try:
            answer = await knowledge_llm.async_answer(self.hass, self.entry,
                user_input.text, user_input.language)
            speech = answer['speech']
            if not isinstance(speech, str) or not speech.strip():
                raise ValueError('invalid_knowledge_speech')
            sources = answer.get('sources', [])
            if (not isinstance(sources, list) or len(sources) > 5
                    or any(not isinstance(row, dict)
                        or not isinstance(row.get('title'), str)
                        or not isinstance(row.get('url'), str) for row in sources)):
                raise ValueError('invalid_knowledge_sources')
        except Exception:
            answer = {}
            speech = 'I could not find a reliable answer right now. Please try again later.'
        result = self._reply(user_input, speech)
        result.response.response_type = intent.IntentResponseType.QUERY_ANSWER
        sources = answer.get('sources', [])
        if sources:
            result.response.async_set_card('Answer sources', '\n\n'.join(
                row['title'] + '\n' + row['url'] for row in sources))
        return result

    async def _media_reply(self, user_input, query):
        """Use the registered media API's source/configuration/idempotency guards."""
        if not self._trusted_source(user_input):
            return self._reply(user_input, 'Use a configured voice assistant to play music in its room.')
        arguments = {'kind':'music', **(query if isinstance(query, dict) else {'query':query})}
        context = user_input.as_llm_context(DOMAIN)
        if target := arguments.get('target_room'):
            try:
                _, room, _, _ = resolve_media_room(self.hass, self.entry, context, target)
                arguments['target_room'] = room['room_id']
            except Exception:
                return self._reply(user_input, 'I could not match that room to a configured music speaker. Check the room name and speaker pairing.')
        call_id = 'ha-router-media-' + hashlib.sha256(json.dumps([
            self.entry.entry_id, user_input.context.id, user_input.device_id, arguments
        ], sort_keys=True).encode()).hexdigest()
        try:
            with bind_request(user_input.text, user_input.language, user_input.context, user_input.device_id):
                api = await llm.async_get_api(self.hass, 'household_voice_media_' + self.entry.entry_id, context)
                result = await api.async_call_tool(llm.ToolInput(tool_name='household_media_play',
                    tool_args=arguments, id=call_id))
                data = result.data if hasattr(result, 'data') else result
        except Exception:
            data = {'status': 'error'}
        speech = music_acknowledgement(data)
        reply = self._reply(user_input, speech)
        if isinstance(data, dict):
            evidence = {key: data[key] for key in ('status', 'reason', 'executed',
                'playback_confirmed', 'retry_automatically', 'playback_dispatch_started',
                'mutation_dispatch_started', 'timeout_phase',
                'playback_kind', 'track_count', 'actions_issued') if key in data}
            if isinstance(data.get('selected'), dict):
                evidence['selected'] = {key: data['selected'][key]
                    for key in ('uri', 'title', 'media_type', 'provider') if key in data['selected']}
            reply.response.async_set_speech(speech, extra_data={'household_media': evidence})
        return reply

    async def async_process(self, user_input):
        active = _ACTIVE_ROUTERS.get()
        if self.entry.entry_id in active:
            return self._reply(user_input, 'The conversation routing configuration loops back to this router.')
        token = _ACTIVE_ROUTERS.set(active | {self.entry.entry_id})
        try:
            unresolved_media_constraint = None
            operation = command(user_input.text, user_input.language)
            body = inline_announcement(user_input.text, user_input.language)
            if body is not None:
                operation = 'start'
            if operation is not None:
                if not self._trusted_source(user_input):
                    return self._reply(user_input, 'Use a configured voice satellite to manage its announcement.')
                runtime = self.coordinator.broadcast_runtime
                try:
                    if operation == 'start':
                        request_id = 'ha-router-' + hashlib.sha256(json.dumps([
                            self.entry.entry_id, user_input.context.id, user_input.device_id, operation
                        ]).encode()).hexdigest()
                        if body is not None:
                            result = await runtime.async_start_inline(user_input.device_id, user_input.context,
                                transcript=user_input.text, body=body, request_id=request_id)
                        else:
                            result = await runtime.async_start(user_input.device_id, user_input.context,
                                                               request_id=request_id)
                    elif operation == 'cancel':
                        result = await runtime.async_cancel(user_input.device_id)
                    else:
                        result = await runtime.async_status(user_input.device_id)
                except Exception:
                    result = {'status': 'error'}
                return self._reply(user_input, acknowledgement(operation, result))
            control=get_schedule_control(self.hass)
            selection=await control.route(self.coordinator.client,self.entry.entry_id,user_input,
                trusted=self._trusted_schedule_source(user_input),
                conversation_id=user_input.conversation_id or ulid_util.ulid_now())
            if selection is not None:
                result=self._reply(user_input,selection['speech'])
                if selection.get('conversation_id'):
                    result.conversation_id=selection['conversation_id']
                result.continue_conversation=selection['continue_conversation']
                return result
            if (request := music_request(user_input.text, user_input.language, _MUSIC_STYLES)) is not None:
                if target := request.get('target_room'):
                    try:
                        resolve_media_room(self.hass, self.entry, user_input.as_llm_context(DOMAIN), target)
                    except Exception:
                        if 'room' in target.casefold().split():
                            return await self._media_reply(user_input, request)
                        # A syntactic suffix can be part of a title (Back in
                        # Black). Delegate the original complete utterance; do
                        # not remove the suffix or substitute the source room.
                        unresolved_media_constraint = dict(request)
                        request = None
                if request is not None:
                    return await self._media_reply(user_input, request)
            if (self.entry.options.get('knowledge_enabled', False)
                    and knowledge_question(user_input.text, user_input.language)):
                return await self._knowledge_reply(user_input)
            fallback = self.entry.options.get('conversation_fallback_agent')
            registered = er.async_get(self.hass).async_get(fallback) if fallback else None
            agent = conversation.async_get_agent(self.hass, fallback) if fallback else None
            if (not fallback or fallback == self.entity_id or registered is None
                    or registered.disabled_by is not None or registered.platform == DOMAIN
                    or agent is None or isinstance(agent, HouseholdConversationRouter)):
                return self._reply(user_input, 'Choose an available fallback conversation agent in Household Voice settings.')
            try:
                extra_prompt = user_input.extra_system_prompt
                if self.entry.options.get('knowledge_enabled', False):
                    policy = family_prompt(self.entry.options)
                    extra_prompt = '\n\n'.join(part for part in (extra_prompt, policy) if part)
                with bind_request(user_input.text, user_input.language, user_input.context, user_input.device_id,
                                  media_constraints=unresolved_media_constraint) as evidence:
                    delegated = await conversation.async_converse(self.hass, text=user_input.text,
                        conversation_id=user_input.conversation_id, context=user_input.context,
                        language=user_input.language, agent_id=fallback, device_id=user_input.device_id,
                        satellite_id=user_input.satellite_id, extra_system_prompt=extra_prompt)
                if media_action_request(user_input.text, user_input.language):
                    receipts = evidence.media_results
                    if not receipts or evidence.media_results_overflow:
                        return self._reply(user_input, 'I could not confirm that the audio request was carried out.')
                    uncertain = [receipt for receipt in receipts if receipt.get('status') == 'error'
                        and (receipt.get('mutation_dispatch_started') or receipt.get('actions_issued', 0)
                             or receipt.get('reason') in ('media_playback_pending_or_uncertain', 'media_result_uncertain'))]
                    outcome = dict(uncertain[-1] if uncertain else receipts[-1])
                    if outcome.get('operation') != 'play' and outcome.get('status') == 'action_accepted':
                        speech = 'Okay.'
                    else:
                        speech = music_acknowledgement(outcome)
                    reply = self._reply(user_input, speech)
                    reply.response.async_set_speech(speech, extra_data={'household_media':{
                        key:outcome[key] for key in ('status','reason','operation','executed',
                            'playback_confirmed','retry_automatically','timeout_phase') if key in outcome}})
                    return reply
                return delegated
            except Exception:
                return self._reply(user_input, 'The fallback conversation agent is unavailable. Please try again.')
        finally:
            _ACTIVE_ROUTERS.reset(token)
