"""Actual HA announce entity service and native schedule worker; synthetic audio sink."""
import asyncio
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType,ModuleType,SimpleNamespace


async def main():
    from homeassistant import loader
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.bootstrap import async_load_base_functionality
    from homeassistant.config_entries import ConfigEntries,ConfigEntry
    from homeassistant.const import __version__
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry as ar,device_registry as dr,entity_registry as er
    from homeassistant.setup import async_setup_component
    from homeassistant.components.assist_satellite import AssistSatelliteEntity,AssistSatelliteEntityFeature,AssistSatelliteConfiguration
    from homeassistant.components.assist_satellite.const import DATA_COMPONENT
    package=ModuleType('native_actual_fixture');package.__path__=[str(Path(__file__).resolve().parents[1]/'custom_components/household_voice')];sys.modules[package.__name__]=package
    native=importlib.import_module(package.__name__+'.native_alarm');worker_module=importlib.import_module(package.__name__+'.ring_worker')
    collector=importlib.import_module(package.__name__+'.area_registry');core=importlib.import_module(package.__name__+'._core.scheduler');runtime_module=importlib.import_module(package.__name__+'._core.runtime')
    with tempfile.TemporaryDirectory(prefix='native-alarm-',dir='/tmp') as directory:
        hass=HomeAssistant(directory);hass.config.skip_pip=True;hass.config.internal_url='http://ha.fixture.invalid:8123'
        loader.async_setup(hass);hass.config_entries=ConfigEntries(hass,{})
        worker=None
        try:
            assert await async_load_base_functionality(hass)
            hass.auth=await auth_manager_from_config(hass,[],[])
            assert await async_setup_component(hass,'network',{})
            assert await async_setup_component(hass,'assist_satellite',{})
            Path(directory,'.storage').mkdir(exist_ok=True)
            owner=ConfigEntry(domain='esphome',title='Native fixture',data={},options={},source='user',version=1,minor_version=1,unique_id=None,discovery_keys=MappingProxyType({}),subentries_data=None)
            hass.config_entries._entries[owner.entry_id]=owner
            area=ar.async_get(hass).async_create('Fixture room');device=dr.async_get(hass).async_get_or_create(config_entry_id=owner.entry_id,identifiers={('esphome','native-fixture')})
            dr.async_get(hass).async_update_device(device.id,area_id=area.id)
            registered=er.async_get(hass).async_get_or_create('assist_satellite','assist_satellite','native-fixture',config_entry=owner,device_id=device.id,suggested_object_id='native_fixture')
            class Satellite(AssistSatelliteEntity):
                _attr_name='Native fixture';_attr_unique_id='native-fixture';_attr_supported_features=AssistSatelliteEntityFeature.ANNOUNCE
                def __init__(self):self.started=asyncio.Event();self.finished=asyncio.Event();self.calls=[]
                def async_get_configuration(self):return AssistSatelliteConfiguration([],[],0)
                async def async_set_configuration(self,config):pass
                def on_pipeline_event(self,event):pass
                async def async_announce(self,announcement):
                    self.calls.append(announcement)
                    assert announcement.original_media_id.endswith('/audio/alarm.wav') and announcement.media_id_source=='url'
                    assert announcement.preannounce_media_id is None and not announcement.message
                    self.started.set();await self.finished.wait()
            satellite=Satellite();await hass.data[DATA_COMPONENT].async_add_entities([satellite]);assert satellite.entity_id==registered.entity_id
            aid=satellite.entity_id
            er.async_get(hass).async_update_entity(aid,device_id=device.id)
            entry=SimpleNamespace(entry_id='native-fixture',options={'live_actions_enabled':True,'ring_actions_enabled':True,'assistant_speaker_overrides':{aid:None}})
            mapping=collector.collect_pairings(hass,entry.options);binding=mapping['assistants'][aid]
            assert binding['schedule_output']=={'kind':'native','entity_id':aid,'device_id':device.id}, binding
            config_module=importlib.import_module(package.__name__+'.runtime_config')
            now=[1704067200.0];scheduler=core.Scheduler(Path(directory)/'schedules.sqlite3',clock=lambda:now[0]);authority=runtime_module.RuntimeAuthority(scheduler)
            desired=config_module.configuration(entry.options,mapping)
            assert all(isinstance(a,str) and 1<=len(a)<=120 for a in desired['assistants'][aid]['aliases']), repr(desired['assistants'][aid]['aliases'])
            authority.configure({**desired,'expected_revision':0})
            timer=scheduler.create_timer('Native test',5,area.id,'create-timer',target_assistant_id=aid);now[0]+=5
            clip=authority.claim()['clip'];assert clip['output_kind']=='native' and clip['target']==aid
            class Client:
                url='http://ha.fixture.invalid:8123/api/household_voice/native-fixture'
                async def validate_ring(self,**args):return authority.validate(**args)
                async def ack_ring(self,**args):return authority.acknowledge(**args)
            worker=worker_module.RingWorker(hass,entry,Client())
            task=asyncio.create_task(worker._play(clip));await asyncio.wait_for(satellite.started.wait(),3)
            assert not task.done() and satellite.state=='responding'
            satellite.finished.set();await task
            assert satellite.state=='idle' and worker.last_error is None
            assert not (await native.get_native_alarm_backend(hass).get_journal()).held(device.id)
            # Dismissal stops future pulses; HA has no clip-specific stop action.
            ringing=next(row for row in scheduler.list() if row['id']==timer['id'])
            scheduler.command(timer['id'],'dismiss','dismiss',ringing['revision']);now[0]+=3;assert authority.claim()['clip'] is None
            satellite.started=asyncio.Event();satellite.finished=asyncio.Event()
            transport=native.NativeAlarmTransport(hass,entry,aid,device.id,timeout=.03)
            uncertain=await transport.play(aid,Client.url+'/audio/alarm.wav')
            assert uncertain['outcome']=='service_outcome_unknown'
            try:await transport.play(aid,Client.url+'/audio/alarm.wav')
            except native.AudioTransportError:pass
            else:raise AssertionError('Uncertain native playback repeated')
            assert len(satellite.calls)==2
            await native.async_reset_native_alarm(hass,entry,aid,True)
            print(json.dumps({'result':'passed','home_assistant_version':__version__,'actual_ha_announce_service_and_entity_completion':True,'actual_registry_native_output_discovery':True,'durable_assistant_timer_to_native_ring_worker':True,'fixed_finite_alarm_media_no_tts_or_chime':True,'timeout_quarantine_and_explicit_recovery':True,'dismiss_prevents_future_clips':True,'physical_audio':False,'household_contacts':False}))
        finally:
            if worker:await worker.async_close()
            await hass.async_stop(force=True)

if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(asyncio.wait_for(main(),180))
