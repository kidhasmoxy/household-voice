"""Opt-in native recording composition using the existing embedded authority.

Call only from the sole loaded Household Voice lifecycle after administrator
opt-in. No remote coordinator: reuse existing EmbeddedVoiceClient's authority,
executor serialization, SQLite files, upload API and outbox delivery runtime.
"""
import asyncio
from functools import partial
from importlib import import_module
from homeassistant.core import Context
from homeassistant.helpers.storage import Store
from .native_recording_backend import RecordingBackend
from .native_recording_handoff import NativeHandoff,composed_runtime_type
from .native_recording_ledger import HandoffAuthority,HandoffLedger
from .native_recording_adapter import attach,event_fence,native_question
from .native_recording_bindings import sources_from_registry,configure_capture_bindings
from .native_recording_journal import async_get_shared_journal
from .native_recording_lifecycle import Composition, DATA_KEY, claim_owner



async def compose(hass,entry,client,*,base_runtime,captured_type,configuration,
                  collect_pairings,source_permitted,execution_authorized=False,adapter=None):
    if execution_authorized is not True:raise ValueError('explicit_native_authorization_required')
    if DATA_KEY in hass.data:raise ValueError('composition_already_owned')
    if not callable(source_permitted):raise ValueError('source_hold_guard_required')
    # These are our existing integration's internals, not Home Assistant private
    # fields. A remote client cannot provide the required atomic authority lock.
    if not hasattr(client,'_authority') or not hasattr(client,'_run'):
        raise ValueError('embedded_authority_required')
    owner=Composition(hass,None,None,[],None,client=client)
    owner.entry=entry
    claim_owner(hass,owner)
    removers=[];backend=None;runtime=None
    try:
        async def run_owned(fn,*args,**kwargs):
            return await client._run(partial(fn,*args,**kwargs))
        def build_authority():
            existing=client._authority
            capture_module=import_module(type(existing.captures).__module__)
            return HandoffAuthority(HandoffLedger(existing.scheduler.path,existing.broadcasts.clock),
                existing.broadcasts,existing.captures,
                lambda:capture_module.room_capture_bindings(existing.runtime.config()['rooms']))
        authority=await run_owned(build_authority)
        def sources():
            return {device:source for device,source in sources_from_registry(hass).items()
                    if source_permitted(source) is True}
        initial=sources_from_registry(hass)
        # Caller supplies original frozen configuration function; adding native
        # authorization profiles does not create or change any Assist pipeline.
        from .native_recording_bindings import configuration_with_native_sources
        desired=configuration_with_native_sources(hass,dict(entry.options),collect_pairings(hass,dict(entry.options)))
        current=await client.runtime_config()
        if any(current.get(key)!=value for key,value in desired.items()):
            await client.configure_runtime(desired,current['revision'])
        def resolve(device):
            source=sources().get(device)
            return source
        journal=await async_get_shared_journal(hass)
        backend=RecordingBackend(journal=journal,ask=partial(guarded_question,hass,entry,resolve),
            fence=partial(event_fence,hass),context_factory=Context,
            validate_source=lambda device,entity: bool((source:=resolve(device)) and source.entity_id==entity))
        owner.backend=backend
        owner.entry=entry;owner.resolve_source=resolve;owner.initial_sources=initial
        handoff=NativeHandoff(backend,client,authority,run_owned,resolve,captured_type)
        from .runtime_config import async_configure
        handoff.refresh=partial(async_configure,client,entry.options,hass=hass)
        def make_runtime():
            return composed_runtime_type(base_runtime)(hass,entry,client,handoff=handoff,
                execution_authorized=True,adapter=adapter)
        runtime=make_runtime()
        owner.runtime=runtime;owner.make_runtime=make_runtime
        def loaded_entries():
            if len(hass.config_entries.async_entries('household_voice'))!=1:return 2
            return sum(getattr(item.state,'value',item.state)=='loaded'
                       for item in hass.config_entries.async_entries('household_voice'))
        removers=attach(hass,backend,loaded_entries)
        owner.removers=removers
        if not journal.failed:await handoff.recover()
        register_management(hass,owner)
        return owner
    except BaseException:
        # Both cleanup branches are attempted even when one fails or the setup
        # caller was canceled. Ownership/evidence stay installed until all quiet.
        await owner.close()
        raise



def validate_options(hass,entry):
    options=entry.options
    if (entry.data.get('mode')!='embedded' or any(options.get(key) is not True for key in
            ('live_actions_enabled','announcements_enabled','assist_tools_enabled'))
            or len(hass.config_entries.async_entries('household_voice'))!=1):
        raise ValueError('native_recording_requires_one_enabled_local_authority')


def source_permitted(hass,entry,source):
    """Fresh registry/config/paired-output checks; persistent holds are awaited below."""
    from .area_registry import collect_pairings
    from .satellite_transport import MAAnnouncementTransport,_UNCERTAIN,_LOCKS,_player_key
    if entry.options.get('native_recording_enabled') is not True:return False
    try:validate_options(hass,entry)
    except ValueError:return False
    from .echomuse_runtime import DATA_KEY as ECHO_KEY
    echo=hass.data.get(ECHO_KEY,{}).get('entries',{}).get(entry.entry_id)
    if echo is None or echo.closed or echo.paused or echo.endpoint.failed:return False
    row=collect_pairings(hass,entry.options).get('assistants',{}).get(source.entity_id,{})
    if row.get('device_id')!=source.device_id or row.get('area_id')!=source.area_id:return False
    # An explicitly configured but offline/invalid output must not silently change rooms.
    if row.get('status') not in ('ready','explicitly_unpaired','choice_required','no_suitable_speaker'):return False
    player=row.get('speaker_id')
    if player:
        try:
            _mass,native=MAAnnouncementTransport(hass,{player}).resolve(player)
            key=_player_key(hass,player,native)
        except Exception:return False
        lock=_LOCKS.get(hass,{}).get(key)
        if key in _UNCERTAIN.get(hass,{}) or (lock is not None and lock.locked()):return False
    return True


