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
            unregister()
            assert api_id not in {api.id for api in llm.async_get_apis(hass)}
            print(json.dumps({"ha_version": __version__, "real_ha_media_llm_runtime": True, "registry_ownership_validated": True,
                "trusted_configured_profile": True, "concurrent_retry_one_search": True, "registration_unload": True,
                "admin_live_gate_tested_with_mock_services":True, "action_retry_not_replayed":True,
                "album_without_query_real_schema_and_mock_execution": True,
                "router_bound_book_cannot_resume_verified_music_queue": True,
                "ha_area_name_resolves_legacy_room_id": True,
                "router_bound_promptless_broad_music_with_nested_metadata": True,
                "physical_actions": False, "live_ha_contacted": False, "real_ma_contacted": False}, sort_keys=True), flush=True)
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    if REAL_HA:
        asyncio.run(asyncio.wait_for(real_ha_smoke(), timeout=120))
    else:
        unittest.main()
