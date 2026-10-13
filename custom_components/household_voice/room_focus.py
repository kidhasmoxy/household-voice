"""One journaled volume authority shared by wake turns and announcements.

Only observed volume/playback ownership is restored. This is not hardware CAS.
No playback/group/queue operations occur here. An uncertain write remains in the
existing satellite restoration journal for operator review.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import secrets
import time

from .policy.routing import AudioFocus
from .policy.satellite_audio import AudioPlan, PlayerObservation, SatelliteAudio, VolumePlan
from .satellite_executor import SatelliteExecutor
from .satellite_journal import SQLiteJournal


@dataclass
class RoomFocusState:
    focus: object
    journal: object
    allowed_players: set = field(default_factory=set)
    lock: object = field(default_factory=asyncio.Lock)
    records: dict = field(default_factory=dict)
    blocked: set = field(default_factory=set)
    protected_players: dict = field(default_factory=dict)
    output_leases: dict = field(default_factory=dict)
    loaded: bool = False
    recovery_attempted: bool = False
    failed: bool = False
    maintenance_task: object = None


def _configured_players(options):
    return {player for row in options.get('satellites', []) for player in row.get('duck_media_players', [])}


def get_room_focus_state(hass, entry, *, clock=time.monotonic):
    """Reuse authority across adapters/reloads; never auto-clear quarantine."""
    states = hass.data.setdefault('_household_voice_room_focus', {})
    state = states.get(entry.entry_id)
    if state is None:
        state = RoomFocusState(AudioFocus(clock=clock), SQLiteJournal(hass.config.path(
            '.storage', 'household_voice_'+entry.entry_id+'_satellite.sqlite3')))
        states[entry.entry_id] = state
    state.allowed_players.update(_configured_players(entry.options))
    try:
        from .area_registry import collect_pairings
        snapshot = collect_pairings(hass, entry.options)
        state.allowed_players.update(snapshot.get('recovery_media_players', ()))
        for output in snapshot.get('outputs', {}).values():
            state.allowed_players.update(output.get('duck_media_players', ()))
            state.allowed_players.update(output.get('physical_aliases', ()))
    except Exception:
        pass  # An unavailable registry grants no additional duck targets.
    if state.maintenance_task is None or state.maintenance_task.done():
        controller = RoomVolumeController(hass, state)
        factory = getattr(hass, 'async_create_background_task', None)
        state.maintenance_task = (factory(_maintain_room_focus(state, controller), 'Household Voice room volume restoration')
            if callable(factory) else asyncio.create_task(_maintain_room_focus(state, controller)))
    return state


class _RoomPolicy:
    """Use the existing executor's observation checks without satellite events."""
    _observation = SatelliteAudio._observation

    def __init__(self, state):
        self.focus, self.clock = state.focus, state.focus.clock
        self.routes, self.sessions = {}, {}
        self.freshness = 2


class RoomVolumeController:
    def __init__(self, hass, state):
        self.hass, self.state = hass, state

    @staticmethod
    def normalize_volume(player, volume):
        return round(volume*100)/100

    def _state(self, player):
        state = self.hass.states.get(player)
        if state is None or state.state in {'unknown', 'unavailable'}:
            return None
        volume = state.attributes.get('volume_level')
        if type(volume) not in (float, int) or not math.isfinite(volume) or not 0 <= volume <= 1:
            return None
        identity = [state.attributes.get(k) for k in ('media_content_id', 'app_id', 'source', 'group_members')]
        generation = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
        return PlayerObservation(volume, generation, self.state.focus.clock())

    async def observe(self):
        return {player: observation for player in self.state.allowed_players
                if (observation := self._state(player)) is not None}

    async def set_volume(self, player, target, *, expected_generation, expected_volume, operation_id):
        from homeassistant.core import Context
        before = self._state(player)
        if (before is None or before.generation != expected_generation
                or abs(before.volume-expected_volume) > .00001
                or not self.hass.services.has_service('media_player', 'volume_set')):
            return 'rejected'
        try:
            await self.hass.services.async_call('media_player', 'volume_set',
                {'entity_id':player, 'volume_level':target}, blocking=True, context=Context())
            deadline = self.state.focus.clock()+2
            while self.state.focus.clock() < deadline:
                after = self._state(player)
                if after is None or after.generation != expected_generation:
                    return 'unknown'
                if abs(after.volume-target) <= .00001:
                    return 'confirmed'
                if abs(after.volume-expected_volume) > .00001:
                    return 'unknown'
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            return 'unknown'
        return 'accepted'


