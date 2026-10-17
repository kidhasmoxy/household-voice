"""Real Core sentence recognition with an inert recording backend; no house I/O."""
import asyncio
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant import loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import ChatLog
from homeassistant.config_entries import ConfigEntries
from homeassistant.const import __version__
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component


async def main():
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        shutil.copytree(Path(__file__).resolve().parents[1]/'custom_components', Path(directory)/'custom_components')
        sys.path.insert(0, directory)
        hass = HomeAssistant(directory)
        hass.config.skip_pip = True
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        try:
            assert await async_load_base_functionality(hass)
            hass.auth = await auth_manager_from_config(hass, [], [])
            assert await async_setup_component(hass, 'conversation', {})
            from custom_components.household_voice.broadcast_trigger import async_register
            entry = SimpleNamespace(entry_id='fixture', disabled_by=None, options={
                'assist_tools_enabled':True, 'native_recording_enabled':True, 'announcements_enabled':True})
            start = AsyncMock(return_value={'status':'queued'})
            coordinator = SimpleNamespace(broadcast_runtime=SimpleNamespace(async_start=start))
            hass.data['household_voice'] = {'fixture':coordinator}
            remove = async_register(hass, entry, coordinator)
            async def recognize(text, device='fixture-device'):
                request = conversation.ConversationInput(text=text, context=Context(), language='en',
                    conversation_id=None, device_id=device, satellite_id=None, agent_id='fixture-agent')
                return await conversation.async_handle_sentence_triggers(hass, request, ChatLog(hass, request.context.id))
            for text in ('broadcast','Broadcast!','announcement','Announcement.','make an announcement','please broadcast','announce'):
                assert await recognize(text) == '', text
            assert start.await_count == 7
            for text in ('cancel announcement','what is an announcement','broadcast dinner is ready','play broadcast','do not broadcast'):
                assert await recognize(text) is None, text
            assert start.await_count == 7
            assert await recognize('broadcast', None)
            assert start.await_count == 7
            start.return_value = {'status':'error','reason':'recording_worker_unavailable'}
            assert await recognize('announcement')
            assert start.await_count == 8
            remove()
            assert await recognize('broadcast') is None
            print(json.dumps({'ha':__version__, 'positive_phrases':7, 'negative_phrases':5,
                'empty_success_speech':True,'missing_source_rejected':True,'unload_verified':True,
                'real_core_recognition':True,'recording_backend':'inert','physical_audio':False}))
        finally:
            await hass.async_stop(force=True)


if __name__ == '__main__':
    asyncio.run(main())
