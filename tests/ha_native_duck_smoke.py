"""Actual HA registries/state events/services + SQLite; synthetic volume sink only."""
import asyncio
import importlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType, ModuleType, SimpleNamespace as NS


async def main():
    from homeassistant import loader
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.bootstrap import async_load_base_functionality
    from homeassistant.config_entries import ConfigEntries, ConfigEntry
    from homeassistant.const import __version__
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
    from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
    from homeassistant.components.media_player import MediaPlayerEntityFeature

    package=ModuleType('native_duck_actual_fixture')
    package.__path__=[str(Path(__file__).resolve().parents[1]/'custom_components/household_voice')]
    sys.modules[package.__name__]=package
    native=importlib.import_module(package.__name__+'.native_duck')
    focus=importlib.import_module(package.__name__+'.room_focus')
    collector=importlib.import_module(package.__name__+'.area_registry')
    with tempfile.TemporaryDirectory(prefix='native-duck-',dir='/tmp') as directory:
        hass=HomeAssistant(directory);hass.config.skip_pip=True
        loader.async_setup(hass);hass.config_entries=ConfigEntries(hass,{})
        runtime=None
        try:
            assert await async_load_base_functionality(hass)
            hass.auth=await auth_manager_from_config(hass,[],[])
            Path(directory,'.storage').mkdir(exist_ok=True)
            def owner(domain):
                value=ConfigEntry(domain=domain,title='Synthetic '+domain,data={},options={},source='user',
                    version=1,minor_version=1,unique_id=None,discovery_keys=MappingProxyType({}),subentries_data=None)
                hass.config_entries._entries[value.entry_id]=value
                return value
            esp=owner('esphome');ma=owner('music_assistant');sonos=owner('sonos')
            area=ar.async_get(hass).async_create('Synthetic room')
            elsewhere=ar.async_get(hass).async_create('Other room')
            def device(entry,name,room,domain=None,identity=None):
                value=dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                    identifiers={(domain or entry.domain,identity or name)})
                return dr.async_get(hass).async_update_device(value.id,area_id=room.id)
            def entity(entry,domain,name,dev):
                return er.async_get(hass).async_get_or_create(domain,entry.domain,name,
                    config_entry=entry,device_id=dev.id,suggested_object_id=name)
            assistants=[]
            for name in ('first_voice','second_voice'):
                dev=device(esp,name,area)
                row=entity(esp,'assist_satellite',name,dev);assistants.append(row.entity_id)
                hass.states.async_set(row.entity_id,'idle',{'supported_features':int(AssistSatelliteEntityFeature.ANNOUNCE)})
                internal=entity(esp,'media_player',name+'_internal',dev)
                hass.states.async_set(internal.entity_id,'playing',{'supported_features':int(MediaPlayerEntityFeature.VOLUME_SET),'volume_level':.9})
            native_id='RINCON_FIXTURE'
            output=entity(ma,'media_player',native_id,device(ma,'output',area))
            alias=entity(sonos,'media_player','native_alias',device(sonos,'alias',area,'sonos',native_id))
            other=entity(sonos,'media_player','other_room',device(sonos,'other',elsewhere))
            ma.runtime_data=NS(mass=NS(players=NS(get=lambda identifier:NS(player_id=identifier,
                available=True,synced_to=None,active_group=None,group_members=[]))))
            attributes={'supported_features':int(MediaPlayerEntityFeature.VOLUME_SET),
                'volume_level':.8,'media_content_id':'synthetic-book','group_members':[]}
            for row in (output,alias,other):hass.states.async_set(row.entity_id,'playing',attributes)
            calls=[]
            async def volume(call):
                target=call.data['entity_id'];value=call.data['volume_level']
                assert target==output.entity_id,('Unexpected volume target',target)
                authority=hass.data['_household_voice_room_focus']['synthetic-native-duck']
                durable=await authority.journal.load()
                assert durable['players'][target]['status'] in ('pending','restoring')
                calls.append((target,value))
                old=hass.states.get(target)
                hass.states.async_set(target,old.state,{**old.attributes,'volume_level':value},context=call.context)
            hass.services.async_register('media_player','volume_set',volume)
            options={'live_actions_enabled':True,'native_room_ducking_enabled':True,
                'assistant_speaker_overrides':{aid:output.entity_id for aid in assistants}}
            entry=NS(entry_id='synthetic-native-duck',options=options)
            original=json.dumps(options,sort_keys=True)
            runtime=await native.async_setup_native_duck(hass,entry)
            def level():return hass.states.get(output.entity_id).attributes['volume_level']
            async def settle(predicate,description):
                async with asyncio.timeout(12):
                    while not predicate():await asyncio.sleep(.02)
                assert predicate(),description
            async def state(aid,value):
                hass.states.async_set(aid,value,{'supported_features':int(AssistSatelliteEntityFeature.ANNOUNCE)})
                await asyncio.sleep(0)
            # Forged payload is only a hint; actual registered state remains idle.
            hass.bus.async_fire('state_changed',{'entity_id':assistants[0],
                'old_state':hass.states.get(assistants[0]),'new_state':State(assistants[0],'listening')})
            await asyncio.sleep(.1);assert calls==[]
            await state(assistants[0],'listening');await settle(lambda:level()==.2,'native wake duck')
            shared=focus.get_room_focus_state(hass,entry)
            saved=await shared.journal.load()
            assert saved['players'][output.entity_id]['baseline']==.8
            assert saved['players'][output.entity_id]['status']=='confirmed'
            assert collector.collect_pairings(hass,entry.options)['assistants'][assistants[0]]['speaker_id']==output.entity_id
            assert json.dumps(options,sort_keys=True)==original
            for value in ('processing','responding','listening','processing','responding'):
                await state(assistants[0],value);await runtime.refresh();assert level()==.2
            assert calls==[(output.entity_id,.2)]
            # Another assistant in the room shares ownership without double duck.
            await state(assistants[1],'listening');await runtime.refresh();assert level()==.2
            await state(assistants[0],'idle');await runtime.refresh();assert level()==.2
            await state(assistants[1],'idle');await settle(lambda:level()==.8,'shared release')
            # Announcement overlap uses the exact same authority and journal.
            await state(assistants[0],'responding');await settle(lambda:level()==.2,'second exchange')
            authority=focus.AnnouncementRoomFocus(shared,focus.RoomVolumeController(hass,shared))
            async with authority.hold({output.entity_id},set()):
                await state(assistants[0],'idle');await runtime.refresh();assert level()==.2
            await settle(lambda:level()==.8,'announcement ownership release')
            # Manual changes during native speech are not restored over the user.
            await state(assistants[0],'listening');await settle(lambda:level()==.2,'third exchange')
            current=hass.states.get(output.entity_id)
            hass.states.async_set(output.entity_id,'playing',{**current.attributes,'volume_level':.55})
            await runtime.refresh();assert level()==.55
            await state(assistants[0],'idle');await settle(lambda:runtime.diagnostics['active_holds']==0,'manual release')
            assert level()==.55 and (output.entity_id,.8)!=calls[-1]
            # Unavailable is a definitive end, without waiting for a new model call.
            await state(assistants[0],'processing');await settle(lambda:level()==.14,'fourth exchange')
            await state(assistants[0],'unavailable');await settle(lambda:level()==.55,'unavailable restores')
            assert hass.states.get(alias.entity_id).attributes['volume_level']==.8
            assert hass.states.get(other.entity_id).attributes['volume_level']==.8
            await runtime.async_close();runtime=None
            assert (await shared.journal.load())['players']=={}
            print(json.dumps({'result':'passed','home_assistant_version':__version__,
                'actual_ha_registry_and_state_event_path':True,'actual_volume_service_with_synthetic_sink':True,
                'sqlite_baseline_before_volume_change':True,'native_active_states_hold_through_exchange':True,
                'same_room_assistant_overlap_no_double_duck':True,'announcement_shared_authority_overlap':True,
                'manual_volume_preserved':True,'unavailable_restores':True,
                'pairings_options_unchanged':True,'native_alias_internal_and_other_room_untouched':True,
                'forged_event_payload_ignored':True,'physical_audio':False,'household_contacts':False}))
        finally:
            if runtime:await runtime.async_close()
            await hass.async_stop(force=True)


if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(asyncio.wait_for(main(),180))
