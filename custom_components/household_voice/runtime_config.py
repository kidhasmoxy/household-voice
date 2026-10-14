"""Synchronize explicit HA room/profile options with the durable coordinator."""


def configuration(options, discovered=None):
    rooms={}
    def room(identifier):
        return rooms.setdefault(identifier,{'name':identifier.replace('_',' ').title(),'satellites':[],'capture_profiles':{}})
    for row in options.get('satellites',[]):
        item=room(row['room_id'])
        if row['satellite_device_id'] not in item['satellites']:
            item['satellites'].append(row['satellite_device_id'])
    for row in options.get('media_rooms',[]): room(row['room_id'])
    assistants={}
    if discovered is not None:
        for area in discovered.get('areas',[]): room(area['id'])['name']=area['name']
        assigned={device for data in rooms.values() for device in data['satellites']}
        for identifier,binding in discovered.get('assistants',{}).items():
            area=binding.get('area_id')
            if area is not None:
                item=room(area)
                if binding.get('device_id') and binding['device_id'] not in assigned:
                    item['satellites'].append(binding['device_id']); assigned.add(binding['device_id'])
            assistants[identifier]={key:binding.get(key) for key in ('device_id','area_id','speaker_id','status','name','schedule_output')}
            assistants[identifier]['aliases']=list(binding.get('aliases',[]))
    alarm_rooms={}
    for row in options.get('alarm_rooms',[]):
        room(row['room_id'])
        alarm_rooms[row['room_id']]=list(row['speakers'])
    for profile in options.get('stt_capture_profiles',[]):
        matched=[r for r in rooms.values() if profile['source_device_id'] in r['satellites']]
        if len(matched)!=1:
            raise ValueError('Capture profile needs one configured satellite room')
        matched[0]['capture_profiles'][profile['profile_id']]=profile['source_device_id']
    return {'rooms':rooms,'alarm_rooms':alarm_rooms,'ring_fallback_speakers':list(options.get('ring_fallback_speakers',[])),
        'assistants':assistants,
        'ring_enabled':options.get('live_actions_enabled') is True and options.get('ring_actions_enabled') is True}


async def async_configure(client,options,*,hass=None):
    discovered=None
    if hass is not None:
        from .area_registry import collect_pairings
        discovered=collect_pairings(hass,options)
    if hass is not None and options.get("native_recording_enabled") is True:
        from .native_recording_bindings import configuration_with_native_sources
        desired=configuration_with_native_sources(hass,options,discovered)
    else:
        desired=configuration(options,discovered)
    if not options and not (discovered or {}).get('assistants'):
        return discovered
    current=await client.runtime_config()
    if all(current.get(k)==v for k,v in desired.items()): return discovered
    await client.configure_runtime(desired,current['revision'])
    return discovered
