"""Generate capture authorization from trusted registry records, never STT names."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class NativeSource:
    device_id: str
    entity_id: str
    area_id: str
    profile_id: str


def sources_from_registry(hass):
    """Public HA registry/entity APIs; shared pipelines are deliberately allowed."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from homeassistant.components.assist_satellite.const import DATA_COMPONENT, AssistSatelliteEntityFeature
    component = hass.data.get(DATA_COMPONENT)
    if component is None: return {}
    result = {}; ambiguous = set()
    for entity in component.entities:
        registered = er.async_get(hass).async_get(entity.entity_id)
        device = dr.async_get(hass).async_get(registered.device_id) if registered and registered.device_id else None
        if (registered is None or registered.disabled_by is not None or device is None
                or device.disabled_by is not None or not entity.available
                or not entity.supported_features & AssistSatelliteEntityFeature.START_CONVERSATION):
            continue
        owner=hass.config_entries.async_get_entry(registered.config_entry_id)
        if (owner is None or owner.disabled_by is not None
                or getattr(owner.state,'value',owner.state)!='loaded'
                or getattr(device,'config_entry_id',None)!=owner.entry_id):continue
        area = registered.area_id or dr.async_get_effective_area_id(hass,device)
        from homeassistant.helpers import area_registry as ar
        if not isinstance(area,str) or not area or ar.async_get(hass).async_get_area(area) is None: continue
        source = native_source(device.id, entity.entity_id, area)
        if device.id in result: ambiguous.add(device.id)
        result[device.id] = source
    return {key:row for key,row in result.items() if key not in ambiguous}


def native_source(device, entity, area):
    for value in (device,entity,area):
        if not isinstance(value,str) or not 0<len(value)<=128: raise ValueError('invalid_source')
    if not entity.startswith('assist_satellite.'): raise ValueError('invalid_source')
    profile = 'native_recording_' + hashlib.sha256(device.encode()).hexdigest()[:32]
    return NativeSource(device,entity,area,profile)


def capture_configuration(base, sources):
    """Input is freshly generated existing runtime_config.configuration output.

    Preserve room/control/alarm fields and old explicit STT bindings. The reserved
    native namespace is generated anew, not read from user-supplied model fields.
    """
    desired = deepcopy(base)
    rooms = desired['rooms']
    seen = set()
    for room in rooms.values():
        if any(key.startswith('native_recording_') for key in room.get('capture_profiles',{})):
            raise ValueError('reserved_profile_namespace')
    for device, source in sources.items():
        if (not isinstance(source,NativeSource) or device != source.device_id or device in seen
                or source != native_source(source.device_id,source.entity_id,source.area_id)):
            raise ValueError('invalid_source_binding')
        seen.add(device)
        assigned = [key for key,row in rooms.items() if device in row.get('satellites',[])]
        if assigned != [source.area_id]: raise ValueError('source_room_conflict')
        rooms[source.area_id].setdefault('capture_profiles',{})[source.profile_id] = device
    return desired


async def configure_capture_bindings(client, base, sources):
    desired = capture_configuration(base,sources)
    current = await client.runtime_config()
    if any(current.get(key)!=value for key,value in desired.items()):
        await client.configure_runtime(desired,current['revision'])
    return dict(sources)


def configuration_with_native_sources(hass,options,discovered):
    """One shared builder used by setup and every subsequent config refresh."""
    from .runtime_config import configuration
    sources=sources_from_registry(hass)
    clean=dict(options)
    # Keep legacy persisted options, but their explicit STT authorization is not
    # needed in native mode. Current trusted device areas own these source routes.
    clean['stt_capture_profiles']=[]
    clean['satellites']=[row for row in options.get('satellites',[])
                         if row.get('satellite_device_id') not in sources]
    desired=configuration(clean,discovered)
    for source in sources.values():
        for room in desired['rooms'].values():
            room['satellites']=[item for item in room['satellites'] if item!=source.device_id]
        room=desired['rooms'].setdefault(source.area_id,{'name':source.area_id,'satellites':[],'capture_profiles':{}})
        room['satellites'].append(source.device_id)
    return capture_configuration(desired,sources)
