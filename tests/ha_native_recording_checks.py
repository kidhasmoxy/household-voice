"""Real Core + existing embedded authority + canonical native recording lifecycle proof."""
import asyncio
import importlib.util
import io
from pathlib import Path
import sys
from types import ModuleType,SimpleNamespace,MappingProxyType
import wave
from homeassistant.auth.const import GROUP_ID_ADMIN,GROUP_ID_USER
from homeassistant.config_entries import ConfigEntry,ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import area_registry as ar,device_registry as dr
from recording_worker.client import CoreCaptureClient,authenticate
from recording_worker.protocol import VERSION


async def check_source_destination_scope(hass,satellite,device,area,entry,composition):
    """Real registries and installed MA models; every audio/provider call is inert."""
    from importlib.metadata import version
    from homeassistant.helpers import entity_registry as er
    from music_assistant_models.player import Player
    from custom_components.household_voice.area_registry import collect_pairings
    from custom_components.household_voice.native_recording import source_permitted
    from custom_components.household_voice.native_recording_bindings import sources_from_registry
    from custom_components.household_voice.announcement_adapter import AnnouncementAdapter
    from custom_components.household_voice.satellite_transport import MAAnnouncementTransport,_UNCERTAIN
    registry=er.async_get(hass)
    ma=ConfigEntry(domain='music_assistant',title='Inert scope output',data={},options={},source='user',version=1,
        minor_version=1,unique_id=None,discovery_keys=MappingProxyType({}),subentries_data=None)
    hass.config_entries._entries[ma.entry_id]=ma
    ma._async_set_state(hass,ConfigEntryState.LOADED,None) # Synthetic provider only
    speaker_device=dr.async_get(hass).async_get_or_create(config_entry_id=ma.entry_id,identifiers={('music_assistant','scope-output')})
    dr.async_get(hass).async_update_device(speaker_device.id,area_id=area.id)
    speaker=registry.async_get_or_create('media_player','music_assistant','scope-output',config_entry=ma,device_id=speaker_device.id)
    player=Player.from_dict({'player_id':'scope-output','provider':'fixture-provider','type':'player','name':'Scope output',
        'available':True,'enabled':True,'device_info':{'model':'Fixture','manufacturer':'Fixture'},
        'supported_features':['play_announcement'],'group_members':[],
        'synced_to':None,'active_group':None,'active_output_protocol':None,'output_protocols':[{
            'output_protocol_id':'native','name':'Native','protocol_domain':'sonos','is_native':True,'available':True,'derived_from':None}]})
    effects=[]
    async def play(*args,**kwargs):effects.append('provider')
    async def active(*args):effects.append('queue-read');return None
    async def pause(*args):effects.append('pause')
    mass=SimpleNamespace(players=SimpleNamespace(get=lambda identity:player if identity=='scope-output' else None,play_announcement=play),
        player_queues=SimpleNamespace(get_active_queue=active,pause=pause))
    ma.runtime_data=SimpleNamespace(mass=mass)
    hass.states.async_set(speaker.entity_id,'idle',{'group_members':[]})
    options={**entry.options,'default_announcement_speakers':[speaker.entity_id],
        'assistant_speaker_overrides':{satellite.entity_id:speaker.entity_id}}
    source_entry=SimpleNamespace(entry_id=entry.entry_id,data=entry.data,options=options)
    source=sources_from_registry(hass)[device]
    transport=MAAnnouncementTransport(hass,{speaker.entity_id})
    original=composition.runtime.adapter;old_wait=composition.runtime._wait_idle
    key=(ma.entry_id,'scope-output')
    try:
        assert transport.preflight(speaker.entity_id)==(mass,'scope-output')
        # No nonexistent server-only protocol_parent_id is required by the client gate.
        assert hasattr(player,'output_protocols') and hasattr(player,'active_output_protocol')
        player.group_members=['scope-output','foreign-room']
        assert collect_pairings(hass,options)['assistants'][satellite.entity_id]['status']=='manual_speaker_unsafe_or_unsupported'
        assert source_permitted(hass,source_entry,source), 'Music grouping must not hide the native microphone'
        async def audio(*args):effects.append('audio-url');return 'http://fixture.invalid/audio/'+'a'*32
        adapter=AnnouncementAdapter(hass,options,audio,allowed_audio_origins=['http://fixture.invalid'],
            execution_authorized=True,transport=transport)
        composition.runtime.adapter=adapter
        denied=await composition.runtime.async_start(device,Context(),request_id='grouped-destination')
        assert denied['status']=='error' and not satellite.prompt.is_set() and not satellite.started.is_set()
        assert await composition.runtime.handoff.get('grouped-destination') is None
        assert not composition.backend.operations and not effects
        refusal=await transport.play(speaker.entity_id,'http://fixture.invalid/body')
        assert refusal['outcome']=='not_invoked' and not effects
        player.group_members=[]
        # A different-room quarantined destination cannot cause question or room focus.
        other=ar.async_get(hass).async_create('Synthetic destination area')
        dr.async_get(hass).async_update_device(speaker_device.id,area_id=other.id)
        _UNCERTAIN.setdefault(hass,{})[key]=True
        assert source_permitted(hass,source_entry,source)
        assert (await composition.runtime.async_start(device,Context(),request_id='held-destination'))['status']=='error'
        assert not satellite.prompt.is_set() and not effects
        _UNCERTAIN[hass].pop(key)
        dr.async_get(hass).async_update_device(speaker_device.id,area_id=area.id)
        player.available=False
        _UNCERTAIN[hass][key]=True
        assert not source_permitted(hass,source_entry,source), 'Offline state must not erase a same-room audio hold'
        _UNCERTAIN[hass].pop(key);player.available=True
        async def regroup_after_idle(entity):
            await old_wait(entity);player.synced_to='foreign-room'
        composition.runtime._wait_idle=regroup_after_idle
        pending=await composition.runtime.async_start(device,Context(),request_id='regroup-during-idle')
        assert pending['status']=='queued'
        await composition.runtime.sessions[device].task
        assert (await composition.runtime.handoff.get('regroup-during-idle'))['phase']=='cancelled'
        assert not satellite.prompt.is_set() and not satellite.started.is_set()
        assert not composition.backend.operations and not composition.backend.journal.holds and not effects
        composition.runtime._wait_idle=old_wait;player.synced_to=None
        # Reservation and owned output locks each await after handoff admission.
        # The final callback must reject after each interval without a stale hold.
        reserve=composition.backend.journal.reserve
        async def reserve_then_regroup(*args):
            await reserve(*args);player.synced_to='foreign-room'
        composition.backend.journal.reserve=reserve_then_regroup
        try:
            assert (await composition.runtime.async_start(device,Context(),request_id='regroup-during-reserve'))['status']=='queued'
            await composition.runtime.sessions[device].task
            assert (await composition.runtime.handoff.get('regroup-during-reserve'))['phase']=='cancelled'
            assert not composition.backend.journal.holds and not satellite.prompt.is_set()
        finally:composition.backend.journal.reserve=reserve
        from custom_components.household_voice.native_alarm import get_native_alarm_backend
        from custom_components.household_voice.echomuse_runtime import async_native_output_guard
        native=get_native_alarm_backend(hass)
        async def wait_for_question_task():
            async with asyncio.timeout(5):
                while not ((op:=composition.backend.operations.get(device)) and op.question is not None):
                    await asyncio.sleep(0)
            assert not op.started
        for label,lock in [('native-lock',native.lock(device)),
                           ('echo-lock',async_native_output_guard(hass,satellite.entity_id))]:
            player.synced_to=None
            async with lock:
                request='regroup-during-'+label
                assert (await composition.runtime.async_start(device,Context(),request_id=request))['status']=='queued'
                task=composition.runtime.sessions[device].task
                await wait_for_question_task();player.synced_to='foreign-room'
            await task
            assert (await composition.runtime.handoff.get(request))['phase']=='cancelled'
            assert composition.runtime.latest[device]['reason']=='announcement_destination_no_longer_available'
            assert not composition.backend.journal.holds and not composition.backend.operations
            assert not satellite.prompt.is_set() and not satellite.started.is_set() and not effects
        player.synced_to=None
        # Actual pinned client deserializes the four public output records seen
        # on ordinary MA Sonos players. Standard legacy routing stays intact;
        # native capture never downgrades after a strict destination refusal.
        linked=[dict(output_protocol_id='native',name='Native',protocol_domain='sonos',
            is_native=True,available=True,derived_from=None)] + [dict(output_protocol_id='fixture-'+domain,
            name=domain,protocol_domain=domain,is_native=False,available=True,derived_from=None)
            for domain in ('airplay','sendspin','dlna')]
        from music_assistant_models.player import OutputProtocol
        player.output_protocols=[OutputProtocol.from_dict(row) for row in linked]
        player.active_output_protocol='native'
        legacy_transport=MAAnnouncementTransport(hass,{speaker.entity_id},transport_policy='legacy_ma')
        for label,members,parent in [('solo',[],None),('leader',['scope-output','fixture-other'],None),
                                     ('member',[],'fixture-other')]:
            player.group_members=members;player.synced_to=parent;effects.clear()
            denied=await composition.runtime.async_start(device,Context(),request_id='four-output-native-'+label)
            assert denied['status']=='error' and not satellite.prompt.is_set() and not satellite.started.is_set()
            assert await composition.runtime.handoff.get('four-output-native-'+label) is None
            assert not effects and not composition.backend.operations
            result=await legacy_transport.play(speaker.entity_id,'http://fixture.invalid/inert')
            assert result['outcome']=='service_completed' and effects.count('provider')==1 and 'pause' not in effects
        player.group_members=[];player.synced_to=None
        # Strict provenance must survive an option-off adapter on the same outputs.
        legacy_adapter=AnnouncementAdapter(hass,{**options,'native_recording_enabled':False},audio,
            allowed_audio_origins=['http://fixture.invalid'],execution_authorized=True,
            transport=legacy_transport,require_recording_provenance=True,focus_entry=entry)
        effects.clear()
        refused=await legacy_adapter.deliver({'kind':'speaker','target':speaker.entity_id},
            {'id':'a'*32,'expires_at':10**12,'transcript':'Fixture','transport_policy':'verified_native'},now=1000)
        assert refused['outcome']=='not_invoked' and not effects
        assert legacy_adapter.transport.transport_policy=='legacy_ma'
        assert legacy_adapter._strict_transport.transport_policy=='verified_native'
        return {'source_available_with_invalid_music_pairing':True,'grouped_destination_no_question_pcm_pause_provider':True,
            'cross_room_unknown_destination_no_question_or_focus':True,'offline_same_room_unknown_audio_hold_preserved':True,
            'idle_wait_regroup_no_question_pcm_and_durable_cancel':True,
            'journal_reserve_regroup_no_question_or_stale_hold':True,
            'native_and_echo_lock_regroup_no_question_pcm_and_durable_cancel':True,
            'actual_ma_client_model_version':version('music-assistant-models'),'actual_ma_client_version':version('music-assistant-client'),
            'actual_client_model_native_output_preflight':True,
            'four_protocol_solo_group_leader_member_legacy_provider_once':True,
            'same_four_protocol_native_no_question_pcm_pause_provider':True,
            'pending_native_recording_flag_off_keeps_strict_destination_policy':True}
    finally:
        composition.runtime.adapter=original;composition.runtime._wait_idle=old_wait
        _UNCERTAIN.get(hass,{}).pop(key,None)
        hass.states.async_remove(speaker.entity_id);registry.async_remove(speaker.entity_id)
        hass.config_entries._entries.pop(ma.entry_id,None)



