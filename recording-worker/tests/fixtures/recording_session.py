"""Fail-closed validation of native question/capture events; no transport or HA I/O.

Only trusted backend callbacks may feed this object. It does not authenticate a
worker, stop a microphone, register listeners, or prove that an event queue drained.
The integration owns the question's HA Context and source registry snapshot. Audio
arrives over the authorized Core capture WebSocket, never from LLM tool arguments.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import math
import time
from collections.abc import Callable


class RecordingRejected(ValueError):
    """Fixed diagnostic category, never audio, transcript, URL or credentials."""


@dataclass(frozen=True)
class RecordedAnswer:
    operation_id: str
    source_device_id: str
    satellite_entity_id: str
    pcm: bytes = field(repr=False)
    transcript: str = field(repr=False)
    sample_rate: int = 16000
    sample_width: int = 2
    channels: int = 1


class RecordingSession:
    """One source, one native question, one bounded original PCM answer.

    This object must be driven on one event loop. The caller must retain a source
    lease and observe configuration/state/recording-start events until disposition.
    A failure clears content, but does not release that lease or prove mic silence.
    """

    def __init__(self, *, operation_id: str, source_device_id: str,
                 satellite_entity_id: str, question_context_id: str,
                 clock: Callable[[], float] = time.monotonic,
                 timeout_seconds: float = 35, max_pcm_bytes: int = 960_000,
                 max_chunk_bytes: int = 64_000, max_chunks: int = 4096):
        for value in (operation_id, source_device_id, satellite_entity_id, question_context_id):
            if not isinstance(value, str) or not value or len(value) > 255:
                raise ValueError('invalid_identity')
        if not satellite_entity_id.startswith('assist_satellite.'):
            raise ValueError('invalid_satellite')
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds < 60):
            raise ValueError('invalid_timeout')
        if any(type(n) is not int or n <= 0 for n in (max_pcm_bytes, max_chunk_bytes, max_chunks)):
            raise ValueError('invalid_bounds')
        if max_pcm_bytes > 960_000 or max_pcm_bytes % 2 or max_chunk_bytes > max_pcm_bytes or max_chunk_bytes % 2 or max_chunks > 4096:
            raise ValueError('invalid_bounds')
        self.operation_id = operation_id
        self.source_device_id = source_device_id
        self.satellite_entity_id = satellite_entity_id
        self.question_context_id = question_context_id
        self._clock = clock
        self.created_at = clock()
        if not math.isfinite(self.created_at):
            raise ValueError('invalid_clock')
        self.deadline = self.created_at + timeout_seconds
        self.max_pcm_bytes, self.max_chunk_bytes, self.max_chunks = max_pcm_bytes, max_chunk_bytes, max_chunks
        self._pcm = bytearray()
        self._transcript = None
        self._chunks = 0
        self._revision = 0
        self._owned_prompt = False
        self._capture_acked = False
        self._recording_starts = 0
        self._listening = False
        self._capture_ended = False
        self._question_done = False
        self._consumed = False
        self.failure: str | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._pcm)

    @property
    def source_release_proven(self) -> bool:
        """Only successful consumption is positive completion evidence here."""
        return self._consumed

    def _reject(self, category: str):
        if self._consumed:
            raise RecordingRejected('already_consumed')
        if self.failure is None:
            self.failure = category
        self._pcm.clear()
        self._transcript = None
        raise RecordingRejected(self.failure)

    def _check(self):
        if self.failure:
            raise RecordingRejected(self.failure)
        if self._consumed:
            raise RecordingRejected('already_consumed')
        now = self._clock()
        if not math.isfinite(now) or now < self.created_at or now >= self.deadline:
            self._reject('expired')
        return now

    def state_event(self, *, source_device_id: str, satellite_entity_id: str,
                    state: str, context_id: str | None):
        """Feed registry-resolved public source events, not a model-supplied room."""
        if source_device_id != self.source_device_id or satellite_entity_id != self.satellite_entity_id:
            return
        self._check()
        self._revision += 1
        if state not in ('idle', 'listening', 'processing', 'responding', 'unknown', 'unavailable'):
            self._reject('unexpected_source_state')
        if state in ('unknown', 'unavailable'):
            self._reject('source_unavailable')
        if state == 'responding':
            if context_id != self.question_context_id or self._owned_prompt:
                self._reject('question_ownership_conflict')
            self._owned_prompt = True
        elif state == 'listening':
            if not self._owned_prompt or not self._capture_acked or self._recording_starts != 1:
                self._reject('listening_before_capture_ready')
            if self._listening or self._capture_ended or self._question_done:
                self._reject('multiple_listening_turns')
            self._listening = True
        elif state == 'processing' and self._owned_prompt:
            self._reject('unexpected_intent_stage')
        # IDLE is allowed for prompt completion and response completion. It is
        # deliberately never used as capture ownership or mic-stop evidence.

    def recording_started(self, *, source_device_id: str):
        if source_device_id != self.source_device_id:
            return
        self._check()
        self._revision += 1
        self._recording_starts += 1
        if not self._owned_prompt or self._recording_starts != 1 or self._capture_ended:
            self._reject('capture_conflict')

    def capture_ack(self, *, request_started_at: float, core_timeout_seconds: float):
        now = self._check()
        self._revision += 1
        if not self._owned_prompt or self._capture_acked or self._listening or self._capture_ended:
            self._reject('invalid_capture_ack')
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
               for n in (request_started_at, core_timeout_seconds)):
            self._reject('invalid_capture_deadline')
        if (not self.created_at <= request_started_at <= now or not 0 < core_timeout_seconds <= 60
                or request_started_at + core_timeout_seconds <= self.deadline):
            # Core emits the same end for timeout as ordinary completion. Its
            # timeout must be strictly later than this session's own deadline.
            self._reject('unsafe_capture_deadline')
        self._capture_acked = True

    def capture_audio(self, *, audio: str, rate: int, width: int, channels: int):
        self._check()
        self._revision += 1
        if not self._listening or self._capture_ended:
            self._reject('audio_outside_answer')
        if any(type(n) is not int for n in (rate, width, channels)) or (rate, width, channels) != (16000, 2, 1):
            self._reject('invalid_audio_format')
        if not isinstance(audio, str) or len(audio) > 4 * ((self.max_chunk_bytes + 2) // 3):
            self._reject('audio_limit')
        try:
            chunk = base64.b64decode(audio, validate=True)
        except (binascii.Error, ValueError):
            self._reject('invalid_audio_encoding')
        self._chunks += 1
        if (not chunk or len(chunk) % 2 or len(chunk) > self.max_chunk_bytes
                or len(self._pcm) + len(chunk) > self.max_pcm_bytes or self._chunks > self.max_chunks):
            self._reject('audio_limit')
        self._pcm.extend(chunk)

    def capture_end(self, *, overflow: bool):
        self._check()
        self._revision += 1
        if type(overflow) is not bool or overflow:
            self._reject('capture_overflow')
        if not self._listening or not self._pcm or self._capture_ended:
            self._reject('invalid_capture_end')
        self._capture_ended = True

    def question_result(self, *, context_id: str, sentence: str):
        """Result of our exact service task, with its backend-retained Context."""
        self._check()
        self._revision += 1
        if context_id != self.question_context_id or not self._listening or self._question_done:
            self._reject('invalid_question_result')
        if not isinstance(sentence, str) or not sentence.strip() or len(sentence) > 4096:
            self._reject('invalid_question_result')
        self._transcript = sentence
        self._question_done = True

    def question_failed(self):
        self._reject('question_failed')

    def source_changed(self):
        """Call for registry/configuration changes or observed interruption."""
        self._reject('source_changed')

    def transport_failed(self):
        self._reject('transport_failed')

    def cancel(self):
        self._reject('canceled')

    def ready_revision(self) -> int | None:
        """Read only after all native event/transport branches have drained.

        This method does not perform or prove that drain. The transport adapter
        must test its own barrier; consumer must not await between read/consume.
        """
        self._check()
        if self._capture_ended and self._question_done and self._recording_starts == 1:
            return self._revision
        return None

    def consume(self, *, expected_revision: int) -> RecordedAnswer:
        self._check()
        if type(expected_revision) is not int or self.ready_revision() != expected_revision:
            self._reject('finalization_conflict')
        answer = RecordedAnswer(self.operation_id, self.source_device_id,
                                self.satellite_entity_id, bytes(self._pcm), self._transcript)
        self._pcm.clear()
        self._transcript = None
        self._consumed = True
        return answer
