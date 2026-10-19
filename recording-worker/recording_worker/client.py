"""Outbound Core audio transport. No services, playback, files or transcripts."""
import asyncio
import base64
import binascii
import aiohttp
from .protocol import CORE_TIMEOUT, MAX_FRAME_BYTES, ProtocolRejected, exact, validate_grant

SUPERVISOR_WEBSOCKET = 'ws://supervisor/core/websocket'
HANDSHAKE_TIMEOUT = 5.0
WEBSOCKET_CLOSE_TIMEOUT = 2.0


async def open_core_socket(session, *, url=SUPERVISOR_WEBSOCKET):
    """Bound one DNS/TCP/HTTP attempt; capture callers never retry it."""
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            return await session.ws_connect(url,max_msg_size=MAX_FRAME_BYTES,heartbeat=15,
                timeout=aiohttp.ClientWSTimeout(ws_close=WEBSOCKET_CLOSE_TIMEOUT))
    except TimeoutError:
        raise ProtocolRejected('connection_timeout') from None


async def receive_json(ws):
    """Classify transport closure without logging any received frame."""
    try:return await ws.receive_json()
    except TypeError:
        if getattr(ws,'closed',False):
            raise ProtocolRejected('connection_interrupted') from None
        raise ProtocolRejected('invalid_shape') from None
    except (ValueError,KeyError):
        raise ProtocolRejected('invalid_shape') from None


async def authenticate(ws, token):
    """One deadline covers the complete challenge, auth and admin check."""
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            challenge=await receive_json(ws)
            if not isinstance(challenge,dict) or challenge.get('type')!='auth_required':
                raise ProtocolRejected('authentication_failed')
            await ws.send_json({'type':'auth','access_token':token})
            authenticated=await receive_json(ws)
            if not isinstance(authenticated,dict) or authenticated.get('type')!='auth_ok':
                raise ProtocolRejected('authentication_failed')
            await ws.send_json({'id':1,'type':'auth/current_user'})
            reply=await receive_json(ws)
            if (not isinstance(reply,dict) or type(reply.get('id')) is not int or reply.get('id')!=1
                or reply.get('type')!='result' or reply.get('success') is not True
                or not isinstance(reply.get('result'),dict) or reply['result'].get('is_admin') is not True):
                raise ProtocolRejected('administrator_required')
    except TimeoutError:
        raise ProtocolRejected('authentication_timeout') from None


class CoreCaptureClient:
    def __init__(self, session, token, *, url=SUPERVISOR_WEBSOCKET):
        self.session,self._token,self.url=session,token,url

    async def record(self, grant, send_frame):
        """Every frame is ACKed before reading/forwarding another; no retries.

        send_frame must return accepted_sequence from the same authenticated
        worker connection. Barrier ACK proves wire drain, not HA event-bus drain.
        """
        if not isinstance(grant,dict):raise ProtocolRejected('invalid_shape')
        grant=validate_grant(grant,grant.get('connection_id'))
        sequence=0
        async def emit(kind,payload):
            nonlocal sequence
            sequence+=1
            frame={'grant_id':grant['grant_id'],'connection_id':grant['connection_id'],
                   'seq':sequence,'kind':kind,'payload':payload}
            result=await send_frame(frame)
            if not isinstance(result,dict) or result.get('accepted_sequence') != sequence or type(result.get('accepted_sequence')) is not int:
                raise ProtocolRejected('frame_ack_failed')
        total=chunks=0
        async with asyncio.timeout(grant['valid_for_seconds']):
            # Closing this socket removes the Core capture subscription; it does
            # NOT cancel ask_question or prove the microphone stopped.
            async with await open_core_socket(self.session,url=self.url) as ws:
                await authenticate(ws,self._token)
                await ws.send_json({'id':2,'type':'assist_pipeline/device/capture',
                    'device_id':grant['source_device_id'],'timeout':CORE_TIMEOUT})
                reply=await ws.receive_json()
                if reply.get('id') != 2 or reply.get('type') != 'result' or reply.get('success') is not True:
                    raise ProtocolRejected('capture_refused')
                await emit('ack',{'core_timeout_seconds':CORE_TIMEOUT})
                while True:
                    message=await ws.receive_json()
                    if message.get('id') != 2 or message.get('type') != 'event':
                        raise ProtocolRejected('unexpected_core_frame')
                    event=message.get('event')
                    if not isinstance(event,dict):raise ProtocolRejected('invalid_core_frame')
                    if event.get('type')=='end':
                        exact(event,('type','overflow'))
                        if type(event['overflow']) is not bool or event['overflow'] or not total:
                            raise ProtocolRejected('capture_incomplete')
                        await emit('end',{'overflow':False})
                        end_sequence=sequence
                        await emit('barrier',{'end_sequence':end_sequence})
                        return {'capture_complete':True,'wire_drained':True,'microphone_stop_command_sent':False}
                    exact(event,('type','audio','rate','width','channels'))
                    if event['type'] != 'audio' or any(type(event[k]) is not int for k in ('rate','width','channels')) or (event['rate'],event['width'],event['channels']) != (16000,2,1):
                        raise ProtocolRejected('invalid_audio_format')
                    encoded=event['audio']
                    if not isinstance(encoded,str) or len(encoded)>4*((grant['max_chunk_bytes']+2)//3):
                        raise ProtocolRejected('audio_limit')
                    try:pcm=base64.b64decode(encoded,validate=True)
                    except (binascii.Error,ValueError):raise ProtocolRejected('invalid_audio_encoding') from None
                    total+=len(pcm);chunks+=1
                    if not pcm or len(pcm)%2 or len(pcm)>grant['max_chunk_bytes'] or total>grant['max_pcm_bytes'] or chunks>grant['max_chunks']:
                        raise ProtocolRejected('audio_limit')
                    del pcm
                    await emit('audio',{key:event[key] for key in ('audio','rate','width','channels')})
