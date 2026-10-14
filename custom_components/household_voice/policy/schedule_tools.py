"""Pure schedule selection and command plans, without scheduler/HA mutations.

The adapter constructs snapshots from Scheduler.list() and request IDs from its
authenticated transport. Model-facing requests contain selectors, never a bare
revision, source device, or an authoritative schedule snapshot. The executor
must pass the selected revision to Scheduler.command and surface CAS conflicts;
it must not silently choose another schedule or retry against a newer revision.
"""
from dataclasses import dataclass
import math
from typing import Literal

from .routing import RoomRegistry

Operation = Literal['list', 'stop', 'pause', 'resume', 'add', 'cancel', 'dismiss',
                    'snooze', 'move', 'enable', 'disable', 'rename', 'delete', 'set', 'reschedule']
OPERATIONS = ('list', 'stop', 'pause', 'resume', 'add', 'cancel', 'dismiss', 'snooze',
              'move', 'enable', 'disable', 'rename', 'delete', 'set', 'reschedule')
STATES = ('active', 'paused', 'ringing', 'cancelled', 'disabled', 'completed', 'missed', 'deleted')
TERMINAL = ('cancelled', 'completed', 'missed', 'deleted')


def _text(value, name, limit=120):
    if not isinstance(value, str) or not 1<=len(value.strip())<=limit or any(ord(c)<32 for c in value):
        raise ValueError(f'Invalid {name}')


def _normal(value):
    return ' '.join(value.casefold().split())


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class ScheduleRequest:
    operation: Operation = 'list'
    schedule_id: str | None = None
    label: str | None = None
    kind: Literal['timer', 'alarm'] | None = None
    room: str | None = None
    room_scope: Literal['target', 'source'] = 'target'
    seconds: float | None = None
    new_target_room: str | None = None
    new_label: str | None = None
    include_terminal: bool = False
    local_time: str | None = None
    timezone: str | None = None
    date: str | None = None
    weekdays: list[int] | None = None
    one_shot: bool | None = None

    def __post_init__(self):
        if self.operation not in OPERATIONS or self.kind not in (None, 'timer', 'alarm') or self.room_scope not in ('target', 'source'):
            raise ValueError('Unsupported operation or selector')
        for name in ('schedule_id', 'label', 'room', 'new_target_room', 'new_label'):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        if type(self.include_terminal) is not bool:
            raise ValueError('include_terminal must be boolean')
        if self.seconds is not None and (not _number(self.seconds) or abs(self.seconds)>604800):
            raise ValueError('Invalid duration')
        if self.seconds is not None and self.operation not in ('add', 'snooze', 'set'):
            raise ValueError('This command does not take a duration')
        if self.new_target_room is not None and self.operation!='move':
            raise ValueError('Only move takes a destination room')
        if self.new_label is not None and self.operation!='rename':
            raise ValueError('Only rename takes a new label')
        fields = ('local_time','timezone','date','weekdays','one_shot')
        if any(getattr(self, name) is not None for name in fields) and self.operation!='reschedule':
            raise ValueError('Only reschedule takes alarm settings')
        for name in ('local_time','timezone','date'):
            if getattr(self,name) is not None:
                _text(getattr(self,name),name)
        if self.weekdays is not None and (not isinstance(self.weekdays,list) or not self.weekdays or any(type(d) is not int or not 0<=d<=6 for d in self.weekdays)):
            raise ValueError('Invalid alarm weekdays')
        if self.one_shot is not None and type(self.one_shot) is not bool:
            raise ValueError('Invalid alarm recurrence')


@dataclass(frozen=True)
class ScheduleRecord:
    id: str
    kind: Literal['timer', 'alarm']
    label: str
    source_room: str
    target_room: str
    state: str
    revision: int
    due: float | None = None
    remaining: float | None = None

    def __post_init__(self):
        for name in ('id', 'label', 'source_room', 'target_room'):
            _text(getattr(self, name), name)
        if self.kind not in ('timer', 'alarm') or self.state not in STATES:
            raise ValueError('Invalid schedule kind/state')
        if type(self.revision) is not int or self.revision<1:
            raise ValueError('Invalid authoritative revision')
        if any(v is not None and not _number(v) for v in (self.due, self.remaining)):
            raise ValueError('Invalid schedule times')
        if self.remaining is not None and self.remaining<0:
            raise ValueError('Invalid remaining duration')

    @classmethod
    def from_row(cls, row):
        """Adapter helper for an actual Scheduler.list() row, never model data."""
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ScheduleSnapshot:
    observed_at: float
    records: tuple[ScheduleRecord, ...]

    def __post_init__(self):
        if not _number(self.observed_at) or not isinstance(self.records, tuple) or len(self.records)>2000:
            raise ValueError('Invalid schedule snapshot')
        if any(not isinstance(r, ScheduleRecord) for r in self.records):
            raise ValueError('Snapshot needs authoritative ScheduleRecord values')
        if len({r.id for r in self.records})!=len(self.records):
            raise ValueError('Duplicate schedule ID in snapshot')


@dataclass(frozen=True)
class ScheduleContext:
    request_id: str

    def __post_init__(self):
        _text(self.request_id, 'trusted request ID', 128)


def _result(status, reason, **details):
    return {'status': status, 'reason': reason, 'mode': 'dry_run', 'executed': False,
            'command': None, **details}


