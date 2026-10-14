"""Disabled app with fresh control connections; captures are never retried."""
import asyncio
import json
import os
from pathlib import Path
import aiohttp
from .client import CoreCaptureClient, SUPERVISOR_WEBSOCKET, authenticate, open_core_socket, receive_json
from .protocol import VERSION, ProtocolRejected, WorkerClaims, exact, identifier

REGISTRATION_TIMEOUT = 5.0
FRAME_ACK_TIMEOUT = 5.0
ROTATION_DRAIN_TIMEOUT = 5.0
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 30.0
STABLE_CONNECTION_SECONDS = 30.0
RETRYABLE = frozenset(('connection_timeout','authentication_timeout',
    'backend_registration_timeout','backend_temporarily_unavailable','connection_interrupted'))
RETRYABLE_REGISTRATION = frozenset(('unknown_command','backend_unavailable','previous_connection_busy'))


async def register_worker(control):
    """Accept only an exact connection result, or named availability errors."""
    try:
        async with asyncio.timeout(REGISTRATION_TIMEOUT):
            await control.send_json({'id':2,'type':'household_voice/recording_worker/connect','protocol_version':VERSION})
            registered=await receive_json(control)
            if isinstance(registered,dict) and registered.get('success') is False:
                exact(registered,('id','type','success','error'))
                if type(registered['id']) is not int or registered['id']!=2 or registered['type']!='result':
                    raise ProtocolRejected('backend_registration_rejected')
                error=registered['error']
                if not isinstance(error,dict) or not isinstance(error.get('code'),str):
                    raise ProtocolRejected('backend_registration_rejected')
                if error['code'] in RETRYABLE_REGISTRATION:
                    raise ProtocolRejected('backend_temporarily_unavailable')
                raise ProtocolRejected('backend_registration_rejected')
            exact(registered,('id','type','success','result'))
            if (type(registered['id']) is not int or registered['id']!=2
                or registered['type']!='result' or registered['success'] is not True):
                raise ProtocolRejected('backend_registration_rejected')
            exact(registered['result'],('connection_id',))
            return identifier(registered['result']['connection_id'])
    except TimeoutError:
        raise ProtocolRejected('backend_registration_timeout') from None
    except (TypeError,KeyError,ValueError) as error:
        if isinstance(error,ProtocolRejected) and str(error) in RETRYABLE:
            raise
        raise ProtocolRejected('backend_registration_rejected') from None


def status(code):
    # Closed vocabulary only; never exceptions, identities, tokens or frames.
    print(json.dumps({'status':code}),flush=True)


