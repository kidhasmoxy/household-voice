"""Media tool fixtures; --ha-runtime uses actual HA registries/API/services.

All catalog, queue and action responses are synthetic. Never contacts MA/provider
servers or household HA, and never plays physical media.
"""
from copy import deepcopy
from dataclasses import dataclass
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REAL_HA = "--ha-runtime" in sys.argv


def load_module():
    package = types.ModuleType("household_media_llm_fixture")
    package.__path__ = [str(ROOT / "custom_components/household_voice")]
    sys.modules[package.__name__] = package
    modules = {}
    if not REAL_HA:
        @dataclass
        class ToolResult:
            data: dict
            error: bool = False
        class Object:
            def __init__(self, **values): self.__dict__.update(values)
        class Invalid(ValueError): pass
        @dataclass(frozen=True)
        class Optional:
            key: str
        class Schema:
            def __init__(self, values): self.schema = values
            def __call__(self, values):
                for key, check in self.schema.items():
                    if key.key not in values: continue
                    value = values[key.key]
                    if isinstance(check, type):
                        if not isinstance(value, check): raise Invalid()
                    else: check(value)
                return values
        def inside(allowed):
            def check(value):
                if value not in allowed: raise Invalid()
                return value
            return check
        modules = {name: types.ModuleType(name) for name in ["probatio", "homeassistant", "homeassistant.core",
            "homeassistant.helpers", "homeassistant.helpers.llm", "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_registry", "homeassistant.helpers.area_registry"]}
        p = modules["probatio"]
        p.Schema, p.Optional, p.Invalid, p.In = Schema, Optional, Invalid, inside
        modules["homeassistant.core"].callback = lambda f: f
        l = modules["homeassistant.helpers.llm"]
        l.API, l.APIInstance, l.Tool, l.ToolResult, l.ToolAnnotations = Object, Object, object, ToolResult, Object
        modules["homeassistant.helpers.device_registry"].async_get = lambda hass: hass.devices
        modules["homeassistant.helpers.entity_registry"].async_get = lambda hass: hass.entities
        modules["homeassistant.helpers.area_registry"].async_get = lambda hass: hass.areas
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(package.__name__ + ".media_llm", Path(package.__path__[0]) / "media_llm.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._fixture_scope = sys.modules[package.__name__ + '.intent_scope']
        return module


module = load_module()
scope_module = module._fixture_scope
native_queue_mode_reader = module._GuardedServices.async_read_queue_mode

async def fixture_queue_mode(guard, player):
    return {'queue_id':'fixture-queue', 'is_dynamic':False, 'shuffle_enabled':False, 'repeat_mode':'off', 'autoplay_enabled':False,
        'queue_item_id':'fixture-item', 'items':1, 'source_uris':['fixture://track/1'],
        'current_provider_uri':'fixture://track/1'}
PLAYER = "media_player.fixture_ma"
TTS = "media_player.fixture_sonos"
BOOK = "library://audiobook/101"


def item(name="Fixture Book", kind="audiobook", uri=BOOK):
    return {"media_type": kind, "uri": uri, "name": name, "version": "", "artists": [], "album": None}


def search(value=None, group="audiobooks"):
    return {key: [value or item()] if key == group else [] for key in ("artists", "albums", "tracks", "playlists", "radio", "audiobooks", "podcasts")}


def queue(kind="audiobook"):
    current = item() if kind == "audiobook" else item("Fixture Music", "track", "library://track/55")
    return {PLAYER: {"queue_id": "fixture-queue", "active": True, "name": "Fixture", "items": 1,
        "shuffle_enabled": False, "repeat_mode": "off", "current_index": 0, "elapsed_time": 100,
        "current_item": {"queue_item_id": "q1", "name": current["name"], "media_item": current}, "next_item": None}}


class Registry:
    def __init__(self, values): self.values = values
    def async_get(self, key): return self.values.get(key)
    def async_get_entry(self, key): return self.values.get(key)
    def async_get_area(self, key): return self.values.get(key)


class Services:
    def __init__(self):
        self.calls, self.search_result, self.queue_result = [], search(), queue()
        self.fail, self.after_search = None, None
    def has_service(self, domain, service): return True
    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, deepcopy(data), kwargs))
        await asyncio.sleep(0)
        if self.fail == service: raise RuntimeError("secret http://private.invalid/token")
        if service == "search":
            if self.after_search: self.after_search()
            return deepcopy(self.search_result)
        if service == "get_queue": return deepcopy(self.queue_result)


def options():
    return {"media_rooms": [{"room_id": "kitchen", "music_player": PLAYER, "config_entry_id": "ma-entry",
        "username": "configured-user", "default_sleep_playlist": "Configured Sleep Playlist"}],
        "satellites": [{"satellite_device_id": "source-device", "room_id": "kitchen", "tts_media_player": TTS}],
        "live_actions_enabled": False, "media_enabled": False}


class MediaLLMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        queue_patch = patch.object(module._GuardedServices, 'async_read_queue_mode', fixture_queue_mode)
        queue_patch.start()
        self.addCleanup(queue_patch.stop)
        self.entry = types.SimpleNamespace(entry_id="voice-entry", title="Fixture", options=options())
        self.services = Services()
        self.hass = types.SimpleNamespace(services=self.services,
            areas=Registry({}),
            devices=Registry({"source-device": types.SimpleNamespace(disabled_by=None)}),
            entities=Registry({PLAYER: types.SimpleNamespace(disabled_by=None, platform="music_assistant", config_entry_id="ma-entry"),
                               TTS: types.SimpleNamespace(disabled_by=None, platform="sonos", config_entry_id="sonos-entry")}),
            states=types.SimpleNamespace(get=lambda entity: types.SimpleNamespace(state="idle")),
            config_entries=Registry({"ma-entry": types.SimpleNamespace(domain="music_assistant", disabled_by=None)}))
        self.api = module.HouseholdMediaAPI(self.hass, self.entry)
        self.context = types.SimpleNamespace(device_id="source-device", context=types.SimpleNamespace(id="context-id", user_id=None))
        self.sequence = 0
    async def call(self, operation="play", args=None, call_id=None, context=None):
        self.sequence += 1
        tool = module.MediaTool("household_media_" + operation, self.api)
        return await tool.async_call(self.hass, types.SimpleNamespace(tool_args=args or {}, id=call_id or f"call-{self.sequence}"), context or self.context)

    async def test_registered_target_trusted_profile_and_dry_run(self):
        result = await self.call(args={"query": "Fixture Book", "kind": "book"})
        self.assertEqual(result.data["status"], "dry_run")
        self.assertFalse(result.data["executed"])
        self.assertEqual([c[1] for c in self.services.calls], ["search"])
        self.assertEqual(self.services.calls[0][2]["username"], "configured-user")
        self.assertEqual(self.services.calls[0][2]["config_entry_id"], "ma-entry")
        self.assertIs(self.services.calls[0][3]["context"], self.context.context)
        self.assertNotIn("actions", result.data)
        self.assertNotIn("username", result.data)

    async def test_native_queue_reader_binds_entity_instance_and_active_queue(self):
        self.hass.entities.values[PLAYER].unique_id = 'native-player'
        signature = module._resolve(self.hass, self.entry, self.context)[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        player = types.SimpleNamespace(active_source='active-queue')
        calls = []
        async def send(command, **args):
            calls.append((command,args))
            return {'queue_id':'active-queue','is_dynamic':True,'shuffle_enabled':True,'repeat_mode':'off',
                'autoplay_enabled':False, 'items':1, 'sources':[],
                'current_item':{'queue_item_id':'item-one','media_item':{'uri':'fixture-domain://track/7'},
                    'streamdetails':{'provider':'fixture-instance','item_id':'7','media_type':'track'}}}
        client = types.SimpleNamespace(send_command=send,
            players=Registry({'native-player':player}),
            player_queues=Registry({'active-queue':types.SimpleNamespace(queue_id='active-queue')}))
        client.players.get = client.players.async_get
        client.player_queues.get = client.player_queues.async_get
        helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
        helper.get_music_assistant_client = lambda hass, entry_id: client
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}):
            result = await native_queue_mode_reader(guard, PLAYER)
            self.assertTrue(result['is_dynamic'])
            self.assertEqual(result['current_provider_uri'], 'fixture-instance://track/7')
            self.assertEqual(calls, [('player_queues/get',{'queue_id':'active-queue'})])
            async def changed(command, **args):
                player.active_source = 'different-queue'
                return {'queue_id':'active-queue','is_dynamic':True,'shuffle_enabled':True,'repeat_mode':'off'}
            client.send_command = changed
            with self.assertRaises(module.MediaConfigurationError):
                await native_queue_mode_reader(guard, PLAYER)

    async def test_artist_playlist_and_mix_dispatch_each_get_one_execution_budget(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        for data in ({'media_id':['library://track/1','library://track/2'], 'radio_mode':False},
                     {'media_id':'library://playlist/1', 'radio_mode':False},
                     {'media_id':'library://track/1', 'radio_mode':True}):
            with self.subTest(data=data):
                deadlines = []
                guard = module._GuardedServices(self.api, self.context, 'kitchen', signature,
                    execution_deadline=types.SimpleNamespace(reschedule=deadlines.append))
                await guard.async_call('music_assistant', 'search', {})
                await guard.async_call('media_player', 'shuffle_set', {'shuffle':False})
                self.assertEqual(deadlines, [])
                self.assertFalse(guard.playback_dispatch_started)
                before = asyncio.get_running_loop().time()
                await guard.async_call('music_assistant', 'play_media', data)
                after = asyncio.get_running_loop().time()
                self.assertTrue(guard.playback_dispatch_started)
                self.assertEqual(len(deadlines), 1)
                self.assertGreaterEqual(deadlines[0], before + 65)
                self.assertLessEqual(deadlines[0], after + 65)
                await guard.async_call('music_assistant', 'play_media', data)
                self.assertEqual(len(deadlines), 1)

    async def test_autoplay_hook_rejects_changed_queue_seed_and_failed_readback(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        expected = await fixture_queue_mode(guard, PLAYER)
        helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
        for scenario in ('valid', 'changed_queue', 'wrong_seed', 'failed_readback', 'changed_config'):
            with self.subTest(scenario=scenario):
                mode = deepcopy(expected)
                calls = []
                if scenario == 'changed_queue': mode['queue_item_id'] = 'new-user-item'
                async def reader(player): return deepcopy(mode)
                guard.async_read_queue_mode = reader
                async def send(command, **args):
                    calls.append((command,args))
                    if scenario != 'failed_readback': mode['autoplay_enabled'] = args['autoplay_enabled']
                helper.get_music_assistant_client = lambda hass, entry_id: types.SimpleNamespace(send_command=send)
                if scenario == 'changed_config': self.entry.options['media_rooms'][0]['username'] = 'other-user'
                with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}):
                    if scenario == 'valid':
                        self.assertTrue(await guard.async_set_queue_autoplay(PLAYER,expected,True,expected_source_uri='fixture://track/1'))
                    else:
                        with self.assertRaises(module.MediaConfigurationError):
                            await guard.async_set_queue_autoplay(PLAYER,expected,True,
                                expected_source_uri='fixture://track/2' if scenario=='wrong_seed' else 'fixture://track/1')
                self.entry.options['media_rooms'][0]['username'] = 'configured-user'
                self.assertEqual(len(calls), 1 if scenario in ('valid','failed_readback') else 0)

    async def test_changed_configuration_cannot_extend_mix_deadline_or_dispatch(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        deadlines = []
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature,
            execution_deadline=types.SimpleNamespace(reschedule=deadlines.append))
        self.entry.options['media_rooms'][0]['username'] = 'changed-user'
        with self.assertRaises(module.MediaConfigurationError):
            await guard.async_call('music_assistant', 'play_media', {'radio_mode': True})
        self.assertEqual(deadlines, [])
        self.assertEqual(self.services.calls, [])
        self.assertFalse(guard.playback_dispatch_started)

    async def test_action_timeout_returns_uncertainty_and_same_call_cannot_replay(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        original_call = self.services.async_call
        async def slow_play(domain, service, data, **kwargs):
            result = await original_call(domain, service, data, **kwargs)
            if service == 'play_media':
                await asyncio.sleep(1)
            return result
        self.services.async_call = slow_play
        timeout = asyncio.timeout
        class ShortTimeout:
            def __init__(self, seconds): self.timer = timeout(.1)
            async def __aenter__(self):
                await self.timer.__aenter__()
                return self
            async def __aexit__(self, *exc): return await self.timer.__aexit__(*exc)
            def reschedule(self, deadline): self.timer.reschedule(asyncio.get_running_loop().time() + .1)
        args = {'query': 'Fixture Playlist', 'kind': 'music', 'media_type':'playlist'}
        self.services.search_result = search(item('Fixture Playlist','playlist','library://playlist/1'),'playlists')
        with patch.object(module.asyncio, 'timeout', side_effect=ShortTimeout):
            first = await self.call(args=args, call_id='timed-out-action')
        self.assertEqual(first.data['reason'], 'media_playback_pending_or_uncertain')
        self.assertTrue(first.error)
        self.assertTrue(first.data['playback_dispatch_started'])
        self.assertFalse(first.data['executed'])
        self.assertFalse(first.data['playback_confirmed'])
        self.assertFalse(first.data['retry_automatically'])
        self.assertIn('may still be preparing', first.data['message'])
        self.assertIn('Do not retry', first.data['response_instruction'])
        instance = await self.api.async_get_api_instance(self.context)
        self.assertIn('Never retry that playback or claim no matching music was found', instance.api_prompt)
        self.assertNotIn('announcement', first.data)
        second = await self.call(args=args, call_id='timed-out-action')
        self.assertEqual(first.data, second.data)
        self.assertEqual(sum(call[1] == 'play_media' for call in self.services.calls), 1)

    async def test_lookup_retains_35_second_budget_and_does_not_claim_dispatch(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        async def slow_search(*args, **kwargs): await asyncio.sleep(1)
        self.services.async_call = slow_search
        timeout = asyncio.timeout
        budgets = []
        def short_lookup(seconds):
            budgets.append(seconds)
            return timeout(.05)
        with patch.object(module.asyncio, 'timeout', side_effect=short_lookup):
            result = await self.call(args={'query':'Fixture Book','kind':'book'})
        self.assertEqual(budgets, [35])
        self.assertEqual(result.data['reason'], 'media_unavailable_or_result_uncertain')
        self.assertNotIn('playback_dispatch_started', result.data)

    async def test_music_lookup_deadline_has_no_mutations_and_caches_failure(self):
        cancelled = asyncio.Event()
        async def slow_search(*args, **kwargs):
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.set()
        self.services.async_call = slow_search
        args = {'query':'Adele', 'kind':'music'}
        start = asyncio.get_running_loop().time()
        with patch.object(module, 'MUSIC_FOREGROUND_SECONDS', .1):
            result = await self.call(args=args, call_id='slow-lookup')
        self.assertLess(asyncio.get_running_loop().time() - start, .5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.data['reason'], 'media_provider_timeout')
        self.assertEqual(result.data['timeout_phase'], 'catalog_lookup')
        self.assertFalse(result.data['mutation_dispatch_started'])
        self.assertFalse(result.data['playback_dispatch_started'])
        self.assertNotIn('announcement', result.data)
        self.assertEqual((await self.call(args=args, call_id='slow-lookup')).data, result.data)

    async def test_artist_dependency_timeout_preserves_nonsecret_phase(self):
        self.assertEqual(module.MUSIC_FOREGROUND_SECONDS, 4.5)
        self.services.search_result = search(item('Adele','artist','fixture://artist/1'),'artists')
        async def slow_candidates(guard, *args):
            guard.phase = 'artist_search'
            await asyncio.sleep(1)
        with patch.object(module._GuardedServices, 'async_read_artist_candidates', slow_candidates), \
             patch.object(module, 'MUSIC_FOREGROUND_SECONDS', .1):
            result = await self.call(args={'query':'Adele','kind':'music'})
        self.assertEqual(result.data['reason'], 'media_provider_timeout')
        self.assertEqual(result.data['timeout_phase'], 'artist_search')
        self.assertEqual([c[1] for c in self.services.calls], ['search'])

    async def test_music_deadline_includes_lock_wait_and_never_replays_timed_out_waiter(self):
        args = {'query':'Adele', 'kind':'music'}
        await self.api.call_lock.acquire()
        try:
            with patch.object(module, 'MUSIC_FOREGROUND_SECONDS', .1):
                result = await self.call(args=args, call_id='waiting')
        finally:
            self.api.call_lock.release()
        self.assertEqual(result.data['reason'], 'media_provider_timeout')
        self.assertEqual(result.data['timeout_phase'], 'waiting_for_media_lock')
        self.assertEqual(self.services.calls, [])
        self.assertEqual((await self.call(args=args, call_id='waiting')).data, result.data)
        self.assertEqual(self.services.calls, [])

    async def test_music_lock_wait_does_not_reset_lookup_budget(self):
        async def slow_search(*args, **kwargs): await asyncio.sleep(1)
        self.services.async_call = slow_search
        await self.api.call_lock.acquire()
        start = asyncio.get_running_loop().time()
        with patch.object(module, 'MUSIC_FOREGROUND_SECONDS', .15):
            task = asyncio.create_task(self.call(args={'query':'Adele','kind':'music'}))
            await asyncio.sleep(.08)
            self.api.call_lock.release()
            result = await task
        self.assertEqual(result.data['timeout_phase'], 'catalog_lookup')
        self.assertLess(asyncio.get_running_loop().time() - start, .21)

    async def test_music_dispatch_and_queue_control_share_fixed_budget(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Fixture Playlist','playlist','library://playlist/1'),'playlists')
        original_call = self.services.async_call
        for slow_service, phase, played in [('shuffle_set','queue_control',False), ('play_media','playback_dispatch',True)]:
            async def slow(domain, service, data, **kwargs):
                result = await original_call(domain, service, data, **kwargs)
                if service == slow_service:
                    await asyncio.sleep(1)
                return result
            self.services.async_call = slow
            start = asyncio.get_running_loop().time()
            with patch.object(module, 'MUSIC_FOREGROUND_SECONDS', .1):
                result = await self.call(args={'query':'Fixture Playlist','kind':'music'}, call_id=slow_service)
            self.assertLess(asyncio.get_running_loop().time() - start, .5)
            self.assertEqual(result.data['reason'], 'media_playback_pending_or_uncertain')
            self.assertEqual(result.data['timeout_phase'], phase)
            self.assertTrue(result.data['mutation_dispatch_started'])
            self.assertEqual(result.data['playback_dispatch_started'], played)
            self.assertFalse(result.data['playback_confirmed'])
            self.assertNotIn('announcement', result.data)
            self.assertFalse(self.api.call_lock.locked())

    async def test_external_cancellation_keeps_reservation_and_duplicate_cannot_cancel_original(self):
        entered = asyncio.Event()
        async def slow_search(*args, **kwargs):
            entered.set()
            await asyncio.sleep(10)
        self.services.async_call = slow_search
        args = {'query':'Adele','kind':'music'}
        first = asyncio.create_task(self.call(args=args, call_id='cancelled'))
        await entered.wait()
        duplicate = asyncio.create_task(self.call(args=args, call_id='cancelled'))
        await asyncio.sleep(0)
        duplicate.cancel()
        with self.assertRaises(asyncio.CancelledError): await duplicate
        self.assertFalse(first.done())
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        result = await self.call(args=args, call_id='cancelled')
        self.assertEqual(result.data['reason'], 'media_result_uncertain')
        self.assertFalse(self.api.pending)
        self.assertFalse(self.api.call_lock.locked())

    async def test_duplicate_deadline_does_not_cancel_or_replay_original(self):
        entered = asyncio.Event()
        async def slow_search(*args, **kwargs):
            entered.set()
            await asyncio.sleep(10)
        self.services.async_call = slow_search
        args = {'query':'Adele','kind':'music'}
        first = asyncio.create_task(self.call(args=args, call_id='in-flight'))
        await entered.wait()
        with patch.object(module, 'MUSIC_FOREGROUND_SECONDS', .1):
            duplicate = await self.call(args=args, call_id='in-flight')
        self.assertEqual(duplicate.data['timeout_phase'], 'waiting_for_existing_call')
        self.assertFalse(first.done())
        self.assertEqual(len(self.api.pending), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        self.assertFalse(self.api.pending)

    async def test_pending_reservations_are_bounded_and_survive_completed_cache_churn(self):
        future = asyncio.get_running_loop().create_future()
        self.api.pending['active'] = (('signature','config'), future)
        for index in range(300):
            self.api.complete_call(f'complete-{index}', ('signature','config'), {'status':'dry_run'}, False)
        self.assertEqual(len(self.api.completed), 256)
        self.assertIs(self.api.pending['active'][1], future)
        for index in range(255):
            self.api.pending[f'pending-{index}'] = (('signature','config'), asyncio.get_running_loop().create_future())
        result = await self.call(args={'query':'Adele','kind':'music'})
        self.assertEqual(result.data['timeout_phase'], 'media_admission')
        self.assertEqual(len(self.api.pending), 256)
        self.assertEqual(self.services.calls, [])

    async def test_shuffle_matilda_soundtrack_uses_album_and_explicit_shuffle(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        title = "Roald Dahl's Matilda The Musical (Soundtrack from the Netflix Film)"
        self.services.search_result = search(item(title, 'album', 'fixture://album/matilda'), 'albums')
        self.context.user_prompt = 'Shuffle the Matilda soundtrack.'
        self.context.language = 'en'
        mode = await fixture_queue_mode(None, PLAYER)
        commands = []
        async def read_mode(guard, player): return deepcopy(mode)
        async def authorize(guard, uri):
            self.assertEqual(uri, 'fixture://album/matilda')
            return True
        async def send(command, **args):
            commands.append((command, args))
            mode['shuffle_enabled'] = True
        helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
        helper.get_music_assistant_client = lambda hass, entry: types.SimpleNamespace(send_command=send)
        # Even a model dropping soundtrack or shuffle cannot lose these explicit
        # parts of the original request on this recognized route.
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}), \
                patch.object(module._GuardedServices, 'async_read_queue_mode', read_mode), \
                patch.object(module._GuardedServices, 'async_authorize_provider_uri', authorize):
            result = await self.call(args={'query':'Matilda', 'kind':'music'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(result.data['selected']['title'], title)
        self.assertFalse(any(service == 'play_media' for _, service, _, _ in self.services.calls))
        self.assertEqual(commands, [('player_queues/play_media', {
            'queue_id':'fixture-queue', 'media':['fixture://album/matilda'],
            'option':'replace', 'radio_mode':False, 'shuffle':True,
            'user':'configured-user', 'require_schema':35})])

    async def test_native_shuffled_album_rejects_unauthorized_uri_and_queue_change(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        data = {'entity_id':PLAYER, 'media_id':'fixture://album/matilda', 'media_type':'album',
                'username':'configured-user', 'enqueue':'replace', 'radio_mode':False, 'shuffle':True}
        for scenario in ('denied', 'changed_queue', 'wrong_user', 'wrong_target', 'book'):
            guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
            current = deepcopy(data)
            if scenario == 'wrong_user': current['username'] = 'other'
            if scenario == 'wrong_target': current['entity_id'] = 'media_player.other'
            if scenario == 'book': current['media_type'] = 'audiobook'
            reads = 0
            async def read_mode(player):
                nonlocal reads
                reads += 1
                mode = await fixture_queue_mode(None, PLAYER)
                if scenario == 'changed_queue' and reads > 1: mode['queue_id'] = 'other'
                return mode
            async def authorize(uri): return scenario != 'denied'
            guard.async_read_queue_mode, guard.async_authorize_provider_uri = read_mode, authorize
            with self.assertRaises(module.MediaConfigurationError):
                await guard.async_play_shuffled_album(current)
            self.assertFalse(guard.playback_dispatch_started)

    async def test_shuffle_is_a_play_option_and_cannot_shuffle_a_book(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search()
        result = await self.call(args={'query':'Fixture Book', 'kind':'book', 'shuffle':True})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertTrue(any(service == 'shuffle_set' and data.get('shuffle') is False
                            for _, service, data, _ in self.services.calls))
        self.assertFalse(any(service == 'shuffle_set' and data.get('shuffle') is True
                             for _, service, data, _ in self.services.calls))

    async def test_default_sleep_playlist_context_factory(self):
        self.services.search_result = search(item("Configured Sleep Playlist", "playlist", "library://playlist/12"), "playlists")
        result = await self.call(args={"kind": "sleep_stories"})
        self.assertEqual(result.data["status"], "dry_run")
        self.assertEqual(self.services.calls[0][2]["name"], "Configured Sleep Playlist")

    async def test_original_broad_music_request_selects_relevant_fresh_playlist(self):
        self.context.user_prompt = 'Play some jazz.'
        self.context.language = 'en'
        self.services.search_result = search(item('Jazz Chill', 'playlist', 'library://playlist/42'), 'playlists')
        result = await self.call(args={'query': 'jazz', 'kind': 'music'})
        self.assertEqual(result.data['status'], 'dry_run')
        self.assertEqual(result.data['selected']['title'], 'Jazz Chill')
        self.assertEqual(self.services.calls[0][2]['media_type'], ['playlist', 'radio'])
        self.assertEqual(result.data['evidence_policy'], 'broad_catalog_recommendation_not_exact_title')

    async def test_model_cannot_grant_broad_selection_for_a_named_title(self):
        self.context.user_prompt = 'Play the album Jazz.'
        self.services.search_result = search(item('Jazz Chill', 'playlist', 'library://playlist/42'), 'playlists')
        result = await self.call(args={'query': 'jazz', 'kind': 'music'})
        self.assertEqual(result.data['reason'], 'no_exact_catalog_match')
        result = await self.call(args={'query': 'jazz', 'selection_mode': 'broad_music'})
        self.assertTrue(result.error)

    async def test_polite_genre_variations_keep_source_bound_selection(self):
        self.services.search_result = search(item('Jazz Chill', 'playlist', 'fixture://playlist/42'), 'playlists')
        for prompt in ('Could you play some jazz please?', 'Put on some jazz.', 'Please play me some jazz.'):
            with scope_module.bind_request(prompt, 'en', self.context.context, self.context.device_id):
                result = await self.call(args={'query':'jazz','kind':'music'})
            self.assertEqual(result.data['selected']['title'], 'Jazz Chill')
            self.assertEqual(result.data['playback_kind'], 'collection')

    async def test_common_moods_and_room_phrasing_select_relevant_playlists(self):
        samples = [
            ('Play some upbeat music.', 'upbeat', 'Upbeat Favorites'),
            ('Put on relaxing music.', 'relaxing', 'Relaxing Evening'),
            ('Play jazz in the living room.', 'jazz', 'Jazz Chill'),
            *[(f'Play some {mood} music.', mood, f'{mood.title()} Favorites')
              for mood in ('happy', 'party', 'workout', 'focus', 'dinner', 'romantic', 'calming')],
        ]
        for prompt, mood, title in samples:
            with self.subTest(prompt=prompt):
                self.services.search_result = search(item(title, 'playlist', 'fixture://playlist/42'), 'playlists')
                with scope_module.bind_request(prompt, 'en', self.context.context, self.context.device_id):
                    result = await self.call(args={'query':mood, 'kind':'music'})
                self.assertEqual(result.data['selected']['title'], title)
                self.assertEqual(self.services.calls[-1][2]['media_type'], ['playlist', 'radio'])
                self.assertEqual(result.data['room'], 'kitchen')

    async def test_mood_words_do_not_relax_named_artist_album_or_spoken_requests(self):
        for prompt, args in [
            ('Play the album Happy.', {'query':'happy', 'media_type':'album'}),
            ('Play the artist Party.', {'query':'party', 'media_type':'artist'}),
            ('Play the audiobook Focus.', {'query':'focus', 'kind':'book'}),
            ('Play the sleep story Romantic.', {'query':'romantic', 'kind':'sleep_story'}),
            ('Play Happy Together.', {'query':'happy'}),
            ('Play some upbeat music.', {'query':'upbeat', 'selected_uri':'fixture://playlist/42'}),
        ]:
            with self.subTest(prompt=prompt):
                with scope_module.bind_request(prompt, 'en', self.context.context, self.context.device_id):
                    self.assertNotIn('selection_mode', module._with_music_selection(args, self.context, 'play'))

    async def test_artist_reader_uses_selected_provider_and_rechecks_routing(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Fleetwood Mac','artist','fixture://artist/7'), 'artists')
        calls = []
        async def send_command(command, **kwargs):
            calls.append((command, kwargs))
            if command == 'music/verify_item_uri': return True
            if command == 'music/search':
                artist = item('Fleetwood Mac','artist','fixture://artist/7')
                artist['provider_mappings'] = [{'provider_instance':'fixture','available':True,'item_id':'7'}]
                return {'artists': [artist]}
            track = item('Fixture Song', 'track', 'fixture://track/9')
            track['provider_mappings'] = [{'provider_instance':'fixture','available':True}]
            return [track]
        helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
        provider = types.SimpleNamespace(instance_id='fixture', domain='fixture', available=True,
                                         supported_features=['artist_toptracks'])
        helper.get_music_assistant_client = lambda hass, entry_id: types.SimpleNamespace(send_command=send_command, providers=[provider])
        from unittest.mock import patch
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers': helper}):
            result = await self.call(args={'query':'Fleetwood Mac','media_type':'artist'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(result.data['playback_kind'], 'artist_top_tracks')
        self.assertEqual(result.data['track_count'], 1)
        self.assertEqual(result.data['announcement'], 'Requested top tracks by Fleetwood Mac.')
        self.assertEqual(calls, [('music/search', {'search_query':'Fleetwood Mac', 'media_types':['artist'],
            'providers':['library','fixture'], 'limit':20, 'user':'configured-user','require_schema':35}),
            ('music/verify_item_uri', {'uri':'fixture://artist/7','user':'configured-user','require_schema':35}),
            ('music/artists/top_tracks', {'item_id':'7',
            'provider_instance_id_or_domain':'fixture','provider_filter':'fixture'})])
        self.assertEqual(self.services.calls[-1][2]['media_id'], ['fixture://track/9'])
        async def changed(command, **kwargs):
            self.entry.options['media_rooms'][0]['username'] = 'changed-user'
            return []
        helper.get_music_assistant_client = lambda hass, entry_id: types.SimpleNamespace(send_command=changed, providers=[provider])
        before = len(self.services.calls)
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers': helper}):
            result = await self.call(args={'query':'Fleetwood Mac','media_type':'artist'})
        self.assertEqual(result.data['status'], 'error')
        self.assertEqual([call[1] for call in self.services.calls[before:]], ['search'])

    async def test_genre_raw_search_is_profile_scoped_and_uses_supported_provider_mapping(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search()
        calls = []
        mode = await fixture_queue_mode(None, PLAYER)
        mode['source_uris'] = ['fixture-instance://track/9']
        mode['current_provider_uri'] = 'fixture-instance://track/9'
        async def queue_mode(guard, player): return deepcopy(mode)
        async def send_command(command, **kwargs):
            calls.append((command, kwargs))
            if command == 'music/verify_item_uri': return True
            if command == 'player_queues/autoplay':
                mode['autoplay_enabled'] = kwargs['autoplay_enabled']
                return None
            raw = item('Fixture Instrumental', 'track', 'fixture-instance://track/9')
            raw.update(metadata={'genres':['Jazz']}, provider_mappings=[
                {'provider_instance':'fixture-instance','available':True,'item_id':'9'}])
            return {'tracks':[raw]}
        helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
        provider = types.SimpleNamespace(instance_id='fixture-instance', available=True,
                                         supported_features=['similar_tracks'])
        helper.get_music_assistant_client = lambda hass, entry_id: types.SimpleNamespace(send_command=send_command, providers=[provider])
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}), patch.object(module._GuardedServices, 'async_read_queue_mode', queue_mode):
            with scope_module.bind_request('Put on some jazz.', 'en', self.context.context, self.context.device_id):
                result = await self.call(args={'query':'jazz'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(result.data['announcement'], 'Requested Fixture Instrumental, with more music to follow.')
        self.assertEqual(calls, [('music/search', {'search_query':'jazz','media_types':['track'],
            'providers':['library','fixture-instance'],'limit':20,'user':'configured-user','require_schema':35}),
            ('music/verify_item_uri', {'uri':'fixture-instance://track/9','user':'configured-user','require_schema':35}),
            ('player_queues/autoplay', {'queue_id':'fixture-queue','autoplay_enabled':True})])
        self.assertEqual(self.services.calls[-1][2]['media_id'], 'fixture-instance://track/9')
        self.assertFalse(self.services.calls[-1][2]['radio_mode'])

    async def test_domain_artist_is_resolved_to_allowed_instance_before_top_tracks(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Fleetwood Mac','artist','fixture-domain://artist/7'), 'artists')
        calls = []
        provider = types.SimpleNamespace(instance_id='fixture-instance',domain='fixture-domain',
            available=True,supported_features=['artist_toptracks'])
        async def send_command(command, **kwargs):
            calls.append((command, kwargs))
            if command == 'music/verify_item_uri': return True
            kind = 'artist' if command == 'music/search' else 'track'
            raw = item('Fleetwood Mac' if kind == 'artist' else 'Fixture Song',kind,f'fixture-domain://{kind}/7')
            raw['provider_mappings'] = [{'provider_instance':'fixture-instance','provider_domain':'fixture-domain',
                                        'item_id':'7','available':True}]
            return {'artists':[raw]} if kind == 'artist' else [raw]
        helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
        helper.get_music_assistant_client = lambda hass, entry_id: types.SimpleNamespace(send_command=send_command,providers=[provider])
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}):
            result = await self.call(args={'artist':'Fleetwood Mac','media_type':'artist'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(calls[0][1]['user'], 'configured-user')
        self.assertEqual(calls[0][1]['providers'], ['library','fixture-instance'])
        self.assertEqual(calls[2][1]['provider_filter'], 'fixture-instance')
        self.assertEqual(self.services.calls[-1][2]['media_id'], ['fixture-instance://track/7'])

    async def test_failed_play_does_not_speak_a_success_announcement(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Jazz Chill','playlist','fixture://playlist/1'),'playlists')
        self.services.fail = 'play_media'
        with scope_module.bind_request('Play some jazz.', 'en', self.context.context, self.context.device_id):
            result = await self.call(args={'query':'jazz'})
        self.assertEqual(result.data['status'], 'error')
        self.assertNotIn('announcement', result.data)

    async def test_promptless_router_context_executes_broad_music_with_live_shaped_metadata(self):
        self.living_area()
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Jazz Chill','playlist','apple_music--fixture://playlist/pl.fixture'), 'playlists')
        self.services.search_result['playlists'] += [item(f'Jazz Mix {i}','playlist',f'apple_music://playlist/{i}') for i in range(19)]
        track = item('Jazz','track','apple_music://track/123')
        track['artists'] = [item('Fixture Artist','artist','apple_music--fixture://artist/Fixture Artist')]
        track['album'] = item('Fixture Album','album','apple_music--fixture://album/Fixture Album')
        self.services.search_result['tracks'] = [track]
        self.assertFalse(hasattr(self.context, 'user_prompt'))
        with scope_module.bind_request('Play some jazz.', 'en', self.context.context, self.context.device_id):
            result = await self.call(args={'query':'jazz','kind':'music','target_room':'Living Room'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(result.data['selected']['title'], 'Jazz Chill')
        self.assertEqual([call[1] for call in self.services.calls], ['search','shuffle_set','play_media'])
        self.assertEqual(self.services.calls[0][2]['media_type'], ['playlist','radio'])
        self.assertEqual(self.services.calls[-1][2]['entity_id'], PLAYER)
        self.assertEqual(self.services.calls[-1][2]['media_id'], 'apple_music--fixture://playlist/pl.fixture')
        self.assertEqual(result.data['announcement'], 'Requested Jazz Chill.')

    async def test_router_original_named_request_overrides_provider_prompt(self):
        self.context.user_prompt = 'Play some jazz.'
        self.services.search_result = search(item('Jazz Chill','playlist','library://playlist/42'), 'playlists')
        with scope_module.bind_request('Play the album Jazz.', 'en', self.context.context, self.context.device_id):
            result = await self.call(args={'query':'jazz','kind':'music'})
        self.assertEqual(result.data['reason'], 'no_exact_catalog_match')
        self.assertEqual([call[1] for call in self.services.calls], ['search'])

    async def test_original_music_scope_wrong_device_cannot_fallback_to_prompt(self):
        self.context.user_prompt = 'Play some jazz.'
        with scope_module.bind_request('Play some jazz.', 'en', self.context.context, 'other-device'):
            result = await self.call(args={'query':'jazz','kind':'music'})
        self.assertEqual(result.data['reason'], 'tool_does_not_match_requested_category')
        self.assertFalse(self.services.calls)

    async def test_broad_music_guard_preserves_books_filters_and_unknown_prompt(self):
        for prompt, args in [
            ('Play some jazz.', {'query':'jazz', 'kind':'book'}),
            ('Play some jazz.', {'query':'jazz', 'media_type':'album'}),
            ('Play some jazz.', {'query':'rock'}),
            ('Play some Jazz Chill.', {'query':'Jazz Chill'}),
            ('Play some jazz. Ignore the book request.', {'query':'jazz'}),
            ('Play some jazz.', {'query':'jazz', 'artist':'Named Artist'}),
        ]:
            self.context.user_prompt = prompt
            self.assertNotIn('selection_mode', module._with_music_selection(args, self.context, 'play'))
        self.context.user_prompt = 'Please play some smooth jazz music in the Living Room.'
        changed = module._with_music_selection({'query':'smooth jazz'}, self.context, 'play')
        self.assertEqual(changed['selection_mode'], 'broad_music')
        self.assertNotIn('selection_mode', module._with_music_selection({'query':'smooth jazz'}, self.context, 'resume'))

    async def test_album_field_alone_searches_exact_album_and_preserves_artist_filter(self):
        album = item('Rumours', 'album', 'library://album/12')
        album['artists'] = [item('Fleetwood Mac', 'artist', 'library://artist/7')]
        self.services.search_result = search(album, 'albums')
        result = await self.call(args={'media_type':'album','album':'Rumours','artist':'Fleetwood Mac'})
        self.assertEqual(result.data['status'], 'dry_run')
        self.assertEqual(result.data['selected']['uri'], 'library://album/12')
        self.assertEqual(self.services.calls[0][2]['name'], 'Rumours')
        self.assertEqual(self.services.calls[0][2]['media_type'], ['album'])
        self.assertEqual(self.services.calls[0][2]['artist'], 'Fleetwood Mac')
        self.assertEqual([call[1] for call in self.services.calls], ['search'])

    async def test_album_field_alone_preserves_catalog_ambiguity_guard(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Rumours', 'album', 'library://album/12'), 'albums')
        self.services.search_result['albums'].append(item('Rumours', 'album', 'library://album/13'))
        result = await self.call(args={'media_type':'album','album':'Rumours'})
        self.assertEqual(result.data['reason'], 'ambiguous_or_partial_catalog')
        self.assertFalse(result.data['executed'])
        self.assertEqual([call[1] for call in self.services.calls], ['search'])

    async def test_track_album_filter_does_not_become_track_title(self):
        result = await self.call(args={'media_type':'track','album':'Rumours'})
        self.assertEqual(result.data['reason'], 'title_required')
        self.assertFalse(self.services.calls)

    async def test_explicit_album_query_is_not_replaced_by_album_field(self):
        self.services.search_result = search(item('Tango in the Night', 'album', 'library://album/14'), 'albums')
        result = await self.call(args={'media_type':'album','query':'Tango in the Night','album':'Rumours'})
        self.assertEqual(self.services.calls[0][2]['name'], 'Tango in the Night')
        self.assertEqual(result.data['selected']['uri'], 'library://album/14')

    async def test_named_album_resume_does_not_resume_unrelated_room_queue(self):
        self.services.search_result = search(item('Rumours', 'album', 'library://album/12'), 'albums')
        result = await self.call('resume', {'media_type':'album','album':'Rumours'})
        self.assertEqual(result.data['status'], 'dry_run')
        self.assertEqual([call[1] for call in self.services.calls], ['search'])

    async def test_router_book_constraint_never_resumes_verified_music_queue(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.queue_result = queue('track')
        async def owner(*args):
            return 'configured-user'
        self.api = module.HouseholdMediaAPI(self.hass, self.entry, resolve_queue_profile=owner)
        with scope_module.bind_request('Continue my audiobook', 'en', self.context.context, self.context.device_id):
            result = await self.call('resume')
            conflict = await self.call('resume', {'kind':'music'})
        self.assertEqual(result.data['reason'], 'different_content_in_queue')
        self.assertEqual(conflict.data['reason'], 'tool_does_not_match_requested_category')
        self.assertEqual([call[1] for call in self.services.calls], ['get_queue'])

    async def test_router_book_constraint_keeps_catalog_validated_book_resume(self):
        with scope_module.bind_request('Continue my audiobook', 'en', self.context.context, self.context.device_id):
            result = await self.call('resume')
        self.assertEqual(result.data['status'], 'dry_run')
        self.assertEqual(result.data['selected']['media_type'], 'audiobook')
        self.assertEqual([call[1] for call in self.services.calls], ['get_queue','search'])

    async def test_model_identity_configuration_and_revision_fields_rejected(self):
        for field in ["username", "config_entry_id", "source_device", "source_room", "expected_revision", "shuffle", "operation"]:
            with self.subTest(field=field):
                result = await self.call(args={field: "forged"})
                self.assertTrue(result.error)
        self.assertFalse(self.services.calls)

    async def test_unknown_source_rejected_even_with_explicit_target(self):
        context = types.SimpleNamespace(device_id="unknown", context=self.context.context)
        result = await self.call(args={"query": "Fixture Book", "target_room": "kitchen"}, context=context)
        self.assertEqual(result.data["reason"], "verified_source_and_media_configuration_required")
        self.assertFalse(self.services.calls)

    def living_area(self):
        self.entry.options['media_rooms'][0]['room_id'] = 'living_room'
        self.entry.options['satellites'][0]['room_id'] = 'living_room'
        self.hass.devices.values['source-device'].area_id = 'ha-area-uuid'
        area = types.SimpleNamespace(name='Living Room', aliases={'Lounge'})
        self.hass.areas.values['ha-area-uuid'] = area
        return area

    async def test_area_name_alias_and_source_default_resolve_configured_slug(self):
        self.living_area()
        for target in (None, 'living_room', 'Living Room', 'lOuNgE'):
            with self.subTest(target=target):
                args = {'query': 'Fixture Book', 'kind': 'book'}
                if target is not None: args['target_room'] = target
                result = await self.call(args=args)
                self.assertEqual(result.data['status'], 'dry_run')
                self.assertEqual(result.data['room'], 'living_room')

    async def test_area_alias_does_not_replace_verified_source(self):
        self.living_area()
        context = types.SimpleNamespace(device_id='unknown', context=self.context.context)
        result = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Lounge'}, context=context)
        self.assertEqual(result.data['reason'], 'verified_source_and_media_configuration_required')
        self.assertFalse(self.services.calls)

    async def test_player_area_can_bind_name_when_source_has_no_area(self):
        self.living_area()
        self.hass.devices.values['source-device'].area_id = None
        self.hass.entities.values[PLAYER].area_id = 'ha-area-uuid'
        result = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Living Room'})
        self.assertEqual(result.data['status'], 'dry_run')

    async def test_conflicting_device_player_areas_do_not_guess_spoken_room(self):
        self.living_area()
        self.hass.entities.values[PLAYER].area_id = 'another-area'
        result = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Living Room'})
        self.assertEqual(result.data['reason'], 'verified_source_and_media_configuration_required')
        self.assertFalse(self.services.calls)
        self.assertEqual((await self.call(args={'query':'Fixture Book','kind':'book'})).data['status'], 'dry_run')

    async def test_duplicate_area_alias_rejected_without_breaking_source_default(self):
        self.living_area()
        self.hass.areas.values['second-area'] = types.SimpleNamespace(name='Bedroom', aliases={'Lounge'})
        self.hass.devices.values['second-source'] = types.SimpleNamespace(disabled_by=None, area_id='second-area')
        self.hass.entities.values['media_player.second_ma'] = types.SimpleNamespace(
            disabled_by=None, platform='music_assistant', config_entry_id='ma-entry')
        self.entry.options['media_rooms'].append(dict(self.entry.options['media_rooms'][0],
            room_id='bedroom', music_player='media_player.second_ma'))
        self.entry.options['satellites'].append(dict(self.entry.options['satellites'][0],
            room_id='bedroom', satellite_device_id='second-source'))
        result = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Lounge'})
        self.assertEqual(result.data['reason'], 'verified_source_and_media_configuration_required')
        self.assertFalse(self.services.calls)
        for target in ('Living Room', 'Bedroom', None):
            args = {'query':'Fixture Book','kind':'book'}
            if target: args['target_room'] = target
            self.assertEqual((await self.call(args=args)).data['status'], 'dry_run')

    async def test_area_rename_changes_signature_and_invalidates_old_name(self):
        area = self.living_area()
        before = module._resolve(self.hass, self.entry, self.context)[2]
        area.name = 'Family Room'
        after = module._resolve(self.hass, self.entry, self.context)[2]
        self.assertNotEqual(before, after)
        old = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Living Room'})
        self.assertEqual(old.data['reason'], 'verified_source_and_media_configuration_required')
        new = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Family Room'})
        self.assertEqual(new.data['status'], 'dry_run')

    async def test_area_alias_change_during_search_prevents_dispatch(self):
        area = self.living_area()
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.after_search = lambda: area.aliases.add('New alias')
        result = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Living Room'})
        self.assertTrue(result.error)
        self.assertEqual([call[1] for call in self.services.calls], ['search'])

    async def test_registry_ownership_and_missing_tts_fail_closed(self):
        self.hass.entities.values[PLAYER].platform = "sonos"
        self.assertEqual((await self.call()).data["status"], "clarify")
        self.hass.entities.values[PLAYER].platform = "music_assistant"
        self.entry.options["satellites"][0].pop("tts_media_player")
        self.assertEqual((await self.call()).data["status"], "clarify")
        self.assertFalse(self.services.calls)

    async def test_selected_uri_must_be_in_fresh_catalog(self):
        result = await self.call(args={"query": "Fixture Book", "kind": "book", "selected_uri": "library://audiobook/999"})
        self.assertEqual(result.data["reason"], "selection_not_in_catalog")
        self.assertEqual([c[1] for c in self.services.calls], ["search"])

    async def test_unknown_music_queue_owner_never_invented(self):
        self.services.queue_result = queue("track")
        result = await self.call("resume")
        self.assertEqual(result.data["reason"], "queue_profile_unverified")
        self.assertEqual([c[1] for c in self.services.calls], ["get_queue"])

    async def test_book_resume_revalidates_profile_catalog_without_seek(self):
        result = await self.call("resume", {"kind": "book"})
        self.assertEqual(result.data["status"], "dry_run")
        self.assertEqual([c[1] for c in self.services.calls], ["get_queue", "search"])
        self.assertEqual(result.data["progress_policy"], "provider_managed_resume_no_seek_or_reset")

    async def test_both_admin_options_gate_execution_and_explicit_kill_switch_wins(self):
        self.entry.options["live_actions_enabled"] = True
        result = await self.call(args={"query": "Fixture Book", "kind": "book"})
        self.assertFalse(result.data["executed"])
        self.entry.options['media_enabled'] = True
        result = await self.call(args={"query": "Fixture Book", "kind": "book"})
        self.assertEqual(result.data["status"], "action_accepted")
        self.assertFalse(result.data["playback_confirmed"])
        self.assertEqual([c[1] for c in self.services.calls[-4:]], ["search", "shuffle_set", "repeat_set", "play_media"])
        self.api = module.HouseholdMediaAPI(self.hass, self.entry, execution_authorized=False)
        result = await self.call(args={"query":"Fixture Book","kind":"book"})
        self.assertEqual(result.data['status'],'dry_run')
        self.entry.options['media_enabled']=False
        self.api = module.HouseholdMediaAPI(self.hass, self.entry, execution_authorized=True)
        self.assertEqual((await self.call(args={"query":"Fixture Book","kind":"book"})).data['status'],'dry_run')

    async def test_concurrent_transport_retry_reuses_one_adapter_and_lookup(self):
        args = {"query": "Fixture Book", "kind": "book"}
        first, second = await asyncio.gather(self.call(args=args, call_id="same"), self.call(args=args, call_id="same"))
        self.assertEqual(first.data, second.data)
        self.assertEqual(len(self.services.calls), 1)
        adapter = next(iter(self.api.adapters.values()))
        await self.call(args=args)
        self.assertIs(next(iter(self.api.adapters.values())), adapter)
        result = await self.call(args={"query": "Other"}, call_id="same")
        self.assertTrue(result.error)

    async def test_options_change_during_search_blocks_first_action(self):
        self.entry.options["live_actions_enabled"] = True
        self.entry.options["media_enabled"] = True
        self.api = module.HouseholdMediaAPI(self.hass, self.entry, execution_authorized=True)
        self.services.after_search = lambda: self.entry.options["media_rooms"][0].update(username="changed-user")
        result = await self.call(args={"query": "Fixture Book", "kind": "book"})
        self.assertEqual(result.data["status"], "error")
        self.assertEqual([c[1] for c in self.services.calls], ["search"])

    async def test_disabling_feature_during_lookup_prevents_action(self):
        self.entry.options.update(live_actions_enabled=True,media_enabled=True)
        self.services.after_search=lambda:self.entry.options.update(media_enabled=False)
        result=await self.call(args={'query':'Fixture Book','kind':'book'})
        self.assertTrue(result.error)
        self.assertEqual([c[1] for c in self.services.calls],['search'])

    async def test_enabled_live_retry_does_not_repeat_actions(self):
        self.entry.options.update(live_actions_enabled=True,media_enabled=True)
        first=await self.call(args={'query':'Fixture Book','kind':'book'},call_id='live-retry')
        second=await self.call(args={'query':'Fixture Book','kind':'book'},call_id='live-retry')
        self.assertEqual(first.data,second.data)
        self.assertEqual([c[1] for c in self.services.calls],['search','shuffle_set','repeat_set','play_media'])

    async def test_redacted_service_failure_cached_without_automatic_retry(self):
        self.services.fail = "search"
        args = {"query": "Fixture Book", "kind": "book"}
        result = await self.call(args=args, call_id="failed")
        self.assertTrue(result.error)
        self.assertNotIn("secret", json.dumps(result.data))
        self.assertNotIn("http", json.dumps(result.data))
        self.assertEqual((await self.call(args=args, call_id="failed")).data, result.data)
        self.assertEqual(len(self.services.calls), 1)


async def real_ha_smoke():
    queue_patch = patch.object(module._GuardedServices, 'async_read_queue_mode', fixture_queue_mode)
    queue_patch.start()
    import logging
    import tempfile
    from types import MappingProxyType
    from homeassistant import loader
    from homeassistant.bootstrap import async_load_base_functionality
    from homeassistant.config_entries import ConfigEntries, ConfigEntry
    from homeassistant.const import __version__
    from homeassistant.core import Context, HomeAssistant, SupportsResponse
    from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, llm
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="household-media-llm-", dir="/tmp") as directory:
        hass = HomeAssistant(directory)
        hass.config.skip_pip = True
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        try:
            assert await async_load_base_functionality(hass)
            def entry(domain):
                value = ConfigEntry(domain=domain, title="Fixture", data={}, options={}, source="user", version=1,
                    minor_version=1, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=None)
                hass.config_entries._entries[value.entry_id] = value
                return value
            ma, esp, voice = entry("music_assistant"), entry("esphome"), entry("household_voice")
            device = dr.async_get(hass).async_get_or_create(config_entry_id=esp.entry_id, identifiers={("esphome", "fixture-media")})
            registry = er.async_get(hass)
            player = registry.async_get_or_create("media_player", "music_assistant", "fixture-ma", config_entry=ma)
            tts = registry.async_get_or_create("media_player", "sonos", "fixture-tts")
            hass.states.async_set(player.entity_id, "idle")
            config = options()
            config["media_rooms"][0].update(music_player=player.entity_id, config_entry_id=ma.entry_id)
            config["satellites"][0].update(satellite_device_id=device.id, tts_media_player=tts.entity_id)
            hass.config_entries.async_update_entry(voice, options=config)
            calls = []
            async def fixture_search(call):
                calls.append(dict(call.data))
                await asyncio.sleep(0)
                if call.data['name'] == 'jazz':
                    result = search(item('Jazz Chill', 'playlist', 'apple_music--fixture://playlist/pl.fixture'), 'playlists')
                    result['playlists'] += [item(f'Jazz Mix {i}','playlist',f'apple_music://playlist/{i}') for i in range(19)]
                    track = item('Jazz', 'track', 'apple_music://track/123')
                    track['artists'] = [item('Fixture Artist','artist','apple_music--fixture://artist/Fixture Artist')]
                    track['album'] = item('Fixture Album','album','apple_music--fixture://album/Fixture Album')
                    result['tracks'] = [track]
                    return result
                if call.data['name'] == 'Rumours':
                    album = item('Rumours', 'album', 'library://album/12')
                    album['artists'] = [item('Fleetwood Mac', 'artist', 'library://artist/7')]
                    return search(album, 'albums')
                if call.data['name'] == 'Fleetwood Mac':
                    return search(item('Fleetwood Mac','artist','library://artist/7'),'artists')
                return search()
            hass.services.async_register("music_assistant", "search", fixture_search, supports_response=SupportsResponse.ONLY)
            context = llm.LLMContext(platform="fixture", context=Context(), language="en", assistant="conversation", device_id=device.id)
            unregister = module.async_register(hass, voice)
            api_id = "household_voice_media_" + voice.entry_id
            instance = await llm.async_get_api(hass, api_id, context)
            if not hasattr(llm, 'ToolResult'):
                native_call = instance.async_call_tool
                async def compatible_call(tool_input):
                    value = await native_call(tool_input)
                    assert type(value) is dict
                    json.dumps(value)
                    from types import SimpleNamespace as _FixtureNamespace
                    return _FixtureNamespace(data=value, error=value.get('error', False))
                from types import SimpleNamespace as _FixtureNamespace
                instance = _FixtureNamespace(tools=instance.tools, async_call_tool=compatible_call)
            assert len(instance.tools) == 4
            request = llm.ToolInput(tool_name=module.NAMES[0], tool_args={"query": "Fixture Book", "kind": "book"}, id="fixture-retry")
            first, second = await asyncio.gather(instance.async_call_tool(request), instance.async_call_tool(request))
            assert first.data == second.data and first.data["status"] == "dry_run" and len(calls) == 1
            assert calls[0]["username"] == "configured-user" and calls[0]["config_entry_id"] == ma.entry_id
            bad = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0], tool_args={"username": "forged"}, id="invalid"))
            assert bad.error and len(calls) == 1
            area = ar.async_get(hass).async_create('Living Room')
            dr.async_get(hass).async_update_device(device.id, area_id=area.id)
            named = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'Fixture Book','kind':'book','target_room':'Living Room'}, id='ha-area-name'))
            assert named.data['status'] == 'dry_run' and named.data['room'] == 'kitchen'
            actions=[]
            async def fixture_action(call):
                actions.append((call.domain,call.service,dict(call.data)))
            for domain,service in [('media_player','shuffle_set'),('media_player','repeat_set'),('music_assistant','play_media')]:
                hass.services.async_register(domain,service,fixture_action)
            config.update(live_actions_enabled=True,media_enabled=True)
            hass.config_entries.async_update_entry(voice,options=config)
            live_request=llm.ToolInput(tool_name=module.NAMES[0],tool_args={'query':'Fixture Book','kind':'book'},id='enabled-fixture')
            accepted=await instance.async_call_tool(live_request)
            assert accepted.data['status']=='action_accepted' and not accepted.data['playback_confirmed']
            assert len(actions)==3
            assert (await instance.async_call_tool(live_request)).data==accepted.data and len(actions)==3
            config.update(media_enabled=False)
            hass.config_entries.async_update_entry(voice,options=config)
            disabled=await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],tool_args={'query':'Fixture Book','kind':'book'},id='disabled-fixture'))
            assert disabled.data['status']=='dry_run' and len(actions)==3
            config.update(media_enabled=True)
            hass.config_entries.async_update_entry(voice,options=config)
            album_request = llm.ToolInput(tool_name=module.NAMES[0], tool_args={
                'media_type':'album','album':'Rumours','artist':'Fleetwood Mac'}, id='album-without-query')
            album_result = await instance.async_call_tool(album_request)
            assert album_result.data['status'] == 'action_accepted' and len(actions) == 5
            assert calls[-1]['name'] == 'Rumours' and calls[-1]['artist'] == 'Fleetwood Mac'
            assert calls[-1]['media_type'] == ['album']
            assert actions[-1][0:2] == ('music_assistant','play_media')
            assert actions[-1][2]['media_id'] == 'library://album/12'
            assert 'query or album' in instance.tools[0].description
            async def queue_music(call):
                return {player.entity_id: next(iter(queue('track').values()))}
            hass.services.async_register('music_assistant','get_queue',queue_music,supports_response=SupportsResponse.ONLY)
            async def known_owner(*args):
                return 'configured-user'
            media_api = instance.tools[0].api
            media_api.resolve_queue_profile = known_owner
            media_api.adapters.clear()
            with scope_module.bind_request('Continue my audiobook','en',context.context,context.device_id):
                guarded = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[1],tool_args={},id='book-over-music'))
            assert guarded.data['reason'] == 'different_content_in_queue' and len(actions) == 5
            assert getattr(context, 'user_prompt', None) is None
            with scope_module.bind_request('Play some jazz.', 'en', context.context, context.device_id):
                jazz = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'jazz','kind':'music','target_room':'Living Room'}, id='promptless-broad-jazz'))
            assert jazz.data['status']=='action_accepted' and len(actions)==7
            assert calls[-1]['media_type']==['playlist','radio']
            assert actions[-1][2]['entity_id']==player.entity_id
            assert actions[-1][2]['media_id']=='apple_music--fixture://playlist/pl.fixture'
            assert jazz.data['announcement'] == 'Requested Jazz Chill.'
            # Real HA registries, LLM schema and action dispatch around a mocked
            # native MA client; no network or physical music is used in CI.
            raw_calls = []
            async def raw_command(command, **kwargs):
                raw_calls.append((command, kwargs))
                if command == 'music/verify_item_uri': return True
                if command == 'music/search' and kwargs['media_types'] == ['artist']:
                    raw = item('Fleetwood Mac','artist','fixture-domain://artist/7')
                    raw['provider_mappings'] = [{'provider_instance':'fixture-instance',
                        'provider_domain':'fixture-domain','item_id':'7','available':True}]
                    return {'artists':[raw]}
                raw = item('Fixture Song','track','library://track/8')
                raw.update(metadata={'genres':['Jazz']}, provider_mappings=[
                    {'provider_instance':'fixture-instance','item_id':'9','available':True}])
                return {'tracks':[raw]} if command == 'music/search' else [raw]
            helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
            provider = types.SimpleNamespace(instance_id='fixture-instance', domain='fixture-domain',
                available=True, supported_features=['artist_toptracks','similar_tracks'])
            helper.get_music_assistant_client = lambda ha, entry_id: types.SimpleNamespace(
                send_command=raw_command, providers=[provider])
            with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}):
                artist_result = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Fleetwood Mac','media_type':'artist'}, id='artist-top-tracks'))
            assert artist_result.data['status'] == 'action_accepted' and len(actions) == 9
            assert artist_result.data['announcement'] == 'Requested top tracks by Fleetwood Mac.'
            assert raw_calls[0][1]['user'] == 'configured-user'
            assert raw_calls[0][1]['providers'] == ['library','fixture-instance']
            assert raw_calls[2][1]['provider_filter'] == 'fixture-instance'
            assert actions[-1][2]['media_id'] == ['fixture-instance://track/9']
            async def soundtrack_search(call):
                assert call.data['name'] == 'Matilda' and call.data['media_type'] == ['album']
                return search(item("Matilda (Original Motion Picture Soundtrack)", 'album',
                    'fixture-instance://album/matilda'), 'albums')
            hass.services.async_register('music_assistant', 'search', soundtrack_search,
                supports_response=SupportsResponse.ONLY)
            shuffled = False
            native_album_calls = []
            async def album_queue(guard, entity):
                state = await fixture_queue_mode(guard, entity)
                state['shuffle_enabled'] = shuffled
                return state
            async def album_command(command, **kwargs):
                nonlocal shuffled
                native_album_calls.append((command, kwargs))
                if command == 'music/verify_item_uri': return True
                assert command == 'player_queues/play_media'
                shuffled = True
            helper.get_music_assistant_client = lambda ha, entry_id: types.SimpleNamespace(send_command=album_command)
            with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}), \
                    patch.object(module._GuardedServices, 'async_read_queue_mode', album_queue), \
                    scope_module.bind_request('Shuffle the Matilda soundtrack in the living room.',
                        'en', context.context, context.device_id):
                soundtrack = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Matilda soundtrack','media_type':'album','shuffle':True,
                               'target_room':'Living Room'}, id='shuffled-soundtrack'))
            assert soundtrack.data['status'] == 'action_accepted' and len(actions) == 9
            assert native_album_calls[-1] == ('player_queues/play_media', {
                'queue_id':'fixture-queue', 'media':['fixture-instance://album/matilda'],
                'option':'replace', 'radio_mode':False, 'shuffle':True,
                'user':'configured-user', 'require_schema':35})
            unregister()
            assert api_id not in {api.id for api in llm.async_get_apis(hass)}
            print(json.dumps({"ha_version": __version__, "real_ha_media_llm_runtime": True, "registry_ownership_validated": True,
                "trusted_configured_profile": True, "concurrent_retry_one_search": True, "registration_unload": True,
                "admin_live_gate_tested_with_mock_services":True, "action_retry_not_replayed":True,
                "album_without_query_real_schema_and_mock_execution": True,
                "router_bound_book_cannot_resume_verified_music_queue": True,
                "ha_area_name_resolves_legacy_room_id": True,
                "router_bound_promptless_broad_music_with_nested_metadata": True,
                "profile_scoped_artist_top_tracks_with_mock_native_client": True,
                "shuffled_soundtrack_native_play_keeps_profile_and_shuffle": True,
                "physical_actions": False, "live_ha_contacted": False, "real_ma_contacted": False}, sort_keys=True), flush=True)
        finally:
            queue_patch.stop()
            await hass.async_stop(force=True)


if __name__ == "__main__":
    if REAL_HA:
        asyncio.run(asyncio.wait_for(real_ha_smoke(), timeout=120))
    else:
        unittest.main()
