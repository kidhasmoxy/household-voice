"""Connection-bound one-use grants and ordered wire validation.

Backend injects the existing RecordingSession validator; this package does not
copy its question state machine. Grant values and frame bodies must never log.
"""
from dataclasses import dataclass, field
import math
import secrets
import time

VERSION = 2
MAX_PCM_BYTES = 960_000
MAX_CHUNK_BYTES = 64_000
MAX_CHUNKS = 4096
MAX_FRAME_BYTES = 90_000
CORE_TIMEOUT = 60


class ProtocolRejected(ValueError):
    """Only fixed categories are raised."""


def exact(data, keys):
    if not isinstance(data, dict) or set(data) != set(keys):
        raise ProtocolRejected('invalid_shape')


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ProtocolRejected('invalid_identifier')
    return value


def validate_grant(data, connection_id):
    exact(data, ('version','connection_id','grant_id','operation_id','source_device_id','question_context_id','valid_for_seconds','core_timeout_seconds','max_pcm_bytes','max_chunk_bytes','max_chunks'))
    if type(data['version']) is not int or data['version'] != VERSION or data['connection_id'] != connection_id:
        raise ProtocolRejected('wrong_connection')
    for key in ('connection_id','grant_id','operation_id','source_device_id','question_context_id'):
        identifier(data[key])
    ttl=data['valid_for_seconds']
    if type(ttl) not in (int,float) or not math.isfinite(ttl) or not 0 < ttl < CORE_TIMEOUT:
        raise ProtocolRejected('invalid_deadline')
    if type(data['core_timeout_seconds']) is not int or data['core_timeout_seconds'] != CORE_TIMEOUT:
        raise ProtocolRejected('invalid_core_timeout')
    for key,cap in [('max_pcm_bytes',MAX_PCM_BYTES),('max_chunk_bytes',MAX_CHUNK_BYTES),('max_chunks',MAX_CHUNKS)]:
        if type(data[key]) is not int or not 0 < data[key] <= cap:
            raise ProtocolRejected('invalid_limits')
    if data['max_pcm_bytes'] % 2 or data['max_chunk_bytes'] % 2 or data['max_chunk_bytes'] > data['max_pcm_bytes']:
        raise ProtocolRejected('invalid_limits')
    return dict(data)


@dataclass
class GrantReceiver:
    grant: dict = field(repr=False)
    validator: object = field(repr=False)
    issued_at: float
    expires_at: float
    sequence: int = 0
    phase: str = 'await_ack'
    end_sequence: int | None = None

    def accept(self, frame, now):
        if now >= self.expires_at or not math.isfinite(now) or now < self.issued_at:
            raise ProtocolRejected('expired')
        exact(frame, ('grant_id','connection_id','seq','kind','payload'))
        if frame['grant_id'] != self.grant['grant_id'] or frame['connection_id'] != self.grant['connection_id']:
            raise ProtocolRejected('wrong_grant')
        if type(frame['seq']) is not int or frame['seq'] != self.sequence+1:
            raise ProtocolRejected('sequence_conflict')
        kind,payload=frame['kind'],frame['payload']
        if self.phase in ('barrier','failed','consumed'):
            raise ProtocolRejected('grant_closed')
        if kind == 'ack':
            exact(payload, ('core_timeout_seconds',))
            if self.phase != 'await_ack' or payload['core_timeout_seconds'] != CORE_TIMEOUT or type(payload['core_timeout_seconds']) is not int:
                raise ProtocolRejected('invalid_ack')
            self.validator.capture_ack(request_started_at=self.issued_at, core_timeout_seconds=CORE_TIMEOUT)
            self.phase='streaming'
        elif kind == 'audio':
            exact(payload, ('audio','rate','width','channels'))
            if self.phase != 'streaming':raise ProtocolRejected('audio_out_of_order')
            self.validator.capture_audio(**payload)
        elif kind == 'end':
            exact(payload, ('overflow',))
            if self.phase != 'streaming':raise ProtocolRejected('end_out_of_order')
            self.validator.capture_end(**payload)
            self.phase='ended';self.end_sequence=frame['seq']
        elif kind == 'barrier':
            exact(payload, ('end_sequence',))
            if self.phase != 'ended' or type(payload['end_sequence']) is not int or payload['end_sequence'] != self.end_sequence:
                raise ProtocolRejected('invalid_barrier')
            self.phase='barrier'
        elif kind in ('error','canceled'):
            exact(payload, ())
            self.phase='failed'
            self.validator.transport_failed() if kind=='error' else self.validator.cancel()
        else:raise ProtocolRejected('unknown_frame')
        self.sequence=frame['seq']
        return {'accepted_sequence':self.sequence}


