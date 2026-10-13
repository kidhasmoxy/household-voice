"""Concrete external-reply runtime; installed only by an explicitly enabled entry.

Native events come from APIClient.subscribe_service_calls on the configured
encrypted ESPHome connection, NOT hass.bus. The callback binds the actual loaded
config entry, device registry MAC and client; event-supplied identities are rejected.
HA 2026.10.1 and aioesphomeapi public subscription APIs are required. Since that
subscription exposes no remover, its callback is weak and inert after unload.

Volume ownership is best-effort: observed playback fingerprint + volume checks,
then HA volume_set and an observed resulting volume. HA offers no atomic CAS.
Changes to a different volume/playback preserve human control; an indistinguishable
simultaneous adjustment to exactly the same value cannot be detected. Journal
recovery likewise uses observed values, not a claim of durable hardware receipts.
MA workflow completion is not acoustic delivery proof. Cancelling a native Sonos
clip prevents further work but may let the in-flight clip finish; never generic stop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import re
import time
from urllib.parse import urlsplit
import weakref

from homeassistant.components.esphome.manager import build_service_name
from homeassistant.core import Context, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.network import get_url

from .policy.satellite_audio import SatelliteAudio, SatelliteEvent, SatelliteRoute, PlayerObservation
from .satellite_executor import SatelliteExecutor, Registration, ExecutorError
from .satellite_journal import SQLiteJournal
from .room_focus import get_room_focus_state, release_policy_focus
from .satellite_transport import MAAnnouncementTransport, AudioTransportError


EVENT = 'esphome.household_voice_external'
PACKET_FIELDS = {'version','boot_id','connection','turn','sequence','kind','media_url','detail'}


class SatelliteDependencyNotReady(ExecutorError):
    """Configured MA or ESPHome runtime is temporarily unavailable at startup."""


@dataclass
class NativeBinding:
    device_id: str
    entry: object
    data: object
    origin: object | None = None
    attached: bool = False
    epoch: str | None = None
    boot: str | None = None
    native_connection: int | None = None
    last_sequence: int = -1
    sequence: int = 0


class HAAudioController:
    def __init__(self, runtime):
        self.runtime, self.hass = runtime, runtime.hass
        self.transport = MAAnnouncementTransport(self.hass, {r.tts_player for r in runtime.policy.routes.values()},
            spoken_media_policy=runtime.entry.options.get('spoken_media_policy','pause_and_resume'))
        self.media = {}
        self.tasks = {}
        self.active_players = set()
        self.players_by_session = {}
        self.proofs = {}

    @staticmethod
    def normalize_volume(player, volume):
        return round(volume*100)/100

    def _state(self, player):
        state = self.hass.states.get(player)
        if state is None or state.state in {'unknown','unavailable'}:
            return None
        volume = state.attributes.get('volume_level')
        if type(volume) not in (float,int) or not math.isfinite(volume) or not 0<=volume<=1:
            return None
        identity = [state.attributes.get(k) for k in ('media_content_id','app_id','source','group_members')]
        generation = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
        return PlayerObservation(volume, generation, self.runtime.clock())

    async def observe(self):
        players = {p for r in self.runtime.policy.routes.values() for p in r.duck_players}
        players.update(self.runtime.focus_state.allowed_players)
        return {p: observation for p in players if p not in self.active_players and (observation := self._state(p))}

    async def set_volume(self, player, target, *, expected_generation, expected_volume, operation_id):
        before = self._state(player)
        if before is None or before.generation != expected_generation or abs(before.volume-expected_volume)>0.00001:
            return 'rejected'
        if not self.hass.services.has_service('media_player','volume_set'):
            return 'rejected'
        try:
            await self.hass.services.async_call('media_player','volume_set',
                {'entity_id':player,'volume_level':target}, blocking=True, context=Context())
            deadline = self.runtime.clock()+2
            while self.runtime.clock()<deadline:
                after = self._state(player)
                if after is None or after.generation != expected_generation:
                    return 'unknown'
                if abs(after.volume-target)<=0.00001:
                    return 'confirmed'  # Observed HA state, never atomic hardware ownership.
                if abs(after.volume-expected_volume)>0.00001:
                    return 'unknown'
                await asyncio.sleep(.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            return 'unknown'
        return 'accepted'

    async def play_reply(self, plan):
        media = self.media.pop(plan.reply_id, None)
        if media is None or plan.session in self.tasks:
            return 'rejected'
        try:
            self.transport.resolve(plan.player)
        except AudioTransportError:
            self.runtime.enqueue(('provider_end', plan.session, False, None))
            return 'rejected'
        # One MA call per reply; the executor lock must not wait for its duration.
        self.active_players.add(plan.player)
        self.players_by_session[plan.session] = plan.player
        self.tasks[plan.session] = asyncio.create_task(self._play(plan, media))
        return 'accepted'

    async def _play(self, plan, media):
        proof = None
        async def still_current():
            identity = self.runtime.sessions.get(plan.session)
            if self.runtime.closed or identity is None:
                return False
            device, epoch = identity
            binding = self.runtime.bindings[device]
            state = self.runtime.policy.sessions.get((device,epoch,plan.session))
            return bool(binding.attached and binding.epoch == epoch and state
                        and not state.ended and state.expires > self.runtime.clock()
                        and state.deadline > self.runtime.clock() and self.runtime._route_current(device,plan.player))
        try:
            result = await self.transport.play(plan.player, media['url'], volume=media['volume'], chime=False,
                                               before_dispatch=still_current)
            success = result['provider_completed']
            if success:
                proof = object()
                self.proofs[plan.session] = proof
        except asyncio.CancelledError:
            raise
        except Exception:
            success = False
        finally:
            self.tasks.pop(plan.session, None)
            self.players_by_session.pop(plan.session, None)
            self.active_players = set(self.players_by_session.values())
        self.runtime.enqueue(('provider_end', plan.session, success, proof))

    async def reply_completed(self, proof, plan):
        return proof is not None and self.proofs.pop(plan.session, None) is proof

    def _action(self, full_name):
        for binding in self.runtime.bindings.values():
            if not binding.data.available or binding.entry.runtime_data is not binding.data:
                continue
            info = binding.data.device_info
            for service in binding.data.services.values():
                if 'esphome.'+build_service_name(info, service) == full_name and service.name == 'external_reply_complete':
                    return binding, service
        return None

    async def service_device(self, service):
        action = self._action(service)
        return action[0].device_id if action else None

    async def complete(self, service, data):
        action = self._action(service)
        if action is None or {arg.name for arg in action[1].args} != {'boot_id','connection','turn','success'}:
            return 'rejected'
        try:
            response = action[0].data.client.execute_service(action[1], data)
            if inspect.isawaitable(response):
                await response
        except Exception:
            return 'unknown'
        return 'accepted'


class SatelliteRuntime:
    def __init__(self, hass, entry, *, clock=time.monotonic):
        self.hass, self.entry, self.clock = hass, entry, clock
        self.rows = {r['satellite_device_id']: dict(r) for r in entry.options.get('satellites',[])}
        if len(self.rows) != len(entry.options.get('satellites',[])):
            raise ExecutorError('Duplicate satellite registration')
        for row in self.rows.values():
            for key, default, minimum in (('reply_volume',.5,0),('duck_factor',.25,0)):
                value = row.get(key,default)
                if type(value) not in (int,float) or not math.isfinite(value) or not minimum<=value<=1 or (key=='duck_factor' and value==0):
                    raise ExecutorError('Invalid satellite audio options')
        routes = {device: SatelliteRoute(r['room_id'], r['tts_media_player'], tuple(r.get('duck_media_players',[])))
                  for device,r in self.rows.items()}
        self.policy = SatelliteAudio(routes, clock=clock)
        self.focus_state=get_room_focus_state(hass,entry,clock=clock)
        self.policy.focus=self.focus_state.focus
        self.bindings = {}
        self.controller = HAAudioController(self)
        self.executor = SatelliteExecutor(self.policy, self.controller,
            self.focus_state.journal,
            {device: Registration(device, r['completion_service']) for device,r in self.rows.items()},
            verifier=self, enabled=True, execution_authorized=True,focus_state=self.focus_state)
        self.queue = asyncio.Queue(maxsize=256)
        self.closed = False
        self.unsubscribers = []
        self.worker = self.ticker = None
        self.sessions = {}
        self.session_routes = {}
        self.errors = []
        self.allowed_origins = set()
        for candidate in (getattr(hass.config,'internal_url',None),getattr(hass.config,'external_url',None)):
            if candidate:
                parsed = urlsplit(candidate)
                self.allowed_origins.add(f'{parsed.scheme}://{parsed.netloc}')
        try:
            parsed = urlsplit(get_url(hass, prefer_internal=True))
            self.allowed_origins.add(f'{parsed.scheme}://{parsed.netloc}')
        except Exception:
            pass

    def enqueue(self, item):
        if self.closed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.errors[:] = ['satellite_queue_overflow']

    def _discovered_route(self,device):
        from .area_registry import collect_pairings
        row=self.rows[device]
        snapshot=collect_pairings(self.hass,self.entry.options)
        assistant=row.get('satellite_entity_id')
        if assistant:
            binding=snapshot['assistants'].get(assistant,{})
            if binding.get('status')!='ready': return None
            return SatelliteRoute(binding['area_id'],binding['speaker_id'],tuple(binding['duck_speakers']))
        # Legacy output-less transport is still explicitly installed. Its exact
        # output must be safe; automatic discovery never creates registrations.
        output=snapshot['outputs'].get(row['tts_media_player'])
        if not output or not output['available']: return None
        return SatelliteRoute(output['area_id'],row['tts_media_player'],tuple(output['duck_media_players']))

    def _route_current(self,device,player):
        try:
            route=self._discovered_route(device)
            return route is not None and route.tts_player==player and route.room==self.policy.routes[device].room
        except Exception:
            return False

    def _refresh_route(self,device):
        route=self._discovered_route(device)
        if route is None: return False
        # Freeze the output for a turn; a new wake invalidates old turns in the
        # existing policy. Every actual reply revalidates the current pairing.
        self.policy.routes[device]=route
        self.focus_state.allowed_players.update(route.duck_players)
        self.controller.transport.allowed_players=frozenset(r.tts_player for r in self.policy.routes.values())
        return True

    async def verify(self, origin, satellite, device_id):
        binding = self.bindings.get(satellite)
        return bool(binding and device_id == binding.device_id and origin is binding.origin
                    and binding.data.available and binding.entry.runtime_data is binding.data)

    def _bind_device(self, device):
        registered = dr.async_get(self.hass).async_get(device)
        if registered is None or registered.disabled_by is not None:
            raise ExecutorError('Configured ESPHome device is unavailable')
        entries = [self.hass.config_entries.async_get_entry(registered.config_entry_id)]
        entries = [e for e in entries if e and e.domain == 'esphome']
        if len(entries)!=1:
            raise ExecutorError('A single ESPHome entry is required')
        entry, = entries
        data = getattr(entry,'runtime_data',None)
        if data is None or not getattr(data,'available',False) or getattr(data,'device_info',None) is None:
            raise SatelliteDependencyNotReady('Configured ESPHome runtime is not ready')
        if not entry.data.get('noise_psk'):
            raise ExecutorError('Encrypted ESPHome native API is required')
        mac = str(getattr(data.device_info,'mac_address','')).lower().replace(':','')
        expected = str(entry.unique_id or '').lower().replace(':','')
        # ESPHome registry entries can use a MAC connection without an identifier.
        # Keep the config-entry and live encrypted-client checks below; neither a
        # name nor an event payload is an authenticated registry identity.
        identities = {str(value).lower().replace(':','') for domain,value in registered.identifiers if domain == 'esphome'}
        identities.update(str(value).lower().replace(':','')
                          for kind,value in getattr(registered,'connections',()) if kind == 'mac')
        if not mac or mac != expected or mac not in identities:
            raise ExecutorError('ESPHome entry, native device and registry identity do not match')
        binding = NativeBinding(device, entry, data)
        self.bindings[device] = binding
        reference = weakref.ref(self)
        @callback
        def changed():
            runtime = reference()
            if runtime is not None and not runtime.closed:
                runtime.enqueue(('connection',device))
        self.unsubscribers.append(data.async_subscribe_device_updated(changed))
        self.enqueue(('connection',device))

    async def start(self):
        for device in self.rows:
            entity=er.async_get(self.hass).async_get(self.rows[device]['tts_media_player'])
            if entity is not None and entity.platform=='music_assistant' and entity.disabled_by is None:
                owner=self.hass.config_entries.async_get_entry(entity.config_entry_id)
                mass=getattr(getattr(owner,'runtime_data',None),'mass',None)
                if mass is None:
                    raise SatelliteDependencyNotReady('Configured Music Assistant runtime is not ready')
                player=mass.players.get(entity.unique_id)
                if player is None or getattr(player,'available',False) is not True:
                    raise SatelliteDependencyNotReady('Configured Music Assistant speaker is not ready')
            self.controller.transport.resolve(self.rows[device]['tts_media_player'])
            self._bind_device(device)
        await self.executor.recover()
        self.worker = asyncio.create_task(self._worker())
        self.ticker = asyncio.create_task(self._ticker())
        await self.queue.join()
        return self

    async def _connection(self, device):
        binding = self.bindings[device]
        available = binding.data.available and binding.entry.runtime_data is binding.data
        if not available:
            if binding.attached:
                # The old origin is intentionally not trusted after disconnect;
                # lifecycle authority here comes from the pinned runtime object.
                async with self.executor.lock:
                    observations = await self.controller.observe()
                    await self.executor._execute(self.policy.disconnect(device,observations))
                binding.attached, binding.origin, binding.epoch = False, None, None
                self.sessions = {k:v for k,v in self.sessions.items() if v[0]!=device}
                self.session_routes = {k:v for k,v in self.session_routes.items() if k in self.sessions}
            return
        if binding.attached:
            return
        binding.origin, binding.attached = object(), True
        binding.boot, binding.native_connection, binding.last_sequence = None, None, -1
        result = await self.executor.connect(binding.origin,device)
        binding.epoch = result['connection']
        origin = binding.origin
        reference = weakref.ref(self)
        @callback
        def native_call(call):
            runtime = reference()
            if runtime is None or runtime.closed or binding.origin is not origin:
                return
            if call.is_event is True and call.service == EVENT:
                # No templates, caller-controlled identity or destination fields.
                if getattr(call,'data_template',None) or getattr(call,'variables',None):
                    return
                runtime.enqueue(('packet', device, origin, dict(call.data)))
        try:
            binding.data.client.subscribe_service_calls(native_call)
        except Exception:
            binding.attached, binding.origin = False, None
            raise ExecutorError('Native satellite subscription is unavailable') from None

    def _packet(self, payload):
        if (not isinstance(payload,dict) or set(payload)!=PACKET_FIELDS
                or any(not isinstance(v,str) for v in payload.values())
                or payload['version']!='1' or not re.fullmatch(r'[a-fA-F0-9]{16}',payload['boot_id'])
                or len(payload['detail'])>128 or len(payload['media_url'])>4096):
            raise ExecutorError('Invalid native satellite packet')
        for key in ('connection','turn','sequence'):
            if not re.fullmatch(r'[0-9]{1,10}',payload[key]):
                raise ExecutorError('Invalid native satellite counter')
        return payload

    def _media_url(self, url):
        if not url or any(ord(c)<33 for c in url) or '\\' in url:
            raise ExecutorError('Invalid satellite TTS URL')
        parsed = urlsplit(url)
        if (parsed.username or parsed.password or parsed.fragment
                or f'{parsed.scheme}://{parsed.netloc}' not in self.allowed_origins
                or not re.fullmatch(r'/api/tts_proxy/[A-Za-z0-9_.-]+',parsed.path)):
            raise ExecutorError('Satellite reply must reference this HA TTS proxy')
        return url

    async def _native(self, device, origin, payload):
        binding = self.bindings[device]
        if not await self.verify(origin,device,device):
            return
        packet = self._packet(payload)
        if binding.boot is None:
            binding.boot, binding.native_connection = packet['boot_id'], int(packet['connection'])
        if packet['boot_id']!=binding.boot or int(packet['connection'])<binding.native_connection:
            return
        if int(packet['connection'])>binding.native_connection:
            # A fresh Assist subscription within the same encrypted API socket
            # invalidates its old turns as well.
            result = await self.executor.connect(origin,device)
            binding.epoch = result['connection']
            binding.boot, binding.native_connection, binding.last_sequence = packet['boot_id'],int(packet['connection']),-1
            self.sessions = {k:v for k,v in self.sessions.items() if v[0]!=device}
            self.session_routes = {k:v for k,v in self.session_routes.items() if k in self.sessions}
        sequence = int(packet['sequence'])
        if sequence<=binding.last_sequence:
            return
        binding.last_sequence = sequence
        session = hashlib.sha256(f"{device}:{binding.epoch}:{packet['turn']}".encode()).hexdigest()
        kind = {'wake':'wake','push_to_talk':'wake','reply_ready':'reply_ready','heartbeat':'heartbeat',
                'cancel_requested':'stop','error':'error','finished':'stop'}.get(packet['kind'])
        if kind is None:
            return  # pipeline_end is not external playback completion.
        binding.sequence += 1
        reply_id, completion = None, None
        if kind == 'wake':
            if not self._refresh_route(device):
                self.errors[:]=['assistant_pairing_not_ready']
                return
            self.policy.focus.factor = self.rows[device].get('duck_factor',.25)
            self.sessions[session] = (device, binding.epoch)
            self.session_routes[session]=self.policy.routes[device]
        elif kind == 'reply_ready':
            if session not in self.sessions:
                return
            selected=self.session_routes.get(session)
            if selected is None or selected!=self.policy.routes[device] or not self._route_current(device,selected.tts_player):
                self.errors[:]=['assistant_pairing_changed_during_turn']
                return
            url = self._media_url(packet['media_url'])
            reply_id = hashlib.sha256(f'{session}:{sequence}'.encode()).hexdigest()
            self.controller.media[reply_id] = {'url':url,'volume':self.rows[device].get('reply_volume',.5)}
            completion = {k:packet[k] if k=='boot_id' else int(packet[k]) for k in ('boot_id','connection','turn')}
        event = SatelliteEvent(device,binding.epoch,binding.sequence,session,kind,reply_id)
        await self.executor.handle(origin,event,completion=completion)
        if reply_id:
            self.controller.media.pop(reply_id,None)
        if kind in {'stop','error'}:
            self.sessions.pop(session,None)
            self.session_routes.pop(session,None)

    async def _provider_end(self, session, success, proof):
        self.session_routes.pop(session,None)
        identity = self.sessions.pop(session,None)
        if identity is None:
            await self.executor.tick()
            return
        device, epoch = identity
        binding = self.bindings[device]
        if epoch!=binding.epoch or not binding.attached:
            return
        binding.sequence += 1
        event = SatelliteEvent(device,epoch,binding.sequence,session,'finished' if success else 'error')
        if not success:
            pending = self.executor.replies.get((device,epoch,session))
            if pending and pending[1]:
                await self.controller.complete(self.rows[device]['completion_service'],{**pending[1],'success':False})
        await self.executor.handle(binding.origin,event,playback_proof=proof)

    async def _worker(self):
        while not self.closed:
            item = await self.queue.get()
            try:
                if item[0]=='connection':
                    await self._connection(item[1])
                elif item[0]=='packet':
                    await self._native(*item[1:])
                elif item[0]=='provider_end':
                    await self._provider_end(*item[1:])
                else:
                    await self.executor.tick()
                    active = {key[2] for key,value in self.policy.sessions.items() if not value.ended}
                    self.sessions = {k:v for k,v in self.sessions.items() if k in active}
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors[:] = ['satellite_operation_failed']  # No URL/packet/error text.
            finally:
                self.queue.task_done()

    async def _ticker(self):
        while not self.closed:
            await asyncio.sleep(1)
            self.enqueue(('tick',))

    async def async_test_pairing(self, device_id, test_url=None):
        binding = self.bindings.get(device_id)
        if binding is None:
            raise ExecutorError('Satellite is not configured')
        self.controller.transport.resolve(self.rows[device_id]['tts_media_player'])
        owner = await self.controller.service_device(self.rows[device_id]['completion_service'])
        ready = binding.attached and owner==device_id
        result = {'ready':ready,'device_id':device_id,
                'speaker':self.rows[device_id]['tts_media_player'],'physical_test_played':False,
                'note':'Routing and native action checked; wake the device for an acoustic test.'}
        if test_url is not None:
            if not ready:
                raise ExecutorError('Pairing is not ready for a test sound')
            result.update(await self.controller.transport.play(self.rows[device_id]['tts_media_player'],
                test_url, volume=self.rows[device_id].get('reply_volume',.5), chime=False))
            result['test_sound_dispatched'] = result['service_accepted'] is True
            result['note'] = 'MA test workflow returned; confirm audibility in the selected room.'
        return result

    async def stop(self):
        self.closed = True
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        for task in (self.worker,self.ticker):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.worker,self.ticker) if t),return_exceptions=True)
        # Await bounded native announcements before restoring their ducked player.
        await asyncio.gather(*list(self.controller.tasks.values()),return_exceptions=True)
        try:
            async with self.executor.lock:
                for device in self.bindings:
                    observations = await self.controller.observe()
                    await self.executor._execute(self.policy.disconnect(device,observations))
        finally:
            await release_policy_focus(self.focus_state,self.policy)
            self.controller.media.clear()
            self.session_routes.clear()


async def async_setup_satellite_runtime(hass, entry):
    """Parent setup hook. Returns runtime or None; no activity while disabled."""
    if entry.options.get('live_actions_enabled') is not True or entry.options.get('satellite_audio_enabled') is not True:
        return None
    if not entry.options.get('satellites'):
        return None
    runtime = SatelliteRuntime(hass,entry)
    try:
        return await runtime.start()
    except BaseException:
        await runtime.stop()
        raise