async def control_cycle(control, connection_id, client):
    """One epoch. Tombstones survive task completion, never connection rotation."""
    claims=WorkerClaims(connection_id)
    next_id=3
    pending={}
    active={}
    wire_drained=set()
    send_lock=asyncio.Lock()

    async def send_frame(frame):
        nonlocal next_id
        if claims.closed:raise ProtocolRejected('connection_closed')
        async with send_lock:
            message_id=next_id;next_id+=1
            future=asyncio.get_running_loop().create_future()
            pending[message_id]=(future,frame['kind'],frame['grant_id'])
            try:
                await control.send_json({'id':message_id,'type':'household_voice/recording_worker/frame','frame':frame})
            except BaseException:
                pending.pop(message_id,None);future.cancel();raise
        try:return await asyncio.wait_for(future,FRAME_ACK_TIMEOUT)
        finally:pending.pop(message_id,None)

    async def capture(grant):
        try:await client.record(grant,send_frame)
        except asyncio.CancelledError:raise
        except Exception:
            # A fresh control can recover availability, never this operation.
            status('capture_failed');await control.close()

    def completed(grant_id,task):
        if active.get(grant_id) is task:active.pop(grant_id,None)
        wire_drained.discard(grant_id)

    try:
        async for incoming in control:
            if incoming.type != aiohttp.WSMsgType.TEXT:break
            try:message=json.loads(incoming.data)
            except (TypeError,ValueError):raise ProtocolRejected('invalid_shape') from None
            if not isinstance(message,dict):raise ProtocolRejected('invalid_shape')
            if message.get('type')=='result':
                message_id=message.get('id')
                if type(message_id) is not int or message_id not in pending:
                    raise ProtocolRejected('unexpected_backend_frame')
                future,kind,grant_id=pending[message_id]
                if future.done():raise ProtocolRejected('duplicate_ack')
                if message.get('success') is True:
                    exact(message,('id','type','success','result'))
                    exact(message['result'],('accepted_sequence',))
                    if kind=='barrier':wire_drained.add(grant_id)
                    future.set_result(message['result'])
                else:
                    future.set_exception(ProtocolRejected('backend_refused'))
                continue
            exact(message,('type','id','event'))
            if message['type']!='event' or type(message['id']) is not int or message['id']!=2:
                raise ProtocolRejected('unexpected_backend_frame')
            event=message['event']
            if not isinstance(event,dict):raise ProtocolRejected('invalid_shape')
            if event.get('type')=='grant':
                exact(event,('type','grant'))
                grant=claims.claim(event['grant'])
                if len(active)>=4:raise ProtocolRejected('worker_busy')
                task=asyncio.create_task(capture(grant))
                active[grant['grant_id']]=task
                task.add_done_callback(lambda done,key=grant['grant_id']:completed(key,done))
            elif event.get('type')=='cancel':
                exact(event,('type','grant_id','connection_id'))
                grant_id=identifier(event['grant_id'])
                if grant_id not in claims.seen or event['connection_id']!=claims.connection_id:
                    raise ProtocolRejected('unknown_cancel')
                if grant_id in active:active[grant_id].cancel()
                # A completed task still has a replay tombstone. No microphone
                # completion claim is sent, including a late backend cancel.
            elif event.get('type')=='unavailable':
                exact(event,('type','connection_id'))
                if event['connection_id']!=claims.connection_id:
                    raise ProtocolRejected('wrong_connection')
                # Backend unload/backup does not prove source completion.
                # Cleanup below cancels capture; only fresh control is retried.
                return 'disconnect'
            elif event.get('type')=='rotate':
                exact(event,('type','connection_id'))
                if event['connection_id']!=claims.connection_id:
                    raise ProtocolRejected('wrong_connection')
                if any(not item[0].done() for item in pending.values()) or set(active)-wire_drained:
                    raise ProtocolRejected('rotate_before_wire_drain')
                # This event, not the wire ACK, asserts backend disposal after
                # consume + durable release. Drain local close before reconnect.
                claims.disconnect()
                async with asyncio.timeout(ROTATION_DRAIN_TIMEOUT):
                    await asyncio.gather(*tuple(active.values()))
                return 'rotate'
            else:raise ProtocolRejected('unknown_backend_event')
        return 'disconnect'
    finally:
        claims.disconnect()
        for future,_,_ in tuple(pending.values()):
            if not future.done():future.cancel()
        tasks=tuple(active.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        pending.clear();active.clear();wire_drained.clear()
        status('disconnected_no_replay')


async def supervise(session,token,*,url=SUPERVISOR_WEBSOCKET):
    """Retry only a fresh control epoch, with bounded exponential backoff."""
    delay=BACKOFF_INITIAL
    while True:
        started=asyncio.get_running_loop().time()
        reason='disconnect'
        try:
            # Context exit closes the old socket BEFORE another registration.
            async with await open_core_socket(session,url=url) as control:
                await authenticate(control,token)
                connection_id=await register_worker(control)
                client=CoreCaptureClient(session,token,url=url)
                status('connected_candidate')
                reason=await control_cycle(control,connection_id,client)
        except ProtocolRejected as error:
            if str(error) not in RETRYABLE:
                status('backend_unavailable');return
        except aiohttp.WSServerHandshakeError as error:
            if error.status in (401,403):
                status('authorization_unavailable');return
        except (aiohttp.ClientError,OSError,TimeoutError):
            pass
        if reason=='rotate':
            status('connection_rotated');delay=BACKOFF_INITIAL
            continue
        if asyncio.get_running_loop().time()-started>=STABLE_CONNECTION_SECONDS:
            delay=BACKOFF_INITIAL
        status('control_reconnecting')
        await asyncio.sleep(delay)
        delay=min(BACKOFF_MAX,delay*2)


async def run():
    options=json.loads(Path('/data/options.json').read_text(encoding='utf-8'))
    if options.get('enabled') is not True:
        status('disabled');return
    token=os.environ.get('SUPERVISOR_TOKEN')
    if not token:
        status('authorization_unavailable');return
    async with aiohttp.ClientSession() as session:
        await supervise(session,token,url=SUPERVISOR_WEBSOCKET)


def main():
    try:asyncio.run(run())
    except KeyboardInterrupt:status('stopped')
    except Exception:status('worker_unavailable')

if __name__=='__main__':main()
