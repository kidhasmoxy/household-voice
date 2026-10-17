"""Source-bound recorded broadcasts and a durable-outbox consumer.

HA 2026.10.1 assist_satellite.ask_question forces its response pipeline to end
at STT. Never use start_conversation/extra_system_prompt to collect the content:
that would let the recorded reply execute arbitrary conversation commands.
The explicit question turn is armed for ordinary two-turn capture. Optional
single-turn capture consumes a short-lived, task-bound microphone buffer and
requires validated alignment before uploading only the announcement body.
Uncertain trimming falls back to a fresh question, never whole-command playback.

Audio and text exist in memory until upload completes, then only in the bounded
coordinator recording store. Pending delivery rows survive HA restart. An expired
lease or a failed await after invocation is uncertain, never blindly replayed.
The coordinator must bind its authenticated capture profiles to the same devices.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import secrets
import time
from urllib.parse import urlsplit

from .announcement_adapter import AnnouncementAdapter
from .conversation_router import inline_announcement

_LOGGER = logging.getLogger(__name__)


class BroadcastError(ValueError):
    """Fixed errors only; no transcript, credentials, or audio URL."""


@dataclass
class _Session:
    device_id: str
    request_id: str
    targets: list
    context: object = field(repr=False)
    wrapper: object = field(repr=False)
    satellite: str
    task: object = field(default=None, repr=False)
    nonce: str | None = None
    capture: object = field(default=None, repr=False)
    inline: object = field(default=None, repr=False)
    phase: str = 'waiting_for_first_turn'
    broadcast_id: str | None = None


def _result(status, reason, **fields):
    return {'status': status, 'reason': reason, 'audio_delivery_confirmed': False,
            'phone_delivery_confirmed': False, **fields}


class BroadcastRuntime:
    def __init__(self, hass, entry, client, *, execution_authorized=False,
                 binding_resolver=None, adapter=None, clock=time.time,transport_policy=None):
        self.hass, self.entry, self.client = hass, entry, client
        self.options = deepcopy(dict(entry.options))
        self.execution_authorized = execution_authorized is True
        self.clock = clock
        self._binding_resolver = binding_resolver or self._resolve_binding
        parsed = urlsplit(client.url)
        self.adapter = adapter or AnnouncementAdapter(hass, self.options, client.resolve_recording,
            transport_policy=transport_policy,require_recording_provenance=hasattr(client,'_authority'),
            allowed_audio_origins=[f'{parsed.scheme}://{parsed.netloc}'],
            allowed_audio_path_prefix=parsed.path.rstrip('/')+'/audio/',
            execution_authorized=self.execution_authorized, focus_entry=entry)
        self.entities = {}
        self.sessions = {}
        self.completed = OrderedDict()
        self.latest = {}
        self._workers = []
        self._delivery_wakeups = []
        self._closed = False
        self._alignment_slots = asyncio.Semaphore(1)

    @property
    def inline_capture_enabled(self):
        return (self.enabled and self.options.get('stt_capture_enabled') is True
                and self.options.get('single_turn_announcements_enabled') is True
                and bool(self.options.get('announcement_alignment_url'))
                and bool(self.options.get('announcement_alignment_model')))

    @staticmethod
    def inline_announcement(text):
        return inline_announcement(text)

    async def _align_inline(self, captured):
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        from .announcement_alignment import AnnouncementAlignmentAdapter
        async with asyncio.timeout(15):
            async with self._alignment_slots:
                aligner = AnnouncementAlignmentAdapter(async_get_clientsession(self.hass),
                    endpoint=self.options['announcement_alignment_url'],
                    model=self.options['announcement_alignment_model'],
                    api_key=self.options.get('announcement_alignment_api_key', ''), timeout=15)
                return await aligner.align_announcement(captured)

    @property
    def enabled(self):
        return (not self._closed and self.execution_authorized
                and dict(self.entry.options) == self.options
                and self.options.get('live_actions_enabled') is True
                and self.options.get('announcements_enabled') is True)

    def register_capture_entities(self, entities):
        if self.sessions:
            raise BroadcastError('Cannot replace active capture bindings')
        values = {entity.source_device_id: entity for entity in entities}
        if len(values) != len(entities):
            raise BroadcastError('Capture source bindings must be unique')
        self.entities = values

    def _resolve_binding(self, device_id):
        from homeassistant.components.assist_pipeline import OPTION_PREFERRED, async_get_pipeline, async_get_pipelines
        from homeassistant.components.assist_satellite.const import DATA_COMPONENT, AssistSatelliteEntityFeature
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        device = dr.async_get(self.hass).async_get(device_id)
        mappings = [row for row in self.options.get('satellites', []) if row.get('satellite_device_id') == device_id]
        wrapper = self.entities.get(device_id)
        if device is None or device.disabled_by is not None or len(mappings) != 1 or wrapper is None or not wrapper.available:
            raise BroadcastError('Configure a dedicated capture profile for this voice device')
        satellite_id = mappings[0].get('satellite_entity_id')
        registered = er.async_get(self.hass).async_get(satellite_id) if satellite_id else None
        component = self.hass.data.get(DATA_COMPONENT)
        satellite = component.get_entity(satellite_id) if component and satellite_id else None
        if (registered is None or registered.disabled_by is not None or registered.device_id != device_id
                or satellite is None or not satellite.available
                or not satellite.supported_features & AssistSatelliteEntityFeature.START_CONVERSATION):
            raise BroadcastError('This device does not support the question workflow')

        def selected(entity):
            selected_id = None
            if selector := entity.pipeline_entity_id:
                state = self.hass.states.get(selector)
                if state is None:
                    raise BroadcastError('Selected Assist pipeline is unavailable')
                if state.state != OPTION_PREFERRED:
                    matches = [p.id for p in async_get_pipelines(self.hass) if p.name == state.state]
                    if len(matches) != 1:
                        raise BroadcastError('Select one dedicated Assist pipeline')
                    selected_id = matches[0]
            return async_get_pipeline(self.hass, selected_id)

        pipeline = selected(satellite)
        if pipeline.stt_engine != wrapper.entity_id:
            raise BroadcastError('Assign the dedicated capture STT entity to this satellite pipeline')
        # Reject visible shared bindings. STT metadata has no source ID, so
        # administrative exclusivity is still required for external API callers.
        for other in component.entities:
            if other.entity_id != satellite_id and other.available and selected(other).stt_engine == wrapper.entity_id:
                raise BroadcastError('The capture pipeline is shared by another satellite')
        return satellite_id, wrapper

    def _spawn(self, coro, name):
        return self.entry.async_create_background_task(self.hass, coro, name)

    async def async_start(self, device_id, context, *, target_rooms=None,
                          include_home_phones=True, request_id=None):
        from .native_recording_journal import source_is_held
        if source_is_held(self.hass,device_id):
            return _result("error","recording_source_requires_recovery")
        if not isinstance(device_id, str) or not device_id or context is None:
            return _result('clarify', 'trusted_voice_device_required')
        request_id = request_id or 'broadcast-' + secrets.token_hex(24)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            return _result('error', 'invalid_request_id')
        if self._closed:
            return _result('error', 'broadcast_runtime_closed')
        try:
            targets = self.adapter.targets(target_rooms, include_home_phones)
            satellite, wrapper = self._binding_resolver(device_id)
        except Exception:
            return _result('clarify', 'configure_exclusive_capture_pipeline_and_targets')
        signature = repr((device_id, targets))
        previous = self.completed.get(request_id)
        if previous:
            return deepcopy(previous[1]) if previous[0] == signature else _result('error', 'request_id_reused')
        active = self.sessions.get(device_id)
        if active:
            if active.request_id == request_id and active.targets == targets:
                return _result('queued', 'question_workflow_already_started')
            return _result('clarify', 'announcement_already_in_progress_on_this_device')
        if not self.enabled or self.options.get('stt_capture_enabled') is not True:
            return _result('dry_run', 'announcement_or_capture_execution_disabled', target_count=len(targets))
        session = _Session(device_id, request_id, targets, context, wrapper, satellite)
        self.sessions[device_id] = session
        session.task = self._spawn(self._run_question(session, signature), 'household voice broadcast question')
        return _result('queued', 'wait_for_prompt_then_speak_announcement', target_count=len(targets))

    async def _wait_idle(self, satellite):
        # The LLM tool must return before ask_question: Core cancels an existing
        # pipeline when starting the question. Await actual idle, including TTS.
        async with asyncio.timeout(45):
            while True:
                state = self.hass.states.get(satellite)
                if state is None or state.state in ('unknown', 'unavailable'):
                    raise BroadcastError('Satellite unavailable')
                if state.state == 'idle':
                    await asyncio.sleep(.25)
                    updated = self.hass.states.get(satellite)
                    if updated is not None and updated.state == 'idle':
                        return
                await asyncio.sleep(.1)

    async def async_start_inline(self, device_id, context, *, transcript, body, request_id):
        """Consume the exact current STT capability before scheduling any work."""
        from .native_recording_journal import source_is_held
        if source_is_held(self.hass,device_id):
            return _result("error","recording_source_requires_recovery")
        if (not isinstance(device_id, str) or not device_id or context is None
                or not isinstance(request_id, str) or not 1 <= len(request_id) <= 200
                or inline_announcement(transcript) != body):
            return _result('clarify', 'trusted_inline_announcement_required')
        try:
            targets = self.adapter.targets()
            satellite, wrapper = self._binding_resolver(device_id)
        except Exception:
            return _result('clarify', 'configure_exclusive_capture_pipeline_and_targets')
        signature = repr((device_id, targets, hashlib.sha256(transcript.encode()).hexdigest()))
        if previous := self.completed.get(request_id):
            return deepcopy(previous[1]) if previous[0] == signature else _result('error', 'request_id_reused')
        if active := self.sessions.get(device_id):
            if active.request_id == request_id and active.targets == targets:
                return _result('queued', 'inline_announcement_already_started')
            return _result('clarify', 'announcement_already_in_progress_on_this_device')
        captured = None
        if self.inline_capture_enabled:
            try:
                captured = wrapper.consume_inline(device_id, transcript)
            except Exception:
                pass  # No text, PCM, capability or provider error in diagnostics.
        if captured is None or captured.body != body:
            # Typed requests, stale capabilities and unavailable alignment all
            # use the existing explicit recording question, never synthesized audio.
            return await self.async_start(device_id, context, request_id=request_id)
        session = _Session(device_id, request_id, targets, context, wrapper, satellite, inline=captured)
        self.sessions[device_id] = session
        session.task = self._spawn(self._run_question(session, signature), 'household voice inline announcement')
        return _result('queued', 'inline_announcement_queued', target_count=len(targets))

    async def async_capture_ready(self, captured):
        """Local STT handoff only; do not upload until ask_question confirms it."""
        session = self.sessions.get(captured.source_device)
        if (session is None or session.phase != 'asking' or session.nonce != captured.nonce
                or session.wrapper.profile_id != captured.profile_id or session.capture is not None):
            raise BroadcastError('Capture does not belong to the active question')
        session.capture = captured

    async def _run_question(self, session, signature):
        result = _result('error', 'announcement_not_recorded')
        try:
            await self._wait_idle(session.satellite)
            if not self.enabled or self._binding_resolver(session.device_id) != (session.satellite, session.wrapper):
                raise BroadcastError('Capture binding changed')
            captured = None
            if session.inline is not None:
                session.phase = 'aligning'
                try:
                    captured = await self._align_inline(session.inline)
                    if (captured.profile_id != session.inline.profile_id
                            or captured.source_device != session.device_id
                            or captured.nonce != session.inline.nonce
                            or captured.transcript != session.inline.body):
                        captured = None
                        raise BroadcastError('Aligned capture binding mismatch')
                except Exception:
                    # Alignment uncertainty asks for a clean new recording. Never
                    # guess a cut, synthesize the body, or replay the command phrase.
                    pass
                finally:
                    session.inline = None
                if not self.enabled or self._binding_resolver(session.device_id) != (session.satellite, session.wrapper):
                    raise BroadcastError('Capture binding changed')
            if captured is None:
                from .native_recording_journal import source_is_held
                if source_is_held(self.hass,session.device_id):
                    raise BroadcastError("recording_source_requires_recovery")
                arm = session.wrapper.arm(session.device_id, ttl=30)
                session.nonce, session.phase = arm.nonce, 'asking'
                async with asyncio.timeout(50):
                    answer = await self.hass.services.async_call('assist_satellite', 'ask_question', {
                        'entity_id': session.satellite,
                        'question': "What's the announcement?",
                        'answers': [], 'preannounce': False}, blocking=True, return_response=True, context=session.context)
                text = answer.get('sentence') if isinstance(answer, dict) else None
                if isinstance(text, str) and text.casefold().strip().rstrip('.!?') in ('cancel', 'cancel announcement', 'never mind', 'nevermind'):
                    result = _result('cancelled', 'announcement_cancelled')
                    return
                captured = session.capture
                if captured is None or not isinstance(text, str) or not text.strip() or text != captured.transcript:
                    result = _result('error', 'silence_or_capture_response_mismatch')
                    return
            else:
                session.nonce = captured.nonce
            session.phase = 'uploading'
            # The durable nonce reservation makes same-nonce upload retries safe.
            # Enqueue uses a separate stable request ID; never create a new one
            # after an unknown HTTP outcome.
            row = None
            for attempt in range(3):
                try:
                    row = await self.client.upload_capture(captured)
                    break
                except Exception:
                    if attempt == 2:
                        raise BroadcastError('Capture upload failed') from None
                    await asyncio.sleep(.25 * (attempt + 1))
            session.capture = None
            session.inline = None
            for attempt in range(3):
                try:
                    queued = await self.client.enqueue_broadcast(row['id'], session.request_id, session.targets)
                    break
                except Exception:
                    if attempt == 2:
                        raise BroadcastError('Broadcast enqueue outcome unknown') from None
                    await asyncio.sleep(.25 * (attempt + 1))
            session.broadcast_id = queued['broadcast_id']
            self._wake_delivery_workers()
            result = _result('queued', 'recording_queued_for_delivery', broadcast_id=session.broadcast_id)
        except asyncio.CancelledError:
            result = _result('cancelled', 'announcement_cancelled_delivery_may_have_been_accepted')
            raise
        except TimeoutError as error:
            _LOGGER.warning('Announcement workflow failed in phase %s (%s)', session.phase, type(error).__name__)
            result = _result('error', 'question_timed_out_no_broadcast')
        except Exception as error:
            _LOGGER.warning('Announcement workflow failed in phase %s (%s)', session.phase, type(error).__name__)
            result = _result('error', 'announcement_workflow_failed')
        finally:
            if session.nonce:
                session.wrapper.cancel(session.device_id, nonce=session.nonce)
            session.capture = None
            session.inline = None
            self.sessions.pop(session.device_id, None)
            self.completed[session.request_id] = (signature, result)
            self.latest[session.device_id] = result
            while len(self.completed) > 256:
                self.completed.popitem(last=False)

    async def async_cancel(self, device_id):
        session = self.sessions.get(device_id)
        if session is None:
            previous = self.latest.get(device_id, {})
            if previous.get('broadcast_id'):
                try:
                    await self.client.cancel_broadcast(previous['broadcast_id'])
                except Exception:
                    return _result('error', 'cancellation_outcome_unknown')
                return _result('cancelled', 'pending_targets_cancelled_already_accepted_delivery_cannot_be_retracted')
            return _result('no_change', 'no_active_question_on_this_device')
        if session.task:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
        # A task cancelled before its first event-loop step never enters its
        # try/finally. Release that source binding explicitly as well.
        if self.sessions.get(device_id) is session:
            if session.nonce:
                session.wrapper.cancel(device_id,nonce=session.nonce)
            session.capture=None
            session.inline=None
            self.sessions.pop(device_id,None)
            self.latest[device_id]=_result('cancelled','question_cancelled_before_capture')
        return _result('cancelled', 'question_cancelled_pending_delivery_may_have_been_accepted')

    async def async_status(self, device_id):
        if active := self.sessions.get(device_id):
            return _result('in_progress', active.phase)
        latest = self.latest.get(device_id)
        if latest is None:
            return _result('no_change', 'no_announcement_on_this_device')
        if latest.get('broadcast_id'):
            try:
                return _result('delivery_status', 'service_outcomes_are_not_physical_delivery_proof',
                    delivery=await self.client.broadcast_status(latest['broadcast_id']))
            except Exception:
                return _result('error', 'delivery_status_unavailable')
        return deepcopy(latest)

    async def async_worker_once(self):
        """Lease one durable target. Safe to call after restart with a new runtime."""
        if not self.enabled:
            return False
        response = await self.client.pull_broadcast()
        delivery = response.get('delivery')
        if delivery is None:
            return False
        identifier, lease = delivery['id'], delivery['lease']
        async def valid():
            if not self.enabled or self.clock() >= min(delivery['expires'], delivery['lease_until']):
                return False
            validated=(await self.client.validate_broadcast(identifier, lease)).get('valid') is True
            return (validated and self.enabled
                    and self.clock() < min(delivery['expires'], delivery['lease_until']))
        outcome = 'permanent_failure'
        try:
            if not await valid():
                outcome = 'not_invoked'
            else:
                payload = delivery['payload']
                recording = delivery['recording']
                if payload['recording_id'] != recording['id']:
                    raise BroadcastError('Outbox recording mismatch')
                async with asyncio.timeout(60):
                    result = await self.adapter.deliver(payload['target'], recording, now=self.clock(), before_dispatch=valid)
                status = result.get('outcome')
                if result.get('provider_completed') is True or status == 'service_completed':
                    outcome = 'service_completed'
                elif result.get('service_accepted') is True:
                    outcome = 'service_accepted'
                elif status == 'service_outcome_unknown':
                    outcome = 'service_outcome_unknown'
                elif status in ('skipped_not_home_or_stale', 'expired', 'transcript_unavailable', 'dry_run'):
                    outcome = 'skipped'
                elif status in ('unavailable', 'audio_resolution_failed', 'lease_invalid', 'not_invoked'):
                    outcome = 'not_invoked'
        except asyncio.CancelledError:
            # Conservatively uncertain even if cancellation happened before the
            # final invocation boundary. Lost acknowledgements are not replayed.
            try:
                await asyncio.shield(self.client.ack_broadcast(identifier, lease, 'service_outcome_unknown'))
            except Exception:
                pass
            raise
        except TimeoutError:
            outcome = 'service_outcome_unknown'
        except (ValueError, KeyError, TypeError):
            outcome = 'permanent_failure'
        except Exception:
            outcome = 'service_outcome_unknown'
        await self.client.ack_broadcast(identifier, lease, outcome)
        return True

    def _wake_delivery_workers(self):
        # Each idle worker owns its event, so one cannot consume another's
        # wakeup. A newly recorded message need not wait for polling timers.
        for wakeup in self._delivery_wakeups:
            wakeup.set()

    async def async_start_worker(self):
        if self._workers or self._closed or not self.enabled:
            return
        async def loop(wakeup):
            while not self._closed:
                try:
                    more = await self.async_worker_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    more = False  # no credentials, URLs or transcript in logs
                if not more:
                    try:
                        await asyncio.wait_for(wakeup.wait(), timeout=2)
                    except TimeoutError:
                        pass
                    wakeup.clear()
        # One slot per configured destination prevents long announcements from
        # serializing the fifth and later speakers behind completed playback.
        # Include explicit room outputs and phones; the durable outbox still
        # leases/validates/acknowledges every destination independently.
        speakers = set(self.options.get('default_announcement_speakers', []))
        speakers.update(row['music_player'] for row in self.options.get('media_rooms', []))
        speakers.update(row['tts_media_player'] for row in self.options.get('satellites', []))
        phones = {row['notify_service'] for row in self.options.get('phones', []) if row.get('enabled') is True}
        worker_count = min(100, max(4, len(speakers) + len(phones)))
        self._delivery_wakeups = [asyncio.Event() for _ in range(worker_count)]
        self._workers = [self._spawn(loop(wakeup), 'household voice broadcast delivery')
                         for wakeup in self._delivery_wakeups]

    async def async_close(self):
        self._closed = True
        tasks = [s.task for s in self.sessions.values() if s.task] + self._workers
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for entity in self.entities.values():
            entity.cancel(entity.source_device_id)
            entity.manager.shutdown()
        self._workers.clear()
        self.entities.clear()
