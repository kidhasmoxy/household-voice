"""Real HA capture WS and question lifecycle; synthetic satellite/audio only.

No production adapter. Run with the stock HA container's Python. Overrides only
hardware announcement delivery, media resolution, and the synthetic STT provider.
Private queue access is confined to the explicitly labeled overflow fault injection.
"""
import asyncio
import base64
import json
import logging
import tempfile
from types import MappingProxyType

from aiohttp.test_utils import TestClient, TestServer
from homeassistant import loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components import assist_pipeline, stt
from homeassistant.components.assist_pipeline.const import EVENT_RECORDING
from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
from homeassistant.components.assist_satellite import entity as sat
from homeassistant.components.assist_satellite.const import DATA_COMPONENT as SAT, AssistSatelliteEntityFeature
from homeassistant.components.stt.const import DATA_COMPONENT as STT
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant, Context, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.setup import async_setup_component


async def main():
    logging.basicConfig(level=logging.WARNING)
    cases = []
    with tempfile.TemporaryDirectory(prefix='recording-proof-', dir='/tmp') as directory:
        hass = HomeAssistant(directory)
        hass.config.skip_pip = True
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        http = None
        sockets = []
        tasks = []
        state = {'transcript': 'Dinner is ready'}
        class Provider(stt.SpeechToTextEntity):
            _attr_name = 'Recording proof STT'
            _attr_unique_id = 'recording-proof-provider'
            supported_languages = ['en']
            supported_formats = [stt.AudioFormats.WAV]
            supported_codecs = [stt.AudioCodecs.PCM]
            supported_bit_rates = [stt.AudioBitRates.BITRATE_16]
            supported_sample_rates = [stt.AudioSampleRates.SAMPLERATE_16000]
            supported_channels = [stt.AudioChannels.CHANNEL_MONO]
            async def async_process_audio_stream(self, metadata, stream):
                async for _ in stream:
                    pass
                return stt.SpeechResult(state['transcript'], stt.SpeechResultState.SUCCESS)
        class Satellite(sat.AssistSatelliteEntity):
            _attr_supported_features = AssistSatelliteEntityFeature.START_CONVERSATION
            def __init__(self, suffix):
                self._attr_name = 'Recording proof ' + suffix
                self._attr_unique_id = 'recording-proof-' + suffix
                self.chunks = asyncio.Queue()
                self.prompt = asyncio.Event()
                self.started = asyncio.Event()
                self.events = []
                self.run = None
                self.prompt_release = None
            def async_get_configuration(self):
                return sat.AssistSatelliteConfiguration([], [], 0)
            async def async_set_configuration(self, config):
                pass
            async def _resolve_announcement_media_id(self, message, media_id, preannounce_media_id=None):
                return sat.AssistSatelliteAnnouncement(message, 'http://fixture.invalid/prompt.wav', 'http://fixture.invalid/prompt.wav', None, 'url')
            async def async_start_conversation(self, announcement):
                # Hardware-only stub: completion callback, then device-originated
                # microphone turn. Core question state and pipeline are untouched.
                self.prompt.set()
                if self.prompt_release is not None:
                    await self.prompt_release.wait()
                self.tts_response_finished()
                self.run = asyncio.create_task(self.async_accept_pipeline_from_satellite(self.stream()))
                tasks.append(self.run)
            async def stream(self):
                while (chunk := await self.chunks.get()) is not None:
                    yield chunk
            def on_pipeline_event(self, event):
                self.events.append(event.type.value)
                if event.type.value == 'stt-start':
                    self.started.set()
        try:
            assert await async_load_base_functionality(hass)
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await async_setup_component(hass, 'network', {})
            assert await async_setup_component(hass, 'websocket_api', {})
            component = EntityComponent(logging.getLogger(__name__), 'stt', hass)
            component.register_shutdown()
            hass.data[STT] = component
            provider = Provider()
            await component.async_add_entities([provider])
            assert await async_setup_component(hass, 'conversation', {})
            assert await async_setup_component(hass, 'assist_pipeline', {})
            assert await async_setup_component(hass, 'assist_satellite', {})
            http = TestClient(TestServer(hass.http.app))
            await http.start_server()
            async def connect(admin=True):
                user = await hass.auth.async_create_user('Synthetic capture user', group_ids=[GROUP_ID_ADMIN if admin else GROUP_ID_USER])
                refresh = await hass.auth.async_create_refresh_token(user, client_id='https://fixture.invalid/')
                ws = await http.ws_connect('/api/websocket')
                assert (await ws.receive_json())['type'] == 'auth_required'
                await ws.send_json({'type':'auth', 'access_token':hass.auth.async_create_access_token(refresh)})
                assert (await ws.receive_json())['type'] == 'auth_ok'
                sockets.append(ws)
                return ws
            admin = await connect()
            pipeline = {'name':'Recording proof', 'conversation_engine':'homeassistant', 'conversation_language':'en', 'language':'en', 'stt_engine':provider.entity_id, 'stt_language':'en', 'tts_engine':None, 'tts_language':None, 'tts_voice':None, 'wake_word_entity':None, 'wake_word_id':None}
            await admin.send_json({'id':1, 'type':'assist_pipeline/pipeline/create', **pipeline})
            reply = await admin.receive_json()
            assert reply['success'], reply
            pipeline_id = reply['result']['id']
            await admin.send_json({'id':2, 'type':'assist_pipeline/pipeline/set_preferred', 'pipeline_id':pipeline_id})
            reply = await admin.receive_json()
            assert reply['success'], reply
            async def make_satellite(suffix):
                entry = ConfigEntry(domain='esphome', title='Synthetic satellite', data={}, options={}, source='user', version=1, minor_version=1, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=None)
                hass.config_entries._entries[entry.entry_id] = entry
                device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={('esphome', suffix)})
                satellite = Satellite(suffix)
                await hass.data[SAT].async_add_entities([satellite])
                satellite.platform.config_entry = entry
                er.async_get(hass).async_update_entity(satellite.entity_id, device_id=device.id)
                return satellite, device.id
            first, device1 = await make_satellite('first')
            second, device2 = await make_satellite('second')
            starts = []
            hass.bus.async_listen(EVENT_RECORDING, lambda event: starts.append(event.data['device_id']))
            async def capture(device, timeout=10):
                ws = await connect()
                await ws.send_json({'id':1, 'type':'assist_pipeline/device/capture', 'device_id':device, 'timeout':timeout})
                reply = await ws.receive_json()
                assert reply == {'id':1, 'type':'result', 'success':True, 'result':None}, reply
                return ws
            async def collect(ws):
                pcm = bytearray()
                while True:
                    message = await asyncio.wait_for(ws.receive_json(), 5)
                    event = message['event']
                    if event['type'] == 'end':
                        return bytes(pcm), event
                    assert event['type'] == 'audio'
                    assert (event['rate'],event['width'],event['channels']) == (16000,2,1)
                    pcm.extend(base64.b64decode(event['audio'], validate=True))
            async def ask(satellite):
                task = asyncio.create_task(hass.services.async_call('assist_satellite', 'ask_question', {'entity_id':satellite.entity_id,'question':"What's the announcement?",'preannounce':False}, blocking=True, return_response=True))
                tasks.append(task)
                await asyncio.wait_for(satellite.started.wait(), 5)
                return task
            async def finish(satellite, pcm):
                satellite.chunks.put_nowait(pcm)
                satellite.chunks.put_nowait(None)
            pcm1 = b'\x10\x00' * 1600
            pcm2 = b'\x20\x00' * 1600
            ws1, ws2 = await capture(device1), await capture(device2)
            q1, q2 = await ask(first), await ask(second)
            await finish(first,pcm1)
            await finish(second,pcm2)
            answer1,answer2 = await q1,await q2
            audio1,end1 = await collect(ws1)
            audio2,end2 = await collect(ws2)
            assert audio1 == pcm1 and audio2 == pcm2
            assert answer1['sentence'] == answer2['sentence'] == state['transcript']
            assert not end1['overflow'] and not end2['overflow']
            assert 'intent-start' not in first.events and 'tts-start' not in first.events
            cases.append('two_devices_identical_transcript_isolated_actual_stt_only')
            # Actual native prompt completion can report IDLE before STT start.
            assert 'stt-end' in first.events and 'run-end' in first.events
            # Supersession: real command replaces queue; transcript continues.
            third, device3 = await make_satellite('third')
            baseline = starts.count(device3)
            original = await capture(device3)
            question = await ask(third)
            third.chunks.put_nowait(pcm1)
            first_audio = await asyncio.wait_for(original.receive_json(),5)
            assert first_audio['event']['type'] == 'audio'
            replacement = await capture(device3)
            old_remaining, old_end = await collect(original)
            assert base64.b64decode(first_audio['event']['audio']) + old_remaining == pcm1
            assert old_end == {'type':'end','overflow':False}
            await finish(third,pcm2)
            answer = await question
            remainder, _ = await collect(replacement)
            await hass.async_block_till_done()
            assert answer['sentence'] == state['transcript'] and remainder == pcm2
            assert starts.count(device3)-baseline == 2
            cases.append('supersession_indistinguishable_terminal_but_two_public_start_events')
            # Canceling the question does not itself cancel the satellite stream.
            fourth, device4 = await make_satellite('fourth')
            cancel_ws = await capture(device4)
            cancel_q = await ask(fourth)
            cancel_q.cancel()
            await asyncio.gather(cancel_q, return_exceptions=True)
            assert not fourth.run.done()
            await cancel_ws.send_json({'id':2,'type':'unsubscribe_events','subscription':1})
            assert (await cancel_ws.receive_json())['success']
            assert device4 not in hass.data[KEY_ASSIST_PIPELINE].device_audio_queues
            await finish(fourth,pcm1)
            await fourth.run
            cases.append('canceled_question_requires_separate_capture_cleanup_mic_not_stopped')
            # Overflow fault injection is test-only: real handler receives a full
            # internal queue, reports overflow, and cleans up. No runtime uses it.
            fifth, device5 = await make_satellite('fifth')
            overflow_ws = await capture(device5, timeout=1)
            overflow_q = await ask(fifth)
            queue = hass.data[KEY_ASSIST_PIPELINE].device_audio_queues[device5]
            queue.queue._maxsize = 1
            queue.queue.put_nowait(pcm1)
            # Invoke real pipeline forwarding synchronously so consumer cannot drain.
            from homeassistant.components.assist_pipeline.pipeline import PipelineRun, AudioSettings, PipelineStage
            run = PipelineRun(hass=hass, context=fifth._context, pipeline=assist_pipeline.async_get_pipeline(hass), start_stage=PipelineStage.STT, end_stage=PipelineStage.STT, event_callback=lambda event:None, audio_settings=AudioSettings())
            run._device_id = device5
            if hasattr(run,'capture_audio'): run.capture_audio(pcm2)
            else: run._capture_chunk(pcm2)
            assert queue.overflow
            await finish(fifth,pcm2)
            await overflow_q
            _, overflow_end = await collect(overflow_ws)
            assert overflow_end['overflow']
            cases.append('overflow_loses_terminal_until_timeout_but_reports_overflow')
            # IDLE is not a task completion barrier: real public callback sets it
            # even while a synthetic hardware-backed STT pipeline is unfinished.
            sixth, device6 = await make_satellite('sixth')
            idle_ws = await capture(device6)
            idle_q = await ask(sixth)
            sixth.tts_response_finished()
            assert sixth.state == 'idle' and not sixth.run.done()
            await finish(sixth,pcm1)
            await idle_q
            await collect(idle_ws)
            cases.append('idle_callback_is_not_old_run_end_barrier')
            # A second native microphone run on the same satellite cancels the
            # first pending response; it must not donate a replacement answer.
            seventh, device7 = await make_satellite('seventh')
            interrupt_ws = await capture(device7)
            interrupted_q = await ask(seventh)
            seventh.chunks.put_nowait(pcm1)
            chunk_event = await asyncio.wait_for(interrupt_ws.receive_json(),5)
            assert chunk_event['event']['type'] == 'audio'
            async def replacement_stream():
                yield pcm2
            replacement_run = asyncio.create_task(seventh.async_accept_pipeline_from_satellite(replacement_stream()))
            tasks.append(replacement_run)
            result = (await asyncio.gather(interrupted_q, return_exceptions=True))[0]
            assert isinstance(result, Exception), repr(result)
            await replacement_run
            remaining, interrupted_end = await collect(interrupt_ws)
            assert base64.b64decode(chunk_event['event']['audio']) + remaining == pcm1
            assert not interrupted_end['overflow']
            cases.append('same_device_native_replacement_fails_question_not_audio_success')
            # A source-bound native service Context marks RESPONDING only after
            # its preceding pipeline has been canceled and awaited. This is the
            # candidate public arm gate; IDLE is deliberately not used.
            eighth, device8 = await make_satellite('eighth')
            old_run = asyncio.create_task(eighth.async_accept_pipeline_from_satellite(eighth.stream(), end_stage=PipelineStage.STT))
            tasks.append(old_run)
            await asyncio.wait_for(eighth.started.wait(),5)
            eighth.started.clear()
            eighth.prompt_release = asyncio.Event()
            owned_context = Context()
            owned_responding = asyncio.Event()
            observed_states = []
            @callback
            def observe_state(event):
                if event.data.get('entity_id') != eighth.entity_id:
                    return
                new = event.data.get('new_state')
                if new is None:
                    return
                observed_states.append((new.state,event.context.id))
                if new.state == 'responding' and event.context.id == owned_context.id:
                    owned_responding.set()
            unsubscribe_state = hass.bus.async_listen('state_changed',observe_state)
            gated_q = asyncio.create_task(hass.services.async_call('assist_satellite','ask_question',{'entity_id':eighth.entity_id,'question':"What's the announcement?",'preannounce':False},blocking=True,return_response=True,context=owned_context))
            tasks.append(gated_q)
            await asyncio.wait_for(owned_responding.wait(),5)
            assert old_run.done(), 'Owned RESPONDING published before preceding pipeline settled'
            assert not eighth.started.is_set(), 'Listening before prompt completion'
            gated_ws = await capture(device8)
            # Capture ACK has arrived before external prompt playback completes.
            eighth.prompt_release.set()
            await asyncio.wait_for(eighth.started.wait(),5)
            await finish(eighth,pcm2)
            gated_answer = await gated_q
            gated_pcm,gated_end = await collect(gated_ws)
            assert gated_answer['sentence'] == state['transcript'] and gated_pcm == pcm2
            assert not gated_end['overflow'] and starts.count(device8) == 1
            assert ('responding',owned_context.id) in observed_states
            unsubscribe_state()
            cases.append('owned_service_context_responding_gate_after_old_run_before_capture_ack_listening')
            family = await connect(False)
            await family.send_json({'id':1,'type':'assist_pipeline/device/capture','device_id':device1,'timeout':1})
            denied = await family.receive_json()
            assert not denied['success']
            cases.append('capture_requires_administrator')
            await hass.async_block_till_done()
            assert not hass.data[KEY_ASSIST_PIPELINE].device_audio_queues
            print(json.dumps({'ha_version':__version__,'cases':cases,'real_authenticated_ws':True,'real_native_question_pipeline':True,'synthetic_hardware_and_stt':True,'production_adapter_proven':False}))
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ws in sockets: await ws.close()
            if http is not None: await http.close()
            await hass.async_stop(force=True)

if __name__ == '__main__':
    asyncio.run(main())
