"""Public HA API adapter draft; intentionally never auto-registered."""
import asyncio
import secrets
import voluptuous as vol
from homeassistant.core import Context, callback
from homeassistant.components import websocket_api
from homeassistant.components.assist_pipeline.const import EVENT_RECORDING
from .recording_protocol import VERSION


async def event_fence(hass):
    """Drain prior public synchronous event callbacks, not unrelated HA tasks.

    Supported Core dispatch runs @callback listeners synchronously and flushes
    nested events before returning. Resolve via a unique marker then consume in
    caller's resumed task; any intervening event still advances/rejects validator.
    """
    future = hass.loop.create_future()
    event_type = 'household_voice_recording_fence_' + secrets.token_hex(16)
    @callback
    def seen(_event):
        if not future.done(): future.set_result(None)
    remove = hass.bus.async_listen(event_type, seen)
    try:
        hass.bus.async_fire(event_type)
        await future
    finally: remove()


def attach(hass, backend, loaded_entries):
    """Caller supplies the sole global backend and trusted registry validation."""
    @callback
    def state(event):
        entity = event.data.get('entity_id')
        new = event.data.get('new_state')
        for source, op in tuple(backend.operations.items()):
            if op.session.satellite_entity_id == entity:
                if new is None: backend.source_changed(source)
                else: backend.state(source, entity, new.state, event.context.id)
    @callback
    def recording(event): backend.recording_started(event.data.get('device_id'))
    @callback
    def registry(event):
        # Conservative: any relevant registry change invalidates pending captures.
        for source in tuple(backend.operations): backend.source_changed(source)
    removers = [hass.bus.async_listen('state_changed', state),
                hass.bus.async_listen(EVENT_RECORDING, recording),
                hass.bus.async_listen('entity_registry_updated', registry),
                hass.bus.async_listen('device_registry_updated', registry)]

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required('type'):'household_voice/recording_worker/connect',vol.Required('protocol_version'):VERSION})
    @callback
    def connect(hass, connection, message):
        try:
            result = backend.register(connection,
                lambda event: connection.send_event(message['id'], event),
                admin=connection.user.is_admin, loaded_entries=loaded_entries())
        except ValueError as error:
            code=str(error) if str(error) in ('backend_unavailable','previous_connection_busy') else 'registration_rejected'
            connection.send_error(message['id'],code,'Recording worker unavailable')
            return
        connection.subscriptions[message['id']] = lambda: backend.disconnect(connection)
        connection.send_result(message['id'],result)

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required('type'):'household_voice/recording_worker/frame',vol.Required('frame'):dict})
    @callback
    def frame(hass, connection, message):
        try: result = backend.frame(connection, message['frame'])
        except ValueError:
            connection.send_error(message['id'],'frame_rejected','Recording frame rejected')
            return
        connection.send_result(message['id'],result)
    websocket_api.async_register_command(hass,connect)
    websocket_api.async_register_command(hass,frame)
    return removers


async def native_question(hass, entity, context):
    return await hass.services.async_call('assist_satellite','ask_question',
        {'entity_id':entity,'question':"What's the announcement?",'answers':[],
         'preannounce':False},blocking=True,return_response=True,context=context)
