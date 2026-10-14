"""HA lifecycle, private provisioning, and shared volume ownership for EchoMuse.

Source keys are admin-provisioned, scoped to one real assistant/device and stored
only in HA's private storage. Disabled by default. No controller discovery, app
installation, or physical stop is performed by this module.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import math
import re
import secrets
from urllib.parse import urlsplit

from .echomuse_endpoint import Binding, EchoMuseAudioEndpoint, Ledger, NativeMode, Source, tts_url_validator
from .echomuse_store import PrivateSourceStore

DATA_KEY = '_household_voice_echomuse'
PATH = '/api/household_voice/echomuse_audio'
NATIVE_KEY = '_household_voice_echomuse_native'


@asynccontextmanager
async def async_native_output_guard(hass,assistant_id):
    """Serialize trusted native output without deadlocking controller admission.

    Caller must revalidate its native assistant selection before service dispatch.
    The controller's signed native begin is permitted during the await; a switch
    to external output is blocked. Its own persistent microphone hold remains
    authoritative. This guard neither stops nor confirms audible playback.
    """
    if not isinstance(assistant_id,str) or not re.fullmatch(r'assist_satellite\.[a-z0-9_]+',assistant_id):
        raise ValueError('Expected trusted assistant entity')
    state=hass.data.setdefault(NATIVE_KEY,{'locks':{},'active':{},'providers':{}})
    lock=state['locks'].setdefault(assistant_id,asyncio.Lock())
    async with lock:
        token=object();state['active'][assistant_id]=token
        try:
            runtimes=tuple(hass.data.get(DATA_KEY,{}).get('entries',{}).values())
            rows_by_runtime=[];identities=set()
            async with AsyncExitStack() as stack:
                for runtime in sorted(runtimes,key=lambda item:item.entry.entry_id):
                    await stack.enter_async_context(runtime.endpoint.lock)
                for runtime in runtimes:
                    keys={key for key,source in runtime.sources.items() if source.assistant==assistant_id}
                    if any(ident[0] in keys for ident in runtime.endpoint.turns):
                        raise ValueError('External audio turn is active')
                    rows=await runtime.endpoint._write(runtime.endpoint.ledger.rows)
                    rows_by_runtime.append((keys,rows))
                    identities.update((row['provider_entry_id'],row['native_player_id']) for row in rows if row['key_id'] in keys and row['provider_entry_id'] and row['native_player_id'])
                for keys,rows in rows_by_runtime:
                    if any((row['key_id'] in keys and row['state']=='unknown') or (row['state'] in {'admitted','dispatched','completed','unknown'} and (row['provider_entry_id'],row['native_player_id']) in identities) for row in rows):
                        raise ValueError('External output requires stopped-output recovery')
                state.setdefault('providers',{})[assistant_id]=identities
            yield
        finally:
            if state['active'].get(assistant_id) is token:
                state['active'].pop(assistant_id,None)
                state.get('providers',{}).pop(assistant_id,None)


class EchoMuseRoomFocus:
    """One shared authority; a wake lease and output protection live until end."""
    def __init__(self, authority, *, factor=.25):
        self.authority,self.factor = authority,factor
        self.turns = {}

    async def begin(self, device, turn, binding):
        ident = device,turn
        if ident in self.turns:
            raise ValueError('Duplicate focus turn')
        # resolve() includes the canonical reply control only while it is playing.
        # Shared authority restores it before MA takes control for the reply.
        context=self.authority.hold(set(binding.ducks),set(),factor=self.factor)
        self.turns[ident]={'wake':context,'reply':None}
        try:
            await context.__aenter__()
        except BaseException:
            self.turns.pop(ident,None)
            raise

    async def prepare_play(self, device, turn, binding):
        state=self.turns.get((device,turn))
        if state is None:
            raise ValueError('Focus turn expired')
        if state['reply'] is not None:
            return
        context=self.authority.hold(set(binding.ducks),set(binding.aliases)|{binding.player},factor=self.factor)
        await context.__aenter__()
        state['reply']=context

    async def end(self, device, turn):
        state=self.turns.pop((device,turn),None)
        if state:
            try:
                if state['reply']:
                    await state['reply'].__aexit__(None,None,None)
            finally:
                await state['wake'].__aexit__(None,None,None)


def _origin(url):
    parsed=urlsplit(url or '')
    if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('A configured Home Assistant origin is required')
    return f'{parsed.scheme}://{parsed.netloc}'


class EchoMuseRuntime:
    def __init__(self,hass,entry,store,ledger,*,collector=None,transport=None,focus=None,identity_resolver=None):
        self.hass,self.entry,self.store = hass,entry,store
        self.sources={}
        self.closed=False
        self.paused=False
        self.admin_lock=asyncio.Lock()
        if collector is None:
            from .area_registry import collect_pairings
            collector=collect_pairings
        self.collector=collector
        if identity_resolver is None:
            def identity_resolver(player):
                from homeassistant.helpers import entity_registry as er
                entity=er.async_get(hass).async_get(player)
                if entity is None or entity.platform!='music_assistant' or not entity.config_entry_id or not entity.unique_id:
                    raise ValueError('Music Assistant identity unavailable')
                return entity.config_entry_id,entity.unique_id
        self.identity_resolver=identity_resolver
        if focus is None:
            from .room_focus import AnnouncementRoomFocus, RoomVolumeController, get_room_focus_state
            state=get_room_focus_state(hass,entry)
            focus=EchoMuseRoomFocus(AnnouncementRoomFocus(state,RoomVolumeController(hass,state)))
        if transport is None:
            from .satellite_transport import MAAnnouncementTransport
            from .announcement_adapter import transport_policy_from_options
            transport_options=dict(entry.options)
            transport_policy=transport_policy_from_options(transport_options)
            spoken_policy=transport_options.get('spoken_media_policy','pause_and_resume')
            # The actual allowlist is derived for each call, never payload data.
            class Transport:
                async def play(inner, player, url, **kwargs):
                    return await MAAnnouncementTransport(hass,{player},timeout=60,spoken_media_policy=spoken_policy,
                        transport_policy=transport_policy).play(player,url,**kwargs)
            transport=Transport()
        origins=set()
        for value in (getattr(hass.config,'internal_url',None),getattr(hass.config,'external_url',None)):
            if value:
                origins.add(_origin(value))
        if not origins:
            from homeassistant.helpers.network import get_url
            try:origins.add(_origin(get_url(hass,prefer_external=False)))
            except Exception:
                if self.enabled():raise ValueError('Home Assistant audio origin unavailable') from None
        def unavailable_url(url):raise ValueError('Home Assistant audio origin unavailable')
        self.endpoint=EchoMuseAudioEndpoint(self.sources,transport,focus,self.resolve,tts_url_validator(origins) if origins else unavailable_url,ledger,enabled=self.enabled)

    def enabled(self):
        return not self.closed and not self.paused and self.entry.options.get('live_actions_enabled') is True and self.entry.options.get('echomuse_audio_enabled') is True

    def resolve(self,source):
        snapshot=self.collector(self.hass,self.entry.options)
        assistant=snapshot.get('assistants',{}).get(source.assistant)
        if not assistant or assistant.get('device_id')!=source.device:
            raise ValueError('Assistant identity unavailable')
        if assistant.get('status') in {'explicitly_unpaired','choice_required','no_suitable_speaker'} or (assistant.get('status')=='assistant_area_unknown' and not assistant.get('speaker_id')):
            raise NativeMode('Assistant has no current pairing')
        if source.assistant in self.hass.data.get(NATIVE_KEY,{}).get('active',{}):
            raise ValueError('Native output owns this assistant turn')
        if (assistant.get('status')!='ready'
                or not assistant.get('area_id') or not assistant.get('speaker_id')):
            raise ValueError('Assistant pairing unavailable')
        output=snapshot.get('outputs',{}).get(assistant['speaker_id'])
        if not output or output.get('available') is not True or output.get('area_id')!=assistant['area_id']:
            raise ValueError('Room speaker unavailable')
        aliases=tuple(sorted(set(output.get('physical_aliases',()))|{assistant['speaker_id']}))
        volume=self.entry.options.get('announcement_volume',.5)
        for row in self.entry.options.get('satellites',()):
            if row.get('satellite_device_id')==source.device:
                volume=row.get('reply_volume',volume)
                break
        if type(volume) not in (int,float) or not math.isfinite(volume) or not 0<=volume<=1:
            raise ValueError('Invalid reply volume')
        ducks=set(output.get('duck_media_players',()))
        playing=self.hass.states.get(assistant['speaker_id'])
        if (playing is not None and playing.state=='playing'
                and assistant['speaker_id'] in snapshot.get('recovery_media_players',())):
            ducks.add(assistant['speaker_id'])
        provider,native=self.identity_resolver(assistant['speaker_id'])
        if any((provider,native) in identities for identities in self.hass.data.get(NATIVE_KEY,{}).get('providers',{}).values()):
            raise ValueError('Native output owns the previous paired output')
        return Binding(source.device,source.assistant,assistant['area_id'],assistant['speaker_id'],aliases,tuple(sorted(ducks)),volume,provider,native)

    async def async_start(self):
        data=await self.store.async_load()
        if data is not None:
            if not isinstance(data,dict) or set(data)!={'sources'} or not isinstance(data['sources'],dict) or len(data['sources'])>64:
                raise ValueError('Invalid private EchoMuse registrations')
            devices=set(); controllers=set(); assistants=set()
            for key_id,row in data['sources'].items():
                if (not re.fullmatch('[a-f0-9]{32}',key_id) or not isinstance(row,dict)
                        or set(row)!={'key','device','assistant','controller_device','enabled'}
                        or not isinstance(row['key'],str) or not re.fullmatch('[a-f0-9]{64}',row['key'])
                        or not isinstance(row['assistant'],str) or not re.fullmatch(r'assist_satellite\.[a-z0-9_]+',row['assistant'])
                        or not isinstance(row['device'],str) or not row['device']
                        or not isinstance(row['controller_device'],str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',row['controller_device'])
                        or type(row['enabled']) is not bool
                        or row['device'] in devices or row['controller_device'] in controllers or row['assistant'] in assistants):
                    raise ValueError('Invalid private EchoMuse registration')
                devices.add(row['device']); controllers.add(row['controller_device']); assistants.add(row['assistant'])
                self.sources[key_id]=Source(bytes.fromhex(row['key']),row['device'],row['assistant'],row['controller_device'],row['enabled'])
        await self.endpoint.start()
        for row in await self.endpoint._write(self.endpoint.ledger.rows):
            if row['state']=='unknown':
                from .satellite_transport import restore_uncertain_audio
                provider,native=row['provider_entry_id'],row['native_player_id']
                if not provider or not native:
                    provider,native=self.identity_resolver(row['player'])
                restore_uncertain_audio(self.hass,provider_entry_id=provider,native_player_id=native)

    async def _save(self,sources):
        async def commit():
            await self.store.async_save({'sources':{key:{'key':source.key.hex(),'device':source.device,'assistant':source.assistant,'controller_device':source.controller_device,'enabled':source.enabled} for key,source in sources.items()}})
            self.sources.clear();self.sources.update(sources)
        task=asyncio.create_task(commit())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def async_provision(self,assistant_id,controller_device_id,*,enabled=False):
        """Only invoke through authenticated admin WS/options; key returned once."""
        if not re.fullmatch(r'assist_satellite\.[a-z0-9_]+',assistant_id) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',controller_device_id) or type(enabled) is not bool:
            raise ValueError('Invalid explicit assistant/controller selection')
        origin=_origin(getattr(self.hass.config,'external_url',None))
        if not origin.startswith('https://'):
            raise ValueError('A trusted HTTPS Home Assistant external URL is required')
        async with self.admin_lock,self.endpoint.lock:
            if self.closed or self.paused or len(self.sources)>=64:
                raise ValueError('EchoMuse provisioning unavailable')
            assistant=self.collector(self.hass,self.entry.options).get('assistants',{}).get(assistant_id)
            if not assistant or not assistant.get('device_id'):
                raise ValueError('Select a registered assistant')
            if any(source.assistant==assistant_id or source.device==assistant['device_id'] or source.controller_device==controller_device_id for source in self.sources.values()):
                raise ValueError('Revoke the existing source before provisioning a replacement')
            source=Source(secrets.token_bytes(32),assistant['device_id'],assistant_id,controller_device_id,enabled)
            try:self.resolve(source)
            except NativeMode:pass
            key_id=secrets.token_hex(16)
            await self._save({**self.sources,key_id:source})
            return {'key_id':key_id,'controller_device_id':controller_device_id,'controller_config':{'endpoint':origin+PATH+'/'+self.entry.entry_id,'key_id':key_id,'key_hex':source.key.hex()},'enabled':enabled,'physical_actions_performed':False}

    async def async_sources(self):
        rows=await self.endpoint._write(self.endpoint.ledger.rows)
        return {'enabled':self.enabled(),'sources':[{'key_id':key,'assistant_id':source.assistant,'device_id':source.device,'controller_device_id':source.controller_device,'enabled':source.enabled,'active':any(ident[0]==key for ident in self.endpoint.turns),'recovery_required':any(row['key_id']==key and row['state']=='unknown' for row in rows)} for key,source in self.sources.items()],'acoustic_delivery_confirmed':False}

    async def async_set_enabled(self,key_id,enabled):
        if type(enabled) is not bool:
            raise ValueError('Explicit enabled choice required')
        async with self.admin_lock,self.endpoint.lock:
            source=self.sources.get(key_id)
            if self.closed or self.paused or source is None or any(ident[0]==key_id for ident in self.endpoint.turns):
                raise ValueError('Unknown source or active turn')
            if enabled:
                try:self.resolve(source)
                except NativeMode:pass
            await self._save({**self.sources,key_id:Source(source.key,source.device,source.assistant,source.controller_device,enabled)})
        return {'enabled':enabled,'physical_actions_performed':False}

    async def async_revoke(self,key_id):
        async with self.admin_lock,self.endpoint.lock:
            rows=await self.endpoint._write(self.endpoint.ledger.rows)
            if (self.closed or self.paused or key_id not in self.sources or any(ident[0]==key_id for ident in self.endpoint.turns)
                    or any(row['key_id']==key_id and row['state']=='unknown' for row in rows)):
                raise ValueError('Wait for active work and recover uncertain output before revoking')
            await self._save({key:source for key,source in self.sources.items() if key!=key_id})
        return {'revoked':True,'physical_actions_performed':False}

    async def async_recover(self,key_id,*,confirm_external_output_stopped=False):
        if confirm_external_output_stopped is not True:
            raise ValueError('Confirm the external output has stopped')
        async with self.admin_lock,self.endpoint.lock:
            if self.closed or self.paused or key_id not in self.sources or any(ident[0]==key_id for ident in self.endpoint.turns):
                raise ValueError('Unknown source or active turn')
            from .satellite_transport import reset_uncertain_audio_identity
            rows=await self.endpoint._write(self.endpoint.ledger.rows)
            identities=set()
            for row in rows:
                if row['key_id']==key_id and row['state']=='unknown':
                    provider,native=row['provider_entry_id'],row['native_player_id']
                    if not provider or not native:
                        provider,native=self.identity_resolver(row['player'])
                    identities.add((provider,native))
            # Persist the administrator's stopped-output acknowledgement before
            # releasing the shared in-memory guard. Failed disk writes must not
            # briefly permit another feature to invoke the uncertain output.
            await self.endpoint._write(self.endpoint.ledger.recover,key_id)
            for provider,native in identities:
                reset_uncertain_audio_identity(self.hass,provider_entry_id=provider,native_player_id=native,operator_confirmed_stopped=True)
        return {'recovered':True,'controller_recovery_also_required':True,'physical_stop_confirmed':False}

    async def async_pause_for_backup(self):
        self.paused=True
        async with self.admin_lock:
            await self.endpoint.expire(all_turns=True)
            await self.endpoint.drain()
            async with self.endpoint.lock:
                pass  # All prior shielded SQLite commits and source saves finished.

    async def async_resume_after_backup(self):
        self.paused=False

    async def async_close(self):
        self.closed=True
        async with self.admin_lock:
            await self.endpoint.close()
        state=self.hass.data.get(DATA_KEY,{})
        if state.get('entries',{}).get(self.entry.entry_id) is self:
            state['entries'].pop(self.entry.entry_id,None)


async def async_setup_echomuse_runtime(hass,entry):
    """One dispatcher per HA instance, one private runtime per loaded entry.

    Register even while disabled so an administrator can provision sources. The
    dispatcher remains inert after unload and resolves the current runtime each
    request; no stale closure captures keys across options reloads.
    """
    state=hass.data.setdefault(DATA_KEY,{'entries':{},'registered':False,'lock':asyncio.Lock()})
    async with state['lock']:
        if entry.entry_id in state['entries']:
            raise ValueError('EchoMuse runtime already loaded')
        runtime=EchoMuseRuntime(hass,entry,PrivateSourceStore(hass.config.path('.storage','household_voice_'+entry.entry_id+'_echomuse_private.sqlite3')),Ledger(hass.config.path('.storage','household_voice_'+entry.entry_id+'_echomuse.sqlite3')))
        try:
            await runtime.async_start()
            if not state['registered']:
                _register(hass,state)
                state['registered']=True
            state['entries'][entry.entry_id]=runtime
            return runtime
        except BaseException:
            await runtime.async_close()
            raise


def _register(hass,state):
    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.components.http import HomeAssistantView

    class View(HomeAssistantView):
        url=PATH+'/{entry_id}'
        name='api:household_voice:echomuse_audio'
        requires_auth=False
        async def post(self,request,entry_id):
            runtime=state['entries'].get(entry_id)
            if runtime is None:
                return EchoMuseAudioEndpoint.response('','disabled',503)
            return await runtime.endpoint.post(request)

    @websocket_api.websocket_command({vol.Required('type'):'household_voice/echomuse',vol.Required('entry_id'):str,vol.Required('operation'):vol.In({'list','provision','enable','revoke','recover'}),vol.Optional('assistant_id'):str,vol.Optional('controller_device_id'):str,vol.Optional('key_id'):str,vol.Optional('enabled'):bool,vol.Optional('confirm_external_output_stopped'):bool})
    @websocket_api.async_response
    async def command(hass,connection,msg):
        if connection.user is None or not connection.user.is_admin:
            connection.send_error(msg['id'],'unauthorized','Administrator access required')
            return
        runtime=state['entries'].get(msg['entry_id'])
        if runtime is None:
            connection.send_error(msg['id'],'unavailable','EchoMuse entry is not loaded')
            return
        fields={'list':set(),'provision':{'assistant_id','controller_device_id','enabled'},'enable':{'key_id','enabled'},'revoke':{'key_id'},'recover':{'key_id','confirm_external_output_stopped'}}
        if set(msg)-{'id','type','entry_id','operation'}-fields[msg['operation']]:
            connection.send_error(msg['id'],'invalid_format','Unexpected operation field')
            return
        try:
            match msg['operation']:
                case 'list': result=await runtime.async_sources()
                case 'provision': result=await runtime.async_provision(msg['assistant_id'],msg['controller_device_id'],enabled=msg.get('enabled',False))
                case 'enable': result=await runtime.async_set_enabled(msg['key_id'],msg['enabled'])
                case 'revoke': result=await runtime.async_revoke(msg['key_id'])
                case 'recover': result=await runtime.async_recover(msg['key_id'],confirm_external_output_stopped=msg.get('confirm_external_output_stopped',False))
            connection.send_result(msg['id'],result)
        except (Exception,):
            connection.send_error(msg['id'],'unavailable','EchoMuse operation unavailable; check registration, active work, pairing and recovery')

    hass.http.register_view(View())
    websocket_api.async_register_command(hass,command)
