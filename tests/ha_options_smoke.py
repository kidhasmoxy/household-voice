"""Offline validators; --ha-runtime exercises actual HA options flow with registries.

Run runtime mode only in a disposable HA container, /work mounted to this package
and writable /tmp. It does not contact a coordinator or an existing HA instance.
"""
from copy import deepcopy
import ast
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace, ModuleType

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType('routing_options_fixture')
package.__path__ = [str(ROOT / 'custom_components/household_voice')]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("routing_options_fixture.options", ROOT / "custom_components/household_voice/options.py")
options = importlib.util.module_from_spec(spec)
spec.loader.exec_module(options)

ENTITIES = {
    "media_player.kitchen": {"device_id": "speaker", "disabled": False},
    "media_player.office": {"device_id": "speaker2", "disabled": False},
    "device_tracker.phone": {"device_id": "phone", "disabled": False},
    "device_tracker.other": {"device_id": "otherphone", "disabled": False},
    "assist_satellite.voice": {"device_id": "voice", "disabled": False},
    "stt.provider": {"device_id": "stt", "disabled": False, "integration": "wyoming"},
}
DEVICES = {"voice": {"domains": ["esphome"]}, "outputless": {"domains": ["esphome"]}, "unrelated": {"domains": ["light"]}}
SERVICES = {"notify.mobile_app_phone", "esphome.voice_external_reply_complete", "light.turn_on", "notify.notify"}


def route():
    return {"satellite_device_id": "voice", "satellite_entity_id": "assist_satellite.voice", "room_id": "kitchen",
            "tts_media_player": "media_player.kitchen", "duck_media_players": ["media_player.office"],
            "completion_service": "esphome.voice_external_reply_complete"}


