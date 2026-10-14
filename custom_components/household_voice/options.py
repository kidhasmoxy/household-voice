"""Pure administrator option validation; new features default to disabled."""
from copy import deepcopy
import math
import re
from urllib.parse import urlsplit, urlunsplit
from .knowledge_policy import initial_settings, validate_knowledge_settings, KnowledgePolicyError

FEATURES = ("live_actions_enabled", "media_enabled", "satellite_audio_enabled",
            "announcements_enabled", "ring_actions_enabled", "stt_capture_enabled",
            "echomuse_audio_enabled", "native_room_ducking_enabled")


class RoutingError(ValueError):
    """Stable translation key for a rejected routing selection."""


def initial_options(existing):
    result = deepcopy(dict(existing))
    result["routing_version"] = 2
    for key in FEATURES:
        result.setdefault(key, False)
    result.setdefault("default_announcement_speakers", [])
    result.setdefault("phones", [])
    result.setdefault("satellites", [])
    result.setdefault("assistant_speaker_overrides", {})
    result.setdefault("media_rooms", [])
    result.setdefault("stt_capture_profiles", [])
    result.setdefault("alarm_rooms", [])
    result.setdefault("ring_fallback_speakers", [])
    result.setdefault("ring_volume", 45)
    result.setdefault("announcement_volume", 0.5)
    result.setdefault("announcement_chime", "none")
    result.setdefault("spoken_media_policy", "pause_and_resume")
    result.setdefault("phone_presence_policy", "recent_five_minutes")
    result.setdefault("conversation_fallback_agent", "")
    result.setdefault("assist_tools_enabled", False)
    result.setdefault("native_recording_enabled", False)
    result.setdefault("single_turn_announcements_enabled", False)
    result.setdefault("announcement_alignment_url", "")
    result.setdefault("announcement_alignment_model", "")
    result.setdefault("announcement_alignment_api_key", "")
    result.update(initial_settings(existing))
    return result


def alignment_url(value):
    """Configured server base URL; append exactly /v1/audio/transcriptions."""
    try:
        if not isinstance(value, str) or not value or len(value)>2048 or any(ord(c)<33 for c in value) or '\\' in value:
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not re.fullmatch(r'[/A-Za-z0-9_.~-]*', parsed.path)
                or any(part in ('.','..') for part in parsed.path.split('/'))):
            raise ValueError()
        parsed.port  # Reject malformed ports before saving.
        path = parsed.path.rstrip('/')
        if path.endswith('/v1'):
            path = path[:-3]
        if path.endswith('/audio/transcriptions'):
            raise ValueError()
        return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    except (ValueError, TypeError):
        raise RoutingError('invalid_alignment_configuration') from None


def alignment_options(data):
    enabled = data.get('single_turn_announcements_enabled', False)
    url = data.get('announcement_alignment_url', '')
    model = data.get('announcement_alignment_model', '')
    key = data.get('announcement_alignment_api_key', '')
    if (type(enabled) is not bool or not isinstance(url, str) or not isinstance(model, str)
            or len(model)>512 or model != model.strip() or any(ord(c)<32 or ord(c)==127 for c in model)
            or not isinstance(key, str) or len(key)>4096 or any(ord(c)<32 or ord(c)==127 for c in key)):
        raise RoutingError('invalid_alignment_configuration')
    if url:
        url = alignment_url(url)
    if enabled and (not url or not model):
        raise RoutingError('invalid_alignment_configuration')
    return {'single_turn_announcements_enabled':enabled,'announcement_alignment_url':url,
            'announcement_alignment_model':model,'announcement_alignment_api_key':key}


def conversation_fallback(value, entities):
    if value == "":
        return ""
    selected = entity(value, "conversation", entities)
    if entities[selected].get("integration") == "household_voice":
        raise RoutingError("invalid_conversation_fallback")
    return selected


def volume(value, name="invalid_volume"):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise RoutingError(name)
    return value


def feature_options(data):
    result = {}
    for key in FEATURES:
        value = data.get(key, False)
        if type(value) is not bool:
            raise RoutingError("invalid_enabled")
        result[key] = value
    return result


def ma_speakers(values, entities):
    result = speakers(values, entities)
    for value in result:
        if entities[value].get("integration") != "music_assistant" or not entities[value].get("config_entry_id"):
            raise RoutingError("invalid_music_player")
    return result


def room_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise RoutingError("invalid_room")
    return value


def alarm_room(data, entities):
    if not isinstance(data, dict):
        raise RoutingError("invalid_mapping")
    selected = ma_speakers(data.get("speakers", []), entities)
    if not selected or len(selected) > 20:
        raise RoutingError("invalid_music_player")
    return {"room_id": room_id(data.get("room_id")), "speakers": selected}