def _choice(record, now):
    remaining = record.remaining
    if record.kind=='timer' and record.state=='active' and record.due is not None:
        remaining = max(0, record.due-now)
    return {'id': record.id, 'kind': record.kind, 'label': record.label, 'state': record.state,
            'source_room': record.source_room, 'target_room': record.target_room,
            'revision': record.revision, 'due': record.due, 'remaining': remaining}


def _eligible(record, operation):
    if record.state=='deleted':
        return False
    if operation in ('stop', 'dismiss', 'snooze'):
        return record.state=='ringing'
    if operation=='pause':
        return record.kind=='timer' and record.state=='active'
    if operation=='resume':
        return record.kind=='timer' and record.state=='paused'
    if operation in ('add','set'):
        return record.kind=='timer' and record.state in ('active', 'paused')
    if operation=='enable':
        return record.kind=='alarm' and record.state=='disabled'
    if operation=='disable':
        return record.kind=='alarm' and record.state in ('active', 'ringing')
    if operation=='reschedule':
        return record.kind=='alarm' and record.state in ('active','disabled')
    return operation=='delete' or record.state not in TERMINAL


def plan_schedule(request: ScheduleRequest, snapshot: ScheduleSnapshot, context: ScheduleContext,
                  registry: RoomRegistry, *, now: float) -> dict:
    """Select globally unless an explicit filter is supplied; never bulk-mutate.

    A room filter refers to the current ringing destination by default. Explicit
    room_scope='source' means creation room. No implicit originating-room filter
    hides timers set elsewhere. Snapshot age over thirty seconds requires refresh.
    """
    if (not isinstance(request, ScheduleRequest) or not isinstance(snapshot, ScheduleSnapshot)
            or not isinstance(context, ScheduleContext) or not isinstance(registry, RoomRegistry) or not _number(now)):
        raise ValueError('Invalid schedule planning input')
    if not 0<=now-snapshot.observed_at<=30:
        return _result('lookup_required', 'fresh_snapshot_required')
    rooms = {}
    for name in ('room', 'new_target_room'):
        value = getattr(request, name)
        if value is not None:
            resolved = registry.aliases.get(value.casefold().strip())
            if resolved is None:
                return _result('clarify', 'unknown_'+name, question='Which room did you mean?')
            rooms[name] = resolved
    records = [r for r in snapshot.records if r.state!='deleted'
               and (request.schedule_id is None or r.id==request.schedule_id)
               and (request.label is None or _normal(r.label)==_normal(request.label))
               and (request.kind is None or r.kind==request.kind)
               and (request.room is None or getattr(r, request.room_scope+'_room')==rooms['room'])]
    records.sort(key=lambda r: (r.kind, _normal(r.label), r.id))
    if request.operation=='list':
        items = [_choice(r, now) for r in records if request.include_terminal or r.state not in TERMINAL]
        return _result('listed', 'current_schedules', schedules=items, count=len(items), scope='global' if request.room is None else request.room_scope+'_room')
    explicit_selector = any(v is not None for v in (request.schedule_id, request.label, request.kind, request.room))
    if not explicit_selector and request.operation!='stop':
        return _result('clarify', 'selector_required', question='Which timer or alarm did you mean?',
                       choices=[_choice(r, now) for r in records if _eligible(r, request.operation)][:10])
    eligible = [r for r in records if _eligible(r, request.operation)]
    if not eligible:
        reason = 'no_ringing_schedule' if request.operation=='stop' else 'no_eligible_schedule'
        return _result('clarify' if explicit_selector else 'noop', reason,
                       choices=[_choice(r, now) for r in records[:10]])
    if len(eligible)!=1:
        return _result('clarify', 'ambiguous_schedule', question='Which timer or alarm did you mean?',
                       choices=[_choice(r, now) for r in eligible[:10]], candidate_count=len(eligible))
    selected = eligible[0]
    operation = 'dismiss' if request.operation=='stop' else request.operation
    command = {'identifier': selected.id, 'operation': operation, 'request_id': context.request_id,
               'expected_revision': selected.revision}
    if operation in ('add', 'snooze', 'set'):
        if request.seconds is None:
            return _result('clarify', 'duration_required', question='For how many seconds?', selected=_choice(selected, now))
        if operation=='snooze' and not 0<request.seconds<=3600:
            return _result('clarify', 'invalid_snooze_duration')
        if operation=='set' and not 0<request.seconds<=604800:
            return _result('clarify', 'invalid_timer_duration')
        if operation=='add':
            remaining = selected.remaining if selected.state=='paused' else selected.due-now if selected.due is not None else None
            if remaining is None or remaining+request.seconds<=0 or request.seconds==0:
                return _result('clarify', 'invalid_adjusted_duration')
        command['seconds'] = request.seconds
    if operation=='move':
        if request.new_target_room is None:
            return _result('clarify', 'destination_required', question='Which room should it ring in?')
        command['target_room'] = rooms['new_target_room']
    if operation=='rename':
        if request.new_label is None:
            return _result('clarify', 'new_label_required', question='What should I call it?')
        command['label'] = request.new_label.strip()
    if operation=='reschedule':
        if request.local_time is None:
            return _result('clarify','alarm_time_required',question='What time should the alarm be?',selected=_choice(selected,now))
        command.update({name:getattr(request,name) for name in ('local_time','timezone','date','weekdays','one_shot') if getattr(request,name) is not None})
    return _result('ready', 'single_schedule_selected', command=command,
                   selected=_choice(selected, now), preconditions={'snapshot_observed_at': snapshot.observed_at,
                   'compare_revision_atomically': True, 'never_retry_with_new_revision_without_reselection': True})
