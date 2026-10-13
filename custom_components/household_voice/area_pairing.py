"""Pure HA-area pairing recommendations; never mutates options or operates audio.

The collector supplies fresh immutable registry/provider observations. Physical
identities and member areas must come from authoritative provider data, never
friendly-name matching. Empty identities remain independent candidates.
Recompute on registry, area, device, availability and group-topology changes;
executors must revalidate snapshot revision before acting. Manual IDs stay exact.
"""
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class Device:
    id: str
    area_id: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class Entity:
    id: str
    device_id: str | None = None
    area_id: str | None = None
    integration: str = ''
    disabled: bool = False
    available: bool = True
    output_capable: bool = False
    volume_capable: bool = False
    playing: bool = False
    internal_assistant_player: bool = False
    topology: str = 'single'  # single, stereo, dynamic, unknown
    physical_ids: tuple[str, ...] = ()
    member_areas: tuple[tuple[str, str | None], ...] = ()
    native_output_capable: bool = False


@dataclass(frozen=True)
class DiscoverySnapshot:
    revision: str
    areas: tuple[str, ...]
    devices: tuple[Device, ...]
    entities: tuple[Entity, ...]

    def __post_init__(self):
        if (not isinstance(self.revision,str) or not self.revision
                or len(set(self.areas))!=len(self.areas)
                or len({d.id for d in self.devices})!=len(self.devices)
                or len({e.id for e in self.entities})!=len(self.entities)):
            raise ValueError('Snapshot identities and revision must be unique')
        for entity in self.entities:
            if (entity.topology not in {'single','stereo','dynamic','unknown'}
                    or len(set(entity.physical_ids))!=len(entity.physical_ids)
                    or len(dict(entity.member_areas))!=len(entity.member_areas)):
                raise ValueError('Invalid physical topology')


def _area(entity, devices, areas):
    # An explicit but deleted entity-area override must not fall back silently.
    value=entity.area_id if entity.area_id is not None else getattr(devices.get(entity.device_id),'area_id',None)
    return value if value in areas else None


def _disabled(entity,devices):
    return entity.disabled or getattr(devices.get(entity.device_id),'disabled',False)


def _safe_player(entity,area,devices,assistant_devices):
    if _disabled(entity,devices): return 'disabled'
    if entity.internal_assistant_player or entity.device_id in assistant_devices: return 'assistant_internal_player'
    if area is None: return 'area_unknown'
    if entity.topology in {'dynamic','unknown'}: return 'group_scope_not_fixed'
    members=dict(entity.member_areas)
    if entity.topology=='stereo':
        if (len(entity.physical_ids)<2 or set(members)!=set(entity.physical_ids)
                or any(value!=area for value in members.values())):
            return 'stereo_scope_unverified_or_cross_area'
    elif len(entity.physical_ids)>1:
        return 'single_endpoint_has_multiple_members'
    if any(value!=area for value in members.values()): return 'cross_area_members'
    return None


def _canonical(entities):
    """Collapse only proven same-endpoint aliases; keep unresolved alternatives."""
    groups={}
    for entity in entities:
        key=('physical',*sorted(entity.physical_ids)) if entity.physical_ids else ('entity',entity.id)
        groups.setdefault(key,[]).append(entity)
    chosen=[]
    for group in groups.values():
        ma=[entity for entity in group if entity.integration=='music_assistant']
        chosen.extend(ma if ma else group)
    # A verified stereo endpoint subsumes its separate member representations.
    stereo=[set(entity.physical_ids) for entity in chosen if entity.topology=='stereo']
    return tuple(sorted((entity for entity in chosen if not entity.physical_ids
        or not any(set(entity.physical_ids)<members for members in stereo)),key=lambda entity:entity.id))