async def guarded_question(hass,entry,resolve,entity,context):
    from .native_alarm import get_native_alarm_backend
    from .echomuse_runtime import async_native_output_guard
    source=next((item for item in sources_from_registry(hass).values() if item.entity_id==entity),None)
    if source is None or resolve(source.device_id)!=source:raise ValueError('source_unavailable')
    native=get_native_alarm_backend(hass)
    async with native.lock(source.device_id):
        if native.paused:raise ValueError('source_paused')
        journal=await native.get_journal()
        if await native.write(journal.held,source.device_id):raise ValueError('native_output_held')
        # This lock checks persisted EchoMuse unknown output/microphone evidence and
        # prevents a simultaneous external turn. Never treats unavailable as clear.
        async with async_native_output_guard(hass,entity):
            if resolve(source.device_id)!=source:raise ValueError('source_changed')
            return await native_question(hass,entity,context)


async def async_setup_native_recording(hass,entry,coordinator):
    validate_options(hass,entry)
    from .native_alarm import get_native_alarm_backend
    from .area_registry import collect_pairings
    from .runtime_config import configuration
    from .broadcast_runtime import BroadcastRuntime
    from .capture import CapturedUtterance
    # Restore durable native output evidence before any question can be admitted.
    await get_native_alarm_backend(hass).get_journal()
    owner=await compose(hass,entry,coordinator.client,base_runtime=BroadcastRuntime,
        captured_type=CapturedUtterance,configuration=configuration,collect_pairings=collect_pairings,
        source_permitted=partial(source_permitted,hass,entry),execution_authorized=True)
    owner.on_runtime=lambda runtime:setattr(coordinator,'broadcast_runtime',runtime)
    owner.on_runtime(owner.runtime)
    return owner


def status(hass,owner):
    from homeassistant.helpers import entity_registry as er
    backend=owner.backend
    sources=sources_from_registry(hass)
    rows=[]
    for device in sorted(set(sources)|set(backend.journal.holds)):
        source=sources.get(device)
        registered=[item for item in er.async_get(hass).entities.values()
                    if item.device_id==device and item.entity_id.startswith('assist_satellite.')]
        entity=source.entity_id if source else registered[0].entity_id if len(registered)==1 else None
        rows.append({'assistant_id':entity,'device_id':device,'area_id':source.area_id if source else None,
            'available':bool(source and owner.resolve_source(device)==source),
            'busy':device in backend.operations,'recovery_required':device in backend.journal.holds})
    return {'enabled':not backend.closed,'paused':backend.paused,'worker_connected':backend.connection is not None,
            'sources':rows,'physical_stop_confirmed':False}


async def recover(hass,owner,assistant_id,confirmed):
    from .native_alarm import get_native_alarm_backend
    from .echomuse_runtime import async_native_output_guard
    backend=owner.backend
    if (confirmed is not True or owner.paused or backend.paused or backend.closed
            or backend.operations or backend.tasks or backend.questions):
        raise ValueError('recording_recovery_unavailable')
    source=next((item for item in sources_from_registry(hass).values() if item.entity_id==assistant_id),None)
    if source is None or owner.resolve_source(source.device_id)!=source:raise ValueError('source_unavailable')
    state=hass.states.get(assistant_id)
    if state is None or state.state!='idle':raise ValueError('source_busy')
    native=get_native_alarm_backend(hass)
    async with native.lock(source.device_id):
        async with async_native_output_guard(hass,assistant_id):
            journal=await native.get_journal()
            if native.paused or await native.write(journal.held,source.device_id):raise ValueError('native_output_held')
            if backend.operations or backend.tasks or backend.questions or owner.resolve_source(source.device_id)!=source:
                raise ValueError('source_busy')
            operation=backend.journal.holds.get(source.device_id)
            if operation is not None:await backend.journal.release(source.device_id,operation)
    return {'cleared':operation is not None,'physical_stop_confirmed':False,'operator_confirmation_required':True}


def register_management(hass,owner):
    from homeassistant.components import websocket_api
    import voluptuous as vol
    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required('type'):'household_voice/native_recording',
        vol.Required('operation'):vol.In({'list','recover'}),vol.Optional('assistant_id'):str,
        vol.Optional('confirm_microphone_stopped'):bool})
    @websocket_api.async_response
    async def manage(hass,connection,message):
        current=hass.data.get(DATA_KEY)
        if current is not owner or owner.backend.closed:
            connection.send_error(message['id'],'backend_unavailable','Native recording is unavailable');return
        try:
            if message['operation']=='list':result=status(hass,owner)
            else:result=await recover(hass,owner,message.get('assistant_id'),message.get('confirm_microphone_stopped'))
        except Exception:
            connection.send_error(message['id'],'recovery_unavailable','Wait for active work, verify the selected source and confirm its microphone stopped');return
        connection.send_result(message['id'],result)
    websocket_api.async_register_command(hass,manage)
