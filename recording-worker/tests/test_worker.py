import asyncio
import base64
import io
import hashlib
import json
import os
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
import sys
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from recording_worker.protocol import GrantBook, WorkerClaims, ProtocolRejected, validate_grant
from recording_worker.client import CoreCaptureClient, authenticate, open_core_socket
from validator_source import load_validator, find_validator
validator=load_validator(ROOT)
PCM=b'\x10\x00'*160
AUDIO={'audio':base64.b64encode(PCM).decode(),'rate':16000,'width':2,'channels':1}


def make_session(clock=None,op='operation',device='device'):
    kwargs={} if clock is None else {'clock':clock}
    session=validator.RecordingSession(operation_id=op,source_device_id=device,satellite_entity_id='assist_satellite.fixture',question_context_id='owned',**kwargs)
    session.state_event(source_device_id=device,satellite_entity_id='assist_satellite.fixture',state='responding',context_id='owned')
    return session


def listening(session):
    session.state_event(source_device_id=session.source_device_id,satellite_entity_id=session.satellite_entity_id,state='listening',context_id='new-native-context')


def frame(grant,seq,kind,payload):
    return {'grant_id':grant['grant_id'],'connection_id':grant['connection_id'],'seq':seq,'kind':kind,'payload':payload}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.now=[100.0];self.session=make_session(lambda:self.now[0]);self.book=GrantBook(connection_id='connection',clock=lambda:self.now[0]);self.grant=self.book.issue(self.session)
    def send(self,seq,kind,payload):
        return self.book.receive(frame(self.grant,seq,kind,payload),connection_id='connection')
    def start(self):
        self.session.recording_started(source_device_id='device');self.send(1,'ack',{'core_timeout_seconds':60});listening(self.session)
    def finish(self):
        self.start();self.send(2,'audio',AUDIO);self.session.question_result(context_id='owned',sentence='Dinner')
        self.send(3,'end',{'overflow':False})
    def test_one_use_grant_full_barrier_required(self):
        self.finish()
        with self.assertRaisesRegex(ProtocolRejected,'barrier_required'):self.book.consume(self.grant['grant_id'],expected_revision=self.session.ready_revision())
        self.send(4,'barrier',{'end_sequence':3})
        answer=self.book.consume(self.grant['grant_id'],expected_revision=self.session.ready_revision());self.assertEqual(answer.pcm,PCM)
        with self.assertRaises(ProtocolRejected):self.book.consume(self.grant['grant_id'],expected_revision=0)
    def test_connection_binding_and_reconnect_reject(self):
        with self.assertRaisesRegex(ProtocolRejected,'wrong_connection'):self.book.receive(frame(self.grant,1,'ack',{'core_timeout_seconds':60}),connection_id='other')
        claims=WorkerClaims('other')
        with self.assertRaisesRegex(ProtocolRejected,'wrong_connection'):claims.claim(self.grant)
        self.book.disconnect()
        with self.assertRaises(ProtocolRejected):self.send(1,'ack',{'core_timeout_seconds':60})
    def test_worker_claim_cannot_replay_or_resume(self):
        claims=WorkerClaims('connection');claims.claim(self.grant)
        with self.assertRaisesRegex(ProtocolRejected,'grant_replay'):claims.claim(self.grant)
        claims.disconnect()
        with self.assertRaisesRegex(ProtocolRejected,'connection_closed'):claims.claim(self.grant)
    def test_sequence_duplicate_gap_boolean_all_reject(self):
        for seq in (0,2,True):
            self.setUp()
            with self.assertRaisesRegex(ProtocolRejected,'frame_rejected'):self.send(seq,'ack',{'core_timeout_seconds':60})
            self.assertEqual(self.session.buffered_bytes,0)
        self.setUp();self.start()
        with self.assertRaises(ProtocolRejected):self.send(1,'ack',{'core_timeout_seconds':60})
    def test_source_busy_and_operation_replay(self):
        with self.assertRaisesRegex(ProtocolRejected,'source_busy'):self.book.issue(make_session(lambda:self.now[0],op='other'))
        with self.assertRaisesRegex(ProtocolRejected,'operation_replay'):self.book.issue(make_session(lambda:self.now[0],device='other'))
    def test_wrong_grant_cannot_select_neighbor_session(self):
        bad=frame(self.grant,1,'ack',{'core_timeout_seconds':60});bad['grant_id']='unknown'
        with self.assertRaisesRegex(ProtocolRejected,'unknown_grant'):self.book.receive(bad,connection_id='connection')
    def test_disconnect_erases_partial_audio_no_mic_claim(self):
        self.start();self.send(2,'audio',AUDIO);self.book.disconnect()
        self.assertEqual(self.session.buffered_bytes,0);self.assertFalse(self.session.source_release_proven)
    def test_barrier_cannot_skip_end_or_reference_wrong_sequence(self):
        for mode in ('early','wrong'):
            self.setUp()
            if mode=='early':self.start();seq=2
            else:self.finish();seq=4
            with self.assertRaises(ProtocolRejected):self.send(seq,'barrier',{'end_sequence':1})
    def test_frames_after_barrier_reject(self):
        self.finish();self.send(4,'barrier',{'end_sequence':3})
        with self.assertRaises(ProtocolRejected):self.send(5,'audio',AUDIO)
        self.assertEqual(self.session.buffered_bytes,0)
    def test_deadline_from_issuer_not_worker_clock(self):
        self.now[0]=130
        with self.assertRaises(ProtocolRejected):self.send(1,'ack',{'core_timeout_seconds':60})
    def test_unknown_fields_never_pass_to_validator(self):
        f=frame(self.grant,1,'ack',{'core_timeout_seconds':60});f['secret']='must not log'
        with self.assertRaisesRegex(ProtocolRejected,'^frame_rejected$'):self.book.receive(f,connection_id='connection')
    def test_grant_cap_and_exact_types(self):
        for key,value in [('core_timeout_seconds',30),('max_pcm_bytes',960002),('max_chunks',True),('valid_for_seconds',60),('version',True)]:
            bad=self.grant|{key:value}
            with self.assertRaises(ProtocolRejected):validate_grant(bad,'connection')
    def test_expiry_timer_wipes_pending_audio_without_release(self):
        self.start();self.send(2,'audio',AUDIO);self.now[0]=131
        self.assertEqual(self.book.expire(),1);self.assertEqual(self.session.buffered_bytes,0)
        self.assertFalse(self.session.source_release_proven)
    def test_expired_finalization_clears_complete_buffer(self):
        self.finish();self.send(4,'barrier',{'end_sequence':3});self.now[0]=131
        with self.assertRaisesRegex(ProtocolRejected,'expired'):self.book.consume(self.grant['grant_id'],expected_revision=0)
        self.assertEqual(self.session.buffered_bytes,0)
    def test_constructor_caps_ledger_memory(self):
        for count in (0,129,True):
            with self.assertRaises(ValueError):GrantBook(max_grants=count)
            with self.assertRaises(ValueError):WorkerClaims('connection',max_grants=count)
    def test_active_pcm_sessions_are_bounded_separately_from_tombstones(self):
        for n in range(3):self.book.issue(make_session(lambda:self.now[0],device=f'device{n}',op=f'op{n}'))
        with self.assertRaisesRegex(ProtocolRejected,'worker_busy'):self.book.issue(make_session(lambda:self.now[0],device='fifth',op='fifth'))
    def test_bounded_tombstones_require_new_connection(self):
        book=GrantBook(connection_id='connection',clock=lambda:self.now[0],max_grants=1);book.issue(self.session)
        with self.assertRaisesRegex(ProtocolRejected,'connection_closed'):book.issue(make_session(lambda:self.now[0],device='other',op='other'))


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session=make_session();self.book=GrantBook(connection_id='wire-connection');self.grant=self.book.issue(self.session)
        self.core_commands=[];self.frames=[];self.core_admin=True;self.events=[{'type':'audio',**AUDIO},{'type':'end','overflow':False}]
        self.release_end=asyncio.Event();self.release_end.set();self.end_received=asyncio.Event();self.backend_refuse=False
        async def core(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            await ws.send_json({'type':'auth_required'})
            assert (await ws.receive_json())=={'type':'auth','access_token':'synthetic-token'}
            await ws.send_json({'type':'auth_ok'})
            msg=await ws.receive_json();self.core_commands.append(msg['type'])
            await ws.send_json({'id':1,'type':'result','success':True,'result':{'is_admin':self.core_admin}})
            if not self.core_admin:return ws
            msg=await ws.receive_json();self.core_commands.append(msg['type']);self.assertEqual(msg['device_id'],'device');self.assertEqual(msg['timeout'],60)
            self.session.recording_started(source_device_id='device')
            await ws.send_json({'id':2,'type':'result','success':True,'result':None})
            for event in self.events:await ws.send_json({'id':2,'type':'event','event':event})
            async for _ in ws:pass
            return ws
        async def backend(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            async for incoming in ws:
                msg=incoming.json();f=msg['frame'];self.frames.append(f['kind'])
                if f['kind']=='end':self.end_received.set();await self.release_end.wait()
                try:
                    if self.backend_refuse:raise ProtocolRejected('fixture_refusal')
                    result=self.book.receive(f,connection_id='wire-connection')
                    if f['kind']=='ack':listening(self.session)
                    if f['kind']=='end':self.session.question_result(context_id='owned',sentence='Dinner')
                    await ws.send_json({'id':msg['id'],'type':'result','success':True,'result':result})
                except ValueError:await ws.send_json({'id':msg['id'],'type':'result','success':False,'error':{'code':'frame_rejected'}})
            return ws
        app=web.Application();app.router.add_get('/core',core);app.router.add_get('/backend',backend)
        self.http=TestClient(TestServer(app));await self.http.start_server()
        self.backend=await self.http.ws_connect('/backend');self.msg_id=0
        async def send(f):
            self.msg_id+=1;await self.backend.send_json({'id':self.msg_id,'type':'household_voice/recording_worker/frame','frame':f})
            reply=await self.backend.receive_json()
            if not reply.get('success'):raise ProtocolRejected('backend_refused')
            return reply['result']
        self.send=send
        self.client=CoreCaptureClient(self.http.session,'synthetic-token',url=str(self.http.make_url('/core')).replace('http:','ws:'))
    async def asyncTearDown(self):
        self.release_end.set();await self.backend.close();await self.http.close()
    async def test_real_wire_order_and_end_ack_barrier(self):
        self.release_end.clear()
        task=asyncio.create_task(self.client.record(self.grant,self.send))
        await asyncio.wait_for(self.end_received.wait(),2)
        self.assertEqual(self.frames,['ack','audio','end']);self.assertFalse(task.done())
        self.release_end.set();result=await task
        self.assertEqual(self.frames,['ack','audio','end','barrier']);self.assertTrue(result['wire_drained'])
        self.assertFalse(result['microphone_stop_command_sent'])
        answer=self.book.consume(self.grant['grant_id'],expected_revision=self.session.ready_revision());self.assertEqual(answer.pcm,PCM)
        self.assertEqual(self.core_commands,['auth/current_user','assist_pipeline/device/capture'])
    async def test_non_admin_fails_before_capture(self):
        self.core_admin=False
        with self.assertRaisesRegex(ProtocolRejected,'administrator_required'):await self.client.record(self.grant,self.send)
        self.assertEqual(self.core_commands,['auth/current_user']);self.assertEqual(self.frames,[])
    async def test_overflow_never_sends_success_end_or_barrier(self):
        self.events[-1]['overflow']=True
        with self.assertRaisesRegex(ProtocolRejected,'capture_incomplete'):await self.client.record(self.grant,self.send)
        self.assertNotIn('end',self.frames);self.assertNotIn('barrier',self.frames)
    async def test_malformed_format_never_forwarded(self):
        self.events[0]['rate']=48000
        with self.assertRaisesRegex(ProtocolRejected,'invalid_audio_format'):await self.client.record(self.grant,self.send)
        self.assertEqual(self.frames,['ack'])
    async def test_backend_failure_has_no_capture_retry(self):
        self.backend_refuse=True
        with self.assertRaisesRegex(ProtocolRejected,'backend_refused'):await self.client.record(self.grant,self.send)
        self.assertEqual(self.core_commands.count('assist_pipeline/device/capture'),1)
    async def test_cancel_during_end_ack_has_no_success_barrier(self):
        self.release_end.clear();task=asyncio.create_task(self.client.record(self.grant,self.send))
        await asyncio.wait_for(self.end_received.wait(),2);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertNotIn('barrier',self.frames);self.book.disconnect();self.assertEqual(self.session.buffered_bytes,0)
        self.release_end.set()

class EntrypointTests(unittest.TestCase):
    def test_disabled_options_do_not_open_network_or_read_audio(self):
        from recording_worker import __main__ as entry
        output=io.StringIO()
        with patch.object(entry.Path,'read_text',return_value='{"enabled":false}'), patch.object(entry.aiohttp,'ClientSession') as network, redirect_stdout(output):
            asyncio.run(entry.run())
        network.assert_not_called();self.assertEqual(output.getvalue().strip(),' {"status": "disabled"}'.strip())
    def test_exception_output_is_fixed_metadata_only(self):
        from recording_worker import __main__ as entry
        async def failure():raise RuntimeError('private token and transcript must not appear')
        output=io.StringIO()
        with patch.object(entry,'run',failure),redirect_stdout(output):entry.main()
        self.assertEqual(output.getvalue().strip(),'{"status": "worker_unavailable"}')


class StartupWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phase='normal';self.connections=0;self.commands=[];self.release=asyncio.Event()
        self.registered={'id':2,'type':'result','success':True,'result':{'connection_id':'fixture-connection'}}
        async def endpoint(request):
            self.connections+=1;phase=self.phase
            if phase=='upgrade':
                await self.release.wait();return web.Response(status=503)
            ws=web.WebSocketResponse();await ws.prepare(request)
            if phase=='challenge':
                async for _ in ws:pass
                return ws
            await ws.send_json({'type':'auth_required'})
            self.assertEqual(await ws.receive_json(),{'type':'auth','access_token':'synthetic-token'})
            if phase=='auth':
                async for _ in ws:pass
                return ws
            await ws.send_json({'type':'auth_ok'})
            self.commands.append(await ws.receive_json())
            if phase=='admin':
                async for _ in ws:pass
                return ws
            await ws.send_json({'id':1,'type':'result','success':True,'result':{'is_admin':True}})
            incoming=await ws.receive()
            if incoming.type==web.WSMsgType.TEXT:
                self.commands.append(incoming.json())
                if phase!='registration':await ws.send_json(self.registered)
            async for _ in ws:pass
            return ws
        app=web.Application();app.router.add_get('/core',endpoint)
        self.http=TestClient(TestServer(app));await self.http.start_server()
        self.url=str(self.http.make_url('/core')).replace('http:','ws:')
    async def asyncTearDown(self):
        self.release.set();await self.http.close()
    async def authenticated(self):
        ws=await open_core_socket(self.http.session,url=self.url)
        await authenticate(ws,'synthetic-token');return ws
    async def test_stalled_http_upgrade_has_one_bounded_attempt(self):
        from recording_worker import client
        self.phase='upgrade'
        with patch.object(client,'HANDSHAKE_TIMEOUT',0.05):
            with self.assertRaisesRegex(ProtocolRejected,'^connection_timeout$'):
                await open_core_socket(self.http.session,url=self.url)
        self.assertEqual(self.connections,1);self.assertEqual(self.commands,[])
    async def test_each_stalled_auth_phase_times_out_without_capture(self):
        from recording_worker import client
        for phase in ('challenge','auth','admin'):
            with self.subTest(phase=phase):
                self.phase=phase
                async with await open_core_socket(self.http.session,url=self.url) as ws:
                    with patch.object(client,'HANDSHAKE_TIMEOUT',0.05):
                        with self.assertRaisesRegex(ProtocolRejected,'^authentication_timeout$'):
                            await authenticate(ws,'synthetic-token')
        self.assertEqual(self.connections,3)
        self.assertFalse(any(c['type']=='assist_pipeline/device/capture' for c in self.commands))
    async def test_authentication_budget_covers_whole_sequence(self):
        from recording_worker import client
        class SlowSocket:
            def __init__(self):self.responses=iter(({'type':'auth_required'},{'type':'auth_ok'},
                {'id':1,'type':'result','success':True,'result':{'is_admin':True}}))
            async def receive_json(self):await asyncio.sleep(0.03);return next(self.responses)
            async def send_json(self,message):pass
        with patch.object(client,'HANDSHAKE_TIMEOUT',0.05):
            with self.assertRaisesRegex(ProtocolRejected,'^authentication_timeout$'):
                await authenticate(SlowSocket(),'synthetic-token')
    async def test_stalled_registration_is_bounded_before_capture(self):
        from recording_worker import __main__ as entry
        self.phase='registration'
        async with await self.authenticated() as ws:
            with patch.object(entry,'REGISTRATION_TIMEOUT',0.05):
                with self.assertRaisesRegex(ProtocolRejected,'^backend_registration_timeout$'):
                    await entry.register_worker(ws)
        self.assertEqual(self.connections,1)
        self.assertEqual([c['type'] for c in self.commands],['auth/current_user','household_voice/recording_worker/connect'])
    async def test_malformed_registration_shapes_are_rejected(self):
        from recording_worker import __main__ as entry
        valid=self.registered
        values=(None,[],{'private':'sentinel'},valid|{'id':True},valid|{'id':3},
            valid|{'type':'event'},valid|{'success':1},valid|{'result':None},
            valid|{'result':{}},valid|{'result':{'connection_id':''}},
            valid|{'result':{'connection_id':7}},valid|{'result':{'connection_id':'x'*256}},
            valid|{'result':{'connection_id':'fixture','unexpected':'private sentinel'}},
            valid|{'unexpected':'private sentinel'})
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                self.registered=value
                async with await self.authenticated() as ws:
                    with self.assertRaisesRegex(ProtocolRejected,'^backend_registration_rejected$'):
                        await entry.register_worker(ws)
        self.assertFalse(any(c['type']=='assist_pipeline/device/capture' for c in self.commands))
    async def test_valid_registration_accepts_only_its_connection_identifier(self):
        from recording_worker import __main__ as entry
        async with await self.authenticated() as ws:
            self.assertEqual(await entry.register_worker(ws),'fixture-connection')
    async def test_registration_write_is_also_bounded(self):
        from recording_worker import __main__ as entry
        class StalledWrite:
            async def send_json(self,message):await asyncio.Event().wait()
        with patch.object(entry,'REGISTRATION_TIMEOUT',0.05):
            with self.assertRaisesRegex(ProtocolRejected,'^backend_registration_timeout$'):
                await entry.register_worker(StalledWrite())
    async def test_malformed_registration_output_never_repeats_payload(self):
        from recording_worker import __main__ as entry
        self.registered={'private':'token transcript must never appear'};output=io.StringIO()
        with patch.object(entry.Path,'read_text',return_value='{"enabled":true}'), \
            patch.dict(os.environ,{'SUPERVISOR_TOKEN':'synthetic-token'},clear=True), \
            patch.object(entry,'SUPERVISOR_WEBSOCKET',self.url), \
            patch.object(entry,'CoreCaptureClient') as capture,redirect_stdout(output):
            await entry.run()
        capture.assert_not_called();self.assertEqual(output.getvalue().strip(),'{"status": "backend_unavailable"}')
    async def test_authentication_cancellation_is_preserved(self):
        class StalledSocket:
            async def receive_json(self):await asyncio.Event().wait()
        task=asyncio.create_task(authenticate(StalledSocket(),'synthetic-token'))
        await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task


class ValidatorLayoutTests(unittest.TestCase):
    def test_public_combined_repository_path(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
            root=Path(directory)/'recording-worker';root.mkdir()
            source=root.parent/'custom_components/household_voice/recording_session.py'
            source.parent.mkdir(parents=True);source.write_text('value=1')
            self.assertEqual(find_validator(root),source)
    def test_explicit_missing_source_does_not_guess_another_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ,{'HOUSEHOLD_VOICE_RECORDING_VALIDATOR':str(Path(directory)/'missing.py')}):
                with self.assertRaises(FileNotFoundError):find_validator(Path(directory))
    def test_portable_fixture_checksum_is_required(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
            root=Path(directory);source=root/'tests/fixtures/recording_session.py'
            source.parent.mkdir(parents=True);source.write_text('value=1')
            provenance=source.parent/'source.json'
            provenance.write_text(json.dumps({'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}))
            self.assertEqual(load_validator(root).value,1)
            source.write_text('value=2')
            with self.assertRaisesRegex(ValueError,'differs'):load_validator(root)

if __name__=='__main__':unittest.main()
