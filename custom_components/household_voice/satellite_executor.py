"""Review-only executor for the injected SatelliteAudio policy. Not registered.

There are deliberately no Home Assistant imports, bus listeners, services, or
automatic startup actions here. The future integration must inject:

* journal.load()/save(snapshot): atomic durable replacement, completed only after
  persistence succeeds (for example a reviewed HA Store adapter). No media URLs,
  transcripts, credentials, or audio enter this journal.
* controller.observe(): fresh PlayerObservation objects in the policy clock's
  domain; generation must identify playback/external volume ownership and remain
  meaningful after restart. controller.set_volume(..., operation_id=...) must
  compare the supplied generation AND volume immediately before changing them.
  It returns 'confirmed' only with a trusted resulting state observation;
  'accepted' means merely service acceptance. HA has no atomic volume CAS or
  durable command receipt. The concrete runtime explicitly uses best-effort
  observed state checks, with residual cross-controller races; service acceptance
  alone does not satisfy even that weaker confirmation.
* controller.play_reply(plan): resolves an adapter-owned reply ID, never a URL
  from an event; returns 'accepted', 'confirmed', 'rejected', or 'unknown'. Even
  'confirmed' here confirms starting playback, not completing it.
* verifier.verify(origin, satellite, device_id): authenticates a transport-bound
  opaque origin on every call. Bus event payload device_id/context alone is NOT
  proof. With no verifier, events and connection changes are unavailable.
* controller.reply_completed(proof, plan): authenticates external playback-end
  evidence. Device pipeline-end events and service acceptance are insufficient.
* controller.service_device(service) and complete(service, data): establish that
  the configured ESPHome completion action belongs to the configured device.

All methods are serialized. The adapter must call tick regularly and recover on
startup before enabling new sessions. Fresh reconnect, stop, error and TTL use
the policy's normal release logic. Observed manual changes relinquish ownership.
Journal writes precede duck/restore. Pending/accepted-only/crash-ambiguous records
are never automatically restored or retried. They require operator reconciliation.
This favors preserving human volume changes over an unprovable restoration.
Process death between confirmed hardware change and journal commit remains an
explicit ambiguity. No claim of exactly-once HA calls or physical confirmation.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import math
import re
import secrets


class ExecutorError(ValueError):
    """Sanitized configuration, provenance, or persistence failure."""


@dataclass(frozen=True)
class Registration:
    device_id: str
    completion_service: str

    def __post_init__(self):
        if not isinstance(self.device_id, str) or not self.device_id or len(self.device_id) > 200:
            raise ExecutorError('Invalid configured device')
        if not isinstance(self.completion_service, str) or not re.fullmatch(
                r'esphome\.[a-z0-9_]+_external_reply_complete', self.completion_service):
            raise ExecutorError('Expected a configured ESPHome completion action')


class MemoryJournal:
    """Test adapter only; NOT durable and never sufficient for a live installation."""
    def __init__(self, snapshot=None):
        self.snapshot = deepcopy(snapshot or {'version': 1, 'players': {}})
    async def load(self):
        return deepcopy(self.snapshot)
    async def save(self, snapshot):
        self.snapshot = deepcopy(snapshot)


def _volume(value):
    return type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1


def _same(a, b):
    return _volume(a) and _volume(b) and abs(a-b) < 0.00001


def _result(status):
    if not isinstance(status, str) or status not in {'accepted', 'confirmed', 'rejected', 'unknown'}:
        status = 'unknown'
    return {'outcome': status, 'service_accepted': True if status in {'accepted', 'confirmed'}
            else False if status == 'rejected' else None,
            'state_confirmed': status == 'confirmed', 'delivery_confirmed': False}


class SatelliteExecutor:
    def __init__(self, policy, controller, journal, registrations, *, verifier=None,
                 enabled=False, execution_authorized=False, focus_state=None):
        if set(registrations) != set(policy.routes) or not all(
                isinstance(r, Registration) for r in registrations.values()):
            raise ExecutorError('Every policy satellite needs an explicit registration')
        if len({r.device_id for r in registrations.values()}) != len(registrations):
            raise ExecutorError('Duplicate device registration')
        self.policy, self.controller, self.journal = policy, controller, journal
        self.focus_state = focus_state
        self.registrations, self.verifier = dict(registrations), verifier
        self.enabled = enabled is True and execution_authorized is True
        if focus_state is None:
            self.lock = asyncio.Lock()
            self.records = {}
            self.loaded = False
            self.recovery_attempted = False
            self.failed = False
            self.blocked = set()
        else:
            self.lock = focus_state.lock
            self.journal = focus_state.journal
            self.records = focus_state.records
            self.blocked = focus_state.blocked
            self.policy.focus = focus_state.focus
            focus_state.allowed_players.update(p for r in policy.routes.values() for p in r.duck_players)
        self.replies = {}

    @property
    def loaded(self):
        return self.focus_state.loaded if self.focus_state is not None else self._loaded

    @loaded.setter
    def loaded(self, value):
        if self.focus_state is not None: self.focus_state.loaded = value
        else: self._loaded = value

    @property
    def recovery_attempted(self):
        return self.focus_state.recovery_attempted if self.focus_state is not None else self._recovery_attempted

    @recovery_attempted.setter
    def recovery_attempted(self, value):
        if self.focus_state is not None: self.focus_state.recovery_attempted = value
        else: self._recovery_attempted = value

    @property
    def failed(self):
        return self.focus_state.failed if self.focus_state is not None else self._failed

    @failed.setter
    def failed(self, value):
        if self.focus_state is not None: self.focus_state.failed = value
        else: self._failed = value

    async def _trusted(self, origin, satellite):
        registration = self.registrations.get(satellite)
        if not registration or self.verifier is None:
            raise ExecutorError('Authenticated satellite origin is unavailable')
        try:
            trusted = await self.verifier.verify(origin, satellite, registration.device_id)
        except Exception:
            trusted = False
        if trusted is not True:
            raise ExecutorError('Authenticated satellite origin is unavailable')

    async def _save(self):
        try:
            await self.journal.save({'version': 1, 'players': deepcopy(self.records)})
        except Exception:
            self.failed = True
            raise ExecutorError('Restoration journal persistence failed; executor halted') from None

    async def _load(self):
        if self.failed:
            raise ExecutorError('Executor halted after persistence failure')
        if self.loaded:
            return
        try:
            snapshot = await self.journal.load()
            if set(snapshot) != {'version', 'players'} or type(snapshot['version']) is not int or snapshot['version'] != 1:
                raise ValueError()
            records = snapshot['players']
            configured = {p for r in self.policy.routes.values() for p in r.duck_players}
            if self.focus_state is not None:
                configured = self.focus_state.allowed_players
            if not isinstance(records, dict) or len(records) > 256:
                raise ValueError()
            for player, record in records.items():
                if player not in configured or set(record) != {'baseline', 'ducked', 'generation', 'operation', 'status'}:
                    raise ValueError()
                if not _volume(record['baseline']) or not _volume(record['ducked']):
                    raise ValueError()
                if any(not isinstance(record[k], str) or not record[k] or len(record[k]) > 200
                       for k in ('generation', 'operation')):
                    raise ValueError()
                if record['status'] not in {'pending', 'confirmed', 'restoring'}:
                    raise ValueError()
        except Exception:
            self.failed = True
            raise ExecutorError('Invalid or unavailable restoration journal') from None
        self.records.clear()
        self.records.update(deepcopy(records))
        self.loaded = True
        if not records:
            self.recovery_attempted = True

    async def _ready(self):
        await self._load()
        if self.records and not self.recovery_attempted:
            raise ExecutorError('Restore journal must be recovered before new sessions')

    async def _observe(self):
        try:
            return await self.controller.observe()
        except Exception:
            raise ExecutorError('Fresh player observation is unavailable') from None

    def _observation(self, player, observations):
        # Reuse the policy's exact freshness/generation validation, not a second
        # weaker interpretation of HA state. The policy is injected and pinned.
        return self.policy._observation(player, observations)

    async def _reconcile(self, observations):
        changed = False
        for player, record in list(self.records.items()):
            observation = self._observation(player, observations)
            if record['status'] != 'confirmed' or observation is None:
                continue
            if observation.generation != record['generation'] or not _same(observation.volume, record['ducked']):
                # A human or other player controller now owns the volume.
                del self.records[player]
                self.blocked.add(player)
                changed = True
        if changed:
            await self._save()

    async def _set(self, player, target, observation, operation):
        try:
            status = await self.controller.set_volume(
                player, target, expected_generation=observation.generation,
                expected_volume=observation.volume, operation_id=operation)
        except Exception:
            status = 'unknown'
        return _result(status)

    async def _restore(self, player, observation):
        record = self.records[player]
        if record['status'] != 'confirmed':
            return {'player': player, 'outcome': 'ambiguous_requires_review'}
        if observation is None:
            return {'player': player, 'outcome': 'deferred_unavailable'}
        if observation.generation != record['generation'] or not _same(observation.volume, record['ducked']):
            del self.records[player]
            await self._save()
            return {'player': player, 'outcome': 'manual_change_preserved'}
        record['status'], record['operation'] = 'restoring', secrets.token_hex(16)
        await self._save()
        result = await self._set(player, record['baseline'], observation, record['operation'])
        if result['state_confirmed']:
            del self.records[player]
            await self._save()
        return {'player': player, **result}

    async def _execute(self, plan):
        outcomes = []
        for change in plan.volumes:
            if self.focus_state is not None and self.focus_state.protected_players.get(change.player, 0):
                outcomes.append({'player': change.player, 'outcome': 'active_output_preserved'})
                continue
            observations = await self._observe()
            observation = self._observation(change.player, observations)
            await self._reconcile(observations)
            record = self.records.get(change.player)
            if change.reason == 'restore':
                if record:
                    outcomes.append(await self._restore(change.player, observation))
                self.blocked.discard(change.player)
                continue
            if change.player in self.blocked:
                outcomes.append({'player': change.player, 'outcome': 'manual_change_preserved'})
                continue
            if record:
                # Overlapping leases reuse the first baseline and duck command.
                outcomes.append({'player': change.player, 'outcome': 'already_owned' if record['status'] == 'confirmed'
                                 else 'ambiguous_requires_review'})
                continue
            if (observation is None or observation.generation != change.expected_generation
                    or not _same(observation.volume, change.expected_volume)):
                self.blocked.add(change.player)
                outcomes.append({'player': change.player, 'outcome': 'observation_changed'})
                continue
            target = (self.controller.normalize_volume(change.player, change.volume)
                      if hasattr(self.controller, 'normalize_volume') else change.volume)
            self.records[change.player] = {
                'baseline': observation.volume, 'ducked': target,
                'generation': observation.generation, 'operation': secrets.token_hex(16), 'status': 'pending'}
            await self._save()  # Must precede the first irreversible service await.
            record = self.records[change.player]
            result = await self._set(change.player, target, observation, record['operation'])
            if result['state_confirmed']:
                record['status'] = 'confirmed'
                await self._save()
            elif result['outcome'] == 'rejected':
                del self.records[change.player]
                self.blocked.add(change.player)
                await self._save()
            outcomes.append({'player': change.player, **result})
        if plan.reply:
            try:
                status = await self.controller.play_reply(plan.reply)
            except Exception:
                status = 'unknown'
            outcomes.append({'reply_session': plan.reply.session, **_result(status)})
        self.blocked.intersection_update(self.policy.focus.players)
        return outcomes

    async def recover(self):
        """Startup recovery; disabled means no journal/controller I/O at all."""
        if not self.enabled:
            return [{'outcome': 'disabled'}]
        async with self.lock:
            # A second adapter sharing this authority must not recover live
            # leases or re-run a prior recovery attempt.
            if self.focus_state is not None and self.recovery_attempted:
                return []
            if self.policy.sessions:
                raise ExecutorError('Startup recovery cannot run during active sessions')
            await self._load()
            self.recovery_attempted = True
            outcomes = []
            for player in list(self.records):
                observations = await self._observe()
                outcomes.append(await self._restore(player, self._observation(player, observations)))
            return outcomes

    async def connect(self, origin, satellite):
        if not self.enabled:
            return {'outcome': 'disabled'}
        async with self.lock:
            await self._trusted(origin, satellite)
            await self._ready()
            epoch = secrets.token_hex(16)  # Never trust a device-supplied epoch.
            observations = await self._observe()
            await self._reconcile(observations)
            plan = self.policy.connect(satellite, epoch, observations)
            self.replies = {k: v for k, v in self.replies.items() if k[0] != satellite}
            return {'connection': epoch, 'actions': await self._execute(plan)}

    async def disconnect(self, origin, satellite):
        if not self.enabled:
            return [{'outcome': 'disabled'}]
        async with self.lock:
            await self._trusted(origin, satellite)
            await self._ready()
            observations = await self._observe()
            await self._reconcile(observations)
            plan = self.policy.disconnect(satellite, observations)
            self.replies = {k: v for k, v in self.replies.items() if k[0] != satellite}
            return await self._execute(plan)

    async def handle(self, origin, event, *, completion=None, playback_proof=None):
        if not self.enabled:
            return [{'outcome': 'disabled'}]
        async with self.lock:
            await self._trusted(origin, event.satellite)
            await self._ready()
            if (self.policy.connections.get(event.satellite) == event.connection
                    and type(event.sequence) is int
                    and event.sequence <= self.policy.sequences.get(event.satellite, -1)):
                return [{'outcome': 'replay_ignored'}]
            key = (event.satellite, event.connection, event.session)
            pending = self.replies.get(key)
            if event.kind == 'finished':
                if not pending:
                    raise ExecutorError('No owned external reply is awaiting completion')
                try:
                    confirmed = await self.controller.reply_completed(playback_proof, pending[0])
                except Exception:
                    confirmed = False
                if confirmed is not True:
                    raise ExecutorError('External playback completion is unconfirmed')
            if completion is not None:
                if (event.kind != 'reply_ready' or not isinstance(completion, dict)
                        or set(completion) != {'boot_id', 'connection', 'turn'}
                        or not isinstance(completion['boot_id'], str) or not 1 <= len(completion['boot_id']) <= 64
                        or any(type(completion[k]) is not int or completion[k] < 0 for k in ('connection', 'turn'))):
                    raise ExecutorError('Invalid trusted completion context')
            observations = await self._observe()
            await self._reconcile(observations)
            plan = self.policy.handle(event, observations)
            results = await self._execute(plan)
            if plan.reply:
                self.replies[key] = (plan.reply, deepcopy(completion))
            if event.kind in {'stop', 'error', 'finished'}:
                self.replies.pop(key, None)
            if event.kind == 'finished' and pending and pending[1] is not None:
                registration = self.registrations[event.satellite]
                try:
                    owner = await self.controller.service_device(registration.completion_service)
                    if owner != registration.device_id:
                        raise ExecutorError('Configured completion action ownership is unverified')
                    status = await self.controller.complete(registration.completion_service,
                                                            {**pending[1], 'success': True})
                except ExecutorError:
                    raise
                except Exception:
                    status = 'unknown'
                results.append({'completion': True, **_result(status)})
            return results

    async def tick(self):
        if not self.enabled:
            return [{'outcome': 'disabled'}]
        async with self.lock:
            await self._ready()
            observations = await self._observe()
            await self._reconcile(observations)
            plan = self.policy.tick(observations)
            active = {key for key, session in self.policy.sessions.items() if not session.ended}
            self.replies = {k: v for k, v in self.replies.items() if k in active}
            results = await self._execute(plan)
            # The policy may have released its lease before a later controller
            # observation went unavailable. The durable record must retain that
            # restoration obligation after the in-memory session disappears.
            for player in list(self.records):
                if player not in self.policy.focus.players and self.records[player]['status'] == 'confirmed':
                    observations = await self._observe()
                    results.append(await self._restore(player, self._observation(player, observations)))
            return results