class GrantBook:
    """Backend connection lifetime ledger; never persisted or resumed."""
    def __init__(self, *, connection_id=None, clock=time.monotonic, max_grants=128):
        if type(max_grants) is not int or not 1 <= max_grants <= 128:raise ValueError('invalid_grant_limit')
        self.connection_id=identifier(connection_id or secrets.token_urlsafe(32))
        self.clock=clock
        self.max_grants=max_grants
        self.records={}
        self.closed=False

    def issue(self, validator, *, ttl_seconds=30):
        if self.closed or len(self.records)>=self.max_grants:raise ProtocolRejected('connection_closed')
        now=self.clock()
        remaining=min(ttl_seconds,validator.deadline-now)
        if remaining<=0:raise ProtocolRejected('expired')
        if sum(r.phase not in ('failed','consumed') for r in self.records.values()) >= 4:raise ProtocolRejected('worker_busy')
        if any(r.phase not in ('failed','consumed') and r.grant['source_device_id']==validator.source_device_id for r in self.records.values()):
            raise ProtocolRejected('source_busy')
        if any(r.grant['operation_id']==validator.operation_id for r in self.records.values()):
            raise ProtocolRejected('operation_replay')
        grant={'version':VERSION,'connection_id':self.connection_id,'grant_id':secrets.token_urlsafe(32),
            'operation_id':validator.operation_id,'source_device_id':validator.source_device_id,
            'question_context_id':validator.question_context_id,'valid_for_seconds':remaining,
            'core_timeout_seconds':CORE_TIMEOUT,'max_pcm_bytes':validator.max_pcm_bytes,
            'max_chunk_bytes':validator.max_chunk_bytes,'max_chunks':validator.max_chunks}
        validate_grant(grant,self.connection_id)
        self.records[grant['grant_id']]=GrantReceiver(grant,validator,now,now+remaining)
        return dict(grant)

    def receive(self, frame, *, connection_id):
        if self.closed or connection_id != self.connection_id:
            raise ProtocolRejected('wrong_connection')
        if not isinstance(frame,dict) or not isinstance(frame.get('grant_id'),str):
            raise ProtocolRejected('invalid_shape')
        record=self.records.get(frame['grant_id'])
        if record is None:raise ProtocolRejected('unknown_grant')
        try:return record.accept(frame,self.clock())
        except Exception:
            record.phase='failed'
            try:record.validator.transport_failed()
            except ValueError:pass
            # Do not expose nested errors or frame content across the wire.
            raise ProtocolRejected('frame_rejected') from None

    def consume(self, grant_id, *, expected_revision):
        if self.closed:raise ProtocolRejected('connection_closed')
        record=self.records.get(grant_id)
        if record is None or record.phase != 'barrier':raise ProtocolRejected('barrier_required')
        if self.clock() >= record.expires_at:
            record.phase='failed'
            try:record.validator.transport_failed()
            except ValueError:pass
            raise ProtocolRejected('expired')
        answer=record.validator.consume(expected_revision=expected_revision)
        record.phase='consumed'
        return answer

    def expire(self):
        """Backend deadline timer calls this; failure does not release source holds."""
        now=self.clock();count=0
        for record in self.records.values():
            if record.phase not in ('failed','consumed') and (not math.isfinite(now) or now>=record.expires_at):
                record.phase='failed';count+=1
                try:record.validator.transport_failed()
                except ValueError:pass
        return count

    def disconnect(self):
        self.closed=True
        for record in self.records.values():
            if record.phase=='consumed':continue
            record.phase='failed'
            try:record.validator.transport_failed()
            except ValueError:pass


class WorkerClaims:
    """Worker-side replay protection tied to one authenticated connection."""
    def __init__(self, connection_id, *, max_grants=128):
        if type(max_grants) is not int or not 1 <= max_grants <= 128:raise ValueError('invalid_grant_limit')
        self.connection_id=identifier(connection_id)
        self.seen=set();self.closed=False;self.max_grants=max_grants
    def claim(self, grant):
        if self.closed or len(self.seen)>=self.max_grants:raise ProtocolRejected('connection_closed')
        grant=validate_grant(grant,self.connection_id)
        if grant['grant_id'] in self.seen:raise ProtocolRejected('grant_replay')
        self.seen.add(grant['grant_id'])
        return grant
    def disconnect(self):self.closed=True
