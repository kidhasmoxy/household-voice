"""Synchronize explicit HA room/profile options with the durable coordinator."""


def configuration(options):
    rooms={}
    def room(identifier):
        return rooms.setdefault(identifier,{'name':identifier.replace('_',' ').title(),'satellites':[],'capture_profiles':{}})
    for row in options.get('satellites',[]):
        item=room(row['room_id'])
        if row['satellite_device_id'] not in item['satellites']:
            item['satellites'].append(row['satellite_device_id'])
    for row in options.get('media_rooms',[]): room(row['room_id'])
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
        'ring_enabled':options.get('live_actions_enabled') is True and options.get('ring_actions_enabled') is True}


async def async_configure(client,options):
    desired=configuration(options)
    if not options:
        return
    current=await client.runtime_config()
    if all(current.get(k)==v for k,v in desired.items()): return
    await client.configure_runtime(desired,current['revision'])