class OptionsTests(unittest.TestCase):
    def test_native_recording_defaults_off_and_requires_explicit_live_shared_announcements(self):
        self.assertFalse(options.validate_options({},ENTITIES,DEVICES,SERVICES)['native_recording_enabled'])
        for value in (1,'true',None,[]):
            with self.subTest(value=value), self.assertRaises(options.RoutingError):
                options.validate_options({'native_recording_enabled':value},ENTITIES,DEVICES,SERVICES)
        for flags in ({'assist_tools_enabled':True},
                      {'assist_tools_enabled':True,'live_actions_enabled':True},
                      {'live_actions_enabled':True,'announcements_enabled':True}):
            with self.subTest(flags=flags), self.assertRaises(options.RoutingError):
                options.validate_options({**flags,'native_recording_enabled':True},ENTITIES,DEVICES,SERVICES)
        configured=options.validate_options({'native_recording_enabled':True,'assist_tools_enabled':True,
            'live_actions_enabled':True,'announcements_enabled':True},ENTITIES,DEVICES,SERVICES)
        self.assertTrue(configured['native_recording_enabled'])

    def test_native_assist_opt_in_is_preserved_and_rejects_truthy_values(self):
        self.assertFalse(options.validate_options({}, ENTITIES, DEVICES, SERVICES)['assist_tools_enabled'])
        for enabled in (True, False):
            self.assertIs(options.validate_options({'assist_tools_enabled':enabled}, ENTITIES, DEVICES, SERVICES)['assist_tools_enabled'], enabled)
        for enabled in (1, 'true', None, []):
            with self.subTest(enabled=enabled), self.assertRaises(options.RoutingError):
                options.validate_options({'assist_tools_enabled':enabled}, ENTITIES, DEVICES, SERVICES)

    def test_area_overrides_preserve_missing_manual_and_existing_single_turn_fields(self):
        data={'assistant_speaker_overrides':{'assist_satellite.voice':'media_player.missing','assist_satellite.other':'auto','assist_satellite.none':None},
              'default_announcement_speakers':['media_player.kitchen'],'announcement_alignment_api_key':'fixture-key'}
        result=options.validate_options(data,ENTITIES,DEVICES,SERVICES)
        self.assertEqual(result['assistant_speaker_overrides'],data['assistant_speaker_overrides'])
        self.assertEqual(result['default_announcement_speakers'],data['default_announcement_speakers'])
        self.assertEqual(result['announcement_alignment_api_key'],'fixture-key')
        self.assertFalse(result['satellite_audio_enabled'])
        with self.assertRaises(options.RoutingError):
            options.validate_options({'assistant_speaker_overrides':{'all rooms':'media_player.kitchen'}},ENTITIES,DEVICES,SERVICES)

    def test_alignment_disabled_by_default_and_requires_explicit_configuration(self):
        result = options.validate_options({}, ENTITIES, DEVICES, SERVICES)
        self.assertFalse(result['single_turn_announcements_enabled'])
        self.assertEqual(result['announcement_alignment_api_key'], '')
        with self.assertRaises(options.RoutingError):
            options.alignment_options({'single_turn_announcements_enabled':True})
        configured = options.alignment_options({'single_turn_announcements_enabled':True,
            'announcement_alignment_url':'http://fixture:8000/v1/', 'announcement_alignment_model':'fixture-model',
            'announcement_alignment_api_key':'fixture-private-key'})
        self.assertEqual(configured['announcement_alignment_url'], 'http://fixture:8000')
        self.assertEqual(configured['announcement_alignment_api_key'], 'fixture-private-key')

    def test_alignment_endpoint_rejects_credentials_full_route_and_unbounded_values(self):
        for url in ['http://user:secret@fixture', 'http://fixture?key=secret', 'http://fixture/#fragment',
                    'file:///audio', 'http://fixture/v1/audio/transcriptions', 'http://fixture/../other', 'http://fixture:bad']:
            with self.subTest(url=url), self.assertRaises(options.RoutingError):
                options.alignment_url(url)
        with self.assertRaises(options.RoutingError):
            options.alignment_options({'announcement_alignment_api_key':'key\nheader'})
    def test_phone_presence_defaults_preserves_choices_and_rejects_invalid(self):
        self.assertEqual(options.validate_options({},ENTITIES,DEVICES,SERVICES)['phone_presence_policy'], 'recent_five_minutes')
        for policy in ['current_state', 'recent_hour', 'recent_five_minutes']:
            self.assertEqual(options.validate_options({'phone_presence_policy':policy},ENTITIES,DEVICES,SERVICES)['phone_presence_policy'], policy)
        with self.assertRaises(options.RoutingError):
            options.validate_options({'phone_presence_policy':'always'},ENTITIES,DEVICES,SERVICES)

    def test_spoken_media_policy_defaults_preserves_and_rejects_guess(self):
        self.assertEqual(options.validate_options({},ENTITIES,DEVICES,SERVICES)['spoken_media_policy'],'pause_and_resume')
        self.assertEqual(options.validate_options({'spoken_media_policy':'pause_and_hold'},ENTITIES,DEVICES,SERVICES)['spoken_media_policy'],'pause_and_hold')
        with self.assertRaises(options.RoutingError):
            options.validate_options({'spoken_media_policy':'auto_resume'},ENTITIES,DEVICES,SERVICES)

    def test_music_profile_uses_registered_ma_owner_not_caller_entry(self):
        entities = deepcopy(ENTITIES)
        entities['media_player.kitchen'].update(integration='music_assistant',config_entry_id='trusted-ma')
        data = {'room_id':'kitchen','music_player':'media_player.kitchen','username':'fixture_listener','default_sleep_playlist':'Fixture Sleep Stories','config_entry_id':'forged'}
        result = options.media_room(data,entities)
        self.assertEqual(result['config_entry_id'],'trusted-ma')
        with self.assertRaises(options.RoutingError):
            options.media_room(data,ENTITIES)
        with self.assertRaises(options.RoutingError):
            options.media_room(dict(data,username=''),entities)
        with self.assertRaises(options.RoutingError):
            options.validate_options({'media_rooms':[data,dict(data,room_id='office')]},entities,DEVICES,SERVICES)

    def test_registry_uses_device_collection_and_single_owning_config_entry(self):
        # Execute the actual method without installing HA for this offline test.
        # Fixtures deliberately reject both deprecated interfaces.
        tree = ast.parse((ROOT / 'custom_components/household_voice/options_flow.py').read_text())
        flow_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        method = next(node for node in flow_class.body if isinstance(node, ast.FunctionDef) and node.name=='_registry')
        class Device:
            def __init__(self, identifier, config_entry_id, disabled_by=None):
                self.id, self.config_entry_id, self.disabled_by = identifier, config_entry_id, disabled_by
            @property
            def config_entries(self):
                raise AssertionError('Deprecated multi-owner config_entries accessed')
        class Devices:
            def __iter__(self):
                return iter([Device('voice','esp'),Device('orphan','missing'),Device('disabled','esp','user')])
            def values(self):
                raise AssertionError('Deprecated devices.values accessed')
        lookups = []
        def get_entry(identifier):
            lookups.append(identifier)
            return SimpleNamespace(domain='esphome') if identifier=='esp' else None
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=get_entry),
            services=SimpleNamespace(async_services=lambda: {'notify':{'mobile_app_fixture':object()}}))
        globals_ = {'dr':SimpleNamespace(async_get=lambda _:SimpleNamespace(devices=Devices())),
                    'er':SimpleNamespace(async_get=lambda _:SimpleNamespace(entities={
                        'one':SimpleNamespace(entity_id='assist_satellite.voice',device_id='voice',disabled_by=None)}))}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])), 'options_flow._registry', 'exec'), globals_)
        entities,devices,services = globals_['_registry'](SimpleNamespace(hass=hass))
        self.assertEqual(lookups,['esp','missing','esp'])
        self.assertEqual(devices['voice'],{'domains':['esphome'],'disabled':False})
        self.assertEqual(devices['orphan']['domains'],[])
        self.assertTrue(devices['disabled']['disabled'])
        self.assertEqual(entities['assist_satellite.voice']['device_id'],'voice')
        self.assertEqual(services,{'notify.mobile_app_fixture'})

    def test_draft_copy_preserves_explicit_live_flag_and_migrates_disabled_features(self):
        original = {"live_actions_enabled": True, "phones": []}
        result = options.initial_options(original)
        result["phones"].append({})
        self.assertEqual(original["phones"], [])
        self.assertTrue(result["live_actions_enabled"])
        self.assertFalse(result["media_enabled"])
        self.assertFalse(options.initial_options({})["live_actions_enabled"])

    def test_exact_speaker_ids_only(self):
        self.assertEqual(options.speakers(["media_player.kitchen"], ENTITIES), ["media_player.kitchen"])
        for values in [["Kitchen"], ["media_player.missing"], ["media_player.kitchen"] * 2, "media_player.kitchen"]:
            with self.subTest(values=values), self.assertRaises(options.RoutingError):
                options.speakers(values, ENTITIES)

    def test_disabled_entity_rejected(self):
        entities = deepcopy(ENTITIES)
        entities["media_player.kitchen"]["disabled"] = True
        with self.assertRaises(options.RoutingError):
            options.speakers(["media_player.kitchen"], entities)

    def test_phone_service_is_registered_mobile_app_only(self):
        for service in ["notify.notify", "notify.mobile_app_missing", "light.turn_on"]:
            with self.subTest(service=service), self.assertRaises(options.RoutingError):
                options.phone({"device_tracker": "device_tracker.phone", "notify_service": service}, ENTITIES, SERVICES)
        self.assertFalse(options.phone({"device_tracker": "device_tracker.phone", "notify_service": "notify.mobile_app_phone", "enabled": False}, ENTITIES, SERVICES)["enabled"])

    def test_device_only_outputless_satellite(self):
        result = options.resolve_satellite({"satellite_device_id": "outputless"}, ENTITIES, DEVICES)
        self.assertEqual(result, {"satellite_device_id": "outputless", "satellite_entity_id": ""})
        for data in [{}, {"satellite_device_id": "unrelated"}, {"satellite_device_id": "voice", "satellite_entity_id": "assist_satellite.voice"}]:
            with self.assertRaises(options.RoutingError):
                options.resolve_satellite(data, ENTITIES, DEVICES)

    def test_mismatched_entity_device_rejected(self):
        data = route()
        data["satellite_device_id"] = "outputless"
        with self.assertRaises(options.RoutingError):
            options.satellite(data, ENTITIES, DEVICES, SERVICES)

    def test_completion_service_exact_allowlist(self):
        data = route()
        self.assertEqual(options.satellite(data, ENTITIES, DEVICES, SERVICES), dict(data,reply_volume=0.5,duck_factor=0.25))
        for service in ["light.turn_on", "esphome.other_external_reply_complete", "notify.mobile_app_phone"]:
            data["completion_service"] = service
            with self.assertRaises(options.RoutingError):
                options.satellite(data, ENTITIES, DEVICES, SERVICES)

    def test_revalidation_rejects_disappeared_service_and_duplicate_phone(self):
        data = options.initial_options({})
        data["phones"] = [{"device_tracker": "device_tracker.phone", "notify_service": "notify.mobile_app_phone", "enabled": True}]
        with self.assertRaises(options.RoutingError):
            options.validate_options(data, ENTITIES, DEVICES, set())
        data["phones"].append({**data["phones"][0], "device_tracker": "device_tracker.other"})
        with self.assertRaises(options.RoutingError):
            options.validate_options(data, ENTITIES, DEVICES, SERVICES)

    def test_duplicate_satellite_and_invalid_room(self):
        with self.assertRaises(options.RoutingError):
            options.validate_options({"satellites": [route(), route()]}, ENTITIES, DEVICES, SERVICES)
        data = route()
        data["room_id"] = "guess this room"
        with self.assertRaises(options.RoutingError):
            options.satellite(data, ENTITIES, DEVICES, SERVICES)

    def test_valid_complete_options_preserves_explicit_execution(self):
        result = options.validate_options({"live_actions_enabled": True, "satellites": [route()]}, ENTITIES, DEVICES, SERVICES)
        self.assertTrue(result["live_actions_enabled"])
        self.assertEqual(result["routing_version"], 2)

    def test_invalid_feature_types_and_volume_bounds(self):
        for key in options.FEATURES:
            with self.subTest(key=key), self.assertRaises(options.RoutingError):
                options.validate_options({key:1},ENTITIES,DEVICES,SERVICES)
        for key,value in [('announcement_volume',float('nan')),('announcement_volume',True),('ring_volume',101),('ring_volume',False),('announcement_chime','https://untrusted.invalid')]:
            with self.subTest(key=key,value=value), self.assertRaises(options.RoutingError):
                options.validate_options({key:value},ENTITIES,DEVICES,SERVICES)
        for factor in (0,-1,float('inf'),True):
            with self.assertRaises(options.RoutingError):
                options.satellite(dict(route(),duck_factor=factor),ENTITIES,DEVICES,SERVICES)

    def test_live_speaker_features_require_ma_owner_while_legacy_disabled_is_editable(self):
        data={'satellites':[route()], 'satellite_audio_enabled':True}
        options.validate_options(data,ENTITIES,DEVICES,SERVICES)
        with self.assertRaises(options.RoutingError):
            options.validate_options(dict(data,live_actions_enabled=True),ENTITIES,DEVICES,SERVICES)
        entities=deepcopy(ENTITIES)
        entities['media_player.kitchen'].update(integration='music_assistant',config_entry_id='ma')
        self.assertTrue(options.validate_options(dict(data,live_actions_enabled=True),entities,DEVICES,SERVICES)['live_actions_enabled'])

    def test_alarm_targets_and_capture_profiles_are_explicit(self):
        entities=deepcopy(ENTITIES)
        entities['media_player.kitchen'].update(integration='music_assistant',config_entry_id='ma')
        profile={'profile_id':'kitchen_capture','source_device_id':'voice','provider_entity_id':'stt.provider'}
        data={'satellites':[route()], 'alarm_rooms':[{'room_id':'kitchen','speakers':['media_player.kitchen']}], 'stt_capture_profiles':[profile]}
        result=options.validate_options(data,entities,DEVICES,SERVICES)
        self.assertEqual(result['stt_capture_profiles'],[profile])
        self.assertEqual(result['ring_volume'],45)
        for bad in (dict(profile,source_device_id='outputless'),dict(profile,provider_entity_id='stt.missing')):
            with self.assertRaises(options.RoutingError):
                options.stt_profile(bad,entities,DEVICES,[route()])
        with self.assertRaises(options.RoutingError):
            options.validate_options(dict(data,stt_capture_profiles=[profile,dict(profile,profile_id='other')]),entities,DEVICES,SERVICES)
        entities['stt.provider']['integration']='household_voice'
        with self.assertRaises(options.RoutingError):
            options.stt_profile(profile,entities,DEVICES,[route()])

    def test_alarm_options_match_coordinator_constraints_before_save(self):
        with self.assertRaisesRegex(options.RoutingError,'alarm_mapping_required'):
            options.validate_options({'live_actions_enabled':True,'ring_actions_enabled':True},{k:v for k,v in ENTITIES.items() if not k.startswith('assist_satellite.')},DEVICES,SERVICES)
        options.validate_options({'live_actions_enabled':True,'ring_actions_enabled':True},ENTITIES,DEVICES,SERVICES)
        entities=deepcopy(ENTITIES)
        entities['media_player.kitchen'].update(integration='music_assistant',config_entry_id='ma')
        with self.assertRaisesRegex(options.RoutingError,'duplicate_mapping'):
            options.validate_options({'alarm_rooms':[{'room_id':room,'speakers':['media_player.kitchen']} for room in ('kitchen','office')]},entities,DEVICES,SERVICES)