def stt_profile(data, entities, devices, satellites):
    if not isinstance(data, dict):
        raise RoutingError("invalid_mapping")
    name = room_id(data.get("profile_id"))
    source = data.get("source_device_id")
    if (not isinstance(source, str) or source not in devices or devices[source].get("disabled")
            or source not in {s.get("satellite_device_id") for s in satellites}):
        raise RoutingError("invalid_capture_source")
    provider = entity(data.get("provider_entity_id"), "stt", entities)
    if entities[provider].get("integration") == "household_voice":
        raise RoutingError("invalid_stt_provider")
    return {"profile_id": name, "source_device_id": source, "provider_entity_id": provider}


def entity(value, domain, entities):
    if not isinstance(value, str) or not value.startswith(domain + "."):
        raise RoutingError("invalid_entity")
    item = entities.get(value)
    if item is None or item.get("disabled"):
        raise RoutingError("invalid_entity")
    return value


def speakers(values, entities):
    if not isinstance(values, list) or len(values) > 100:
        raise RoutingError("invalid_entity")
    result = [entity(value, "media_player", entities) for value in values]
    if len(set(result)) != len(result):
        raise RoutingError("duplicate_mapping")
    # No name matching or guessed MA/Sonos aliases. The administrator must choose
    # one endpoint per physical speaker; the registry cannot prove equivalence.
    return result


def phone(data, entities, services):
    if not isinstance(data, dict):
        raise RoutingError("invalid_mapping")
    tracker = entity(data.get("device_tracker"), "device_tracker", entities)
    service = data.get("notify_service")
    if not isinstance(service, str) or not service.startswith("notify.mobile_app_") or service not in services:
        raise RoutingError("invalid_notify_service")
    if type(data.get("enabled", True)) is not bool:
        raise RoutingError("invalid_enabled")
    return {"device_tracker": tracker, "notify_service": service, "enabled": data.get("enabled", True)}


def resolve_satellite(data, entities, devices):
    if not isinstance(data, dict):
        raise RoutingError("invalid_mapping")
    entity_id = data.get("satellite_entity_id") or ""
    device_id = data.get("satellite_device_id") or ""
    if not isinstance(entity_id, str) or not isinstance(device_id, str):
        raise RoutingError("invalid_device")
    if bool(entity_id) == bool(device_id):
        raise RoutingError("select_one_satellite")
    if entity_id:
        entity(entity_id, "assist_satellite", entities)
        device_id = entities[entity_id].get("device_id") or ""
        if not device_id or device_id not in devices or devices[device_id].get("disabled"):
            raise RoutingError("invalid_device")
        return {"satellite_entity_id": entity_id, "satellite_device_id": device_id}
    item = devices.get(device_id)
    if item is None or item.get("disabled") or "esphome" not in item.get("domains", []):
        raise RoutingError("invalid_device")
    # Output-less ESPHome firmware may have no Assist entity. The explicit device
    # selection is still useful; it is not evidence that pairing is operational.
    return {"satellite_entity_id": "", "satellite_device_id": device_id}


def satellite(data, entities, devices, services):
    if not isinstance(data, dict):
        raise RoutingError("invalid_mapping")
    entity_id = data.get("satellite_entity_id") or ""
    device_id = data.get("satellite_device_id") or ""
    selected = resolve_satellite({"satellite_entity_id": entity_id} if entity_id else {"satellite_device_id": device_id}, entities, devices)
    if device_id and selected["satellite_device_id"] != device_id:
        raise RoutingError("invalid_device")
    room = room_id(data.get("room_id"))
    player = entity(data.get("tts_media_player"), "media_player", entities)
    duck = speakers(data.get("duck_media_players", []), entities)
    completion = data.get("completion_service") or ""
    if not isinstance(completion, str) or (completion and (completion not in services or not completion.startswith("esphome.") or not completion.endswith("_external_reply_complete"))):
        raise RoutingError("invalid_completion_service")
    factor = volume(data.get("duck_factor", 0.25))
    if factor == 0:
        raise RoutingError("invalid_volume")
    return {**selected, "room_id": room, "tts_media_player": player,
            "duck_media_players": duck, "completion_service": completion,
            "reply_volume": volume(data.get("reply_volume", 0.5)),
            "duck_factor": factor}


