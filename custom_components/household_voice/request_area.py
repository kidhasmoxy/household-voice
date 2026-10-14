"""Trusted request location shared by capability tools; no utterance parsing."""


class RequestAreaError(ValueError):
    """The requesting device cannot be bound to one current area."""


def resolve_source_area(hass, options, context):
    """Prefer HA's effective device area; keep old room IDs only as a fallback.

    A deleted explicit area or an ambiguous old mapping never becomes a guessed
    room. No model-provided source or scan of processing satellites is involved.
    """
    from homeassistant.helpers import area_registry as ar, device_registry as dr

    device_id = getattr(context, 'device_id', None)
    if (not isinstance(device_id, str) or not device_id
            or getattr(context, 'context', None) is None):
        raise RequestAreaError('trusted_source_room_required')
    device = dr.async_get(hass).async_get(device_id)
    if device is None or getattr(device, 'disabled_by', None) is not None:
        raise RequestAreaError('trusted_source_room_required')
    effective = getattr(dr, 'async_get_effective_area_id', None)
    area_id = effective(hass, device) if effective else getattr(device, 'area_id', None)
    if area_id:
        area = ar.async_get(hass).async_get_area(area_id)
        if area is None:
            raise RequestAreaError('source_area_missing')
        return {'device_id':device_id, 'area_id':area.id,
                'area_name':area.name, 'legacy':False}
    if getattr(device, 'area_id', None):
        raise RequestAreaError('source_area_missing')
    rows = options.get('satellites', [])
    if not isinstance(rows, list):
        raise RequestAreaError('trusted_source_room_required')
    matches = {row.get('room_id') for row in rows if isinstance(row, dict)
               and row.get('satellite_device_id') == device_id}
    if len(matches) != 1:
        raise RequestAreaError('trusted_source_room_required')
    room = next(iter(matches))
    if not isinstance(room, str) or not 1 <= len(room) <= 64 or any(ord(c) < 32 for c in room):
        raise RequestAreaError('trusted_source_room_required')
    return {'device_id':device_id, 'area_id':room, 'area_name':room, 'legacy':True}