class AnnouncementRoomFocus:
    def __init__(self, state, controller):
        self.state = state
        self.executor = SatelliteExecutor(_RoomPolicy(state), controller, state.journal, {},
            enabled=True, execution_authorized=True, focus_state=state)

    async def _acquire(self, lease, players, outputs, factor):
        state, executor = self.state, self.executor
        state.allowed_players.update(players | outputs)
        await executor.recover()
        async with state.lock:
            await executor._ready()
            observations = await executor._observe()
            await executor._reconcile(observations)
            # Reserve outputs first. A prior wake lease may own their baseline.
            # Restore that owned duck before MA sets the announcement volume.
            for player in sorted(outputs):
                if not state.protected_players.get(player) and player in state.records:
                    await executor._restore(player, executor._observation(player, observations))
                state.protected_players[player] = state.protected_players.get(player, 0)+1
                state.output_leases.setdefault(lease, set()).add(player)
            changes = []
            previous_factor = state.focus.factor
            try:
                state.focus.factor = factor
                for player in sorted(players-outputs):
                    if state.protected_players.get(player):
                        continue
                    observation = executor._observation(player, observations)
                    if observation is None:
                        continue
                    volume = state.focus.acquire(player, lease, observation.volume, observation.generation)
                    # MA announcement transport has a bounded 60s wait. Avoid a
                    # wake release expiring this independent lease mid-announcement.
                    state.focus.players[player].leases[lease] = state.focus.clock()+120
                    changes.append(VolumePlan(player, volume, observation.generation, observation.volume, 'announcement'))
            finally:
                state.focus.factor = previous_factor
            await executor._execute(AudioPlan(tuple(changes)))
            # Volume confirmation may await several devices. Start the playback
            # lease window only after acquisition completes.
            for owned in state.focus.players.values():
                if lease in owned.leases:
                    owned.leases[lease] = state.focus.clock()+120

    async def _release(self, lease, outputs):
        state, executor = self.state, self.executor
        async with state.lock:
            try:
                # Preserve pending/unknown records even when acquisition was cancelled.
                observations = {} if state.failed else await executor._observe()
                if not state.failed:
                    await executor._reconcile(observations)
                changes = []
                for player, owned in list(state.focus.players.items()):
                    if lease not in owned.leases:
                        continue
                    observation = executor._observation(player, observations)
                    generation = observation.generation if observation else owned.generation
                    volume = state.focus.release(player, lease, generation)
                    if volume is not None and observation is not None:
                        changes.append(VolumePlan(player, volume, generation, observation.volume, 'restore'))
                if not state.failed:
                    await executor._execute(AudioPlan(tuple(changes)))
            finally:
                for player in state.output_leases.pop(lease, set()):
                    count = state.protected_players.get(player, 0)
                    if count <= 1: state.protected_players.pop(player, None)
                    else: state.protected_players[player] = count-1

    @asynccontextmanager
    async def hold(self, players, outputs, *, factor=.25):
        if type(factor) not in (int, float) or not math.isfinite(factor) or not 0 < factor <= 1:
            raise ValueError('Invalid room duck factor')
        lease = 'announcement-'+secrets.token_hex(16)
        outputs = set(outputs)
        try:
            async with asyncio.timeout(15):
                await self._acquire(lease, set(players), outputs, factor)
            yield
        finally:
            # A cancelled playback caller must not cancel the journaled release.
            release = asyncio.create_task(self._release(lease, outputs))
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError:
                await release
                raise


async def retry_room_focus(state, controller):
    """Retry only confirmed, unleased restoration; unknown writes stay quarantined."""
    if not state.loaded or not state.recovery_attempted or state.failed:
        return
    executor = AnnouncementRoomFocus(state, controller).executor
    async with state.lock:
        now = state.focus.clock()
        for player, owned in list(state.focus.players.items()):
            owned.leases = {lease:deadline for lease,deadline in owned.leases.items() if deadline > now}
            if not owned.leases:
                state.focus.players.pop(player, None)
        observations = await executor._observe()
        await executor._reconcile(observations)
        for player, record in list(state.records.items()):
            if (record['status'] == 'confirmed' and player not in state.focus.players
                    and not state.protected_players.get(player)):
                await executor._restore(player, executor._observation(player, observations))


async def release_policy_focus(state, policy):
    """Forget only a departing satellite policy's leases, not announcements."""
    async with state.lock:
        leases = {session.lease for session in policy.sessions.values()}
        for player, owned in list(state.focus.players.items()):
            for lease in leases:
                owned.leases.pop(lease, None)
            if not owned.leases:
                state.focus.players.pop(player, None)
        policy.sessions.clear()
    # The entry-owned maintenance loop retries the durable records, including
    # players that are currently unavailable. It outlives this satellite runtime.


async def _maintain_room_focus(state, controller):
    while True:
        await asyncio.sleep(1)
        try:
            await retry_room_focus(state, controller)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Failures retain records; never log signed URLs, transcripts or keys.
            pass


@asynccontextmanager
async def announcement_room_focus(hass, entry, speaker):
    """Discover competing physical speakers afresh for each configured output."""
    from .area_registry import collect_pairings
    snapshot = collect_pairings(hass, entry.options)
    output = snapshot.get('outputs', {}).get(speaker)
    if not output:
        yield
        return
    state = get_room_focus_state(hass, entry)
    authority = AnnouncementRoomFocus(state, RoomVolumeController(hass, state))
    async with authority.hold(output.get('duck_media_players', ()),
            {speaker, *output.get('physical_aliases', ())}):
        yield
