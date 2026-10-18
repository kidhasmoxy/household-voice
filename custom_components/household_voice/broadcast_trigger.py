"""Native sentence shortcuts for the existing source-bound recording workflow.

Core handles these before the shared LLM agent. Return explicitly empty speech
on success: the owned ask_question operation supplies the only spoken prompt.
No message wildcard, conversation router, or recording content enters the LLM.
"""
import hashlib
import json

from homeassistant.components.conversation.agent_manager import get_agent_manager
from homeassistant.helpers import entity_registry as er

SENTENCES = ["[please] broadcast", "[please] [make an] announcement", "[please] announce"]


def async_register(hass, entry, coordinator):
    if (entry.options.get('assist_tools_enabled') is not True
            or entry.options.get('announcements_enabled') is not True
            or not any(entry.options.get(key) is True for key in
                       ('native_recording_enabled', 'stt_capture_enabled'))):
        return lambda: None

    async def handle(user_input, _matched):
        return await async_handle(hass, entry, coordinator, user_input)

    return get_agent_manager(hass).register_trigger(sentences=SENTENCES, trigger_callback=handle)


async def async_handle(hass, entry, coordinator, user_input):
    if (entry.options.get('announcements_enabled') is not True
            or not any(entry.options.get(key) is True for key in
                       ('native_recording_enabled', 'stt_capture_enabled'))):
        return 'Announcements are unavailable right now.'
    # Resolve identity exclusively from HA's request context/registry, as
    # Core's own conversation trigger does, rejecting conflicting metadata.
    device = user_input.device_id
    satellite = getattr(user_input, 'satellite_id', None)
    if satellite:
        registered = er.async_get(hass).async_get(satellite)
        if (registered is None or registered.disabled_by is not None
                or not registered.device_id
                or device is not None and device != registered.device_id):
            return 'Please use a configured voice assistant to make an announcement.'
        device = registered.device_id
    context = user_input.context
    if not device or not context or not getattr(context, 'id', None):
        return 'Please use a configured voice assistant to make an announcement.'
    if (entry.options.get('assist_tools_enabled') is not True
            or getattr(entry, 'disabled_by', None) is not None):
        return 'Announcements are unavailable right now.'
    # Multiple authorities must never both record or broadcast one command.
    authorities = hass.data.get('household_voice', {})
    if len(authorities) != 1 or authorities.get(entry.entry_id) is not coordinator:
        return 'Choose one Household Voice controller for announcements.'
    request_id = 'ha-broadcast-sentence-' + hashlib.sha256(json.dumps(
        [entry.entry_id, context.id, device], separators=(',', ':')).encode()).hexdigest()
    try:
        result = await coordinator.broadcast_runtime.async_start(
            device, context, request_id=request_id)
    except Exception:
        return 'Announcements are unavailable right now.'
    if result.get('status') == 'queued':
        return ''
    if result.get('reason') == 'announcement_already_in_progress_on_this_device':
        return 'An announcement is already in progress.'
    # Never turn rejection/uncertainty into another capture attempt.
    return 'I couldn\'t start the announcement. Check Household Voice settings.'