async def real_ha_options_smoke():
    import asyncio
    import logging
    import shutil
    import tempfile
    from types import MappingProxyType
    from homeassistant import loader
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.bootstrap import async_load_base_functionality
    from homeassistant.config_entries import ConfigEntries, ConfigEntry
    from homeassistant.const import __version__
    from homeassistant.core import HomeAssistant, SupportsResponse
    from homeassistant.data_entry_flow import InvalidData
    from homeassistant.helpers import device_registry as dr, entity_registry as er, area_registry as ar

    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="household-voice-options-", dir="/tmp") as directory:
        shutil.copytree(ROOT / "custom_components", Path(directory) / "custom_components")
        sys.path.insert(0, directory)
        hass = HomeAssistant(directory)
        hass.config.skip_pip = True
        loader.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        called = []
        async def never_called(call):
            called.append(call.service)
        try:
            assert await async_load_base_functionality(hass)
            hass.auth = await auth_manager_from_config(hass, [], [])
            def fixture_entry(domain):
                entry = ConfigEntry(domain=domain, title="Options fixture", data={}, options={}, source="user",
                    version=1, minor_version=1, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=None)
                # Fixture-only registry entries. Do not set up ESPHome or any network integration.
                hass.config_entries._entries[entry.entry_id] = entry
                return entry
            entry = fixture_entry("household_voice")
            esp = fixture_entry("esphome")
            ma = fixture_entry('music_assistant')
            knowledge_provider = fixture_entry('local_openai')
            device = dr.async_get(hass).async_get_or_create(config_entry_id=esp.entry_id, identifiers={("esphome", "fixture-options-voice")}, name="Fixture voice")
            assert device.config_entry_id == esp.entry_id
            registry = er.async_get(hass)
            speaker = registry.async_get_or_create("media_player", "fixture", "speaker", suggested_object_id="fixture_speaker")
            music = registry.async_get_or_create('media_player','music_assistant','music',config_entry=ma,suggested_object_id='fixture_music')
            tracker = registry.async_get_or_create("device_tracker", "fixture", "phone", suggested_object_id="fixture_phone")
            satellite_entity = registry.async_get_or_create("assist_satellite", "esphome", "voice", config_entry=esp, device_id=device.id, suggested_object_id="fixture_voice")
            knowledge_agent = registry.async_get_or_create('conversation','local_openai','knowledge-fixture',
                config_entry=knowledge_provider,suggested_object_id='fixture_knowledge')
            area=ar.async_get(hass).async_create('Fixture kitchen')
            dr.async_get(hass).async_update_device(device.id,area_id=area.id)
            registry.async_update_entity(music.entity_id,area_id=area.id)
            ma.runtime_data=SimpleNamespace(mass=SimpleNamespace(players=SimpleNamespace(get=lambda key:SimpleNamespace(
                player_id='fixture_music',available=True,synced_to=None,active_group=None,group_members=[]))))
            hass.states.async_set(music.entity_id,'idle',{'supported_features':4})
            hass.states.async_set(satellite_entity.entity_id,'idle')
            for domain, service in [("notify", "mobile_app_fixture"), ("esphome", "fixture_external_reply_complete")]:
                hass.services.async_register(domain, service, never_called)
            manager = hass.config_entries.options
            result = await manager.async_init(entry.entry_id)
            assert result["type"] == "menu"
            flow_id = result["flow_id"]
            async def step(data):
                return await manager.async_configure(flow_id, data)
            knowledge_form = await step({'next_step_id':'knowledge_family'})
            assert knowledge_form['step_id'] == 'knowledge_family'
            knowledge_fields = {str(key):value for key,value in knowledge_form['data_schema'].schema.items()}
            assert knowledge_fields['knowledge_brave_api_key'].config['type'] == 'password'
            assert 'local_openai' in repr(knowledge_fields['knowledge_agent'].config)
            knowledge_values = {'knowledge_enabled':True,'knowledge_agent':knowledge_agent.entity_id,
                'knowledge_domains':'nasa.gov','knowledge_prompt':'One kind, short explanation.',
                'knowledge_age_policy':'preteen','knowledge_daily_limit':12,'knowledge_monthly_limit':100}
            rejected_knowledge = await step({**knowledge_values,'knowledge_domains':'localhost',
                'knowledge_brave_api_key':'fixture-knowledge-secret'})
            assert rejected_knowledge['errors']['base'] == 'invalid_knowledge_domains'
            assert 'fixture-knowledge-secret' not in repr(rejected_knowledge['data_schema'].schema)
            assert (await step({**knowledge_values,'knowledge_brave_api_key':'fixture-knowledge-secret'}))['type'] == 'menu'
            knowledge_form = await step({'next_step_id':'knowledge_family'})
            assert 'fixture-knowledge-secret' not in repr(knowledge_form['data_schema'].schema)
            assert (await step({**knowledge_values,'knowledge_brave_api_key':'','knowledge_reset_prompt':True}))['type'] == 'menu'
            assistant_form=await step({'next_step_id':'assistant_pairing'})
            assert assistant_form['step_id']=='assistant_pairing'
            assistant_selector=next(iter(assistant_form['data_schema'].schema.values()))
            assert assistant_selector.config['mode']=='dropdown'
            assert not assistant_selector.config.get('custom_value',False)
            assert 'Fixture kitchen' in assistant_selector.config['options'][0]['label']
            selection=await step({'assistant':satellite_entity.entity_id})
            assert selection['step_id']=='assistant_speaker'
            assert selection['description_placeholders']['area']=='Fixture kitchen'
            speaker_selector=next(iter(selection['data_schema'].schema.values()))
            assert speaker_selector.config['mode']=='dropdown'
            assert not speaker_selector.config.get('custom_value',False)
            assert any(row['value']=='auto' and 'Fixture kitchen' in row['label'] for row in speaker_selector.config['options'])
            try:
                await step({'speaker':'media_player.invented'})
            except InvalidData:
                pass
            else:
                raise AssertionError('Pairing selector accepted an invented speaker ID')
            assert (await step({'speaker':'auto'}))['type']=='menu'
            alignment_form = await step({'next_step_id':'single_turn_announcements'})
            assert alignment_form['step_id'] == 'single_turn_announcements'
            secret_field = next(value for key,value in alignment_form['data_schema'].schema.items() if str(key)=='announcement_alignment_api_key')
            assert secret_field.config['type'] == 'password'
            rejected_alignment = await step({'single_turn_announcements_enabled':True,
                'announcement_alignment_url':'http://fixture:8000','announcement_alignment_api_key':'fixture-secret'})
            assert rejected_alignment['errors']['base'] == 'invalid_alignment_configuration'
            assert 'fixture-secret' not in repr(rejected_alignment['data_schema'].schema)
            assert (await step({'single_turn_announcements_enabled':True,
                'announcement_alignment_url':'http://fixture:8000/v1','announcement_alignment_model':'fixture-model',
                'announcement_alignment_api_key':'fixture-secret'}))['type']=='menu'
            saved_key_form = await step({'next_step_id':'single_turn_announcements'})
            assert 'fixture-secret' not in repr(saved_key_form['data_schema'].schema)
            assert (await step({'single_turn_announcements_enabled':True,
                'announcement_alignment_url':'http://fixture:8000','announcement_alignment_model':'fixture-model',
                'announcement_alignment_api_key':''}))['type']=='menu'
            assert (await step({"next_step_id": "announcements"}))["step_id"] == "announcements"
            assert (await step({"default_announcement_speakers": [music.entity_id], "announcement_chime":"default", "announcement_volume":0.4}))["type"] == "menu"
            await step({"next_step_id": "phone"})
            assert (await step({"device_tracker": tracker.entity_id}))["step_id"] == "phone_details"
            try:
                await step({"notify_service": "notify.mobile_app_missing", "enabled": True})
            except InvalidData:
                pass  # HA's registered-choice selector rejects before our handler.
            else:
                raise AssertionError("HA selector accepted an unregistered notification service")
            assert (await step({"notify_service": "notify.mobile_app_fixture", "enabled": True}))["type"] == "menu"
            await step({"next_step_id": "satellite"})
            bad = await step({"satellite_entity_id": satellite_entity.entity_id, "satellite_device_id": device.id})
            assert bad["errors"]["base"] == "select_one_satellite"
            assert (await step({"satellite_entity_id": satellite_entity.entity_id}))["step_id"] == "satellite_details"
            assert (await step({"room_id": "fixture_kitchen", "tts_media_player": music.entity_id, "duck_media_players": [speaker.entity_id], "completion_service": "esphome.fixture_external_reply_complete"}))["type"] == "menu"
            await step({'next_step_id':'media'})
            assert (await step({'room_id':'fixture_kitchen','music_player':music.entity_id,'username':'fixture_listener','default_sleep_playlist':'Fixture Sleep Stories'}))['type']=='menu'
            await step({'next_step_id':'alarms'})
            assert (await step({'room_id':'fixture_kitchen','speakers':[music.entity_id],'ring_volume':45,'ring_fallback_speakers':[]}))['type']=='menu'
            stt_provider=registry.async_get_or_create('stt','fixture','stt_provider')
            await step({'next_step_id':'capture'})
            assert (await step({'profile_id':'kitchen_capture','source_device_id':device.id,'provider_entity_id':stt_provider.entity_id}))['type']=='menu'
            await step({'next_step_id':'shared_assistant'})
            assert (await step({'assist_tools_enabled':True}))['type']=='menu'
            shared_features=await step({'next_step_id':'features'})
            shared_keys={str(key) for key in shared_features['data_schema'].schema}
            assert shared_keys==set(options.FEATURES)-{'satellite_audio_enabled','echomuse_audio_enabled','stt_capture_enabled'}
            enabled={key:False for key in shared_keys}
            enabled.update(live_actions_enabled=True,media_enabled=True)
            assert (await step(enabled))['type']=='menu'
            await step({"next_step_id": "review"})
            result = await step({})
            assert result["type"] == "create_entry"
            assert entry.options["live_actions_enabled"] is True and entry.options['media_enabled'] is True
            assert entry.options['assist_tools_enabled'] is True
            assert entry.options['single_turn_announcements_enabled'] is True
            assert entry.options['assistant_speaker_overrides'][satellite_entity.entity_id]=='auto'
            assert entry.options['announcement_alignment_api_key'] == 'fixture-secret'
            from custom_components.household_voice.knowledge_policy import DEFAULT_PROMPT
            assert entry.options['knowledge_enabled'] is True
            assert entry.options['knowledge_agent'] == knowledge_agent.entity_id
            assert entry.options['knowledge_prompt'] == DEFAULT_PROMPT
            assert entry.options['knowledge_age_policy'] == 'preteen'
            assert entry.options['knowledge_brave_api_key'] == 'fixture-knowledge-secret'
            assert entry.options["phones"][0]["device_tracker"] == tracker.entity_id
            assert entry.options["satellites"][0]["satellite_device_id"] == device.id
            assert entry.options['media_rooms'][0]['config_entry_id']==ma.entry_id
            assert entry.options['media_rooms'][0]['username']=='fixture_listener'
            assert entry.options['stt_capture_profiles'][0]['source_device_id']==device.id
            assert entry.options['alarm_rooms'][0]['speakers']==[music.entity_id]
            assert entry.options['default_announcement_speakers']==[music.entity_id]
            reopened=await manager.async_init(entry.entry_id)
            flow_id=reopened['flow_id']
            assert reopened['menu_options'][0]=='announcements'
            assert not set(reopened['menu_options']) & {'conversation_routing','capture','single_turn_announcements','satellite','test_pairing','alarms'}
            assert {'alarm_defaults','maintenance'} <= set(reopened['menu_options'])
            announcement_form=await step({'next_step_id':'announcements'})
            speaker_key=next(key for key in announcement_form['data_schema'].schema
                             if str(key)=='default_announcement_speakers')
            assert speaker_key.default()==[music.entity_id]
            manager.async_abort(flow_id)
            # Alarm defaults are editable without inventing a room mapping.
            hass.config_entries.async_update_entry(entry, options={**entry.options,'stt_capture_enabled':True})
            result=await manager.async_init(entry.entry_id);flow_id=result['flow_id']
            await step({'next_step_id':'features'});await step(enabled)
            await step({'next_step_id':'review'});assert (await step({}))['type']=='create_entry'
            assert entry.options['stt_capture_enabled'] is True
            result=await manager.async_init(entry.entry_id);flow_id=result['flow_id']
            await step({'next_step_id':'alarm_defaults'})
            invalid=await step({'ring_volume':0,'ring_fallback_speakers':[]})
            assert invalid['errors']['base']=='invalid_volume'
            await step({'ring_volume':40,'ring_fallback_speakers':[music.entity_id]})
            await step({'next_step_id':'review'});assert (await step({}))['type']=='create_entry'
            assert entry.options['ring_volume']==40 and entry.options['alarm_rooms'][0]['speakers']==[music.entity_id]
            result=await manager.async_init(entry.entry_id);flow_id=result['flow_id']
            maintenance=await step({'next_step_id':'maintenance'})
            assert set(maintenance['menu_options'])=={'audio_recovery','native_audio_recovery','remove','init'}
            assert (await step({'next_step_id':'init'}))['step_id']=='init'
            manager.async_abort(flow_id)
            # Compatibility settings remain functional for legacy installations.
            hass.config_entries.async_update_entry(entry, options={**entry.options,'assist_tools_enabled':False})
            enabled={key:False for key in options.FEATURES}
            enabled.update(live_actions_enabled=True,media_enabled=True)
            # Only the explicit pairing confirmation calls a fixture service.
            pairing=[]
            async def fixture_pairing(call):
                pairing.append(dict(call.data))
                return {'status':'action_accepted','playback_confirmed':False}
            hass.services.async_register('household_voice','test_pairing',fixture_pairing,supports_response=SupportsResponse.ONLY)
            result=await manager.async_init(entry.entry_id)
            flow_id=result['flow_id']
            await step({'next_step_id':'test_pairing'})
            blocked=await step({'satellite_device_id':device.id,'confirm_test':True})
            assert blocked['errors']['base']=='pairing_disabled' and not pairing
            manager.async_abort(flow_id)
            result=await manager.async_init(entry.entry_id)
            flow_id=result['flow_id']
            await step({'next_step_id':'features'})
            enabled.update(satellite_audio_enabled=True)
            await step(enabled)
            await step({'next_step_id':'review'})
            assert (await step({}))['type']=='create_entry'
            result=await manager.async_init(entry.entry_id)
            flow_id=result['flow_id']
            await step({'next_step_id':'test_pairing'})
            blocked=await step({'satellite_device_id':device.id,'confirm_test':False})
            assert blocked['errors']['base']=='confirm_pairing_test' and not pairing
            result=await step({'satellite_device_id':device.id,'confirm_test':True})
            assert result['step_id']=='pairing_result' and len(pairing)==1
            assert pairing[0]=={'config_entry_id':entry.entry_id,'satellite_device_id':device.id}
            assert (await step({}))['type']=='menu'
            manager.async_abort(flow_id)
            # Real selector/flow + shared scope helper; mocked local quarantine
            # reset only, never a physical stop or resume service.
            import importlib
            from unittest.mock import patch
            transport=importlib.import_module('custom_components.household_voice.satellite_transport')
            recovered=[]
            def reset_fixture(hass_arg,player_id,**kwargs):
                recovered.append((player_id,kwargs))
                return {'cleared':True,'physical_stop_confirmed':False}
            with patch.object(transport,'reset_uncertain_audio',reset_fixture):
                result=await manager.async_init(entry.entry_id)
                flow_id=result['flow_id']
                await step({'next_step_id':'audio_recovery'})
                rejected=await step({'media_player':music.entity_id,'confirm_audio_stopped':False})
                assert rejected['errors']['base']=='audio_recovery_failed' and not recovered
                assert (await step({'media_player':music.entity_id,'confirm_audio_stopped':True}))['type']=='menu'
                assert recovered==[(music.entity_id,{'operator_confirmed_stopped':True})]
                manager.async_abort(flow_id)
            native=importlib.import_module('custom_components.household_voice.native_alarm')
            backend=native.get_native_alarm_backend(hass)
            journal=await backend.get_journal()
            await backend.write(journal.begin,device.id,satellite_entity.entity_id)
            result=await manager.async_init(entry.entry_id);flow_id=result['flow_id']
            native_form=await step({'next_step_id':'native_audio_recovery'})
            assert native_form['step_id']=='native_audio_recovery'
            rejected=await step({'assistant_id':satellite_entity.entity_id,'confirm_audio_stopped':False})
            assert rejected['errors']['base']=='audio_recovery_failed'
            assert await backend.write(journal.held,device.id)
            assert (await step({'assistant_id':satellite_entity.entity_id,'confirm_audio_stopped':True}))['type']=='menu'
            assert not await backend.write(journal.held,device.id)
            manager.async_abort(flow_id)
            # Reopen and remove a mapping; persisted selections must survive.
            result = await manager.async_init(entry.entry_id)
            flow_id = result["flow_id"]
            await step({"next_step_id": "remove"})
            assert (await step({"mapping": "phone:" + tracker.entity_id}))["type"] == "menu"
            await step({"next_step_id": "review"})
            assert (await step({}))["type"] == "create_entry"
            assert entry.options["phones"] == [] and len(entry.options["satellites"]) == 1
            assert entry.options['live_actions_enabled'] and entry.options['media_enabled']
            assert entry.options['assist_tools_enabled'] is True
            assert called == [], "Options flow called a physical action"
            print(json.dumps({"home_assistant_version": __version__, "real_ha_options_flow": True,
                "registry_validated": True, "persist_reopen_remove": True, "admin_feature_switches_preserved": True,
                "capture_alarm_chime_options":True,
                "knowledge_family_form_save_reset_and_local_openai_selection":True,
                "knowledge_secret_never_suggested_and_blank_preserves":True,
                "explicit_pairing_test_mock_action":True,
                "explicit_audio_recovery_confirmation_and_saved_scope":True,
                "native_output_recovery_real_journal_explicit_confirmation":True,
                "physical_service_calls": 0, "live_ha_contacted": False, "coordinator_contacted": False}, sort_keys=True), flush=True)
        finally:
            await hass.async_stop(force=True)
            sys.path.remove(directory)


if __name__ == "__main__":
    if "--ha-runtime" in sys.argv:
        import asyncio
        asyncio.run(asyncio.wait_for(real_ha_options_smoke(), timeout=120))
    else:
        unittest.main()