async def run_worker_checks(hass,http,make_satellite,finish,pcm):
    from custom_components.household_voice import backup
    from custom_components.household_voice.runtime_config import async_configure
    from custom_components.household_voice.native_recording import recover
    from custom_components.household_voice.native_recording_lifecycle import DATA_KEY
    from homeassistant.components.assist_satellite.const import AssistSatelliteEntityFeature
    from homeassistant.helpers import entity_registry as er
    satellite,device=await make_satellite('canonical')
    satellite._attr_supported_features |= AssistSatelliteEntityFeature.ANNOUNCE
    satellite.async_write_ha_state()
    area=ar.async_get(hass).async_create('Synthetic recording area')
    dr.async_get(hass).async_update_device(device,area_id=area.id)
    entry=hass.config_entries.async_entries('household_voice')[0]
    assert hass.data['household_voice'][entry.entry_id].native_recording is None
    hass.config_entries.async_update_entry(entry,options={'live_actions_enabled':True,'announcements_enabled':True,
        'assist_tools_enabled':True,'native_recording_enabled':True,'stt_capture_enabled':True})
    await hass.async_block_till_done()
    coordinator=hass.data['household_voice'][entry.entry_id]
    composition=coordinator.native_recording
    assert composition is not None and 'stt' not in coordinator.platforms
    hass.config_entries.async_update_entry(entry,options={**entry.options,'stt_capture_enabled':False})
    await hass.async_block_till_done()
    coordinator=hass.data['household_voice'][entry.entry_id];composition=coordinator.native_recording
    client=coordinator.client
    from homeassistant.helpers import llm
    from custom_components.household_voice.assist_tools import build_assist_tools
    context=llm.LLMContext(platform='fixture',context=Context(),language='en',assistant='conversation',device_id=device)
    tools=build_assist_tools(hass,context,llm.async_get_apis(hass))
    assert 'household_voice__broadcast' in {tool.name for tool in tools.tools}
    targets=[{'kind':'speaker','target':'media_player.synthetic'},
        {'kind':'phone','target':'notify.mobile_app_synthetic','device_tracker':'device_tracker.synthetic'}]
    delivered=[]
    async def deliver(target,recording,**kwargs):
        assert await kwargs['before_dispatch']()
        delivered.append((target,recording['transcript']))
        return {'outcome':'service_completed','provider_completed':True}
    adapter=SimpleNamespace(targets=lambda *args:targets,deliver=deliver,preflight_targets=lambda targets:None)
    composition.runtime.adapter=adapter
    # Stop only background delivery fixture tasks, retaining the real acquisition runtime.
    for task in composition.runtime._workers:task.cancel()
    await asyncio.gather(*composition.runtime._workers,return_exceptions=True)
    composition.runtime._workers=[]
    control=None;worker=None;client_closed=False
    try:
        current=await client.runtime_config()
        profiles=current['rooms'][area.id]['capture_profiles']
        assert len(profiles)==1 and next(iter(profiles)).startswith('native_recording_')
        assert 'stt_capture_profiles' not in entry.options
        await async_configure(client,entry.options,hass=hass)
        assert (await client.runtime_config())['rooms'][area.id]['capture_profiles']==profiles
        user=await hass.auth.async_create_user('Synthetic composed worker',group_ids=[GROUP_ID_ADMIN])
        refresh=await hass.auth.async_create_refresh_token(user,client_id='https://fixture.invalid/')
        token=hass.auth.async_create_access_token(refresh)
        control=await http.ws_connect('/api/websocket');await authenticate(control,token)
        await control.send_json({'id':2,'type':'household_voice/recording_worker/connect','protocol_version':VERSION})
        assert (await control.receive_json())['success']
        scope_proof=await check_source_destination_scope(hass,satellite,device,area,entry,composition)
        coordinator.echomuse_runtime.endpoint.failed=True
        assert (await composition.runtime.async_start(device,Context(),request_id='failed-dependency'))['status']=='error'
        assert not satellite.prompt.is_set() and not composition.backend.journal.holds
        coordinator.echomuse_runtime.endpoint.failed=False
        from custom_components.household_voice.native_alarm import get_native_alarm_backend
        native=get_native_alarm_backend(hass);native_journal=await native.get_journal()
        await native.write(native_journal.begin,device,satellite.entity_id)
        assert (await composition.runtime.async_start(device,Context(),request_id='native-held'))['status']=='queued'
        await composition.runtime.sessions[device].task
        assert composition.runtime.latest[device]['status']=='error' and not satellite.prompt.is_set()
        try:await recover(hass,composition,satellite.entity_id,True)
        except ValueError:pass
        else:raise AssertionError('Native output hold bypassed')
        await native.write(native_journal.clear,device)
        assert not (await recover(hass,composition,satellite.entity_id,True))['cleared'] # No native question was invoked.
        # Persisted EchoMuse unknown output is independently authoritative.
        hass.config.external_url='https://fixture.invalid'
        echo=coordinator.echomuse_runtime
        provision=await echo.async_provision(satellite.entity_id,'synthetic-controller')
        key=provision['key_id'];turn='1'*32
        assert await echo.endpoint._write(echo.endpoint.ledger.reserve,key,'2'*32,turn,'begin',1)== 'reserved'
        assert await echo.endpoint._write(echo.endpoint.ledger.reserve,key,'3'*32,turn,'play',2)== 'reserved'
        await echo.endpoint._write(echo.endpoint.ledger.finish,key,turn,'unknown')
        assert (await composition.runtime.async_start(device,Context(),request_id='echo-held'))['status']=='queued'
        await composition.runtime.sessions[device].task
        assert composition.runtime.latest[device]['status']=='error' and not satellite.prompt.is_set()
        try:await recover(hass,composition,satellite.entity_id,True)
        except ValueError:pass
        else:raise AssertionError('EchoMuse unknown hold bypassed')
        await echo.endpoint._write(echo.endpoint.ledger.recover,key)
        assert not (await recover(hass,composition,satellite.entity_id,True))['cleared'] # Admission refusal kept no extra mic hold.
        # Seed consumed tombstones to exercise the threshold, not 119 fake captures.
        composition.backend.book.records.update({str(i):SimpleNamespace(phase='consumed',
            grant={'operation_id':'prior-'+str(i),'source_device_id':'prior-source'}) for i in range(119)})
        upload=client.upload_capture
        upload_calls=[]
        async def lost_ack(captured):
            receipt=await upload(captured);upload_calls.append(receipt['id'])
            raise TimeoutError('synthetic lost upload acknowledgement')
        client.upload_capture=lost_ack
        satellite.prompt_release=asyncio.Event()
        receipt=await composition.runtime.async_start(device,Context(),request_id='composed-proof')
        assert receipt['status']=='queued'
        task=composition.runtime.sessions[device].task
        event=await asyncio.wait_for(control.receive_json(),5);grant=event['event']['grant']
        message_id=2
        async def send(frame):
            nonlocal message_id
            message_id+=1
            await control.send_json({'id':message_id,'type':'household_voice/recording_worker/frame','frame':frame})
            response=await control.receive_json();assert response.get('success')
            if frame['kind']=='ack':satellite.prompt_release.set()
            return response['result']
        transport=CoreCaptureClient(http.session,token,url=str(http.make_url('/api/websocket')).replace('http:','ws:'))
        worker=asyncio.create_task(transport.record(grant,send))
        await asyncio.wait_for(satellite.started.wait(),5);await finish(satellite,pcm)
        await asyncio.wait_for(worker,5);await asyncio.wait_for(task,5)
        assert composition.runtime.latest[device]['status']=='error',composition.runtime.latest[device]
        assert len(upload_calls)==3 and len(set(upload_calls))==1
        rotate=(await asyncio.wait_for(control.receive_json(),5))['event']
        assert rotate=={'type':'rotate','connection_id':grant['connection_id']}
        assert composition.backend.connection is None and not composition.backend.operations
        ledger=composition.runtime.handoff.authority.ledger
        row=await client._run(ledger.get,'composed-proof')
        assert row['phase']=='uploading' and row['upload_signature'] and row['recording_id'] is None
        committed_recording=upload_calls[0]
        raw=await client._run(client._authority.recordings.audio,committed_recording)
        with wave.open(io.BytesIO(raw),'rb') as wav:assert wav.readframes(wav.getnframes())==pcm
        assert not composition.backend.journal.holds
        # Actual config-entry reload reopens the same existing authority and recovers.
        await control.close();control=None
        from unittest.mock import patch
        from custom_components.household_voice.broadcast_runtime import BroadcastRuntime
        with patch.object(BroadcastRuntime,'async_start_worker',return_value=None):
            assert await hass.config_entries.async_reload(entry.entry_id)
        coordinator=hass.data['household_voice'][entry.entry_id]
        composition=coordinator.native_recording;client=coordinator.client
        composition.runtime.adapter=adapter
        for delivery_task in composition.runtime._workers:delivery_task.cancel()
        await asyncio.gather(*composition.runtime._workers,return_exceptions=True)
        composition.runtime._workers=[]
        recovered=await composition.runtime.handoff.get('composed-proof')
        assert recovered['phase']=='queued' and recovered['recording_id']==committed_recording
        status=await composition.runtime.async_status(device)
        assert status['status']=='delivery_status'
        assert await composition.runtime.async_worker_once()
        assert await composition.runtime.async_worker_once()
        assert len(delivered)==2 and all(text=='Dinner is ready' for _,text in delivered)
        assert (await composition.runtime.async_start(device,Context(),request_id='composed-proof'))['status']=='no_change'
        # A canceled native question retains the unknown-source journal and the
        # durable request cancellation; it cannot be revived after backup.
        control=await http.ws_connect('/api/websocket');await authenticate(control,token)
        await control.send_json({'id':2,'type':'household_voice/recording_worker/connect','protocol_version':VERSION})
        assert (await control.receive_json())['success']
        satellite.prompt_release.clear()
        assert (await composition.runtime.async_start(device,Context(),request_id='cancel-proof'))['status']=='queued'
        event=await asyncio.wait_for(control.receive_json(),5);assert event['event']['type']=='grant'
        await composition.runtime.async_cancel(device)
        assert composition.backend.journal.holds=={device:'cancel-proof'}
        canceled=await composition.runtime.handoff.get('cancel-proof');assert canceled['cancel_requested']
        await backup.async_pre_backup(hass)
        assert composition.paused and client._backup_paused
        await backup.async_post_backup(hass)
        assert coordinator.broadcast_runtime is composition.runtime
        assert not client._backup_paused
        composition.runtime.adapter=adapter
        assert composition.runtime.enabled and not composition.backend.paused
        # Disabling native capture cannot bypass its uncertain microphone hold.
        saved_journal=composition.backend.journal
        hass.config_entries.async_update_entry(entry,options={**entry.options,'native_recording_enabled':False})
        await hass.async_block_till_done()
        legacy=hass.data['household_voice'][entry.entry_id]
        assert legacy.native_recording is None
        refused=await legacy.broadcast_runtime.async_start(device,Context(),request_id='legacy-held')
        assert refused['reason']=='recording_source_requires_recovery'
        assert await legacy.client.schedules() # Durable schedules remain usable.
        saved_journal.failed=True
        assert (await legacy.broadcast_runtime.async_start('different-device',Context()))['reason']=='recording_source_requires_recovery'
        saved_journal.failed=False
        hass.config_entries.async_update_entry(entry,options={**entry.options,'native_recording_enabled':True})
        await hass.async_block_till_done()
        coordinator=hass.data['household_voice'][entry.entry_id]
        composition=coordinator.native_recording;client=coordinator.client
        assert composition.backend.journal is saved_journal and device in saved_journal.holds
        composition.runtime.adapter=adapter
        # Admin-only recovery never accepts a missing confirmation or arbitrary source.
        denied_user=await hass.auth.async_create_user('Synthetic nonadmin',group_ids=[GROUP_ID_USER])
        denied_refresh=await hass.auth.async_create_refresh_token(denied_user,client_id='https://fixture.invalid/')
        denied=await http.ws_connect('/api/websocket');await denied.receive_json()
        await denied.send_json({'type':'auth','access_token':hass.auth.async_create_access_token(denied_refresh)})
        assert (await denied.receive_json())['type']=='auth_ok'
        await denied.send_json({'id':1,'type':'household_voice/native_recording','operation':'recover',
            'assistant_id':satellite.entity_id,'confirm_microphone_stopped':True})
        assert (await denied.receive_json())['error']['code']=='unauthorized';await denied.close()
        for target,confirmation in [(satellite.entity_id,False),('assist_satellite.unknown',True)]:
            try:await recover(hass,composition,target,confirmation)
            except ValueError:pass
            else:raise AssertionError('Unsafe recovery admitted')
        assert device in composition.backend.journal.holds
        # Recovery is exact-source and explicit, after genuine canceled pipeline cleanup.
        assert satellite.run.done()
        await asyncio.sleep(0)
        result=await recover(hass,composition,satellite.entity_id,True)
        assert result['cleared'] and not composition.backend.journal.holds
        await control.send_json({'id':77,'type':'household_voice/native_recording','operation':'list'})
        # unavailable from backup remains queued on this old control epoch.
        while True:
            response=await control.receive_json()
            if response.get('id')==77:break
        assert response['success'] and response['result']['sources'][0]['assistant_id']==satellite.entity_id
        # Whole integration backup failure must not resume a replacement runtime.
        old_runtime=composition.runtime;gate=asyncio.Event();original_close=old_runtime.async_close
        async def blocked_close():
            await gate.wait();await original_close()
        old_runtime.async_close=blocked_close;composition.pause_shutdown.timeout=.02
        try:await backup.async_pre_backup(hass)
        except ValueError:pass
        else:raise AssertionError('Unfinished pause was accepted')
        assert composition.runtime is old_runtime and composition.paused
        assert hass.data[DATA_KEY] is composition and not composition.pause_complete
        try:await composition.resume_after_backup()
        except ValueError:pass
        else:raise AssertionError('Unsafe resume admitted')
        gate.set();await composition.pause_for_backup();await composition.resume_after_backup()
        assert coordinator.broadcast_runtime is composition.runtime
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert DATA_KEY not in hass.data
        client_closed=True
        return {**scope_proof,'persisted_native_and_echomuse_holds_block_question_and_recovery':True,
            'actual_integration_setup_reload_backup_unload':True,'source_recovery_exact_and_confirmed':True,
            'native_disable_reload_cannot_bypass_unknown_source_hold':True,
            'whole_backup_failure_cannot_replace_owned_runtime':True,'recovery_denies_nonadmin_wrong_source_missing_confirmation':True,
            'periodic_config_preserves_native_profile':True,'native_mode_no_stt_platform':True,
            'actual_native_question_to_existing_capture_api':True,'generated_binding_no_stt_wrapper':True,
            'exact_original_wav_stored':True,'existing_outbox_survives_authority_restart':True,
            'original_broadcast_runtime_delivery_worker':True,'phone_target_transcript_preserved':True,
            'duplicate_request_no_recapture':True,'backup_quiescence':True,
            'three_lost_upload_acks_recovered_without_audio_retry':True,
            'actual_v2_rotation_after_durable_release_seeded_119_tombstones':True,
            'native_cancel_retains_unknown_source_hold':True,'backup_resume_preserves_hold':True,
            'synthetic_transport_only':True,'supervisor_proxy_tested':False,'production_adapter_proven':False}
    finally:
        if worker and not worker.done():worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        if control:await control.close()
        if not client_closed:await hass.config_entries.async_unload(entry.entry_id)
