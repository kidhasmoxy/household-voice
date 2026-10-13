"""One-use, task-context-bound provisional PCM; no disk, upload, or cropping."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import asyncio
import time

_CURRENT = ContextVar('household_voice_inline_capture', default=None)


@dataclass(frozen=True)
class InlineUtterance:
    profile_id: str
    source_device: str
    nonce: str
    pcm: bytes = field(repr=False)
    transcript: str = field(repr=False)
    body: str = field(repr=False)
    duration_seconds: float
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2


@dataclass(eq=False)
class _Holder:
    profile: str
    source: str
    nonce: str
    generation: object = field(repr=False)
    transcript: str = field(repr=False)
    body: str = field(repr=False)
    audio: bytearray = field(repr=False)
    deadline: float
    timer: object = field(default=None, repr=False)
    valid: bool = True

    def clear(self):
        self.valid = False
        self.audio.clear()
        self.transcript = self.body = ''
        if self.timer:
            self.timer.cancel()
            self.timer = None


class InlineCaptureStore:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.pending = {}

    def invalidate(self, profile):
        holder = self.pending.pop(profile, None)
        if holder:
            holder.clear()
            if _CURRENT.get() is holder:
                _CURRENT.set(None)

    def invalidate_current(self):
        holder = _CURRENT.get()
        if holder is not None:
            if self.pending.get(holder.profile) is holder:
                self.pending.pop(holder.profile)
            holder.clear()
            _CURRENT.set(None)

    def expire(self):
        for profile, holder in tuple(self.pending.items()):
            if self.clock() >= holder.deadline:
                self.invalidate(profile)

    def shutdown(self):
        for profile in tuple(self.pending):
            self.invalidate(profile)

    def publish(self, *, profile, source, nonce, generation, transcript, body, audio):
        """Take ownership of the bytearray; callers must not retain/mutate it."""
        self.invalidate(profile)
        holder = _Holder(profile, source, nonce, generation, transcript, body, audio, self.clock()+10)
        self.pending[profile] = holder
        def expired():
            # A stale timer must never invalidate a newer stream on this profile.
            if self.pending.get(profile) is holder:
                self.invalidate(profile)
        holder.timer = asyncio.get_running_loop().call_later(10, expired)
        _CURRENT.set(holder)

    def consume(self, profile, source, transcript, generation):
        """No awaits: exact current capability is removed before returning PCM."""
        self.expire()
        holder = self.pending.get(profile)
        if (holder is None or not holder.valid or _CURRENT.get() is not holder
                or holder.source != source or holder.generation is not generation
                or holder.transcript != transcript):
            return None
        result = InlineUtterance(profile, source, holder.nonce, bytes(holder.audio),
            holder.transcript, holder.body, len(holder.audio)/32000)
        self.invalidate(profile)
        return result
