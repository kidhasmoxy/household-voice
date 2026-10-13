"""Bounded, explicitly armed original-utterance capture; no HA actions or disk I/O.

Review contract (HA Core 2026.10.1, commit abe1fdd7cd6b4de1715c87b6fd87d92699514ef9):
SpeechToTextEntity.async_process_audio_stream(metadata, stream) is the supported
extension point. SpeechMetadata contains language/format/codec/bit_rate/sample_rate/
channel, NOT source identity. SpeechResult has text and result (success/error).
Each wrapper entity MUST have a dedicated Assist pipeline assigned exclusively to
one registered satellite. The trusted HA adapter arms it after the first command
turn has completed STT, before asking for the separate announcement utterance.
Never infer source identity from transcript, model arguments, or a global last-wake.

Sources:
https://developers.home-assistant.io/docs/core/entity/stt/
https://github.com/home-assistant/core/blob/abe1fdd7cd6b4de1715c87b6fd87d92699514ef9/homeassistant/components/stt/models.py
https://github.com/home-assistant/core/blob/abe1fdd7cd6b4de1715c87b6fd87d92699514ef9/homeassistant/components/wyoming/stt.py

The HA/Wyoming pipeline stream contains raw PCM, despite format=wav metadata. We
wrap captured PCM in a WAV container only after success. No idle audio buffering.
Normal STT receives unchanged chunks and its original result, including when a
capture is rejected. A concurrent stream on the same profile invalidates capture
of BOTH streams because metadata cannot disambiguate them. Different profiles
remain independent. Failure/cancellation/oversize/partial consumption discard.

Limits: arm <=30s; mono signed little-endian PCM16 at16kHz; .1..30s audio;
<=960000 PCM bytes; <=45s in-memory retention from stream entry (including STT);
<=4 concurrent captures by default. Expiry never cancels ordinary STT. Captured
results leave this manager immediately; caller owns onward retention, authenticated
transport, replay protection and actual delivery. This module cannot send audio.
Python buffer clearing releases references, not a forensic memory-erasure promise.

Integration gate: wire this into an STT entity, proxy the configured provider's
capabilities/audio_processing, require its check_metadata, and use the pinned
Core's state-updating internal_async_process_audio_stream delegation path (a Core
implementation helper, requiring a compatibility check on upgrade). Return
outcome.speech_result unchanged and
deliver outcome.capture only through the reviewed local coordinator client. Setup,
pipeline assignment, arm service, capture endpoint and live tests need approval.
Do not monkey-patch Core or silently wrap all existing pipelines.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import io
import math
import secrets
import time
from typing import Any
import wave

RATE = 16000
WIDTH = 2
MIN_BYTES = 3200
MAX_BYTES = 960000
MAX_TRANSCRIPT = 8000  # Matches the authenticated coordinator recording contract.


class CaptureError(ValueError):
    """Invalid binding or arm request. Never includes audio or transcript."""


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise CaptureError("Invalid registered identity")


def _value(value):
    return getattr(value, "value", value)


def _supported(metadata) -> bool:
    return all(not isinstance(actual := _value(getattr(metadata, key, None)), bool) and actual == value for key, value in (
        ("format", "wav"), ("codec", "pcm"), ("bit_rate", 16),
        ("sample_rate", RATE), ("channel", 1),
    ))


@dataclass(frozen=True)
class CaptureArm:
    profile_id: str
    source_device: str
    nonce: str
    expires_at: float


@dataclass(frozen=True)
class CapturedUtterance:
    profile_id: str
    source_device: str
    nonce: str
    wav_base64: str = field(repr=False)
    transcript: str = field(repr=False)
    duration_seconds: float
    sample_rate: int = RATE
    channels: int = 1
    sample_width: int = WIDTH


@dataclass(frozen=True)
class CaptureOutcome:
    speech_result: Any = field(repr=False)
    capture: CapturedUtterance | None = None
    capture_status: str = "not_armed"


@dataclass(eq=False)
class _Recording:
    arm: CaptureArm
    deadline: float
    audio: bytearray = field(default_factory=bytearray, repr=False)
    consumed: bool = False
    reason: str | None = None

    def discard(self, reason):
        if self.reason is None:
            self.reason = reason
        self.audio.clear()


class CaptureSessionManager:
    """One-event-loop manager, with immutable one-to-one profile/source bindings."""
    def __init__(self, profiles: Mapping[str, str], *, clock=time.monotonic,
                 nonce_factory=lambda: secrets.token_urlsafe(24), max_captures=4):
        if not profiles or len(profiles) > 64:
            raise CaptureError("Configure between one and 64 dedicated profiles")
        for profile, source in profiles.items():
            _identity(profile)
            _identity(source)
        if len(set(profiles.values())) != len(profiles):
            raise CaptureError("Each source requires exactly one dedicated profile")
        if type(max_captures) is not int or not 1 <= max_captures <= 16:
            raise CaptureError("Invalid concurrent capture limit")
        self._profiles = dict(profiles)
        self._clock, self._nonce_factory, self._limit = clock, nonce_factory, max_captures
        self._arms: dict[str, CaptureArm] = {}
        self._recordings: dict[str, _Recording] = {}
        self._streams: dict[str, set[object]] = {}
        self._closed = False

    def _bound(self, profile, source=None):
        _identity(profile)
        if profile not in self._profiles:
            raise CaptureError("Unregistered capture profile")
        if source is not None and self._profiles[profile] != source:
            raise CaptureError("Trusted source does not match dedicated profile")

    def arm(self, profile_id: str, trusted_source: str, *, ttl=30) -> CaptureArm:
        """Explicit two-turn request; caller must supply authenticated source context."""
        if self._closed:
            raise CaptureError("Capture manager has been shut down")
        _identity(trusted_source)
        self._bound(profile_id, trusted_source)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or not 0 < ttl <= 30:
            raise CaptureError("Arm TTL must be within 30 seconds")
        self.expire()
        if profile_id in self._arms or self._streams.get(profile_id):
            raise CaptureError("Profile is already armed or processing a stream")
        nonce = self._nonce_factory()
        _identity(nonce)
        arm = CaptureArm(profile_id, trusted_source, nonce, self._clock() + ttl)
        self._arms[profile_id] = arm
        return arm

    def cancel(self, profile_id: str, trusted_source: str, *, nonce: str | None = None) -> bool:
        """Cancel only the current arm/recording; optional nonce rejects late cancel."""
        _identity(trusted_source)
        self._bound(profile_id, trusted_source)
        arm = self._arms.get(profile_id)
        recording = self._recordings.get(profile_id)
        changed = False
        if arm and (nonce is None or nonce == arm.nonce):
            del self._arms[profile_id]
            changed = True
        if recording and (nonce is None or nonce == recording.arm.nonce):
            recording.discard("cancelled")
            changed = True
        return changed

    def expire(self) -> None:
        now = self._clock()
        for profile, arm in list(self._arms.items()):
            if now >= arm.expires_at:
                del self._arms[profile]
        for recording in self._recordings.values():
            if now >= recording.deadline:
                recording.discard("capture_timeout")

    def shutdown(self) -> None:
        """Discard captures on adapter unload; do not cancel unrelated STT tasks."""
        self._closed = True
        self._arms.clear()
        for recording in self._recordings.values():
            recording.discard("shutdown")

    async def process(self, profile_id: str, metadata, stream: AsyncIterable[bytes],
                      delegate: Callable[[Any, AsyncIterable[bytes]], Awaitable[Any]]) -> CaptureOutcome:
        """Proxy one STT call; consume the arm before the first asynchronous wait."""
        self._bound(profile_id)
        self.expire()
        streams = self._streams.setdefault(profile_id, set())
        concurrent = bool(streams)
        stream_token = object()
        streams.add(stream_token)
        arm = self._arms.pop(profile_id, None)  # Exactly one attempt, even if unsupported.
        recording = None
        status = "not_armed"
        if concurrent:
            if existing := self._recordings.get(profile_id):
                existing.discard("ambiguous_concurrent_stream")
            status = "ambiguous_concurrent_stream"
        elif arm:
            if not _supported(metadata):
                status = "unsupported_audio"
            elif sum(r.reason is None for r in self._recordings.values()) >= self._limit:
                status = "capture_capacity"
            else:
                recording = _Recording(arm, self._clock() + 45)
                self._recordings[profile_id] = recording
                status = "recording"

        async def tee():
            # The delegate owns consumption. Never drain more audio after it returns.
            try:
                async for chunk in stream:
                    if recording.reason is None:
                        if self._clock() >= recording.deadline:
                            recording.discard("capture_timeout")
                        elif not isinstance(chunk, bytes):
                            recording.discard("invalid_pcm_chunk")
                        elif len(recording.audio) + len(chunk) > MAX_BYTES:
                            recording.discard("audio_too_long")
                        else:
                            recording.audio.extend(chunk)
                            if len(recording.audio) >= 12 and recording.audio[:4] == b"RIFF" and recording.audio[8:12] == b"WAVE":
                                recording.discard("container_in_pcm_stream")
                    yield chunk
                recording.consumed = True
            finally:
                if not recording.consumed:
                    recording.discard("stream_not_consumed")

        wrapped = tee() if recording else stream
        expiry = (asyncio.get_running_loop().call_later(45, recording.discard, "capture_timeout")
                  if recording else None)
        try:
            speech_result = await delegate(metadata, wrapped)
            self.expire()
            captured = None
            if recording:
                if not recording.consumed:
                    recording.discard("stream_not_consumed")
                if _value(getattr(speech_result, "result", None)) != "success":
                    recording.discard("stt_failed")
                transcript = getattr(speech_result, "text", None)
                if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > MAX_TRANSCRIPT:
                    recording.discard("invalid_transcript")
                elif isinstance(transcript, str):
                    try:
                        transcript.encode("utf-8")
                    except UnicodeEncodeError:
                        recording.discard("invalid_transcript")
                if not MIN_BYTES <= len(recording.audio) <= MAX_BYTES or len(recording.audio) % WIDTH:
                    recording.discard("invalid_audio_length")
                if recording.reason is None:
                    with io.BytesIO() as output:
                        with wave.open(output, "wb") as wav:
                            wav.setnchannels(1)
                            wav.setsampwidth(WIDTH)
                            wav.setframerate(RATE)
                            wav.writeframes(recording.audio)
                        encoded = base64.b64encode(output.getvalue()).decode("ascii")
                    captured = CapturedUtterance(profile_id, recording.arm.source_device,
                                                recording.arm.nonce, encoded, transcript,
                                                len(recording.audio) / (RATE * WIDTH))
                    status = "captured"
                else:
                    status = recording.reason
            return CaptureOutcome(speech_result, captured, status)
        finally:
            if expiry:
                expiry.cancel()
            try:
                if recording:
                    recording.audio.clear()
                    if self._recordings.get(profile_id) is recording:
                        del self._recordings[profile_id]
                    await wrapped.aclose()
            finally:
                streams.discard(stream_token)
                if not streams:
                    self._streams.pop(profile_id, None)
