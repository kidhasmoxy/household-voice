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
    adapter=SimpleNamespace(targets=lambda *args:targets,deliver=deliver)
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
        assert (await recover(hass,composition,satellite.entity_id,True))['cleared']
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
        assert (await recover(hass,composition,satellite.entity_id,True))['cleared']
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
        return {'persisted_native_and_echomuse_holds_block_question_and_recovery':True,
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
