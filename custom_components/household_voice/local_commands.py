"""Conservative local commands; unsupported wording belongs to the shared agent."""
import re

from .schedule_language import duration, normalize

# HA's intent_filter returns True to EXCLUDE a match. Keep this an allowlist:
# timer, media, shopping and future upstream intents must never slip through.
LOCAL_INTENTS = frozenset({
    'HassTurnOn', 'HassTurnOff', 'HassLightSet', 'HassGetState',
    'HassClimateSetTemperature', 'HassClimateGetTemperature',
    'HassSetPosition',
    'HassGetCurrentTime',
})


def exclude_intent(result):
    return result.intent.name not in LOCAL_INTENTS


def timer_request(text, language='en'):
    """Only explicit, complete new timers. Never consume a target or an edit."""
    value = normalize(text, language)
    if not value:
        return None
    match = re.fullmatch(
        r'(?:set|start|create|make) (?:(?:a |an )(?:new )?|new )'
        r'(?:(?P<before>.+?) timer|timer for (?P<after>.+?))'
        r'(?: (?:called|named) (?P<label>[\w -]{1,80}))?', value)
    if not match:
        return None
    raw = match['before'] or match['after']
    raw = re.sub(r'(?<=\d)-(?=[a-z])', ' ', raw)
    parts = re.split(r'(?<=hours)\s+(?:and )?|(?<=hour)\s+(?:and )?|(?<=minutes)\s+(?:and )?|(?<=minute)\s+(?:and )?', raw)
    seconds = [duration(part) for part in parts]
    if not seconds or any(part is None for part in seconds) or sum(seconds) > 604800:
        return None
    result = {'seconds': sum(seconds)}
    if match['label']:
        result['label'] = match['label']
    return result
