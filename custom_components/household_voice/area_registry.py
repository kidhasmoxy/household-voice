"""Read HA registries/state into pure area recommendations; no household writes."""
from dataclasses import asdict
from collections.abc import Mapping
import hashlib
import json
import re

from .area_pairing import Device, Entity, DiscoverySnapshot, discover_pairings


def _entries(collection):
    return collection.values() if isinstance(collection,Mapping) else collection


def _physical_ids(device):
    # Sonos registry identifiers are authoritative RINCON identities. MAC is a
    # fallback only when registered by an integration, never parsed from a name.
    identifiers=getattr(device,'identifiers',set()) if device else set()
    sonos=sorted(str(value).upper() for domain,value in identifiers if domain=='sonos' and str(value).upper().startswith('RINCON_'))
    if sonos: return tuple('sonos:'+value for value in sonos)
    connections=getattr(device,'connections',set()) if device else set()
    macs=sorted(re.sub(r'[^0-9a-f]','',str(value).lower()) for kind,value in connections if kind=='mac')
    return tuple('mac:'+value for value in macs if len(value)==12)


def collect_pairings(hass,options, *, read_only=False, volume_only=False, music=False):
    """Synchronous event-loop registry read; recompute for each routed operation.

    MA transport currently supports MA outputs only. Native Sonos entities remain
    eligible room-volume controls. Provider groups are conservatively excluded
    unless the collector can establish a fixed, same-area endpoint. read_only
    permits observing an existing preferred grouped output; callers must never
    use that view to authorize playback or queue mutations.
    music permits MA group candidates only; the music resolver separately binds
    the current registered group leader and validates topology before dispatch.
    """
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
    area_entries=list(_entries(ar.async_get(hass).areas))
    devices_raw={item.id:item for item in _entries(dr.async_get(hass).devices)}
    registry=list(_entries(er.async_get(hass).entities))
    area_names={item.id:item.name for item in area_entries}
    devices=tuple(Device(item.id,getattr(dr,"async_get_effective_area_id",lambda _hass,device:getattr(device,"area_id",None))(hass,item),getattr(item,'disabled_by',None) is not None) for item in devices_raw.values())
    entities=[]; names={}; aliases={}
    for item in registry:
        if not item.entity_id.startswith(('assist_satellite.','media_player.')): continue
        state=hass.states.get(item.entity_id)
        attrs=dict(getattr(state,'attributes',{}) or {})
        device=devices_raw.get(item.device_id)
        integration=getattr(item,'platform','')
        owner=hass.config_entries.async_get_entry(getattr(item,'config_entry_id',None))
        disabled=getattr(item,'disabled_by',None) is not None or getattr(owner,'disabled_by',None) is not None
        available=state is not None and state.state not in ('unknown','unavailable')
        features=attrs.get('supported_features',0)
        # HA media-player volume_set flag is stable; no volume feature is guessed.
        from homeassistant.components.media_player import MediaPlayerEntityFeature
        volume=bool(isinstance(features,int) and features & MediaPlayerEntityFeature.VOLUME_SET)
        from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
        native_output=bool(item.entity_id.startswith('assist_satellite.') and isinstance(features,int) and features & AssistSatelliteEntityFeature.ANNOUNCE)
        physical=_physical_ids(device)
        topology='single'
        members=attrs.get('group_members',[])
        if isinstance(members,(list,tuple)) and any(member!=item.entity_id for member in members):
            topology='dynamic'
        if integration=='music_assistant':
            mass=getattr(getattr(owner,'runtime_data',None),'mass',None)
            player=mass.players.get(item.unique_id) if mass is not None else None
            if player is not None:
                available=available and getattr(player,'available',False) is True
                # Exact provider player ID is not universally a physical ID. Only
                # use an explicit Sonos RINCON ID (other MA IDs stay independent).
                native=getattr(player,'player_id',None)
                if isinstance(native,str) and native.upper().startswith('RINCON_'):
                    physical=('sonos:'+native.upper(),)
                if (getattr(player,'synced_to',None) or getattr(player,'active_group',None)
                        or any(member != native for member in (getattr(player,'group_members',()) or ()))):
                    topology='dynamic'
            else:
                available=False
        # Sonos volume_set addresses self.soco (one physical player), not its
        # coordinator. A grouped native Sonos control can therefore duck its
        # own room without granting grouped playback/queue authority to MA.
        if volume_only and integration=='sonos' and len(physical)==1 and physical[0].startswith('sonos:'):
            topology='single'
        names[item.entity_id]=next((value for value in
            (getattr(item,'name',None),getattr(item,'original_name',None),attrs.get('friendly_name'),item.entity_id)
            if isinstance(value,str) and value.strip() and len(value)<=120),item.entity_id)
        aliases[item.entity_id]=sorted(value for value in (getattr(item,'aliases',set()) or set())
            if isinstance(value,str) and 1<=len(value)<=120)[:19]
        entities.append(Entity(item.entity_id,item.device_id,getattr(item,'area_id',None),integration,disabled,available,
            output_capable=integration=='music_assistant',volume_capable=volume,playing=getattr(state,'state',None)=='playing',
            topology='single' if read_only or (music and integration=='music_assistant') else topology,physical_ids=physical,native_output_capable=native_output))
    wire={'areas':area_names,'devices':[asdict(item) for item in devices],'entities':[asdict(item) for item in entities]}
    revision=hashlib.sha256(json.dumps(wire,sort_keys=True,default=str).encode()).hexdigest()
    snapshot=DiscoverySnapshot(revision,tuple(area_names),devices,tuple(entities))
    # Old explicit TTS pairings are manual choices; no options/database migration
    # overwrites them or default_announcement_speakers (including Kitchen).
    manual={row['satellite_entity_id']:row['tts_media_player'] for row in options.get('satellites',[])
        if row.get('satellite_entity_id') and row.get('tts_media_player')}
    for assistant,player in options.get('assistant_speaker_overrides',{}).items():
        if player=='auto': manual.pop(assistant,None)
        else: manual[assistant]=player
    # Migrate once by room, never retain a per-assistant runtime preference.
    # Conflicting old choices remain explicit until an administrator chooses.
    if 'room_speaker_defaults' in options:
        defaults=dict(options['room_speaker_defaults'])
    else:
        old=discover_pairings(snapshot,manual)
        candidates={}
        for identifier, player in manual.items():
            area=old['assistants'].get(identifier,{}).get('area_id')
            if area: candidates.setdefault(area,set()).add(player)
        defaults={area:next(iter(values)) if len(values)==1 else 'conflict'
                  for area,values in candidates.items()}
    automatic=discover_pairings(snapshot)
    room_manual={identifier:defaults[row['area_id']]
                 for identifier,row in automatic['assistants'].items()
                 if row['area_id'] in defaults and defaults[row['area_id']]!='auto'}
    result=discover_pairings(snapshot,room_manual)
    result['room_speaker_defaults']=defaults
    result['areas']=[{'id':identifier,'name':name} for identifier,name in area_names.items()]
    for identifier,row in result['assistants'].items():
        if defaults.get(row['area_id'])=='conflict':
            row.update(status='room_default_conflict',speaker_id=None,schedule_output=None)
        row['name']=names[identifier]
        row['aliases']=list(dict.fromkeys([*aliases[identifier],area_names.get(row['area_id'],'')]))
        row['aliases']=[value for value in row['aliases'] if value]
        row['speaker_name']=names.get(row['speaker_id'],row['speaker_id'])
        if (not volume_only and not read_only and row['status']=='manual_speaker_unsafe_or_unsupported'
                and result['excluded_speakers'].get(row['speaker_id'])=='group_scope_not_fixed'):
            from .alarm_groups import sonos_endpoint
            if sonos_endpoint(hass,row['speaker_id']) is not None:
                row['schedule_output']={'kind':'ma','entity_id':row['speaker_id'],'device_id':row['device_id']}
                row['alarm_isolation_available']=True
        for choice in row['choices']: choice['name']=names.get(choice['entity_id'],choice['entity_id'])
    result['speaker_names']=names
    result['pairing_revision']=hashlib.sha256(json.dumps(result,sort_keys=True,default=str).encode()).hexdigest()
    return result


def collect_room_volume_pairings(hass, options):
    """Volume-only discovery; never use this view for playback or grouping."""
    return collect_pairings(hass, options, volume_only=True)
