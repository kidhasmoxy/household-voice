"""Optional exact-command announcement router with an existing agent fallback."""
from contextvars import ContextVar
import hashlib
import json

from homeassistant.components import conversation
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent
from homeassistant.util import ulid as ulid_util

from .const import DOMAIN
from .conversation_router import acknowledgement, command

_ACTIVE_ROUTERS = ContextVar('household_voice_active_conversation_routers', default=frozenset())


async def async_setup_entry(hass, entry, async_add_entities):
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

    async def async_process(self, user_input):
        active = _ACTIVE_ROUTERS.get()
        if self.entry.entry_id in active:
            return self._reply(user_input, 'The conversation routing configuration loops back to this router.')
        token = _ACTIVE_ROUTERS.set(active | {self.entry.entry_id})
        try:
            operation = command(user_input.text, user_input.language)
            if operation is not None:
                if not self._trusted_source(user_input):
                    return self._reply(user_input, 'Use a configured voice satellite to manage its announcement.')
                runtime = self.coordinator.broadcast_runtime
                try:
                    if operation == 'start':
                        request_id = 'ha-router-' + hashlib.sha256(json.dumps([
                            self.entry.entry_id, user_input.context.id, user_input.device_id, operation
                        ]).encode()).hexdigest()
                        result = await runtime.async_start(user_input.device_id, user_input.context,
                                                           request_id=request_id)
                    elif operation == 'cancel':
                        result = await runtime.async_cancel(user_input.device_id)
                    else:
                        result = await runtime.async_status(user_input.device_id)
                except Exception:
                    result = {'status': 'error'}
                return self._reply(user_input, acknowledgement(operation, result))
            fallback = self.entry.options.get('conversation_fallback_agent')
            registered = er.async_get(self.hass).async_get(fallback) if fallback else None
            agent = conversation.async_get_agent(self.hass, fallback) if fallback else None
            if (not fallback or fallback == self.entity_id or registered is None
                    or registered.disabled_by is not None or registered.platform == DOMAIN
                    or agent is None or isinstance(agent, HouseholdConversationRouter)):
                return self._reply(user_input, 'Choose an available fallback conversation agent in Household Voice settings.')
            try:
                return await conversation.async_converse(self.hass, text=user_input.text,
                    conversation_id=user_input.conversation_id, context=user_input.context,
                    language=user_input.language, agent_id=fallback, device_id=user_input.device_id,
                    satellite_id=user_input.satellite_id, extra_system_prompt=user_input.extra_system_prompt)
            except Exception:
                return self._reply(user_input, 'The fallback conversation agent is unavailable. Please try again.')
        finally:
            _ACTIVE_ROUTERS.reset(token)
