"""Room volume focus around unchanged native Assist playback.

HA's registered satellite state is the activity source: listening, processing,
responding hold focus; idle/unavailable release it. RUN_END is deliberately not a
release event because stock TTS can still be playing. This observes HA's native
completion, not acoustic delivery, and starts after native wake/network latency.
No conversation, microphone, wake model, player queue or firmware is modified.
"""
from __future__ import annotations

import asyncio
import math
import time

from homeassistant.core import callback

from .area_registry import collect_pairings
from .room_focus import AnnouncementRoomFocus, RoomVolumeController, get_room_focus_state

ACTIVE = frozenset({"listening", "processing", "responding"})


class NativeDuckRuntime:
    def __init__(self, hass, entry, *, discover=collect_pairings, authority=None,
                 poll_seconds=5, max_activity_seconds=300, idle_grace_seconds=.4, clock=time.monotonic):
        if not 0 < poll_seconds <= 30 or not 1 <= max_activity_seconds <= 600:
            raise ValueError("Invalid native activity bounds")
        self.hass, self.entry, self.discover = hass, entry, discover
        self.authority = authority
        self.poll_seconds, self.max_activity_seconds, self.clock = poll_seconds, max_activity_seconds, clock
        if not 0 <= idle_grace_seconds <= 1:
            raise ValueError("Invalid idle handoff grace")
        self.idle_grace_seconds = idle_grace_seconds
        self._closed = False
        self._paused = False
        self._runner = None
        self._remove = None
        self._changed = asyncio.Event()
        self._holds = {}
        self._assistants = set()
        self._watch_players = set()
        self._blocked = set()
        self._since = {}
        self._idle_since = {}
        self._observed_idle = set()
        self._last_error = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self):
        return (not self._closed and not self._paused
            and self.entry.options.get("live_actions_enabled") is True
            and self.entry.options.get("native_room_ducking_enabled") is True)

    @property
    def diagnostics(self):
        return {"enabled": self.enabled, "paused": self._paused,
            "mode": "native_conversation", "registered_assistants": len(self._assistants),
            "active_holds": len(self._holds), "watchdog_blocked": len(self._blocked),
            "idle_handoff_grace_seconds": self.idle_grace_seconds,
            "max_activity_seconds": self.max_activity_seconds, "last_error": self._last_error,
            "wake_detection": "registered_satellite_state_after_native_wake",
            "completion_basis": "native_satellite_idle_or_unavailable"}

    async def start(self):
        if self._closed or self._runner is not None:
            return self
        if self.enabled:
            if self.authority is None:
                state = get_room_focus_state(self.hass, self.entry)
                self.authority = AnnouncementRoomFocus(state, RoomVolumeController(self.hass, state))
            # Payload is only a wake-up hint. Every action rereads HA registries
            # and states; event-supplied state/device identities are not trusted.
            self._remove = self.hass.bus.async_listen("state_changed", self._on_state)
            await self.refresh()
            self._runner = asyncio.create_task(self._run())
        return self

    @callback
    def _on_state(self, event):
        identifier = event.data.get("entity_id")
        if identifier in self._assistants or identifier in self._watch_players:
            # Latch a real idle even if a rapid native follow-up becomes active
            # before the runner wakes. Never trust event-supplied new_state.
            if identifier in self._assistants:
                state = self.hass.states.get(identifier)
                if state is not None and state.state == 'idle':
                    self._observed_idle.add(identifier)
            self._changed.set()

    def _snapshot(self):
        actual = self.discover(self.hass, self.entry.options)
        # Discovery already excludes satellite-owned speakers, dynamic groups,
        # cross-area ambiguity and duplicate physical controls. Derive native
        # competing-speaker selection without writing options or actual pairing.
        options = dict(self.entry.options)
        options["assistant_speaker_overrides"] = {
            identifier: None for identifier in actual.get("assistants", {})}
        return self.discover(self.hass, options)

    async def refresh(self):
        async with self._lock:
            if not self.enabled:
                await self._release_all()
                return
            try:
                snapshot = self._snapshot()
            except Exception:
                self._last_error = "registry_unavailable"
                await self._release_all()
                return
            rows = snapshot.get("assistants", {})
            self._assistants = set(rows)
            self._watch_players = set(snapshot.get("recovery_media_players", ()))
            desired = {}
            new_turns = set()
            for identifier, row in rows.items():
                state = self.hass.states.get(identifier)
                if state is None or state.state not in ACTIVE:
                    # A real native idle ends the activity epoch immediately.
                    # The short volume handoff grace may bridge a follow-up,
                    # but must not charge its watchdog for the previous turn.
                    self._blocked.discard(identifier)
                    self._since.pop(identifier, None)
                    self._observed_idle.discard(identifier)
                    if state is not None and state.state == 'idle' and identifier in self._holds:
                        idle_since = self._idle_since.setdefault(identifier, self.clock())
                        if self.clock() - idle_since < self.idle_grace_seconds:
                            desired[identifier] = frozenset(self._holds[identifier])
                            continue
                    self._idle_since.pop(identifier, None)
                    continue
                if identifier in self._idle_since or identifier in self._observed_idle:
                    new_turns.add(identifier)
                    self._blocked.discard(identifier)
                    self._since.pop(identifier, None)
                self._observed_idle.discard(identifier)
                self._idle_since.pop(identifier, None)
                since = self._since.setdefault(identifier, self.clock())
                if self.clock() - since >= self.max_activity_seconds:
                    self._blocked.add(identifier)
                    self._last_error = "activity_watchdog_expired"
                if identifier in self._blocked:
                    continue
                endpoint = snapshot.get("outputs", {}).get(identifier)
                if (row.get("status") != "explicitly_unpaired" or not row.get("device_id")
                        or not endpoint or endpoint.get("available") is not True):
                    continue
                # An independently scheduled announcement owns its output. Do
                # not retain an empty native context that would prevent a fresh
                # duck after that output reservation ends during a long reply.
                protected = self.authority.state.protected_players
                players = frozenset(player for player in endpoint.get("duck_media_players", ())
                                    if not protected.get(player))
                if players:
                    desired[identifier] = players
            self._blocked.intersection_update(rows)
            self._observed_idle.intersection_update(rows)
            for identifier in set(self._since) - set(rows):
                self._since.pop(identifier, None)
            relinquished = set()
            if new_turns:
                relinquished = await self.authority.async_relinquished_players(
                    {player for identifier in new_turns for player in self._holds.get(identifier, {})})
            for identifier, contexts in tuple(self._holds.items()):
                for player, context in tuple(contexts.items()):
                    if (player in desired.get(identifier, ())
                            and not (identifier in new_turns and player in relinquished)):
                        continue
                    contexts.pop(player)
                    await context.__aexit__(None, None, None)
                if not contexts:
                    self._holds.pop(identifier, None)
            for identifier, players in desired.items():
                factor = .25
                for row in self.entry.options.get("satellites", ()):
                    if row.get("satellite_entity_id") == identifier:
                        factor = row.get("duck_factor", .25)
                        break
                if type(factor) not in (int, float) or not math.isfinite(factor) or not 0 < factor <= 1:
                    self._last_error = "invalid_duck_factor"
                    continue
                contexts = self._holds.setdefault(identifier, {})
                for player in sorted(players - contexts.keys()):
                    context = self.authority.hold({player}, set(), factor=factor, renew=True)
                    try:
                        await context.__aenter__()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._last_error = "focus_acquisition_failed"
                        continue
                    contexts[player] = context

    async def _release_all(self):
        for identifier, contexts in tuple(self._holds.items()):
            self._holds.pop(identifier, None)
            for context in contexts.values():
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    self._last_error = "focus_release_pending_journal_recovery"

    async def _run(self):
        try:
            while not self._closed:
                try:
                    timeout = min(self.poll_seconds, self.idle_grace_seconds) if self._idle_since else self.poll_seconds
                    await asyncio.wait_for(self._changed.wait(), timeout)
                except TimeoutError:
                    pass
                self._changed.clear()
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._last_error = "focus_update_failed"
        finally:
            async with self._lock:
                await self._release_all()

    async def async_pause_for_backup(self):
        self._paused = True
        await self.refresh()

    async def async_resume_after_backup(self):
        self._paused = False
        await self.refresh()

    async def async_close(self):
        self._closed = True
        if self._remove is not None:
            self._remove()
            self._remove = None
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        else:
            async with self._lock:
                await self._release_all()


async def async_setup_native_duck(hass, entry):
    return await NativeDuckRuntime(hass, entry).start()