def discover_pairings(snapshot: DiscoverySnapshot, manual_overrides=None):
    """Return assistant-ID keyed decisions and exact room competing-player IDs.

    Overrides map assist_satellite entity IDs to exact speaker entity IDs; None
    means explicitly unpaired. A stale override remains reported and is never
    silently replaced. Temporary unavailability never reduces candidate count.
    Cross-area manual choices are retained and explicitly marked as such.
    """
    manual=dict(manual_overrides or {})
    devices={device.id:device for device in snapshot.devices}
    entities={entity.id:entity for entity in snapshot.entities}
    assistants=[entity for entity in snapshot.entities if entity.id.startswith('assist_satellite.')]
    assistant_devices={entity.device_id for entity in assistants if entity.device_id}
    areas={entity.id:_area(entity,devices,snapshot.areas) for entity in snapshot.entities}
    physical_areas={}
    for entity in snapshot.entities:
        if entity.id.startswith('media_player.') and not _disabled(entity,devices):
            for identity in entity.physical_ids:
                physical_areas.setdefault(identity,set()).add(areas[entity.id])
    conflicts={identity for identity,values in physical_areas.items() if len(values)>1}
    safe=[]; excluded={}
    for entity in snapshot.entities:
        if not entity.id.startswith('media_player.'): continue
        reason=_safe_player(entity,areas[entity.id],devices,assistant_devices)
        if conflicts.intersection(entity.physical_ids): reason='conflicting_physical_member_areas'
        if reason: excluded[entity.id]=reason
        else: safe.append(entity)
    output=[entity for entity in safe if entity.output_capable]
    result={}
    for assistant in sorted(assistants,key=lambda entity:entity.id):
        area=areas[assistant.id]
        choices=_canonical([entity for entity in output if areas[entity.id]==area]) if area else ()
        choice_rows=[{'entity_id':entity.id,'available':entity.available,'integration':entity.integration} for entity in choices]
        selected=None; status='choice_required' if len(choices)>1 else 'no_suitable_speaker'
        provenance='area'
        if assistant.id in manual:
            selected=manual[assistant.id]; provenance='manual'
            if selected is None: status='explicitly_unpaired'
            elif selected not in entities: status='manual_speaker_missing'
            elif selected in excluded or not entities[selected].id.startswith('media_player.') or not entities[selected].output_capable:
                status='manual_speaker_unsafe_or_unsupported'
            else: status='ready' if entities[selected].available else 'speaker_unavailable'
        elif len(choices)==1:
            selected=choices[0].id
            status='ready' if choices[0].available else 'speaker_unavailable'
        if assistant.device_id not in devices: status='assistant_device_unknown'
        elif _disabled(assistant,devices): status='assistant_disabled'
        elif not assistant.available: status='assistant_unavailable'
        elif not area: status='assistant_area_unknown'
        chosen=entities.get(selected)
        occupied=set(chosen.physical_ids) if chosen else set()
        competing=_canonical([entity for entity in safe if areas[entity.id]==area and entity.available
            and entity.playing and entity.volume_capable and entity.id!=selected
            and not (occupied and occupied.intersection(entity.physical_ids))])
        duplicate_controls={entity.id for entity in competing if entity.physical_ids and
            sum(set(other.physical_ids)==set(entity.physical_ids) for other in competing)>1}
        duck_blocked={entity.id:excluded.get(entity.id,'volume_control_unavailable')
            for entity in snapshot.entities if entity.id.startswith('media_player.')
            and areas[entity.id]==area and entity.available and entity.playing and entity.id!=selected
            and not _disabled(entity,devices) and entity.device_id not in assistant_devices
            and not entity.internal_assistant_player
            and not (occupied and occupied.intersection(entity.physical_ids))
            and (entity.id in excluded or not entity.volume_capable)}
        duck_blocked.update({identifier:'ambiguous_duplicate_controls' for identifier in duplicate_controls})
        schedule_output=None
        if status=='ready':
            schedule_output={'kind':'ma','entity_id':selected,'device_id':assistant.device_id}
        elif (selected is None and status in {'explicitly_unpaired','choice_required','no_suitable_speaker'}
                and assistant.native_output_capable):
            schedule_output={'kind':'native','entity_id':assistant.id,'device_id':assistant.device_id}
        result[assistant.id]={'schedule_output':schedule_output,'assistant_id':assistant.id,'device_id':assistant.device_id,'area_id':area,
            'status':status,'speaker_id':selected,'selection':provenance,'choices':choice_rows,
            'manual_cross_area':bool(provenance=='manual' and chosen and areas.get(selected)!=area),
            'duck_speakers':[entity.id for entity in competing if entity.id not in duplicate_controls] if status=='ready' else [],
            'duck_blocked_speakers':duck_blocked,
            'snapshot_revision':snapshot.revision}
    outputs={}
    for endpoint in output:
        area=areas[endpoint.id]; occupied=set(endpoint.physical_ids)
        aliases=[entity.id for entity in snapshot.entities if entity.id==endpoint.id or
            (occupied and occupied.intersection(entity.physical_ids))]
        competitors=_canonical([entity for entity in safe if areas[entity.id]==area and entity.available
            and entity.playing and entity.volume_capable and entity.id not in aliases])
        # Multiple controls with the same proven identity are not independent.
        competitors=[entity for entity in competitors if not entity.physical_ids or
            sum(set(other.physical_ids)==set(entity.physical_ids) for other in competitors)==1]
        outputs[endpoint.id]={'area_id':area,'available':endpoint.available,
            'duck_media_players':[entity.id for entity in competitors], 'physical_aliases':sorted(aliases)}
    for assistant_id,binding in result.items():
        if (binding.get('schedule_output') or {}).get('kind')=='native':
            area=binding['area_id']
            competing=_canonical([entity for entity in safe if areas[entity.id]==area and entity.available and entity.playing and entity.volume_capable])
            competing=[entity for entity in competing if not entity.physical_ids or sum(set(other.physical_ids)==set(entity.physical_ids) for other in competing)==1]
            aliases=[assistant_id,*[entity.id for entity in snapshot.entities if entity.device_id==binding['device_id'] and entity.id.startswith('media_player.')]]
            outputs[assistant_id]={'area_id':area,'available':True,'duck_media_players':[entity.id for entity in competing],'physical_aliases':sorted(aliases)}
    response={'snapshot_revision':snapshot.revision,'assistants':result,'outputs':outputs,'excluded_speakers':excluded,
        'recovery_media_players':sorted(entity.id for entity in safe if entity.volume_capable),
        'orphan_manual_overrides':{key:value for key,value in manual.items() if key not in result}}
    # Manual changes can alter a route without a registry event. Revalidate both.
    response['pairing_revision']=hashlib.sha256(json.dumps(response,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return response


def resolve_new_alarm(pairings, listening_assistant, *, target_assistant=None):
    """New alarms only: choose an assistant and its output; global control unchanged."""
    assistants=pairings['assistants']
    if listening_assistant not in assistants:
        return {'status':'clarify','reason':'unknown_listening_assistant'}
    if assistants[listening_assistant]['status'] in {'assistant_disabled','assistant_unavailable','assistant_device_unknown'}:
        return {'status':'clarify','reason':'listening_assistant_unavailable'}
    target=listening_assistant if target_assistant is None else target_assistant
    if target not in assistants:
        return {'status':'clarify','reason':'unknown_target_assistant'}
    binding=assistants[target]
    if not binding.get('schedule_output'):
        return {'status':'clarify','reason':binding['status'],'assistant_id':target,'choices':binding['choices']}
    return {'status':'ready','assistant_id':target,'source_assistant_id':listening_assistant,
        'speaker_id':binding['speaker_id'],'schedule_output':binding['schedule_output'],'area_id':binding['area_id'],
        'snapshot_revision':pairings['snapshot_revision'],'pairing_revision':pairings['pairing_revision'],
        'requires_revalidation':True}
