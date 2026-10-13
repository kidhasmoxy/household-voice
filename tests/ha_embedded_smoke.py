"""Actual HA lifecycle with local SQLite and HTTP; no household endpoints."""
import asyncio
import base64
import io
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch
import wave

from aiohttp.test_utils import TestClient, TestServer
from homeassistant import loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.config_entries import ConfigEntries, SOURCE_USER
from homeassistant.const import __version__
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component


async def main():
    logging.basicConfig(level=logging.WARNING)
    root = Path(__file__).resolve().parents[1]
    proof = {'home_assistant_version': __version__, 'household_contacts': False,
             'physical_audio': False, 'coordinator': 'embedded_sqlite'}
    with tempfile.TemporaryDirectory(prefix='household-voice-local-', dir='/tmp') as directory:
        config = Path(directory)
        shutil.copytree(root/'custom_components', config/'custom_components')
        sys.path.insert(0, str(config))
        hass = HomeAssistant(str(config))
        hass.config.skip_pip = True
        await hass.config.async_set_time_zone('UTC')
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        http = None
        try:
            assert await async_load_base_functionality(hass)
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await async_setup_component(hass, 'network', {})
            assert await async_setup_component(hass, 'household_voice', {})
            form = await hass.config_entries.flow.async_init('household_voice', context={'source': SOURCE_USER})
            assert form['type'] == 'menu'
            form = await hass.config_entries.flow.async_configure(form['flow_id'], {'next_step_id': 'embedded'})
            bad = await hass.config_entries.flow.async_configure(form['flow_id'], {'audio_base_url': 'http://user:secret@example.invalid'})
            assert bad['errors']['base'] == 'invalid_audio_url'
            result = await hass.config_entries.flow.async_configure(form['flow_id'], {'audio_base_url': 'http://ha.example.invalid:8123'})
            assert result['type'] == 'create_entry', result
            entry = result['result']
            await hass.async_block_till_done()
            coordinator = hass.data['household_voice'][entry.entry_id]
            client = coordinator.client
            assert coordinator.data['mode'] == 'embedded'
            assert coordinator.data['maintenance']['healthy']
            assert 'household-voice' in hass.data['frontend_panels']
            assert 'household_voice_schedules_'+entry.entry_id in {api.id for api in llm.async_get_apis(hass)}
            # Registration is insufficient: instantiate every API to catch versioned
            # tool contracts (for example ToolAnnotations introduced in HA 2026.10).
            context = llm.LLMContext(platform='fixture', context=Context(), language='en',
                assistant='conversation', device_id=None)
            tool_count = 0
            for api in llm.async_get_apis(hass):
                if not api.id.startswith('household_voice_'):
                    continue
                instance = await llm.async_get_api(hass, api.id, context)
                tool_count += len(instance.tools)
                # Exercise each API's safe read/clarification path as well as construction.
                result = await instance.async_call_tool(llm.ToolInput(
                    tool_name=instance.tools[0].name, tool_args={}, id='compat-'+api.id))
                if hasattr(llm, 'ToolResult'):
                    assert isinstance(result, llm.ToolResult)
                    assert isinstance(result.data, dict)
                else:
                    assert type(result) is dict
                    json.dumps(result)
                assert all(tool.name and tool.parameters for tool in instance.tools)
                if hasattr(llm, 'ToolAnnotations'):
                    assert all(isinstance(tool.annotations, llm.ToolAnnotations) for tool in instance.tools)
            assert tool_count == 17, tool_count
            # Exercise HA's lazy platform discovery, which also discovers llm.py.
            # Named Household Voice APIs must not silently inject duplicate tools
            # or prompts into the default Assist API.
            from homeassistant.components import llm as llm_component
            from custom_components.household_voice import llm as schedule_platform
            assert await async_setup_component(hass, 'llm', {})
            platforms = await hass.data[llm_component.DATA_PLATFORMS].async_get_platforms()
            assert platforms['household_voice'] is schedule_platform
            with patch.object(schedule_platform, 'async_get_tools', wraps=schedule_platform.async_get_tools) as discovery, \
                    patch.object(llm_component._LOGGER, 'exception') as discovery_error:
                assist = await llm.async_get_api(hass, llm.LLM_API_ASSIST, context)
                discovery.assert_called_once_with(hass, context, llm.LLM_API_ASSIST)
                discovery_error.assert_not_called()
            assert not any(tool.name.startswith('household_') for tool in assist.tools)
            assert 'Household Voice' not in assist.api_prompt
            assert len([api for api in llm.async_get_apis(hass) if api.id.startswith('household_voice_')]) == 4
            proof['real_llm_platform_discovery_no_duplicate_assist_tools'] = True
            proof['registered_tool_instances'] = tool_count
            proof['setup_panel_tools_local_authority'] = True
            # Repeated installation cannot introduce a second local writer.
            duplicate = await hass.config_entries.flow.async_init('household_voice', context={'source': SOURCE_USER})
            await hass.config_entries.flow.async_configure(duplicate['flow_id'], {'next_step_id': 'embedded'})
            duplicate = await hass.config_entries.flow.async_configure(duplicate['flow_id'], {'audio_base_url': 'http://ha.example.invalid:8123'})
            assert duplicate['type'] == 'abort' and duplicate['reason'] == 'already_configured'
            proof['duplicate_local_install_rejected'] = True
            runtime = await client.runtime_config()
            await client.configure_runtime({'rooms': {'test_room': {'name': 'Test room', 'satellites': ['fixture_device'], 'capture_profiles': {'fixture_profile': 'fixture_device'}}},
                'alarm_rooms': {}, 'ring_fallback_speakers': [], 'ring_enabled': False}, runtime['revision'])
            timer = await hass.services.async_call('household_voice', 'create_timer', {
                'label': 'Durability fixture', 'seconds': 3600, 'source_room': 'test_room', 'request_id': 'fixture-timer'}, blocking=True, return_response=True)
            timer_id = timer['schedule']['id']
            retry = await client.mutate('create_timer', {'label': 'Durability fixture', 'seconds': 3600, 'source_room': 'test_room', 'request_id': 'fixture-timer'})
            assert retry['schedule']['id'] == timer_id
            alarm = await client.mutate('create_alarm', {'label': 'Alarm fixture', 'local_time': '07:00', 'timezone': 'UTC', 'weekdays': [0,1,2,3,4,5,6], 'source_room': 'test_room'})
            # Original PCM capture + signed/expiring single-recording media route.
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b'\x00\x00' * 3200)
            raw = buffer.getvalue()
            capture = SimpleNamespace(profile_id='fixture_profile', source_device='fixture_device', nonce='fixture_nonce_00000001',
                wav_base64=base64.b64encode(raw).decode(), transcript='Synthetic fixture announcement')
            recording = await client.upload_capture(capture)
            signed = await client.resolve_recording(recording['id'])
            path = urlsplit(signed).path+'?'+urlsplit(signed).query
            http = TestClient(TestServer(hass.http.app))
            await http.start_server()
            async with http.get(path) as response:
                assert response.status == 200
                assert await response.read() == raw
            async with http.get(path, headers={'Range': 'bytes=0-43'}) as response:
                assert response.status == 206 and len(await response.read()) == 44
            async with http.head(path) as response:
                assert response.status == 200 and int(response.headers['Content-Length']) == len(raw)
                assert await response.read() == b''
            async with http.get(urlsplit(signed).path) as response:
                assert response.status == 404
            async with http.get(urlsplit(signed).path+'?expires=1&sig='+'0'*64) as response:
                assert response.status == 404
            async with http.get('/household_voice_static/household-voice-panel.js') as response:
                assert response.status == 200 and 'household-voice-panel' in await response.text()
            proof['signed_audio_original_range_head_and_rejection'] = True
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert 'household-voice' not in hass.data['frontend_panels']
            async with http.get(path) as response:
                assert response.status == 404
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            restored = hass.data['household_voice'][entry.entry_id].client
            rows = (await restored.schedules())['schedules']
            assert {timer_id, alarm['schedule']['id']} <= {row['id'] for row in rows}
            assert (await restored.upload_capture(capture))['id'] == recording['id']
            async with http.get(path) as response:
                assert response.status == 200 and await response.read() == raw
            assert 'household-voice' in hass.data['frontend_panels']
            proof['unload_reload_state_key_capture_dedup_preserved'] = True
            from custom_components.household_voice import backup
            await backup.async_pre_backup(hass)
            assert restored.maintenance_health['error'] == 'backup_in_progress'
            await backup.async_post_backup(hass)
            assert restored.maintenance_health['healthy']
            proof['backup_hooks_quiesce_and_resume'] = True
            assert await hass.config_entries.async_unload(entry.entry_id)
            import custom_components.household_voice.satellite_runtime as satellites
            from homeassistant.config_entries import ConfigEntryState
            async def temporarily_unavailable(*args):
                raise satellites.SatelliteDependencyNotReady('Fixture dependency unavailable')
            with patch.object(satellites, 'async_setup_satellite_runtime', temporarily_unavailable):
                assert await hass.config_entries.async_setup(entry.entry_id) is False
            assert entry.state == ConfigEntryState.SETUP_RETRY
            assert entry.entry_id not in hass.data['household_voice']
            assert await hass.config_entries.async_reload(entry.entry_id)
            assert timer_id in {row['id'] for row in (await hass.data['household_voice'][entry.entry_id].client.schedules())['schedules']}
            assert await hass.config_entries.async_unload(entry.entry_id)
            proof['failed_setup_releases_local_authority_for_retry'] = True
            print(json.dumps({'result': 'passed', **proof}, sort_keys=True), flush=True)
        finally:
            if http:
                await http.close()
            await hass.async_stop(force=True)
            sys.path.remove(str(config))


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), timeout=180))
