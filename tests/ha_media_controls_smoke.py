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
    hass.states.async_set(player_entity.entity_id,'playing')
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
            assert {'household_voice__media_status','household_voice__media_queue_options'}<=names
            status=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_status',tool_args={},id='controls-status')))
            assert status['status']=='ready' and status['title']=='Fixture song' and status['position_seconds']==40
            assert not mutations() and 'inert-controls' not in str(status)
            request=llm.ToolInput(tool_name='household_voice__media_queue_options',tool_args={'shuffle':True,'repeat':'all'},id='controls-set')
            changed=data(await instance.async_call_tool(request))
            assert changed['settings_confirmed'] and changed['shuffle'] and changed['repeat']=='all'
            assert len(mutations())==2
            assert data(await instance.async_call_tool(request))==changed and len(mutations())==2
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False,'target_room':'Not a registered area'},id='controls-invalid-area')))
            assert denied['status']!='action_accepted' and len(mutations())==2
            # A preferred grouped output remains observable, but the same
            # topology is still ineligible for every mutating media tool.
            native.group_members=['inert-controls','other-player']
            player['group_members']=['inert-controls','other-player']
            grouped=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_status',
                tool_args={},id='controls-grouped-status')))
            assert grouped['status']=='ready' and grouped['title']=='Fixture song' and len(mutations())==2
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False},id='controls-grouped-options')))
            assert denied['status']!='action_accepted' and len(mutations())==2
            from custom_components.household_voice.media_llm import _resolve
            try:
                _resolve(hass,entry,context)
            except Exception:
                pass
            else:
                raise AssertionError('Read-only group routing leaked into playback scope')
            native.group_members=[];player['group_members']=[]
            for change_protection,reason in (('spoken','spoken_or_unknown_content_queue_options_blocked'),
                    ('dynamic','dynamic_or_unknown_queue_mode')):
                denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                    tool_args={'shuffle':False},id='controls-late-'+change_protection)))
                assert denied['reason']==reason and len(mutations())==2
                queue['is_dynamic']=False;queue['current_item']['media_item'].pop('metadata',None)
            change_protection=None
            queue['current_item']['media_item'].update(media_type='audiobook',uri='fixture://audiobook/1')
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False},id='controls-book')))
            assert denied['reason']=='spoken_or_unknown_content_queue_options_blocked' and len(mutations())==2
            queue['current_item']['media_item'].update(media_type='track',uri='fixture://track/1')
            change_area=True
            denied=data(await instance.async_call_tool(llm.ToolInput(tool_name='household_voice__media_queue_options',
                tool_args={'shuffle':False},id='controls-area-changed')))
            assert denied['status']!='action_accepted' and len(mutations())==2
    finally:
        entry.options=old_options
        dr.async_get(hass).async_update_device(context.device_id,area_id=area_id)
        hass.states.async_remove(player_entity.entity_id)
        registry.async_remove(player_entity.entity_id)
        dr.async_get(hass).async_remove_device(player_device.id)
        hass.config_entries._entries.pop(ma.entry_id)
    return {'actual_native_schema_and_auto_area':True,'inert_ma_source_guard_readback':True,
            'book_and_area_change_protection':True,'household_contact':False,'physical_audio':False}
