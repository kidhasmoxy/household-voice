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

    async def test_web_discovery_is_opt_in_and_books_never_search_web(self):
        import importlib
        from unittest.mock import AsyncMock
        web = importlib.import_module(module.__package__ + '.apple_web')
        with patch.object(web, 'search_web', AsyncMock()) as lookup:
            await self.call(args={'kind':'book', 'query':'Fixture Book'})
            self.entry.options['apple_web_search_enabled'] = True
            await self.call(args={'kind':'book', 'query':'Fixture Book'})
            lookup.assert_not_awaited()

    def grouped_fixture(self):
        leader = 'media_player.fixture_leader'
        self.hass.entities.values[PLAYER].unique_id = 'member'
        self.hass.entities.values[PLAYER].entity_id = PLAYER
        self.hass.entities.values[leader] = types.SimpleNamespace(entity_id=leader,
            unique_id='leader', platform='music_assistant', config_entry_id='ma-entry', disabled_by=None)
        self.hass.entities.entities = self.hass.entities.values
        players = {'member':types.SimpleNamespace(available=True, synced_to='leader', active_group=None, group_members=[]),
            'leader':types.SimpleNamespace(available=True, synced_to=None, active_group=None, group_members=['leader','member'])}
        self.hass.config_entries.values['ma-entry'].runtime_data = types.SimpleNamespace(
            mass=types.SimpleNamespace(players=types.SimpleNamespace(get=players.get)))
        return leader, players

    async def test_group_member_routes_play_and_pause_to_current_leader(self):
        leader, players = self.grouped_fixture()
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Song','track','fixture://track/1'),'tracks')
        result = await self.call(args={'query':'Song','media_type':'track'})
        self.assertTrue(result.data['executed'], result.data)
        self.assertEqual(self.services.calls[-1][2]['entity_id'], leader)
        self.assertEqual(self.services.calls[-1][2]['username'], 'configured-user')
        self.services.queue_result = {leader: self.services.queue_result[PLAYER]}
        result = await self.call('pause')
        self.assertTrue(result.data['executed'], result.data)
        self.assertEqual(self.services.calls[-1][2]['entity_id'], leader)
        self.assertEqual(players['member'].synced_to, 'leader')

    async def test_group_change_after_search_never_dispatches(self):
        _, players = self.grouped_fixture()
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Song','track','fixture://track/1'),'tracks')
        self.services.after_search = lambda: players['leader'].group_members.append('new-member')
        result = await self.call(args={'query':'Song','media_type':'track'})
        self.assertFalse(result.data['executed'])
        self.assertEqual([call[1] for call in self.services.calls], ['search'])

    def test_group_resolution_rejects_stale_cycles_missing_and_cross_instance_leader(self):
        leader, players = self.grouped_fixture()
        for change in ('cycle','missing','membership','unavailable','cross_instance'):
            with self.subTest(change=change):
                leader, players = self.grouped_fixture()
                if change == 'cycle': players['leader'].synced_to = 'member'
                if change == 'missing': del players['leader']
                if change == 'membership': players['leader'].group_members = ['leader']
                if change == 'unavailable': players['leader'].available = False
                if change == 'cross_instance': self.hass.entities.values[leader].config_entry_id = 'other'
                with self.assertRaises(module.MediaConfigurationError): module._resolve(self.hass,self.entry,self.context)

    async def test_web_failure_uses_existing_catalog(self):
        import importlib
        from unittest.mock import AsyncMock
        web = importlib.import_module(module.__package__ + '.apple_web')
        self.entry.options['apple_web_search_enabled'] = True
        self.services.search_result = search(item('Song','track','fixture://track/1'), 'tracks')
        with patch.object(web, 'search_web', AsyncMock(return_value={'status':'unavailable'})) as lookup:
            result = await self.call(args={'query':'Song','media_type':'track'})
        lookup.assert_awaited_once()
        self.assertIn('selected', result.data, result.data)
        self.assertEqual(result.data['selected']['title'], 'Song')
        self.assertFalse(result.data['executed'])

    async def test_irrelevant_web_result_falls_back_to_catalog(self):
        import importlib
        from unittest.mock import AsyncMock
        web = importlib.import_module(module.__package__ + '.apple_web')
        self.entry.options['apple_web_search_enabled'] = True
        self.services.search_result = search(item('Song','track','fixture://track/1'), 'tracks')
        wrong = search(item('Other','track','apple_music--one://track/2'), 'tracks')
        with patch.object(web, 'discover', AsyncMock(return_value=(wrong, {'apple_music--one://track/2':'link'}))):
            result = await self.call(args={'query':'Song','media_type':'track'})
        self.assertEqual(result.data['selected']['title'], 'Song')
        self.assertEqual(len([c for c in self.services.calls if c[1]=='search']), 1)

    async def test_web_toggle_invalidates_existing_guard(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        self.entry.options['apple_web_search_enabled'] = True
        with self.assertRaises(module.MediaConfigurationError):
            await guard.async_call('media_player','media_play',{'entity_id':PLAYER})
        self.assertFalse(self.services.calls)

    async def test_native_web_dispatch_and_no_retry_after_failure(self):
        import importlib
        from unittest.mock import AsyncMock
        web = importlib.import_module(module.__package__ + '.apple_web')
        pairings, collector = self.auto_area_fixture()
        self.entry.options.update(apple_web_search_enabled=True, apple_web_native_enabled=True,
            live_actions_enabled=True, media_enabled=True)
        self.hass.states.get = lambda entity: types.SimpleNamespace(state='idle',
            attributes={'group_members':[entity], 'shuffle':False, 'repeat':'off'})
        found = search(item('Song','track','apple_music--one://track/1'), 'tracks')
        async def empty(guard, player):
            return dict(await fixture_queue_mode(guard, player), items=0)
        with patch.object(collector, 'collect_pairings', return_value=pairings), \
                patch.object(module._GuardedServices, 'async_read_queue_mode', empty), \
                patch.object(web, 'discover', AsyncMock(return_value=(found,
                    {'apple_music--one://track/1':'https://music.apple.com/us/song/item/1'}))):
            result = await self.call(args={'query':'Song','media_type':'track'})
            self.assertTrue(result.data['executed'], result.data)
            self.assertFalse(result.data['playback_confirmed'])
            self.assertEqual(result.data['playback_kind'], 'apple_web_native_sonos')
            self.assertEqual(self.services.calls[-1][2]['entity_id'], TTS)
            self.services.calls.clear()
            self.services.fail = 'play_media'
            result = await self.call(args={'query':'Song','media_type':'track'})
            self.assertFalse(result.data['executed'])
            self.assertEqual(len(self.services.calls), 1)
            self.assertEqual(self.services.calls[0][0], 'media_player')

    def content_client(self, tracks, *, collections=None):
        """Native MA contract fixtures; no provider or household is contacted."""
        self.entry.options.update(block_explicit_tracks=True, live_actions_enabled=True, media_enabled=True)
        self.content_reads = []
        self.content_tracks = {track['uri']: track for track in tracks}
        self.content_queue = [{'media_item':track} for track in tracks[:1]]
        self.content_search = {group:[] for group in module.GROUPS}
        self.content_search['tracks'] = tracks
        async def send(command, **args):
            self.content_reads.append((command, deepcopy(args)))
            if command in ('music/search', 'music/verify_item_uri'):
                self.assertEqual(args['user'], 'configured-user')
                self.assertEqual(args['require_schema'], 35)
            else:
                self.assertNotIn('user', args)  # These MA endpoints do not allow impersonation.
            if command == 'music/search': return deepcopy(self.content_search)
            if command == 'music/verify_item_uri': return True
            if command == 'music/item_by_uri': return deepcopy(self.content_tracks[args['uri']])
            if command in ('music/albums/album_tracks', 'music/playlists/playlist_tracks'):
                return deepcopy(collections or tracks)
            if command == 'player_queues/items': return deepcopy(self.content_queue)
            raise AssertionError(command)
        helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
        helper.get_music_assistant_client = lambda hass, entry_id: types.SimpleNamespace(send_command=send)
        patcher = patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper})
        patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def rated_track(name='Song', rating=False, uri='fixture://track/1'):
        return dict(item(name, 'track', uri), metadata={'explicit':rating})

    async def test_content_filter_excludes_explicit_suggestions_and_direct_selection(self):
        clean = self.rated_track('Song', False)
        dirty = self.rated_track('Song', True, 'fixture://track/2')
        self.content_client([clean, dirty])
        result = await self.call(args={'query':'Song', 'media_type':'track'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(result.data['selected']['uri'], clean['uri'])
        self.assertNotIn(dirty['uri'], str(result.data))
        self.services.calls.clear()
        result = await self.call(args={'query':'Song', 'media_type':'track', 'selected_uri':dirty['uri']})
        self.assertFalse(result.data['executed'])
        self.assertNotIn(dirty['uri'], str(result.data))
        self.assertEqual(self.services.calls, [])

    async def test_content_filter_unknown_defaults_strict_and_is_configurable(self):
        self.content_client([self.rated_track(rating=None)])
        blocked = await self.call(args={'query':'Song', 'media_type':'track'})
        self.assertEqual(blocked.data['reason'], 'music_content_filter_no_allowed_results')
        self.assertEqual(self.services.calls, [])
        self.entry.options['block_unrated_tracks'] = False
        allowed = await self.call(args={'query':'Song', 'media_type':'track'})
        self.assertEqual(allowed.data['status'], 'action_accepted')

    async def test_content_filter_changed_rating_blocks_before_any_action(self):
        track = self.rated_track()
        self.content_client([track])
        self.content_tracks[track['uri']] = self.rated_track(rating=True)
        result = await self.call(args={'query':'Song', 'media_type':'track'})
        self.assertEqual(result.data['reason'], 'music_content_filter_blocked')
        self.assertFalse(result.data['executed'])
        self.assertEqual(self.services.calls, [])

    async def test_content_filter_collections_dispatch_only_finite_allowed_tracks(self):
        for kind in ('album', 'playlist'):
            for shuffled in (False, True):
                with self.subTest(kind=kind, shuffle=shuffled):
                    self.content_client([self.rated_track(), self.rated_track('Explicit', True, 'fixture://track/2'),
                        self.rated_track('Unknown', None, 'fixture://track/3')])
                    self.content_search = {group:[] for group in module.GROUPS}
                    self.content_search[kind+'s'] = [item('Collection', kind, f'fixture://{kind}/1')]
                    self.services.calls.clear()
                    result = await self.call(args={'query':'Collection', 'media_type':kind, 'shuffle':shuffled})
                    self.assertEqual(result.data['status'], 'action_accepted', result.data)
                    played = [call[2] for call in self.services.calls if call[1]=='play_media']
                    self.assertEqual(len(played), 1)
                    self.assertEqual(played[0]['media_id'], ['fixture://track/1'])
                    self.assertEqual(played[0]['media_type'], 'track')
                    self.assertFalse(played[0]['radio_mode'])
                    self.assertNotIn('shuffle', played[0])
                    self.assertIn(shuffled, [call[2]['shuffle'] for call in self.services.calls if call[1]=='shuffle_set'])

    async def test_content_filter_radio_and_dynamic_queue_cannot_bypass(self):
        self.content_client([self.rated_track()])
        self.content_search['radio'] = [item('Station', 'radio', 'fixture://radio/1')]
        result = await self.call(args={'query':'Station', 'media_type':'radio'})
        self.assertFalse(result.data['executed'])
        for mode in ('is_dynamic', 'autoplay_enabled'):
            async def dynamic(guard, player):
                return dict(await fixture_queue_mode(guard, player), **{mode:True})
            with patch.object(module._GuardedServices, 'async_read_queue_mode', dynamic):
                result = await self.call(args={'query':'Song', 'media_type':'track'})
                self.assertFalse(result.data['executed'])
        self.assertEqual(self.services.calls, [])

    async def test_content_filter_queue_audits_every_item_and_keeps_books(self):
        self.content_client([self.rated_track()])
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        await guard.async_check_content_queue(PLAYER)
        self.content_tracks['fixture://track/1']['metadata']['explicit'] = True
        with self.assertRaises(module.MusicContentError):
            await guard.async_check_content_queue(PLAYER)
        self.content_queue = [{'media_item':item()}]
        await guard.async_check_content_queue(PLAYER)
        self.content_queue = [{'media_item':self.rated_track()}, {'media_item':self.rated_track('Explicit', True, 'fixture://track/2')}]
        self.content_tracks['fixture://track/1'] = self.rated_track()
        self.content_tracks['fixture://track/2'] = self.rated_track('Explicit', True, 'fixture://track/2')
        async def two(guard, player): return dict(await fixture_queue_mode(guard, player), items=2)
        with patch.object(module._GuardedServices, 'async_read_queue_mode', two):
            with self.assertRaises(module.MusicContentError):
                await guard.async_call('media_player', 'media_play', {'entity_id':PLAYER})
        self.assertEqual(self.services.calls, [])

    async def test_content_filter_toggle_invalidates_guard_and_saved_choice(self):
        self.content_client([self.rated_track('Song', False, 'fixture://track/1'),
                             self.rated_track('Song', False, 'fixture://track/2')])
        result = await self.call(args={'query':'Song', 'media_type':'track'})
        token = result.data['choice_token']
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        self.entry.options['block_unrated_tracks'] = False
        with self.assertRaises(module.MediaConfigurationError):
            await guard.async_call('media_player', 'media_play', {'entity_id':PLAYER})
        result = await self.call(args={'choice_token':token, 'choice':1})
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(self.services.calls, [])

    async def test_content_filter_rejects_later_queue_edit_with_same_count(self):
        self.content_client([self.rated_track()])
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        original = guard._content_read
        reads = 0
        async def changed(command, **args):
            nonlocal reads
            if command == 'player_queues/items':
                reads += 1
                if reads == 2:
                    return [{'media_item':self.rated_track('Changed', True, 'fixture://track/9')}]
            return await original(command, **args)
        guard._content_read = changed
        with self.assertRaises(module.MusicContentError):
            await guard.async_check_content_queue(PLAYER)
        self.assertEqual(self.services.calls, [])

    async def test_content_filter_audiobook_play_does_not_seek_or_filter_book_rating(self):
        self.content_client([])
        self.content_search['audiobooks'] = [dict(item(), metadata={'explicit':True})]
        result = await self.call(args={'query':'Fixture Book', 'kind':'book'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertFalse(any(call[1]=='media_seek' for call in self.services.calls))

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

    def auto_area_fixture(self):
        helpers = types.ModuleType('homeassistant.helpers')
        helpers.area_registry, helpers.device_registry = module.ar, module.dr
        modules = patch.dict(sys.modules, {'homeassistant':types.ModuleType('homeassistant'),
            'homeassistant.helpers':helpers})
        modules.start()
        self.addCleanup(modules.stop)
        self.entry.options.update(assist_tools_enabled=True, satellites=[])
        self.hass.devices.values['source-device'].area_id = 'current-area'
        self.hass.entities.values[PLAYER].device_id = 'speaker-device'
        self.hass.devices.values['speaker-device'] = types.SimpleNamespace(area_id='current-area', disabled_by=None)
        area = types.SimpleNamespace(id='current-area', name='Current Room', aliases={'Family Room'})
        self.hass.areas.values['current-area'] = area
        self.hass.areas.areas = self.hass.areas.values
        pairing = {'assistant_id':'assist_satellite.fixture', 'device_id':'source-device',
            'area_id':'current-area', 'speaker_id':PLAYER, 'status':'ready', 'selection':'area',
            'choices':[{'entity_id':PLAYER,'available':True}], 'manual_cross_area':False}
        result = {'assistants':{'assist_satellite.fixture':pairing},
            'outputs':{PLAYER:{'area_id':'current-area','available':True,'physical_aliases':[PLAYER,TTS]}}}
        collector = importlib.import_module(module.__package__ + '.area_registry')
        return result, collector

    async def test_native_current_area_and_unique_profile_need_no_manual_source_row(self):
        pairings, collector = self.auto_area_fixture()
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Jazz Chill','playlist','fixture://playlist/jazz'), 'playlists')
        with patch.object(collector, 'collect_pairings', return_value=pairings):
            result = await self.call(args={'query':'jazz','selection_mode':'broad_music'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(self.services.calls[0][2]['username'], 'configured-user')
        self.assertEqual(self.services.calls[-1][2]['entity_id'], PLAYER)
        self.assertFalse(hasattr(self.context, 'user_prompt'))

    async def test_native_current_area_wins_over_old_room_mapping_and_revalidates(self):
        pairings, collector = self.auto_area_fixture()
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.entry.options['satellites'] = options()['satellites']
        with patch.object(collector, 'collect_pairings', return_value=pairings):
            registry, row, before, _ = module._resolve(self.hass, self.entry, self.context)
            self.assertEqual(row['room_id'], 'current-area')
            self.assertEqual(registry.resolve(source_device='source-device')['area'], 'current-area')
            def move_source():
                self.hass.devices.values['source-device'].area_id = 'moved-area'
            self.services.after_search = move_source
            result = await self.call(args={'query':'Fixture Book','kind':'book'})
        self.assertNotEqual(result.data['status'], 'action_accepted')
        self.assertEqual([call[1] for call in self.services.calls], ['search'])

    async def test_native_area_output_and_profile_ambiguity_fail_closed(self):
        pairings, collector = self.auto_area_fixture()
        with patch.object(collector, 'collect_pairings', return_value=pairings):
            for status in ('choice_required','explicitly_unpaired','speaker_unavailable',
                           'manual_speaker_unsafe_or_unsupported'):
                with self.subTest(status=status):
                    pairings['assistants']['assist_satellite.fixture']['status'] = status
                    result = await self.call(args={'query':'Fixture Book','kind':'book'})
                    self.assertEqual(result.data['reason'], 'verified_source_and_media_configuration_required')
            pairings['assistants']['assist_satellite.fixture']['status'] = 'ready'
            other = deepcopy(self.entry.options['media_rooms'][0])
            other['username'] = 'another-profile'
            self.entry.options['media_rooms'].append(other)
            result = await self.call(args={'query':'Fixture Book','kind':'book'})
            self.assertEqual(result.data['reason'], 'verified_source_and_media_configuration_required')
        self.assertEqual(self.services.calls, [])

    async def test_native_area_explicit_unknown_or_duplicate_alias_never_defaults(self):
        pairings, collector = self.auto_area_fixture()
        self.hass.areas.values['other-area'] = types.SimpleNamespace(id='other-area', name='Other', aliases={'Family Room'})
        with patch.object(collector, 'collect_pairings', return_value=pairings):
            for target in ('Missing Room','Family Room'):
                result = await self.call(args={'query':'Fixture Book','kind':'book','target_room':target})
                self.assertEqual(result.data['reason'], 'verified_source_and_media_configuration_required')
            result = await self.call(args={'query':'Fixture Book','kind':'book','target_room':'Current Room'})
            self.assertEqual(result.data['status'], 'dry_run')

    async def test_native_new_player_inherits_only_verified_unique_instance_profile(self):
        pairings, collector = self.auto_area_fixture()
        new_player = 'media_player.new_ma'
        self.hass.entities.values[new_player] = types.SimpleNamespace(platform='music_assistant',
            config_entry_id='ma-entry', disabled_by=None, device_id='speaker-device')
        binding = pairings['assistants']['assist_satellite.fixture']
        binding.update(speaker_id=new_player, selection='manual')
        pairings['outputs'][new_player] = pairings['outputs'].pop(PLAYER)
        with patch.object(collector, 'collect_pairings', return_value=pairings):
            _, row, _, _ = module._resolve(self.hass,self.entry,self.context)
            self.assertEqual(row['music_player'], new_player)
            self.assertEqual(row['username'], 'configured-user')
            self.hass.entities.values[new_player].config_entry_id = 'unconfigured-instance'
            with self.assertRaises(module.MediaConfigurationError):
                module._resolve(self.hass,self.entry,self.context)

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

    async def test_artist_playlist_and_mix_dispatch_track_mutations_without_deadlines(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        for data in ({'media_id':['library://track/1','library://track/2'], 'radio_mode':False},
                     {'media_id':'library://playlist/1', 'radio_mode':False},
                     {'media_id':'library://track/1', 'radio_mode':True}):
            with self.subTest(data=data):
                guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
                with patch.object(module.asyncio, 'timeout', side_effect=AssertionError('No artificial deadline')):
                    await guard.async_call('music_assistant', 'search', {})
                    self.assertFalse(guard.mutation_dispatch_started)
                    await guard.async_call('media_player', 'shuffle_set', {'shuffle':False})
                    self.assertTrue(guard.mutation_dispatch_started)
                    self.assertFalse(guard.playback_dispatch_started)
                    await guard.async_call('music_assistant', 'play_media', data)
                    self.assertTrue(guard.playback_dispatch_started)

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

    async def test_changed_configuration_cannot_dispatch(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        self.entry.options['media_rooms'][0]['username'] = 'changed-user'
        with self.assertRaises(module.MediaConfigurationError):
            await guard.async_call('music_assistant', 'play_media', {'radio_mode': True})
        self.assertEqual(self.services.calls, [])
        self.assertFalse(guard.playback_dispatch_started)

    async def test_action_timeout_returns_uncertainty_and_same_call_cannot_replay(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        original_call = self.services.async_call
        async def provider_timeout(domain, service, data, **kwargs):
            result = await original_call(domain, service, data, **kwargs)
            if service == 'play_media':
                raise TimeoutError('Native provider timeout')
            return result
        self.services.async_call = provider_timeout
        args = {'query': 'Fixture Playlist', 'kind': 'music', 'media_type':'playlist'}
        self.services.search_result = search(item('Fixture Playlist','playlist','library://playlist/1'),'playlists')
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

    async def test_successful_native_fallback_clears_prior_timeout_phase(self):
        signature = module._resolve(self.hass, self.entry, self.context, 'kitchen')[2]
        guard = module._GuardedServices(self.api, self.context, 'kitchen', signature)
        async def timeout(): raise TimeoutError('Provider timeout')
        async def success(): return True
        guard.phase = 'genre_search'
        with self.assertRaises(TimeoutError): await guard._native_response(timeout())
        self.assertEqual(guard.native_timeout_phase, 'genre_search')
        self.assertTrue(await guard._native_response(success()))
        self.assertIsNone(guard.native_timeout_phase)

    async def test_tool_receipts_record_wrapped_and_native_dict_results_including_cached_errors(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Fixture Playlist','playlist','library://playlist/1'),'playlists')
        for native in (False, True):
            with self.subTest(native=native):
                args = {'query':'Fixture Playlist','kind':'music'}
                patches = {'ToolResult': None} if native else {}
                with patch.multiple(module.llm, **patches) if patches else patch.dict({}, {}):
                    with scope_module.bind_request('Play Fixture Playlist', 'en', self.context.context, self.context.device_id) as evidence:
                        first = await self.call(args=args, call_id='receipt-' + str(native))
                        second = await self.call(args=args, call_id='receipt-' + str(native))
                        await self.call(args={'query': object()}, call_id='invalid-receipt-' + str(native))
                self.assertEqual(len(evidence.media_results), 3)
                self.assertEqual(evidence.media_results[0], evidence.media_results[1])
                self.assertEqual(evidence.media_results[0]['operation'], 'play')
                self.assertEqual(evidence.media_results[0]['status'], 'action_accepted')
                self.assertTrue(evidence.media_results[0]['executed'])
                self.assertEqual(evidence.media_results[0]['selected']['title'], 'Fixture Playlist')
                self.assertEqual(evidence.media_results[2]['reason'], 'invalid_media_arguments')
                self.assertNotIn('actions', evidence.media_results[0])

    async def test_cancelled_tool_records_uncertainty_before_scope_exit(self):
        entered = asyncio.Event()
        async def slow_search(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.services.async_call = slow_search
        with scope_module.bind_request('Play Adele', 'en', self.context.context, self.context.device_id) as evidence:
            task = asyncio.create_task(self.call(args={'query':'Adele'}, call_id='receipt-cancelled'))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            cached = await self.call(args={'query':'Adele'}, call_id='receipt-cancelled')
        self.assertEqual(len(evidence.media_results), 2)
        self.assertEqual(evidence.media_results[0], evidence.media_results[1])
        self.assertEqual(evidence.media_results[0]['reason'], 'media_result_uncertain')
        self.assertFalse(self.api.pending)

    async def test_native_lookup_timeout_has_no_mutations_and_caches_failure(self):
        async def provider_timeout(*args, **kwargs):
            raise TimeoutError('Native provider timeout')
        self.services.async_call = provider_timeout
        args = {'query':'Adele', 'kind':'music'}
        result = await self.call(args=args, call_id='slow-lookup')
        self.assertEqual(result.data['reason'], 'media_provider_timeout')
        self.assertEqual(result.data['timeout_phase'], 'catalog_lookup')
        self.assertFalse(result.data['mutation_dispatch_started'])
        self.assertFalse(result.data['playback_dispatch_started'])
        self.assertNotIn('announcement', result.data)
        self.assertEqual((await self.call(args=args, call_id='slow-lookup')).data, result.data)

    async def test_artist_dependency_native_timeout_preserves_nonsecret_phase(self):
        self.services.search_result = search(item('Adele','artist','fixture://artist/1'),'artists')
        async def provider_timeout(guard, *args):
            guard.phase = 'artist_search'
            guard.native_timeout_phase = guard.phase
            raise TimeoutError('Native provider timeout')
        with patch.object(module._GuardedServices, 'async_read_artist_candidates', provider_timeout):
            result = await self.call(args={'query':'Adele','kind':'music'})
        self.assertEqual(result.data['reason'], 'media_provider_timeout')
        self.assertEqual(result.data['timeout_phase'], 'artist_search')
        self.assertEqual([c[1] for c in self.services.calls], ['search'])

    async def test_legitimate_music_longer_than_five_seconds_completes_once(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Fixture Playlist','playlist','library://playlist/1'),'playlists')
        original_call = self.services.async_call
        async def slow_play(domain, service, data, **kwargs):
            result = await original_call(domain, service, data, **kwargs)
            if service == 'play_media':
                await asyncio.sleep(5.1)
            return result
        self.services.async_call = slow_play
        args = {'query':'Fixture Playlist', 'kind':'music'}
        started = asyncio.get_running_loop().time()
        with patch.object(module.asyncio, 'timeout', side_effect=AssertionError('No artificial deadline')), \
                patch.object(module.asyncio, 'timeout_at', side_effect=AssertionError('No artificial deadline')):
            result = await self.call(args=args, call_id='slow-valid')
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 5)
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertFalse(result.data['playback_confirmed'])
        self.assertTrue(result.data['announcement'].startswith('Requested '))
        self.assertEqual((await self.call(args=args, call_id='slow-valid')).data, result.data)
        self.assertEqual(sum(c[1]=='play_media' for c in self.services.calls), 1)

    async def test_music_waits_for_lock_and_cancelled_waiter_cannot_replay(self):
        args = {'query':'Adele', 'kind':'music'}
        await self.api.call_lock.acquire()
        task = asyncio.create_task(self.call(args=args, call_id='waiting'))
        await asyncio.sleep(.05)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.api.call_lock.release()
        result = await self.call(args=args, call_id='waiting')
        self.assertEqual(result.data['reason'], 'media_result_uncertain')
        self.assertEqual(self.services.calls, [])
        self.assertFalse(self.api.pending)

    async def test_native_queue_control_timeout_remains_uncertain(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.services.search_result = search(item('Fixture Playlist','playlist','library://playlist/1'),'playlists')
        original_call = self.services.async_call
        async def provider_timeout(domain, service, data, **kwargs):
            result = await original_call(domain, service, data, **kwargs)
            if service == 'shuffle_set': raise TimeoutError('Native provider timeout')
            return result
        self.services.async_call = provider_timeout
        result = await self.call(args={'query':'Fixture Playlist','kind':'music'})
        self.assertEqual(result.data['reason'], 'media_playback_pending_or_uncertain')
        self.assertEqual(result.data['timeout_phase'], 'queue_control')
        self.assertTrue(result.data['mutation_dispatch_started'])
        self.assertFalse(result.data['playback_dispatch_started'])
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

    async def test_duplicate_waits_for_original_completion_without_replay(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_call = self.services.async_call
        async def slow_search(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_call(*args, **kwargs)
        self.services.async_call = slow_search
        args = {'query':'Adele','kind':'music'}
        first = asyncio.create_task(self.call(args=args, call_id='in-flight'))
        await entered.wait()
        duplicate = asyncio.create_task(self.call(args=args, call_id='in-flight'))
        await asyncio.sleep(.05)
        self.assertFalse(first.done())
        self.assertFalse(duplicate.done())
        self.assertEqual(len(self.api.pending), 1)
        release.set()
        original_result, duplicate_result = await asyncio.gather(first, duplicate)
        self.assertEqual(original_result.data, duplicate_result.data)
        self.assertEqual(len(self.services.calls), 1)
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
        # Standard native agent: identical explicit semantics, no original text
        # or router binding. Native album dispatch must retain shuffle/profile.
        del self.context.user_prompt
        mode['shuffle_enabled'] = False
        commands.clear()
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':helper}), \
                patch.object(module._GuardedServices, 'async_read_queue_mode', read_mode), \
                patch.object(module._GuardedServices, 'async_authorize_provider_uri', authorize), \
                patch.object(module, '_with_requested_music_options', side_effect=AssertionError('No transcript parser')):
            native = await self.call(args={'query':'Matilda soundtrack', 'kind':'music',
                'selection_mode':'exact', 'media_type':'album', 'shuffle':True})
        self.assertEqual(native.data['status'], 'action_accepted')
        self.assertEqual(commands[-1][1]['shuffle'], True)
        self.assertEqual(commands[-1][1]['user'], 'configured-user')
        self.assertEqual(commands[-1][1]['media'], ['fixture://album/matilda'])

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

    async def test_named_album_constraint_cannot_be_widened_to_broad_selection(self):
        self.context.user_prompt = 'Play the album Jazz.'
        self.services.search_result = search(item('Jazz Chill', 'playlist', 'library://playlist/42'), 'playlists')
        result = await self.call(args={'query': 'jazz', 'kind': 'music'})
        self.assertEqual(result.data['reason'], 'no_exact_catalog_match')
        result = await self.call(args={'query': 'jazz', 'media_type':'album', 'selection_mode': 'broad_music'})
        self.assertTrue(result.error)

    async def test_native_semantic_broad_mode_without_original_prompt_or_scope(self):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        self.assertFalse(hasattr(self.context, 'user_prompt'))
        self.services.search_result = search(item('Jazz Chill', 'playlist', 'fixture://playlist/42'), 'playlists')
        with patch.object(module, '_with_music_selection', side_effect=AssertionError('No transcript parser')), \
                patch.object(module, '_with_requested_music_options', side_effect=AssertionError('No transcript parser')):
            result = await self.call(args={'query':'jazz', 'kind':'music', 'selection_mode':'broad_music'})
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(result.data['announcement'], 'Requested Jazz Chill.')
        self.assertEqual(self.services.calls[0][2]['username'], 'configured-user')
        self.assertEqual(self.services.calls[0][2]['media_type'], ['playlist','radio'])
        self.assertEqual(self.services.calls[-1][2]['media_id'], 'fixture://playlist/42')
        self.assertEqual(self.services.calls[-1][2]['entity_id'], PLAYER)

    async def test_explicit_exact_mode_not_reinterpreted_by_legacy_prompt(self):
        self.context.user_prompt = 'Play some jazz.'
        self.context.language = 'en'
        self.services.search_result = search(item('Jazz', 'album', 'fixture://album/42'), 'albums')
        with patch.object(module, '_with_music_selection', side_effect=AssertionError('No transcript parser')), \
                patch.object(module, '_with_requested_music_options', side_effect=AssertionError('No transcript parser')):
            result = await self.call(args={'query':'Jazz', 'media_type':'album', 'selection_mode':'exact'})
        self.assertEqual(result.data['status'], 'dry_run')
        self.assertEqual(result.data['selected']['media_type'], 'album')
        self.assertEqual(self.services.calls[0][2]['media_type'], ['album'])

    async def test_semantic_broad_contradictions_rejected_before_catalog_lookup(self):
        cases = ({'query':''}, {'query':'   '}, {'kind':'book'}, {'kind':'sleep_story'},
            {'kind':'station'}, {'media_type':'album'}, {'media_type':'artist'}, {'media_type':'track'},
            {'artist':'Artist'}, {'album':'Album'}, {'selected_uri':'fixture://playlist/42'})
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = await self.call(args={'query':'jazz', 'selection_mode':'broad_music', **overrides})
                self.assertEqual(result.data['reason'], 'invalid_media_arguments')
                self.assertEqual(self.services.calls, [])
        for operation in ('resume','pause','stop'):
            result = await self.call(operation, {'selection_mode':'broad_music'})
            self.assertEqual(result.data['reason'], 'invalid_media_arguments')
        self.assertEqual(self.services.calls, [])

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
        with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers': helper}), \
                patch.object(module.asyncio, 'timeout', side_effect=AssertionError('No artist read cutoff')):
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

    async def offer_media_choices(self, *, book=False, operation='play', extra=None):
        self.entry.options.update(live_actions_enabled=True, media_enabled=True)
        kind, group = ('audiobook', 'audiobooks') if book else ('track', 'tracks')
        rows = [dict(item('Fixture Title', kind, f'library://{kind}/{n}'),
                     version=version, artists=[item('Fixture Artist', 'artist', 'library://artist/3')])
                for n, version in ((1, 'Live'), (2, 'Original'))]
        self.services.search_result = search(rows[0], group)
        self.services.search_result[group] = rows
        args = dict(query='Fixture Title', kind='book' if book else 'music', media_type=kind)
        args.update(extra or {})
        response = (await self.call(operation, args)).data
        self.assertEqual(response['status'], 'clarify', response)
        self.assertIn('choice_token', response)
        self.assertEqual([c['choice'] for c in response['choices']], [1, 2])
        self.assertIn('1, Fixture Title (Live)', response['question'])
        self.assertIn('2, Fixture Title (Original)', response['question'])
        self.assertEqual([c[1] for c in self.services.calls], ['search'])
        return response

    def later_media_context(self, **overrides):
        result = types.SimpleNamespace(**vars(self.context))
        result.context = types.SimpleNamespace(id='later-request', user_id=self.context.context.user_id)
        for key, value in overrides.items():
            setattr(result, key, value)
        return result

    async def test_media_followup_second_choice_preserves_constraints_and_dispatches_once(self):
        offered = await self.offer_media_choices(extra={'artist':'Fixture Artist', 'shuffle':True})
        args = {'choice_token':offered['choice_token'], 'choice':2}
        context = self.later_media_context()
        first, duplicate = await asyncio.gather(
            self.call(args=args, context=context, call_id='selection'),
            self.call(args=args, context=context, call_id='selection'))
        self.assertEqual(first.data, duplicate.data)
        self.assertEqual(first.data['status'], 'action_accepted')
        self.assertFalse(first.data['playback_confirmed'])
        plays = [c[2] for c in self.services.calls if c[1] == 'play_media']
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]['media_id'], 'library://track/2')
        self.assertEqual(plays[0]['username'], 'configured-user')
        lookup = [c[2] for c in self.services.calls if c[1] == 'search'][-1]
        self.assertEqual(lookup['name'], 'Fixture Title')
        self.assertEqual(lookup['artist'], 'Fixture Artist')
        self.assertTrue(next(c[2]['shuffle'] for c in self.services.calls if c[1] == 'shuffle_set'))
        replay = await self.call(args=args, context=context, call_id='different-call')
        self.assertEqual(replay.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len([c for c in self.services.calls if c[1] == 'play_media']), 1)

    async def test_media_followup_rejects_same_turn_other_source_user_and_conversation(self):
        self.context.conversation_id = 'conversation-one'
        offered = await self.offer_media_choices()
        args = {'choice_token':offered['choice_token'], 'choice':1}
        wrong_user = self.later_media_context()
        wrong_user.context.user_id = 'another-user'
        for context in (self.context, self.later_media_context(device_id='other-device'),
                        wrong_user, self.later_media_context(conversation_id='other-conversation')):
            with self.subTest(context=vars(context)):
                result = await self.call(args=args, context=context)
                self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len(self.services.calls), 1)

    async def test_media_followup_rejects_invented_indices_or_changed_content(self):
        offered = await self.offer_media_choices()
        args = {'choice_token':offered['choice_token'], 'choice':1}
        for override in ({'choice':0}, {'choice':3}, {'choice':True}, {'choice':1.0},
                         {'query':'Different'}, {'kind':'book'}, {'artist':'Different'},
                         {'selected_uri':'library://track/99'}, {'provider':'different'},
                         {'choice_token':'invented'}, {'choice_token':''}):
            with self.subTest(override=override):
                result = await self.call(args=dict(args, **override), context=self.later_media_context())
                self.assertIn(result.data['reason'], ('media_choice_expired_or_changed', 'invalid_media_arguments'))
        self.assertEqual(len(self.services.calls), 1)

    async def test_media_followup_cannot_bypass_pending_choice_with_raw_uri(self):
        await self.offer_media_choices()
        for context in (self.context, self.later_media_context()):
            result = await self.call(args={'query':'Fixture Title','media_type':'track',
                'selected_uri':'library://track/2'}, context=context)
            self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len(self.services.calls), 1)

    async def test_media_followup_expires_and_restart_drops_tokens(self):
        now = [100.0]
        self.api.choices.clock = lambda: now[0]
        offered = await self.offer_media_choices()
        now[0] += 120
        args = {'choice_token':offered['choice_token'], 'choice':1}
        result = await self.call(args=args, context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.api = module.HouseholdMediaAPI(self.hass, self.entry)
        result = await self.call(args=args, context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len(self.services.calls), 1)

    async def test_media_followup_cancel_discards_selection_without_stopping_playback(self):
        offered = await self.offer_media_choices()
        token = offered['choice_token']
        result = await self.call(args={'choice_token':token, 'cancel_choice':True}, context=self.later_media_context())
        self.assertEqual(result.data['status'], 'cancelled')
        self.assertEqual(len(self.services.calls), 1)
        result = await self.call(args={'choice_token':token, 'choice':1}, context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len(self.services.calls), 1)

    async def test_media_followup_config_change_rejected_before_lookup(self):
        offered = await self.offer_media_choices()
        self.entry.options['media_rooms'][0]['username'] = 'changed-profile'
        result = await self.call(args={'choice_token':offered['choice_token'], 'choice':1},
            context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len(self.services.calls), 1)

    async def test_media_followup_fresh_catalog_metadata_change_never_dispatches(self):
        for mutation in ('remove', 'title', 'version', 'artist', 'unavailable'):
            self.setUp()
            with self.subTest(mutation=mutation):
                offered = await self.offer_media_choices()
                row = self.services.search_result['tracks'][0]
                if mutation == 'remove': self.services.search_result['tracks'].pop(0)
                elif mutation == 'title': row['name'] = 'Different title'
                elif mutation == 'version': row['version'] = 'Remastered'
                elif mutation == 'artist': row['artists'] = [item('Different Artist', 'artist', 'library://artist/4')]
                else: row['available'] = False
                result = await self.call(args={'choice_token':offered['choice_token'], 'choice':1},
                    context=self.later_media_context())
                self.assertEqual(result.data['reason'], 'media_choice_catalog_changed')
                self.assertEqual([c[1] for c in self.services.calls], ['search', 'search'])

    async def test_media_followup_new_search_supersedes_old_choices(self):
        offered = await self.offer_media_choices()
        new = await self.call(args={'query':'Different title', 'media_type':'track'}, context=self.later_media_context())
        self.assertNotEqual(new.data.get('choice_token'), offered['choice_token'])
        count = len(self.services.calls)
        result = await self.call(args={'choice_token':offered['choice_token'], 'choice':1}, context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len(self.services.calls), count)

    async def test_media_followup_book_resume_preserves_progress_and_kind(self):
        offered = await self.offer_media_choices(book=True, operation='resume')
        args = {'choice_token':offered['choice_token'], 'choice':2}
        wrong = await self.call('play', args, context=self.later_media_context())
        self.assertEqual(wrong.data['reason'], 'media_choice_expired_or_changed')
        result = await self.call('resume', args, context=self.later_media_context())
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertEqual(result.data['progress_policy'], 'provider_managed_resume_no_seek_or_reset')
        play = next(c[2] for c in self.services.calls if c[1] == 'play_media')
        self.assertEqual(play['media_type'], 'audiobook')
        self.assertFalse(play['radio_mode'])
        self.assertFalse(next(c[2]['shuffle'] for c in self.services.calls if c[1] == 'shuffle_set'))
        self.assertFalse(any(c[1] == 'media_seek' for c in self.services.calls))

    async def test_media_followup_explicit_shuffle_change_and_book_rejection(self):
        offered = await self.offer_media_choices(extra={'shuffle':True})
        result = await self.call(args={'choice_token':offered['choice_token'], 'choice':1, 'shuffle':False},
            context=self.later_media_context())
        self.assertEqual(result.data['status'], 'action_accepted')
        self.assertFalse(next(c[2]['shuffle'] for c in self.services.calls if c[1] == 'shuffle_set'))
        self.services.calls.clear()
        offered = await self.offer_media_choices(book=True)
        result = await self.call(args={'choice_token':offered['choice_token'], 'choice':1, 'shuffle':True},
            context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertEqual(len(self.services.calls), 1)

    async def test_media_followup_cancellation_consumes_choice_and_never_retries(self):
        offered = await self.offer_media_choices()
        self.services.fail = 'search'
        args = {'choice_token':offered['choice_token'], 'choice':1}
        result = await self.call(args=args, context=self.later_media_context())
        self.assertEqual(result.data['status'], 'error')
        self.services.fail = None
        result = await self.call(args=args, context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_expired_or_changed')
        self.assertFalse(any(c[1] == 'play_media' for c in self.services.calls))

    async def test_media_followup_explicit_room_change_uses_registered_destination(self):
        destination = 'media_player.fixture_bedroom'
        self.hass.entities.values[destination] = types.SimpleNamespace(
            disabled_by=None, platform='music_assistant', config_entry_id='ma-entry')
        self.entry.options['media_rooms'].append(dict(self.entry.options['media_rooms'][0],
            room_id='bedroom', music_player=destination))
        self.hass.devices.values['bedroom-device'] = types.SimpleNamespace(disabled_by=None)
        self.entry.options['satellites'].append(dict(satellite_device_id='bedroom-device',
            room_id='bedroom', tts_media_player=TTS))
        offered = await self.offer_media_choices(extra={'target_room':'kitchen'})
        result = await self.call(args={'choice_token':offered['choice_token'], 'choice':2,
            'target_room':'bedroom'}, context=self.later_media_context())
        self.assertEqual(result.data['status'], 'action_accepted')
        play = next(c[2] for c in self.services.calls if c[1] == 'play_media')
        self.assertEqual(play['entity_id'], destination)
        self.assertEqual(play['media_id'], 'library://track/2')

    async def test_media_followup_rejects_room_with_different_profile(self):
        destination = 'media_player.fixture_bedroom'
        self.hass.entities.values[destination] = types.SimpleNamespace(
            disabled_by=None, platform='music_assistant', config_entry_id='ma-entry')
        self.entry.options['media_rooms'].append(dict(self.entry.options['media_rooms'][0],
            room_id='bedroom', music_player=destination, username='different-user'))
        self.hass.devices.values['bedroom-device'] = types.SimpleNamespace(disabled_by=None)
        self.entry.options['satellites'].append(dict(satellite_device_id='bedroom-device',
            room_id='bedroom', tts_media_player=TTS))
        offered = await self.offer_media_choices()
        result = await self.call(args={'choice_token':offered['choice_token'], 'choice':2,
            'target_room':'bedroom'}, context=self.later_media_context())
        self.assertEqual(result.data['reason'], 'media_choice_profile_changed')
        self.assertEqual(len(self.services.calls), 1)

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
            jazz = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'jazz','kind':'music','selection_mode':'broad_music',
                           'target_room':'Living Room'}, id='native-promptless-broad-jazz'))
            assert jazz.data['status']=='action_accepted' and len(actions)==7
            assert calls[-1]['media_type']==['playlist','radio']
            assert actions[-1][2]['entity_id']==player.entity_id
            assert actions[-1][2]['media_id']=='apple_music--fixture://playlist/pl.fixture'
            assert jazz.data['announcement'] == 'Requested Jazz Chill.'
            action_count = len(actions)
            lookup_count = len(calls)
            rejected = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'Jazz','media_type':'album','selection_mode':'broad_music'},
                id='native-conflicting-selection'))
            assert rejected.data['reason'] == 'invalid_media_arguments'
            assert len(actions) == action_count and len(calls) == lookup_count
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
                    patch.object(module._GuardedServices, 'async_read_queue_mode', album_queue):
                soundtrack = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Matilda soundtrack','media_type':'album','shuffle':True,
                               'selection_mode':'exact', 'target_room':'Living Room'}, id='shuffled-soundtrack'))
            assert soundtrack.data['status'] == 'action_accepted' and len(actions) == 9
            assert native_album_calls[-1] == ('player_queues/play_media', {
                'queue_id':'fixture-queue', 'media':['fixture-instance://album/matilda'],
                'option':'replace', 'radio_mode':False, 'shuffle':True,
                'user':'configured-user', 'require_schema':35})
            # Native shared-agent routing uses actual HA registries and the real
            # pairing collector. No satellite/media row for this new source.
            from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
            speaker_device = dr.async_get(hass).async_get_or_create(config_entry_id=ma.entry_id,
                identifiers={('sonos','RINCON_FIXTURE')})
            dr.async_get(hass).async_update_device(speaker_device.id, area_id=area.id)
            registry.async_update_entity(player.entity_id, device_id=speaker_device.id)
            registry.async_update_entity(tts.entity_id, device_id=speaker_device.id)
            satellite = registry.async_get_or_create('assist_satellite','esphome','native-fixture',
                config_entry=esp, device_id=device.id)
            hass.states.async_set(satellite.entity_id,'idle',
                {'supported_features':int(AssistSatelliteEntityFeature.ANNOUNCE)})
            hass.states.async_set(tts.entity_id,'idle',{'supported_features':4})
            ma_players = {'fixture-ma':types.SimpleNamespace(player_id='RINCON_FIXTURE', available=True,
                synced_to=None, active_group=None, group_members=[])}
            ma.runtime_data = types.SimpleNamespace(mass=types.SimpleNamespace(
                players=types.SimpleNamespace(get=ma_players.get)))
            config.update(assist_tools_enabled=True, satellites=[])
            hass.config_entries.async_update_entry(voice,options=config)
            hass.services.async_register('music_assistant','search',fixture_search,supports_response=SupportsResponse.ONLY)
            native_area = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'jazz','selection_mode':'broad_music'},id='native-auto-area'))
            assert native_area.data['status'] == 'action_accepted'
            assert calls[-1]['username'] == 'configured-user'
            assert actions[-1][2]['entity_id'] == player.entity_id
            assert native_area.data['room'] == area.id
            from household_media_llm_fixture.area_registry import collect_pairings
            discovered = collect_pairings(hass, config)
            assert discovered['assistants'][satellite.entity_id]['speaker_id'] == player.entity_id
            assert tts.entity_id in discovered['outputs'][player.entity_id]['physical_aliases']
            # A second distinct MA speaker requires a dropdown override. It can
            # inherit the unique verified profile, without adding a media row.
            other_device = dr.async_get(hass).async_get_or_create(config_entry_id=ma.entry_id,
                identifiers={('music_assistant','second-fixture')})
            dr.async_get(hass).async_update_device(other_device.id,area_id=area.id)
            other_player = registry.async_get_or_create('media_player','music_assistant','second-fixture',
                config_entry=ma,device_id=other_device.id)
            hass.states.async_set(other_player.entity_id,'idle')
            ma_players['second-fixture'] = types.SimpleNamespace(player_id='second-fixture',available=True,
                synced_to=None,active_group=None,group_members=[])
            count = len(actions)
            ambiguous = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'jazz','selection_mode':'broad_music'},id='native-two-outputs'))
            assert ambiguous.data['status'] == 'clarify' and len(actions) == count
            config['assistant_speaker_overrides'] = {satellite.entity_id:other_player.entity_id}
            hass.config_entries.async_update_entry(voice,options=config)
            _, auto_row, _, _ = module._resolve(hass,voice,context)
            assert auto_row['music_player'] == other_player.entity_id and auto_row['username'] == 'configured-user'
            # Real registries: music follows a grouped member to its leader,
            # while general pairing remains excluded for room-local outputs.
            ma_players['second-fixture'].synced_to = 'fixture-ma'
            ma_players['fixture-ma'].group_members = ['fixture-ma','second-fixture']
            assert collect_pairings(hass, config)['assistants'][satellite.entity_id]['status'] == 'manual_speaker_unsafe_or_unsupported'
            grouped = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'jazz','selection_mode':'broad_music'},id='native-group-member'))
            assert grouped.data['status'] == 'action_accepted', grouped.data
            assert actions[-1][2]['entity_id'] == player.entity_id
            assert calls[-1]['username'] == 'configured-user'
            assert ma_players['second-fixture'].synced_to == 'fixture-ma'
            _, _, grouped_signature, _ = module._resolve(hass, voice, context)
            ma_players['fixture-ma'].group_members.append('new-member')
            assert module._resolve(hass, voice, context)[2] != grouped_signature
            ma_players['second-fixture'].synced_to = None
            ma_players['fixture-ma'].group_members = []
            count = len(actions)
            # Real HA tool schemas and later request contexts around synthetic
            # catalog/services: no household audio or provider is contacted.
            async def choices_search(call):
                rows = [dict(item('Fixture Choice', 'track', f'library://track/{n}'), version=version)
                        for n, version in ((41, 'Live'), (42, 'Original'))]
                response = search(rows[0], 'tracks')
                response['tracks'] = rows
                return response
            hass.services.async_register('music_assistant','search',choices_search,supports_response=SupportsResponse.ONLY)
            offered = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'Fixture Choice','media_type':'track'},id='native-offer-choices'))
            assert offered.data['status'] == 'clarify' and offered.data['choice_token']
            assert '2, Fixture Choice (Original)' in offered.data['question']
            selected_input = llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'choice_token':offered.data['choice_token'],'choice':2},id='native-select-choice')
            same_turn = await instance.async_call_tool(selected_input)
            assert same_turn.data['reason'] == 'media_choice_expired_or_changed' and len(actions) == count
            later_context = llm.LLMContext(platform='fixture', context=Context(), language='en',
                assistant='conversation', device_id=device.id)
            later_instance = await llm.async_get_api(hass, api_id, later_context)
            chosen = await later_instance.async_call_tool(selected_input)
            chosen_data = chosen.data if hasattr(chosen, 'data') else chosen
            assert chosen_data['status'] == 'action_accepted' and not chosen_data['playback_confirmed']
            assert actions[-1][2]['media_id'] == 'library://track/42'
            assert actions[-1][2]['entity_id'] == other_player.entity_id
            count = len(actions)
            repeated = await later_instance.async_call_tool(selected_input)
            assert (repeated.data if hasattr(repeated, 'data') else repeated) == chosen_data
            assert len(actions) == count
            # Real HA tool serialization and saved policy around a native MA
            # fixture, including mixed collections and a changed rating.
            clean = dict(item('Filtered Song', 'track', 'fixture://track/51'), metadata={'explicit':False})
            dirty = dict(item('Filtered Song', 'track', 'fixture://track/52'), metadata={'explicit':True})
            unrated = dict(item('Unrated Song', 'track', 'fixture://track/53'), metadata={})
            fresh_tracks = {row['uri']:row for row in (clean,dirty,unrated)}
            async def content_command(command, **args):
                if command == 'music/search':
                    assert args['user'] == 'configured-user'
                    if args['media_types'] == ['album']:
                        return {'albums':[item('Filtered Album','album','fixture://album/1')]}
                    return {'tracks':[clean,dirty,unrated]}
                if command == 'music/verify_item_uri':
                    assert args['user'] == 'configured-user'
                    return True
                assert 'user' not in args
                if command == 'music/item_by_uri': return fresh_tracks[args['uri']]
                if command == 'music/albums/album_tracks': return [clean,dirty,unrated]
                raise AssertionError(command)
            content_helper = types.ModuleType('homeassistant.components.music_assistant.helpers')
            content_helper.get_music_assistant_client = lambda *args: types.SimpleNamespace(send_command=content_command)
            config.update(block_explicit_tracks=True, block_unrated_tracks=True)
            hass.config_entries.async_update_entry(voice, options=config)
            with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':content_helper}):
                filtered = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Filtered Song','media_type':'track'},id='native-content-clean'))
                assert filtered.data['status'] == 'action_accepted', filtered.data
                assert actions[-1][2]['media_id'] == clean['uri']
                album_filtered = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Filtered Album','media_type':'album','shuffle':True},id='native-content-album'))
                assert album_filtered.data['status'] == 'action_accepted', album_filtered.data
                assert actions[-1][2]['media_id'] == [clean['uri']]
                count = len(actions)
                unknown = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Unrated Song','media_type':'track','selected_uri':unrated['uri']},id='native-content-unrated'))
                assert unknown.data['executed'] is False and len(actions) == count
                fresh_tracks[clean['uri']] = dict(clean, metadata={'explicit':True})
                blocked = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Filtered Song','media_type':'track'},id='native-content-changed'))
                assert blocked.data['reason'] == 'music_content_filter_blocked' and len(actions) == count
            # Exercise the web discovery adapter inside the actual HA LLM tool.
            # Only web/provider I/O and physical services are synthetic.
            import importlib
            from unittest.mock import AsyncMock
            web = importlib.import_module(module.__package__ + '.apple_web')
            config.update(block_explicit_tracks=False, apple_web_search_enabled=True,
                          apple_web_native_enabled=False)
            hass.config_entries.async_update_entry(voice, options=config)
            web_item = dict(item('Web Found Album', 'album', 'apple_music://album/123'),
                provider_mappings=[{'provider_domain':'apple_music', 'provider_instance':'apple_music--fixture',
                                    'item_id':'123', 'available':True}])
            async def web_command(command, **args):
                if command == 'music/verify_item_uri':
                    assert args['user'] == 'configured-user'
                    return True
                assert command == 'music/item_by_uri' and args['uri'] == 'apple_music://album/123'
                return web_item
            content_helper.get_music_assistant_client = lambda *args: types.SimpleNamespace(send_command=web_command)
            with patch.dict(sys.modules, {'homeassistant.components.music_assistant.helpers':content_helper}), \
                    patch.object(web, 'search_web', AsyncMock(return_value={'status':'ready', 'sources':[
                        {'url':'https://music.apple.com/us/album/web-found/123'}]})) as web_search:
                found = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                    tool_args={'query':'Web Found Album','media_type':'album'},id='web-discovery-album'))
                assert found.data['status'] == 'action_accepted', found.data
                assert actions[-1][2]['media_id'] == 'apple_music--fixture://album/123'
                assert actions[-1][2]['entity_id'] == other_player.entity_id
                web_search.assert_awaited_once()
                assert found.data['evidence_policy'].startswith('web_discovered_')
            count = len(actions)
            config.update(block_explicit_tracks=False, apple_web_search_enabled=False)
            hass.config_entries.async_update_entry(voice, options=config)
            # Moving the requesting device while catalog lookup is awaited must
            # invalidate the route before any queue control or media action.
            moved = ar.async_get(hass).async_create('Moved Room')
            async def move_after_search(call):
                response = await fixture_search(call)
                dr.async_get(hass).async_update_device(device.id,area_id=moved.id)
                return response
            hass.services.async_register('music_assistant','search',move_after_search,supports_response=SupportsResponse.ONLY)
            raced = await instance.async_call_tool(llm.ToolInput(tool_name=module.NAMES[0],
                tool_args={'query':'jazz','selection_mode':'broad_music'},id='native-area-race'))
            assert raced.data['status'] != 'action_accepted' and len(actions) == count
            unregister()
            assert api_id not in {api.id for api in llm.async_get_apis(hass)}
            print(json.dumps({"ha_version": __version__, "real_ha_media_llm_runtime": True, "registry_ownership_validated": True,
                "trusted_configured_profile": True, "concurrent_retry_one_search": True, "registration_unload": True,
                "admin_live_gate_tested_with_mock_services":True, "action_retry_not_replayed":True,
                "album_without_query_real_schema_and_mock_execution": True,
                "router_bound_book_cannot_resume_verified_music_queue": True,
                "ha_area_name_resolves_legacy_room_id": True,
                "native_promptless_broad_music_with_nested_metadata": True,
                "native_conflicting_selection_rejected_without_lookup_or_action": True,
                "profile_scoped_artist_top_tracks_with_mock_native_client": True,
                "shuffled_soundtrack_native_play_keeps_profile_and_shuffle": True,
                "native_area_without_manual_source_row_and_unique_profile": True,
                "actual_pairing_collector_alias_collapse_and_dropdown_override": True,
                "source_area_change_blocks_dispatch": True,
                "native_later_turn_catalog_choice_and_deduplication": True,
                "native_content_filter_clean_mixed_album_unrated_and_changed_rating": True,
                "web_discovery_profile_authorized_album_uses_existing_room_and_ma_playback": True,
                "physical_actions": False, "live_ha_contacted": False, "real_ma_contacted": False}, sort_keys=True), flush=True)
        finally:
            queue_patch.stop()
            await hass.async_stop(force=True)


if __name__ == "__main__":
    if REAL_HA:
        asyncio.run(asyncio.wait_for(real_ha_smoke(), timeout=120))
    else:
        unittest.main()
