"""Actual HA native discovery and room routing with inert Music Assistant APIs."""
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, llm


async def verify_media_controls(hass, entry, context, satellite):
    from homeassistant.components.music_assistant import helpers
    registry=er.async_get(hass)
    area_id=dr.async_get(hass).async_get(context.device_id).area_id
    ma=ConfigEntry(domain='music_assistant',title='Inert media controls',data={},options={},
        source='user',version=1,minor_version=1,unique_id=None,discovery_keys=MappingProxyType({}),subentries_data=None)
    hass.config_entries._entries[ma.entry_id]=ma
    player_device=dr.async_get(hass).async_get_or_create(config_entry_id=ma.entry_id,
        identifiers={('music_assistant','inert-controls')})
    dr.async_get(hass).async_update_device(player_device.id,area_id=area_id)
    player_entity=registry.async_get_or_create('media_player','music_assistant','inert-controls',
        config_entry=ma,device_id=player_device.id)
    native=SimpleNamespace(player_id='inert-controls',available=True,synced_to=None,active_group=None,group_members=[])
    ma.runtime_data=SimpleNamespace(mass=SimpleNamespace(players=SimpleNamespace(get=lambda key:native if key=='inert-controls' else None)))
    old_options=deepcopy(entry.options)
    hass.states.async_set(satellite.entity_id,'idle')
    from homeassistant.components.media_player import MediaPlayerEntityFeature as F
    control_features=int(F.NEXT_TRACK|F.PREVIOUS_TRACK|F.VOLUME_SET|F.VOLUME_MUTE)
    hass.states.async_set(player_entity.entity_id,'playing',{'supported_features':control_features,
        'volume_level':.8,'is_volume_muted':False,'media_content_id':'fixture-song'})
    entry.options.update(live_actions_enabled=True,media_enabled=True,media_rooms=[{
        'room_id':area_id,'music_player':player_entity.entity_id,'config_entry_id':ma.entry_id,
        'username':'fixture-profile','default_sleep_playlist':''}])
    queue={'queue_id':'inert-controls','active':True,'is_dynamic':False,'shuffle_enabled':False,'repeat_mode':'off',
        'sources':[],'current_item':{'queue_item_id':'fixture-item','media_item':{
            'media_type':'track','uri':'fixture://track/1','name':'Fixture song','album':{'name':'Fixture album'}}}}
    player={'player_id':'inert-controls','available':True,'active_source':'inert-controls','group_members':[],
        'playback_state':'playing','current_media':{'title':'Fixture song','artist':'Fixture artist',
            'album':'Fixture album','media_type':'track','duration':180,'elapsed_time':40}}
    calls=[]; change_area=False; change_protection=None
    async def send(command,**args):
        calls.append((command,args))
        if command=='players/get':return deepcopy(player)
        if command=='player_queues/get_active_queue':return deepcopy(queue)
        if command=='music/verify_item_uri':
            assert args=={'uri':'fixture://track/1','user':'fixture-profile','require_schema':35}
            if change_area:dr.async_get(hass).async_update_device(context.device_id,area_id=None)
            if change_protection=='dynamic':queue['is_dynamic']=True
            if change_protection=='spoken':queue['current_item']['media_item']['metadata']={'genres':['Spoken Word']}
            return True
        assert args['player_id']=='inert-controls' and args['source_id']=='inert-controls'
        if command=='players/cmd/shuffle':queue['shuffle_enabled']=args['shuffle_enabled'];return
        if command=='players/cmd/repeat':queue['repeat_mode']=args['repeat_mode'];return
        raise AssertionError(command)
    client=SimpleNamespace(send_command=send)
    def data(value):return value.data if hasattr(value,'data') else value
    def mutations():return [row for row in calls if row[0].startswith('players/cmd/')]
    try:
        with patch.object(helpers,'get_music_assistant_client',return_value=client):
            instance=await llm.async_get_api(hass,'assist',context)
            names={tool.name for tool in instance.tools}
            assert {'household_voice__media_status','household_voice__media_queue_options','household_voice__media_control'}<=names
            status=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_status',tool_args={},id='controls-status')))
            assert status['status']=='ready' and status['title']=='Fixture song' and status['position_seconds']==40
            assert not mutations() and 'inert-controls' not in str(status)
            for raw_state,expected_state,active in (('idle','idle',False),('off','unknown',None)):
                player['playback_state']=raw_state
                stopped=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_status',
                    tool_args={},id='controls-state-'+raw_state)))
                assert stopped['state']==expected_state and stopped['playback_active'] is active
                assert all(stopped[key] is None for key in
                    ('title','artist','album','media_type','position_seconds','duration_seconds'))
                assert not mutations() and player['current_media']['title']=='Fixture song'
            player['playback_state']='paused'
            paused=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_status',
                tool_args={},id='controls-state-paused')))
            assert paused['state']=='paused' and paused['playback_active'] is False and paused['title']=='Fixture song'
            assert not mutations()
            player['playback_state']='playing'
            # Real Assist schemas, source-area resolution and service dispatch,
            # with only the final provider/HA hardware boundary made inert.
            service_calls=[]
            async def service(domain,service,data,**kwargs):
                assert domain=='media_player' and data['entity_id']==player_entity.entity_id
                service_calls.append((service,deepcopy(data)))
                current=dict(hass.states.get(player_entity.entity_id).attributes)
                if service=='volume_set':current['volume_level']=data['volume_level']
                elif service=='volume_mute':current['is_volume_muted']=data['is_volume_muted']
                else:
                    assert service in ('media_next_track','media_previous_track')
                    queue['current_item']['queue_item_id']='next-fixture-item'
                hass.states.async_set(player_entity.entity_id,'playing',current)
            sequence=0
            async def paired(args):
                nonlocal sequence
                sequence+=1
                return data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_control',
                    tool_args=args,id='paired-fixture-'+str(sequence))))
            with patch.object(type(hass.services),'async_call',side_effect=service),patch.object(type(hass.services),'has_service',return_value=True):
                skipped=await paired({'operation':'next'})
                assert skipped['status']=='action_accepted' and skipped['playback_confirmed'] is False
                assert service_calls[-1]==('media_next_track',{'entity_id':player_entity.entity_id})
                queue['is_dynamic']=True
                assert (await paired({'operation':'previous'}))['status']=='action_accepted'
                queue['is_dynamic']=False
                queue['current_item']['media_item'].update(media_type='audiobook',uri='fixture://audiobook/1')
                count=len(service_calls)
                assert (await paired({'operation':'next'}))['reason']=='spoken_or_unknown_content_skip_blocked'
                assert len(service_calls)==count
                authorizations=sum(command=='music/verify_item_uri' for command,_ in calls)
                assert (await paired({'operation':'volume_set','volume_percent':40}))['settings_confirmed']
                assert sum(command=='music/verify_item_uri' for command,_ in calls)==authorizations
                from custom_components.household_voice.room_focus import get_room_focus_state,RoomVolumeController,AnnouncementRoomFocus
                focus=get_room_focus_state(hass,entry)
                authority=AnnouncementRoomFocus(focus,RoomVolumeController(hass,focus))
                await authority._acquire('fixture-wake',{player_entity.entity_id},set(),.25)
                assert hass.states.get(player_entity.entity_id).attributes['volume_level']==.1
                changed_volume=await paired({'operation':'volume_up','volume_delta':10})
                assert changed_volume['volume_percent']==50 and changed_volume['temporary_duck_active']
                assert hass.states.get(player_entity.entity_id).attributes['volume_level']==.12
                await authority._release('fixture-wake',set())
                assert hass.states.get(player_entity.entity_id).attributes['volume_level']==.5
                assert (await paired({'operation':'mute'}))['is_volume_muted'] is True
                assert (await paired({'operation':'unmute'}))['is_volume_muted'] is False
                count=len(service_calls)
                invalid=await paired({'operation':'volume_set','volume_percent':20,'target_room':'Imaginary room'})
                assert invalid['status']!='action_accepted' and len(service_calls)==count
                # Final admission after the journal await must not trust stale
                # registry topology or ignore an independent transport hold.
                from custom_components.household_voice import room_focus as rf, satellite_transport as transport
                from custom_components.household_voice.satellite_executor import MemoryJournal
                for race in ('raw_group','native_rebind','existing_hold','late_hold'):
                    isolated=rf.RoomFocusState(rf.AudioFocus(),MemoryJournal(),
                        allowed_players={player_entity.entity_id},loaded=True,recovery_attempted=True)
                    key=(ma.entry_id,'inert-controls')
                    def mutate():
                        if race=='raw_group':player['group_members']=['inert-controls','foreign-player']
                        elif race=='native_rebind':registry.async_update_entity(player_entity.entity_id,new_unique_id='foreign-player')
                        else:transport.restore_uncertain_audio(hass,provider_entry_id=key[0],native_player_id=key[1])
                    save=isolated.journal.save
                    async def save_then_change(value):
                        await save(value)
                        mutate()
                    isolated.journal.save=save_then_change
                    if race=='existing_hold':mutate()
                    count=len(service_calls)
                    try:
                        with patch.object(rf,'get_room_focus_state',return_value=isolated):
                            refused=await paired({'operation':'volume_set','volume_percent':35})
                        assert refused['status']=='error' and refused['mutation_dispatched'] is False,refused
                        assert len(service_calls)==count
                        if race!='existing_hold':assert isolated.records[player_entity.entity_id]['status']=='pending'
                        if race in ('existing_hold','late_hold'):assert key in transport._UNCERTAIN[hass]
                    finally:
                        # Isolated fixture cleanup only, never runtime recovery.
                        player['group_members']=[]
                        if registry.async_get(player_entity.entity_id).unique_id!='inert-controls':
                            registry.async_update_entity(player_entity.entity_id,new_unique_id='inert-controls')
                        transport._UNCERTAIN.get(hass,{}).pop(key,None)
                queue['current_item']['media_item'].update(media_type='track',uri='fixture://track/1')
            request=llm.ToolInput(tool_name='household_voice__media_queue_options',tool_args={'shuffle':True,'repeat':'all'},id='controls-set')
            changed=data(await instance.async_call_tool(request))
            assert changed['settings_confirmed'] and changed['shuffle'] and changed['repeat']=='all'
            assert len(mutations())==2
            assert data(await instance.async_call_tool(request))==changed and len(mutations())==2
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False,'target_room':'Not a registered area'},id='controls-invalid-area')))
            assert denied['status']!='action_accepted' and len(mutations())==2
            # A stable current group supports queue controls on its leader.
            native.group_members=['inert-controls','other-player']
            player['group_members']=['inert-controls','other-player']
            grouped=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_status',
                tool_args={},id='controls-grouped-status')))
            assert grouped['status']=='ready' and grouped['title']=='Fixture song' and len(mutations())==2
            changed=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False},id='controls-grouped-options')))
            assert changed['status']=='action_accepted' and len(mutations())==3
            from custom_components.household_voice.media_llm import _resolve
            assert _resolve(hass,entry,context)[1]['music_player']==player_entity.entity_id
            player['group_members'].append('unexpected-member')
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':True},id='controls-group-changed')))
            assert denied['status']!='action_accepted' and len(mutations())==3
            native.group_members=[];player['group_members']=[]
            for change_protection,reason in (('spoken','spoken_or_unknown_content_queue_options_blocked'),
                    ('dynamic','dynamic_or_unknown_queue_mode')):
                denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                    tool_args={'shuffle':False},id='controls-late-'+change_protection)))
                assert denied['reason']==reason and len(mutations())==3
                queue['is_dynamic']=False;queue['current_item']['media_item'].pop('metadata',None)
            change_protection=None
            queue['current_item']['media_item'].update(media_type='audiobook',uri='fixture://audiobook/1')
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False},id='controls-book')))
            assert denied['reason']=='spoken_or_unknown_content_queue_options_blocked' and len(mutations())==3
            queue['current_item']['media_item'].update(media_type='track',uri='fixture://track/1')
            change_area=True
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False},id='controls-area-changed')))
            assert denied['status']!='action_accepted' and len(mutations())==3
    finally:
        entry.options=old_options
        dr.async_get(hass).async_update_device(context.device_id,area_id=area_id)
        hass.states.async_remove(player_entity.entity_id)
        registry.async_remove(player_entity.entity_id)
        dr.async_get(hass).async_remove_device(player_device.id)
        hass.config_entries._entries.pop(ma.entry_id)
    return {'actual_native_schema_and_auto_area':True,'inert_ma_source_guard_readback':True,
            'paired_next_previous_volume_mute':True,'final_volume_admission_identity_topology_and_transport_hold':True,'relative_volume_preserves_wake_duck_baseline':True,
            'idle_and_unknown_retained_metadata_suppressed':True,'paused_metadata_state_qualified':True,
            'book_and_area_change_protection':True,'household_contact':False,'physical_audio':False}
