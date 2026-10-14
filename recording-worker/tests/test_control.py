"""Real loopback control reconnection and one-use PCM transport acceptance."""
import asyncio
from contextlib import redirect_stdout
import io
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from recording_worker import __main__ as entry
from recording_worker.protocol import GrantBook, WorkerClaims, VERSION
from test_worker import make_session, listening, AUDIO, PCM


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.complete=asyncio.Event();self.capture_closed=asyncio.Event()
        self.target=1;self.count=0;self.epochs=[];self.claims=[];self.captures=[]
        self.records=[];self.sockets=[];self.active_control=None
        self.mode='normal';self.reject=[];self.admin=True;self.auth=True
        self.commands=[];self.connections=0;self.old_grant=None
        self.current={};self.task=None;self.output=io.StringIO()
        self.handshakes=0;self.close_phase=None;self.http_status=None
        owner=self
        class ObservedClaims(WorkerClaims):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);owner.claims.append(self)
        self.claim_patch=patch.object(entry,'WorkerClaims',ObservedClaims);self.claim_patch.start()

        async def endpoint(request):
            self.handshakes+=1
            if self.http_status is not None:return web.Response(status=self.http_status)
            ws=web.WebSocketResponse();await ws.prepare(request);self.sockets.append(ws)
            if self.handshakes==1 and self.close_phase=='challenge':await ws.close();return ws
            await ws.send_json({'type':'auth_required'})
            self.assertEqual(await ws.receive_json(),{'type':'auth','access_token':'synthetic-token'})
            if self.handshakes==1 and self.close_phase=='auth':await ws.close();return ws
            if not self.auth:
                await ws.send_json({'type':'auth_invalid','message':'private sentinel'});return ws
            await ws.send_json({'type':'auth_ok'})
            self.assertEqual((await ws.receive_json())['type'],'auth/current_user')
            if self.handshakes==1 and self.close_phase=='admin':await ws.close();return ws
            await ws.send_json({'id':1,'type':'result','success':True,'result':{'is_admin':self.admin}})
            if not self.admin:return ws
            incoming=await ws.receive()
            if incoming.type!=web.WSMsgType.TEXT:return ws
            command=incoming.json();self.commands.append(command['type'])
            if self.handshakes==1 and self.close_phase=='registration':await ws.close();return ws
            if command['type']=='assist_pipeline/device/capture':
                source=command['device_id'];self.captures.append(source)
                session=self.current[source];session.recording_started(source_device_id=source)
                await ws.send_json({'id':2,'type':'result','success':True,'result':None})
                await ws.send_json({'id':2,'type':'event','event':{'type':'audio',**AUDIO}})
                await ws.send_json({'id':2,'type':'event','event':{'type':'end','overflow':False}})
                async for _ in ws:pass
                self.capture_closed.set();return ws
            self.assertEqual(command['type'],'household_voice/recording_worker/connect')
            self.assertEqual(command['protocol_version'],VERSION)
            self.connections+=1
            if self.reject:
                code=self.reject.pop(0)
                await ws.send_json({'id':2,'type':'result','success':False,'error':{'code':code,'message':'private sentinel'}})
                return ws
            if self.active_control is not None:
                await ws.send_json({'id':2,'type':'result','success':False,'error':{'code':'previous_connection_busy'}})
                return ws
            self.active_control=ws
            book=GrantBook(connection_id=f'epoch-{len(self.epochs)}');self.epochs.append(book)
            await ws.send_json({'id':2,'type':'result','success':True,'result':{'connection_id':book.connection_id}})

            async def grant():
                source=f'source-{self.count}'
                session=make_session(op=f'op-{self.count}',device=source)
                self.current[source]=session
                issued=book.issue(session)
                await ws.send_json({'id':2,'type':'event','event':{'type':'grant','grant':issued}})
                return issued
            try:
                if self.mode=='replay_new_epoch' and self.old_grant is not None:
                    await ws.send_json({'id':2,'type':'event','event':{'type':'grant','grant':self.old_grant}})
                else:
                    issued=await grant()
                    if self.mode=='premature_rotate':
                        await ws.send_json({'id':2,'type':'event','event':{'type':'rotate','connection_id':book.connection_id}})
                async for message in ws:
                    command=message.json();self.commands.append(command['type']);frame=command['frame']
                    record=book.records[frame['grant_id']];session=record.validator
                    result=book.receive(frame,connection_id=book.connection_id)
                    if frame['kind']=='ack':listening(session)
                    if frame['kind']=='end':session.question_result(context_id='owned',sentence='Original transcript')
                    if self.mode in ('disconnect_audio','replay_new_epoch','unavailable') and len(self.epochs)==1 and frame['kind']=='audio':
                        self.old_grant=record.grant;self.count+=1
                        if self.mode=='unavailable':
                            await ws.send_json({'id':2,'type':'event','event':{'type':'unavailable','connection_id':book.connection_id}})
                        else:await ws.close()
                        break
                    await ws.send_json({'id':command['id'],'type':'result','success':True,'result':result})
                    if self.mode=='cancel' and frame['kind']=='ack':
                        await ws.send_json({'id':2,'type':'event','event':{'type':'cancel',
                            'connection_id':book.connection_id,'grant_id':frame['grant_id']}})
                        self.complete.set();continue
                    if frame['kind']!='barrier':continue
                    answer=book.consume(frame['grant_id'],expected_revision=session.ready_revision())
                    self.records.append(answer);self.count+=1
                    if self.mode=='replay_completed':
                        await asyncio.sleep(0.01)
                        await ws.send_json({'id':2,'type':'event','event':{'type':'grant','grant':record.grant}})
                    elif self.count>=self.target:self.complete.set()
                    elif len(book.records)>=120:
                        await ws.send_json({'id':2,'type':'event','event':{'type':'rotate','connection_id':book.connection_id}})
                        book.disconnect();self.active_control=None
                    else:await grant()
            finally:
                book.disconnect()
                if self.active_control is ws:self.active_control=None
            return ws

        app=web.Application();app.router.add_get('/core',endpoint)
        self.http=TestClient(TestServer(app));await self.http.start_server()
        self.url=str(self.http.make_url('/core')).replace('http:','ws:')

    async def asyncTearDown(self):
        if self.task is not None and not self.task.done():self.task.cancel()
        if self.task is not None:await asyncio.gather(self.task,return_exceptions=True)
        for ws in self.sockets:await ws.close()
        await self.http.close();self.claim_patch.stop()

    async def start(self):
        async def worker():
            with patch.object(entry,'BACKOFF_INITIAL',0.01),patch.object(entry,'BACKOFF_MAX',0.02),redirect_stdout(self.output):
                await entry.supervise(self.http.session,'synthetic-token',url=self.url)
        self.task=asyncio.create_task(worker())

    async def completed(self):
        await self.start();await asyncio.wait_for(self.complete.wait(),20)
        # Let task-completion callbacks run before inspecting retained state.
        await asyncio.sleep(0.02)

    async def test_140_sessions_rotate_fresh_claims_and_bounded_memory(self):
        self.target=140;await self.completed()
        self.assertEqual([len(book.records) for book in self.epochs],[120,20])
        self.assertEqual([len(claims.seen) for claims in self.claims],[120,20])
        self.assertEqual(len(self.captures),140);self.assertEqual(len(set(self.captures)),140)
        self.assertEqual(len(self.records),140)
        self.assertTrue(all(record.pcm==PCM and record.transcript=='Original transcript' for record in self.records))
        async with asyncio.timeout(3):
            while True:
                captures=[task for task in asyncio.all_tasks() if task.get_coro().__qualname__.endswith('control_cycle.<locals>.capture')]
                if not captures:break
                await asyncio.sleep(0.01)
        self.assertEqual(captures,[])
        self.assertTrue(self.claims[0].closed);self.assertFalse(self.claims[1].closed)

    async def test_transport_loss_discards_partial_pcm_never_replays_capture(self):
        self.mode='disconnect_audio';self.target=2;await self.completed()
        self.assertEqual(self.connections,2);self.assertEqual(self.captures,['source-0','source-1'])
        abandoned=next(iter(self.epochs[0].records.values())).validator
        self.assertEqual(abandoned.buffered_bytes,0);self.assertFalse(abandoned.source_release_proven)
        self.assertEqual(len(self.records),1)

    async def test_backend_unload_event_discards_capture_then_reconnects(self):
        self.mode='unavailable';self.target=2;await self.completed()
        self.assertEqual(self.connections,2);self.assertEqual(self.captures,['source-0','source-1'])
        abandoned=next(iter(self.epochs[0].records.values())).validator
        self.assertEqual(abandoned.buffered_bytes,0);self.assertFalse(abandoned.source_release_proven)
        self.assertEqual(len(self.records),1)

    async def test_old_grant_cannot_replay_on_new_control(self):
        self.mode='replay_new_epoch';await self.start();await asyncio.wait_for(self.task,5)
        self.assertEqual(self.connections,2);self.assertEqual(self.captures,['source-0'])
        self.assertEqual(self.records,[]);self.assertTrue(all(c.closed for c in self.claims))

    async def test_completed_task_is_pruned_but_tombstone_rejects_replay(self):
        self.mode='replay_completed';await self.start();await asyncio.wait_for(self.task,5)
        self.assertEqual(self.captures,['source-0']);self.assertEqual(len(self.records),1)
        self.assertEqual(len(self.claims[0].seen),1);self.assertTrue(self.claims[0].closed)

    async def test_core_reload_and_old_connection_busy_are_control_only_retries(self):
        self.reject=['unknown_command','backend_unavailable','previous_connection_busy'];await self.completed()
        self.assertEqual(self.connections,4);self.assertEqual(self.captures,['source-0'])
        self.assertEqual(len(self.claims),1)

    async def test_invalid_registration_stops_without_retry_or_capture(self):
        self.reject=['registration_rejected'];await self.start();await asyncio.wait_for(self.task,5)
        self.assertEqual(self.connections,1);self.assertEqual(self.captures,[])
        self.assertNotIn('private sentinel',self.output.getvalue())

    async def test_invalid_authentication_stops_without_registration(self):
        self.auth=False;await self.start();await asyncio.wait_for(self.task,5)
        self.assertEqual(self.commands,[]);self.assertEqual(self.captures,[])

    async def test_non_admin_stops_without_registration(self):
        self.admin=False;await self.start();await asyncio.wait_for(self.task,5)
        self.assertEqual(self.commands,[]);self.assertEqual(self.captures,[])

    async def test_proxy_http_unauthorized_stops_without_retry(self):
        self.http_status=401;await self.start();await asyncio.wait_for(self.task,5)
        self.assertEqual(self.handshakes,1);self.assertEqual(self.captures,[])

    async def handshake_disconnect(self,phase):
        self.close_phase=phase;await self.completed()
        self.assertEqual(self.captures,['source-0']);self.assertEqual(len(self.claims),1)

    async def test_challenge_socket_loss_retries_only_control(self):
        await self.handshake_disconnect('challenge')

    async def test_auth_socket_loss_retries_only_control(self):
        await self.handshake_disconnect('auth')

    async def test_admin_socket_loss_retries_only_control(self):
        await self.handshake_disconnect('admin')

    async def test_registration_socket_loss_retries_only_control(self):
        await self.handshake_disconnect('registration')

    async def test_premature_rotation_never_replays_or_reconnects(self):
        self.mode='premature_rotate';await self.start();await asyncio.wait_for(self.task,5)
        self.assertEqual(self.connections,1);self.assertEqual(self.records,[])
        self.assertLessEqual(len(self.captures),1)

    async def test_backend_cancel_closes_capture_without_success_or_retry(self):
        self.mode='cancel';await self.completed()
        await asyncio.wait_for(self.capture_closed.wait(),5)
        self.assertEqual(self.captures,['source-0']);self.assertEqual(self.records,[])
        self.assertEqual(self.connections,1)

    async def test_process_cancel_closes_epoch_and_does_not_reconnect(self):
        await self.completed();self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):await self.task
        self.assertTrue(self.claims[0].closed);self.assertEqual(self.connections,1)


class BackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_attempts_have_capped_backoff_and_no_capture(self):
        delays=[]
        async def unavailable(*args,**kwargs):raise OSError('private sentinel')
        async def sleep(delay):
            delays.append(delay)
            if len(delays)==7:raise asyncio.CancelledError
        with patch.object(entry,'open_core_socket',unavailable),patch.object(entry.asyncio,'sleep',sleep), \
            patch.object(entry,'CoreCaptureClient') as capture,redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.CancelledError):await entry.supervise(object(),'synthetic-token')
        self.assertEqual(delays,[1,2,4,8,16,30,30]);capture.assert_not_called()


if __name__=='__main__':unittest.main()
