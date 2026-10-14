"""Narrow HTTP contract shared by the HA adapter and coordinator.

This authority stores schedules only. No speaker or HA control is implicit in a
successful mutation. A transport must authenticate before calling dispatch.
"""
from dataclasses import dataclass
import json


@dataclass
class APIError(ValueError):
    message: str
    status: int = 400


class ScheduleAPI:
    def __init__(self, scheduler, rooms=None):
        self.scheduler = scheduler
        self.rooms = rooms or []

    def snapshot(self):
        self.scheduler.tick()
        with self.scheduler.db() as db:
            table=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_settings'").fetchone()
            row=db.execute('SELECT body FROM runtime_settings WHERE id=1').fetchone() if table else None
            assistants=json.loads(row['body']).get('assistants',{}) if row else {}
        return {'mode': 'dry_run', 'ha_connected': False,
                'schedules': self.scheduler.list(), 'events': self.scheduler.events(), 'rooms': self.rooms,
                'assistants':[{'id':identifier,**binding} for identifier,binding in assistants.items()]}

    def dispatch(self, operation, payload):
        schemas = {
            'timer': ({'label', 'seconds', 'source_room', 'request_id'}, {'target_room','target_assistant_id'}),
            'alarm': ({'label', 'local_time', 'timezone', 'weekdays', 'source_room', 'request_id','target_assistant_id'}, {'target_room', 'one_shot'}),
            'command': ({'identifier', 'operation', 'request_id', 'expected_revision'}, {'seconds', 'target_room', 'label','target_assistant_id'}),
            'dismiss_ringing': ({'request_id', 'expected'}, set()),
        }
        if operation not in schemas:
            raise APIError('Unknown operation', 404)
        required, optional = schemas[operation]
        if not isinstance(payload, dict) or required - payload.keys() or payload.keys() - required - optional:
            raise APIError('Missing or unexpected fields')
        data = dict(payload)
        if operation == 'alarm':
            data['tz_name'] = data.pop('timezone')
        try:
            if operation == 'dismiss_ringing':
                return {'mode':'dry_run','ha_connected':False,
                        'ringing_decision':self.scheduler.dismiss_ringing(**data)}
            result = getattr(self.scheduler, {'timer': 'create_timer', 'alarm': 'create_alarm', 'command': 'command'}[operation])(**data)
            return {'mode': 'dry_run', 'ha_connected': False, 'schedule': result}
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            # Never emit arguments/credentials from unexpected exceptions.
            message = str(exc) if isinstance(exc, ValueError) else 'Invalid schedule arguments'
            conflict = message.startswith(('Revision conflict', 'Idempotency key reused'))
            raise APIError(message, 409 if conflict else 400) from exc


async def read_json(request, limit=16384):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise APIError('Request body exceeds limit', 413)
    try:
        def reject_constant(value):
            raise ValueError('Non-finite JSON number')
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate JSON key')
                result[key] = value
            return result
        value = json.loads(body, parse_constant=reject_constant, object_pairs_hook=unique_object)
        pending, nodes = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 32 or nodes > 10000:
                raise ValueError('JSON structure exceeds limits')
            if isinstance(item, dict):
                for key, child in item.items():
                    key.encode('utf-8', errors='strict')
                    pending.append((child, depth+1))
            elif isinstance(item, list):
                pending.extend((child, depth+1) for child in item)
            elif isinstance(item, str):
                item.encode('utf-8', errors='strict')
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise APIError('Invalid JSON') from exc