def validate_options(data, entities, devices, services):
    result = initial_options(data)
    if type(result['assist_tools_enabled']) is not bool or type(result['native_recording_enabled']) is not bool:
        raise RoutingError('invalid_enabled')
    try:
        result.update(validate_knowledge_settings(result))
        selected = result['knowledge_agent']
        if selected:
            item = entities.get(selected)
            if item is None or item.get('disabled') or item.get('integration') != 'local_openai':
                raise KnowledgePolicyError('invalid_knowledge_agent')
    except KnowledgePolicyError as exc:
        raise RoutingError(str(exc)) from None
    result.update(alignment_options(result))
    result["conversation_fallback_agent"] = conversation_fallback(result["conversation_fallback_agent"], entities)
    result.update(feature_options(result))
    if result['native_recording_enabled'] and not all(result[key] for key in
            ('assist_tools_enabled','live_actions_enabled','announcements_enabled')):
        raise RoutingError('native_recording_prerequisites')
    overrides=result['assistant_speaker_overrides']
    if not isinstance(overrides,dict) or len(overrides)>100:
        raise RoutingError('invalid_mapping')
    for assistant,player in overrides.items():
        # Preserve unavailable/removed exact choices so a transient registry change
        # cannot silently select another speaker. The runtime resolves safety.
        if not isinstance(assistant,str) or not re.fullmatch(r'assist_satellite\.[a-z0-9_]+',assistant):
            raise RoutingError('invalid_mapping')
        if player not in (None,'auto') and (not isinstance(player,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',player)):
            raise RoutingError('invalid_mapping')
    result["announcement_volume"] = volume(result["announcement_volume"])
    if result["announcement_chime"] not in ("none", "default"):
        raise RoutingError("invalid_chime")
    if result['spoken_media_policy'] not in ('provider_default','pause_and_resume','pause_and_hold'):
        raise RoutingError('invalid_spoken_media_policy')
    if result['phone_presence_policy'] not in ('current_state','recent_hour','recent_five_minutes'):
        raise RoutingError('invalid_phone_presence_policy')
    if type(result["ring_volume"]) is not int or not 1 <= result["ring_volume"] <= 100:
        raise RoutingError("invalid_volume")
    result["ring_fallback_speakers"] = ma_speakers(result["ring_fallback_speakers"], entities)
    if len(result["ring_fallback_speakers"]) > 20:
        raise RoutingError("invalid_mapping")
    result["default_announcement_speakers"] = speakers(result["default_announcement_speakers"], entities)
    if (not isinstance(result["phones"], list) or not isinstance(result["satellites"], list)
            or len(result["phones"]) > 100 or len(result["satellites"]) > 100):
        raise RoutingError("invalid_mapping")
    result["phones"] = [phone(item, entities, services) for item in result["phones"]]
    for key in ("device_tracker", "notify_service"):
        values = [item[key] for item in result["phones"]]
        if len(set(values)) != len(values):
            raise RoutingError("duplicate_mapping")
    result["satellites"] = [satellite(item, entities, devices, services) for item in result["satellites"]]
    if result["live_actions_enabled"] and result["satellite_audio_enabled"]:
        ma_speakers(list(dict.fromkeys(row["tts_media_player"] for row in result["satellites"])), entities)
    if result["live_actions_enabled"] and result["announcements_enabled"]:
        ma_speakers(result["default_announcement_speakers"], entities)
    ids = [item["satellite_device_id"] for item in result["satellites"]]
    if len(set(ids)) != len(ids):
        raise RoutingError("duplicate_mapping")
    rooms = result['media_rooms']
    if not isinstance(rooms, list) or len(rooms)>100:
        raise RoutingError('invalid_mapping')
    result['media_rooms'] = [media_room(item, entities) for item in rooms]
    for key in ('room_id', 'music_player'):
        values = [item[key] for item in result['media_rooms']]
        if len(set(values)) != len(values):
            raise RoutingError('duplicate_mapping')
    for name in ("alarm_rooms", "stt_capture_profiles"):
        if not isinstance(result[name], list) or len(result[name]) > 64:
            raise RoutingError("invalid_mapping")
    result["alarm_rooms"] = [alarm_room(row, entities) for row in result["alarm_rooms"]]
    alarm_targets = [target for row in result["alarm_rooms"] for target in row["speakers"]]
    if len(set(alarm_targets)) != len(alarm_targets):
        raise RoutingError("duplicate_mapping")
    if result["live_actions_enabled"] and result["ring_actions_enabled"] and not result["alarm_rooms"] and not any(key.startswith('assist_satellite.') for key in entities):
        raise RoutingError("alarm_mapping_required")
    if len({row["room_id"] for key in ("satellites", "media_rooms", "alarm_rooms") for row in result[key]}) > 100:
        raise RoutingError("invalid_mapping")
    result["stt_capture_profiles"] = [stt_profile(row, entities, devices, result["satellites"]) for row in result["stt_capture_profiles"]]
    for rows, keys in ((result["alarm_rooms"], ("room_id",)),
                       (result["stt_capture_profiles"], ("profile_id", "source_device_id"))):
        for key in keys:
            if len({row[key] for row in rows}) != len(rows):
                raise RoutingError("duplicate_mapping")
    return result


def media_room(data, entities):
    if not isinstance(data, dict):
        raise RoutingError('invalid_mapping')
    room = data.get('room_id')
    if not isinstance(room,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',room):
        raise RoutingError('invalid_room')
    player = entity(data.get('music_player'), 'media_player', entities)
    owner = entities[player]
    if owner.get('integration') != 'music_assistant' or not owner.get('config_entry_id'):
        raise RoutingError('invalid_music_player')
    username, playlist = data.get('username'), data.get('default_sleep_playlist')
    for value, limit in ((username,120),(playlist,512)):
        if not isinstance(value,str) or not value.strip() or len(value)>limit or any(ord(c)<32 for c in value):
            raise RoutingError('invalid_media_profile')
    return {'room_id':room, 'music_player':player, 'config_entry_id':owner['config_entry_id'],
            'username':username.strip(), 'default_sleep_playlist':playlist.strip()}
