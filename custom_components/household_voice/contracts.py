"""Shared validation without HA imports; backend remains authoritative."""
import math
import re
from datetime import date as civil_date
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4

OPERATIONS = {"pause", "resume", "add", "set", "cancel", "dismiss", "snooze", "move", "enable", "disable", "rename", "delete", "reschedule"}


def normalize_url(value):
    if not isinstance(value, str):
        raise ValueError("Coordinator URL must be text")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Use an HTTP(S) coordinator URL without credentials, query or fragment")
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Invalid coordinator URL")
    _ = parts.port
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def validate_token(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 512 or not all(33 <= ord(c) <= 126 for c in value):
        raise ValueError("Token must contain 32 to 512 printable ASCII characters without whitespace")
    return value


def text(value, name, maximum=120):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return value.strip()


def number(value, name, low, high, exclusive_low=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value > high or (value <= low if exclusive_low else value < low):
        raise ValueError(f"Invalid {name}")
    return value


def validate_payload(service, data):
    if not isinstance(data, dict):
        raise ValueError("Service data must be an object")
    required = {"create_timer": {"label", "seconds", "source_room"},
                "create_alarm": {"label", "local_time", "timezone", "weekdays", "source_room","target_assistant_id"},
                "command": {"identifier", "operation", "expected_revision"}}
    optional = {"create_timer": {"target_room","target_assistant_id"}, "create_alarm": {"target_room", "one_shot", "date"},
                "command": {"seconds", "target_room", "label","target_assistant_id", "local_time", "timezone", "weekdays", "one_shot", "date"}}
    if service not in required or not required[service].issubset(data) or set(data) - required[service] - optional[service] - {"config_entry_id", "request_id"}:
        raise ValueError("Unknown service, missing required fields, or extra fields")
    result = dict(data)
    for field in ("label", "source_room", "target_room", "identifier", "config_entry_id","target_assistant_id"):
        if field in result:
            result[field] = text(result[field], field)
    result["request_id"] = text(result.get("request_id", str(uuid4())), "request_id", 128)
    if 'target_assistant_id' in result and not re.fullmatch(r'assist_satellite\.[a-z0-9_]+',result['target_assistant_id']):
        raise ValueError('Select an exact assistant entity')
    if service == "create_timer":
        number(result["seconds"], "seconds", 0, 604800, True)
    if service == "create_alarm":
        if "one_shot" in result and type(result["one_shot"]) is not bool:
            raise ValueError("one_shot must be boolean")
        if not isinstance(result["local_time"], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", result["local_time"]):
            raise ValueError("local_time must use HH:MM")
        try:
            ZoneInfo(result["timezone"])
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            raise ValueError("Use a valid IANA timezone") from None
        days = result["weekdays"]
        if not isinstance(days, list) or not days or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ValueError("weekdays must be a nonempty list, Monday=0 through Sunday=6")
        result["weekdays"] = sorted(set(days))
        if 'date' in result:
            value = result['date']
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                raise ValueError('Alarm date must use YYYY-MM-DD')
            day = civil_date.fromisoformat(value)
            if day.weekday() not in result['weekdays'] or result.get('one_shot', True) is not True:
                raise ValueError('An alarm date requires a compatible one-shot alarm')
            result['one_shot'] = True
    if service == "command":
        op = result["operation"]
        if op == "set": number(result.get("seconds"), "seconds", 0, 604800, True)
        if not isinstance(op, str) or op not in OPERATIONS:
            raise ValueError("Unsupported command")
        if type(result["expected_revision"]) is not int or result["expected_revision"] < 1:
            raise ValueError("expected_revision must be a positive integer")
        number(result.get("seconds", 0), "seconds", -604800, 604800)
        if op == "snooze" and not 0 < result.get("seconds", 0) <= 3600:
            raise ValueError("Snooze requires 1 to 3600 seconds")
        if op == "move" and not {'target_room','target_assistant_id'}.intersection(result):
            raise ValueError("Move requires an assistant for alarms or a room for timers")
        if op == "rename" and "label" not in result:
            raise ValueError("Rename requires label")
        fields = {'local_time','timezone','weekdays','one_shot','date'}
        if fields.intersection(result) and op!='reschedule':
            raise ValueError('Only reschedule takes alarm settings')
        if op=='reschedule':
            if not isinstance(result.get('local_time'),str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',result['local_time']):
                raise ValueError('Reschedule requires HH:MM local_time')
            if 'seconds' in result or {'target_room','label','target_assistant_id'}.intersection(result):
                raise ValueError('Reschedule only changes alarm settings')
            if 'timezone' in result:
                try:
                    ZoneInfo(result['timezone'])
                except (ValueError, TypeError, ZoneInfoNotFoundError):
                    raise ValueError('Use a valid IANA timezone') from None
            if 'date' in result:
                value = result['date']
                if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value):
                    raise ValueError('Alarm date must use YYYY-MM-DD')
                civil_date.fromisoformat(value)
            if 'weekdays' in result:
                days = result['weekdays']
                if not isinstance(days,list) or not days or any(type(d) is not int or not 0<=d<=6 for d in days):
                    raise ValueError('Invalid alarm weekdays')
                result['weekdays'] = sorted(set(days))
            if 'one_shot' in result and type(result['one_shot']) is not bool:
                raise ValueError('one_shot must be boolean')
    return result
