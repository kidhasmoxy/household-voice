"""Administrative routing and explicit feature controls; pairing tests are opt-in."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector, DeviceSelector, DeviceSelectorConfig, EntitySelector,
    EntitySelectorConfig, SelectSelector, SelectSelectorConfig, TextSelector,
    NumberSelector, NumberSelectorConfig, TextSelectorConfig, TextSelectorType,
)
from .options import (FEATURES, RoutingError, initial_options, phone, resolve_satellite,
                      satellite, speakers, validate_options, media_room, alarm_room,
                      stt_profile, feature_options, volume, ma_speakers, conversation_fallback, alignment_options)
from .knowledge_policy import validate_knowledge_settings, KnowledgePolicyError


class HouseholdVoiceOptionsFlow(config_entries.OptionsFlow):
    def _ensure_draft(self):
        if not hasattr(self, "draft"):
            self.draft = initial_options(self.config_entry.options)

    def _registry(self):
        entities = {}
        for item in er.async_get(self.hass).entities.values():
            owner_id = getattr(item, 'config_entry_id', None)
            owner = self.hass.config_entries.async_get_entry(owner_id) if owner_id else None
            entities[item.entity_id] = {"device_id":item.device_id, "disabled":item.disabled_by is not None or getattr(owner, "disabled_by", None) is not None,
                "config_entry_id":owner_id, "integration":owner.domain if owner else None}
        devices = {}
        # HA2026.10 devices is a collection of entries; mapping access and the
        # former multi-owner config_entries property are deprecated.
        for item in dr.async_get(self.hass).devices:
            entry = self.hass.config_entries.async_get_entry(item.config_entry_id)
            domains = [entry.domain] if entry is not None else []
            devices[item.id] = {"domains": domains, "disabled": item.disabled_by is not None}
        services = {f"{domain}.{name}" for domain, names in self.hass.services.async_services().items() for name in names}
        return entities, devices, services

    @staticmethod
    def _media(multiple=False):
        return EntitySelector(EntitySelectorConfig(domain="media_player", multiple=multiple))

    @staticmethod
    def _volume():
        return NumberSelector(NumberSelectorConfig(min=0, max=1, step=0.05, mode="slider"))

    def _rooms(self):
        # Friendly suggestions, with explicit custom room IDs allowed.
        rooms = {item["room_id"] for name in ("satellites", "media_rooms", "alarm_rooms") for item in self.draft[name]}
        return SelectSelector(SelectSelectorConfig(options=sorted(rooms), custom_value=True, mode="dropdown"))

    def _form(self, step, schema, errors=None, values=None, placeholders=None):
        return self.async_show_form(step_id=step,
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), values or {}),
            errors=errors or {}, description_placeholders=placeholders or {})

    async def async_step_init(self, user_input=None):
        self._ensure_draft()
        if self.draft['assist_tools_enabled']:
            return self.async_show_menu(step_id='init', menu_options=[
                'announcements', 'assistant_pairing', 'features', 'shared_assistant',
                'knowledge_family', 'phone', 'media', 'alarm_defaults', 'maintenance', 'review'])
        return self.async_show_menu(step_id="init", menu_options=["announcements", "assistant_pairing", "features", "shared_assistant", "conversation_routing", "knowledge_family", "single_turn_announcements", "phone", "satellite", "media", "alarms", "capture", "test_pairing", "audio_recovery", "native_audio_recovery", "remove", "review"])

    async def async_step_maintenance(self, user_input=None):
        return self.async_show_menu(step_id='maintenance', menu_options=[
            'audio_recovery', 'native_audio_recovery', 'remove', 'init'])

    async def async_step_alarm_defaults(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                level = user_input.get('ring_volume', self.draft['ring_volume'])
                if type(level) not in (int, float) or not float(level).is_integer() or not 1 <= level <= 100:
                    raise RoutingError('invalid_volume')
                fallback = ma_speakers(user_input.get('ring_fallback_speakers', []), self._registry()[0])
            except RoutingError as exc:
                errors['base'] = str(exc)
            else:
                self.draft.update(ring_volume=int(level), ring_fallback_speakers=fallback)
                return await self.async_step_init()
        return self._form('alarm_defaults', {
            vol.Required('ring_volume', default=self.draft['ring_volume']): NumberSelector(NumberSelectorConfig(min=1, max=100, step=1, mode='slider')),
            vol.Optional('ring_fallback_speakers', default=self.draft['ring_fallback_speakers']): EntitySelector(EntitySelectorConfig(domain='media_player', integration='music_assistant', multiple=True)),
        }, errors, user_input or self.draft)

    async def async_step_shared_assistant(self, user_input=None):
        """Expose capabilities to the ordinary agent; never change its assignments."""
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            enabled = user_input.get('assist_tools_enabled')
            native = user_input.get('native_recording_enabled',self.draft['native_recording_enabled'])
            if type(enabled) is not bool or type(native) is not bool:
                errors['base'] = 'invalid_enabled'
            else:
                self.draft['assist_tools_enabled'] = enabled
                self.draft['native_recording_enabled'] = native
                return await self.async_step_init()
        return self._form('shared_assistant', {
            vol.Required('assist_tools_enabled', default=self.draft['assist_tools_enabled']): BooleanSelector(),
            vol.Optional('native_recording_enabled', default=self.draft['native_recording_enabled']): BooleanSelector(),
        }, errors, user_input or self.draft)

    async def async_step_knowledge_family(self, user_input=None):
        """Administrative HA options only; never a model-accessible settings tool."""
        self._ensure_draft()
        sources = [entry for entry in self.hass.config_entries.async_entries('llm_intents')
                   if entry.disabled_by is None]
        choices = [{'value':'', 'label':'Reuse the sole configured Brave credential automatically'}]
        choices += [{'value':entry.entry_id, 'label':entry.title} for entry in sources]
        errors = {}
        if user_input is not None:
            try:
                updated = {**self.draft, **{key:value for key,value in user_input.items()
                    if key.startswith('knowledge_') and key not in ('knowledge_brave_api_key', 'knowledge_clear_api_key', 'knowledge_reset_prompt')}}
                if user_input.get('knowledge_clear_api_key') is True:
                    updated['knowledge_brave_api_key'] = ''
                elif user_input.get('knowledge_brave_api_key'):
                    updated['knowledge_brave_api_key'] = user_input['knowledge_brave_api_key']
                values = validate_knowledge_settings(updated, reset_prompt=user_input.get('knowledge_reset_prompt',False))
                if values['knowledge_search_entry'] not in {row['value'] for row in choices}:
                    raise KnowledgePolicyError('invalid_knowledge_search_entry')
                selected = values['knowledge_agent']
                item = self._registry()[0].get(selected) if selected else None
                if selected and (item is None or item.get('disabled') or item.get('integration') != 'local_openai'):
                    raise KnowledgePolicyError('invalid_knowledge_agent')
            except KnowledgePolicyError as exc:
                errors['base'] = str(exc)
            else:
                self.draft.update(values)
                return await self.async_step_init()
        # Saved/newly-entered credentials never appear as suggested form values.
        values = {key:value for key,value in (user_input or self.draft).items()
                  if key.startswith('knowledge_') and key not in ('knowledge_brave_api_key','knowledge_clear_api_key','knowledge_reset_prompt')}
        if isinstance(values.get('knowledge_domains'),list):
            values['knowledge_domains'] = '\n'.join(values['knowledge_domains'])
        return self._form('knowledge_family', {
            vol.Optional('knowledge_enabled', default=False): BooleanSelector(),
            vol.Optional('knowledge_age_policy', default='family_friendly'): SelectSelector(SelectSelectorConfig(options=[
                {'value':'family_friendly','label':'Family friendly (about age eight)'},
                {'value':'preteen','label':'Preteen (ten to twelve)'},
                {'value':'teen','label':'Teen, with the same content safeguards'}], mode='dropdown')),
            vol.Optional('knowledge_agent'): EntitySelector(EntitySelectorConfig(domain='conversation', integration='local_openai')),
            vol.Optional('knowledge_search_entry', default=''): SelectSelector(SelectSelectorConfig(options=choices,mode='dropdown')),
            vol.Required('knowledge_domains'): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Required('knowledge_prompt'): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional('knowledge_reset_prompt', default=False): BooleanSelector(),
            vol.Optional('knowledge_daily_limit', default=20): NumberSelector(NumberSelectorConfig(min=1,max=100,step=1,mode='box')),
            vol.Optional('knowledge_monthly_limit', default=300): NumberSelector(NumberSelectorConfig(min=1,max=1000,step=1,mode='box')),
            vol.Optional('knowledge_brave_api_key'): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional('knowledge_clear_api_key', default=False): BooleanSelector(),
        }, errors, values)

    async def async_step_assistant_pairing(self,user_input=None):
        self._ensure_draft()
        from .area_registry import collect_pairings
        discovery=collect_pairings(self.hass,self.draft)
        pairings=discovery['assistants']
        areas={area['id']:area['name'] for area in discovery['areas']}
        errors={}
        if user_input is not None:
            selected=user_input.get('assistant')
            if selected not in pairings: errors['base']='invalid_mapping'
            else:
                self.pairing_assistant=selected
                return await self.async_step_assistant_speaker()
        choices=[{'value':identifier,'label':f"{areas.get(row['area_id'],'No Home Assistant room')} — {row['name']} → {row['speaker_name'] or 'Built-in speaker / choose a default'}"}
                 for identifier,row in pairings.items()]
        choices.sort(key=lambda item:item['label'].casefold())
        return self._form('assistant_pairing',{
            vol.Required('assistant'):SelectSelector(SelectSelectorConfig(options=choices,mode='dropdown')),
        },errors,user_input)

    async def async_step_assistant_speaker(self,user_input=None):
        from .area_registry import collect_pairings
        pairings=collect_pairings(self.hass,self.draft)
        row=pairings['assistants'].get(self.pairing_assistant)
        if row is None: return await self.async_step_assistant_pairing()
        area=next((item['name'] for item in pairings['areas'] if item['id']==row['area_id']),'No Home Assistant room')
        choices=[{'value':'auto','label':f'Automatic — suitable speaker in {area}'},
                 {'value':'none','label':'No paired speaker — use the assistant’s built-in speaker for alarms and timers'}]
        choices += [{'value':item['entity_id'],'label':item['name']+(' (unavailable)' if not item['available'] else '')} for item in row['choices']]
        # Preserve a deliberate cross-area or currently unavailable exact override.
        if row.get('speaker_id') and row['speaker_id'] not in {item['value'] for item in choices}:
            choices.append({'value':row['speaker_id'],'label':f"{row['speaker_name']} (current explicit choice)"})
        errors={}
        if user_input is not None:
            choice=user_input.get('speaker')
            if choice not in {item['value'] for item in choices}: errors['base']='invalid_mapping'
            else:
                self.draft['assistant_speaker_overrides'][self.pairing_assistant]=None if choice=='none' else choice
                return await self.async_step_init()
        current=self.draft['assistant_speaker_overrides'].get(self.pairing_assistant,row['speaker_id'] if row['selection']=='manual' else 'auto')
        return self._form('assistant_speaker',{
            vol.Required('speaker',default=current or 'none'):SelectSelector(SelectSelectorConfig(options=choices,mode='dropdown')),
        },errors,user_input,{'assistant':row['name'],'area':area,'status':row['status'].replace('_',' ')})

    async def async_step_single_turn_announcements(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                updated = {**self.draft, **{key:value for key,value in user_input.items()
                    if key in ('single_turn_announcements_enabled','announcement_alignment_url','announcement_alignment_model')}}
                key = user_input.get('announcement_alignment_api_key', '')
                if user_input.get('announcement_alignment_clear_api_key') is True:
                    updated['announcement_alignment_api_key'] = ''
                elif key:
                    updated['announcement_alignment_api_key'] = key
                values = alignment_options(updated)
            except RoutingError as exc:
                errors['base'] = str(exc)
            else:
                self.draft.update(values)
                return await self.async_step_init()
        # Never return a saved or newly supplied credential as a form suggestion.
        values = {key:value for key,value in (user_input or self.draft).items()
                  if key in ('single_turn_announcements_enabled','announcement_alignment_url','announcement_alignment_model')}
        return self._form('single_turn_announcements', {
            vol.Optional('single_turn_announcements_enabled', default=False): BooleanSelector(),
            vol.Optional('announcement_alignment_url'): TextSelector(),
            vol.Optional('announcement_alignment_model'): TextSelector(),
            vol.Optional('announcement_alignment_api_key'): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Optional('announcement_alignment_clear_api_key', default=False): BooleanSelector(),
        }, errors, values)

    async def async_step_conversation_routing(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                selected = conversation_fallback(user_input.get('conversation_fallback_agent', ''), self._registry()[0])
            except RoutingError as exc:
                errors['base'] = str(exc)
            else:
                self.draft['conversation_fallback_agent'] = selected
                return await self.async_step_init()
        values = user_input if user_input is not None else {
            'conversation_fallback_agent': self.draft['conversation_fallback_agent']
        } if self.draft['conversation_fallback_agent'] else {}
        return self._form('conversation_routing', {
            vol.Optional('conversation_fallback_agent'): EntitySelector(EntitySelectorConfig(domain='conversation')),
        }, errors, values)

    async def async_step_audio_recovery(self,user_input=None):
        self._ensure_draft()
        errors={}
        if user_input is not None:
            try:
                # This flow is administrator-owned. Use the same scoped recovery
                # helper as the admin service, without inventing a user context.
                from . import reset_configured_audio
                reset_configured_audio(self.hass,self.config_entry,user_input.get('media_player'),
                                       user_input.get('confirm_audio_stopped'))
            except Exception:
                errors['base']='audio_recovery_failed'
            else:
                return await self.async_step_init()
        return self._form('audio_recovery',{
            vol.Required('media_player'):EntitySelector(EntitySelectorConfig(domain='media_player',integration='music_assistant')),
            vol.Required('confirm_audio_stopped',default=False):BooleanSelector(),
        },errors,user_input)

    async def async_step_native_audio_recovery(self,user_input=None):
        self._ensure_draft()
        errors={}
        if user_input is not None:
            try:
                from .native_alarm import async_reset_native_alarm
                await async_reset_native_alarm(self.hass,self.config_entry,
                    user_input.get('assistant_id'),
                    confirm_output_stopped=user_input.get('confirm_audio_stopped'))
            except Exception:
                errors['base']='audio_recovery_failed'
            else:
                return await self.async_step_init()
        return self._form('native_audio_recovery',{
            vol.Required('assistant_id'):EntitySelector(EntitySelectorConfig(domain='assist_satellite')),
            vol.Required('confirm_audio_stopped',default=False):BooleanSelector(),
        },errors,user_input)

    async def async_step_features(self, user_input=None):
        self._ensure_draft()
        keys = tuple(key for key in FEATURES if not self.draft['assist_tools_enabled'] or key not in
                     ('satellite_audio_enabled', 'echomuse_audio_enabled', 'stt_capture_enabled'))
        errors = {}
        if user_input is not None:
            try:
                self.draft.update(feature_options({**self.draft, **{key: user_input[key] for key in keys if key in user_input}}))
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                return await self.async_step_init()
        return self._form("features", {vol.Required(key, default=self.draft[key]): BooleanSelector() for key in keys}, errors, user_input or self.draft)

    async def async_step_alarms(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                entities = self._registry()[0]
                selected = alarm_room(user_input, entities)
                fallback = ma_speakers(user_input.get("ring_fallback_speakers", []), entities)
                level = user_input.get("ring_volume", 45)
                if type(level) not in (int, float) or not float(level).is_integer() or not 1 <= level <= 100:
                    raise RoutingError("invalid_volume")
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                self.draft["alarm_rooms"] = [row for row in self.draft["alarm_rooms"] if row["room_id"] != selected["room_id"]] + [selected]
                self.draft.update(ring_volume=int(level), ring_fallback_speakers=fallback)
                return await self.async_step_init()
        return self._form("alarms", {
            vol.Required("room_id"): self._rooms(),
            vol.Required("speakers"): EntitySelector(EntitySelectorConfig(domain="media_player", integration="music_assistant", multiple=True)),
            vol.Required("ring_volume", default=self.draft["ring_volume"]): NumberSelector(NumberSelectorConfig(min=1, max=100, step=1, mode="slider")),
            vol.Optional("ring_fallback_speakers", default=self.draft["ring_fallback_speakers"]): EntitySelector(EntitySelectorConfig(domain="media_player", integration="music_assistant", multiple=True)),
        }, errors, user_input)

    async def async_step_capture(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                entities, devices, _ = self._registry()
                selected = stt_profile(user_input, entities, devices, self.draft["satellites"])
                others = [row for row in self.draft["stt_capture_profiles"] if row["profile_id"] != selected["profile_id"]]
                if any(row["source_device_id"] == selected["source_device_id"] for row in others):
                    raise RoutingError("duplicate_mapping")
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                self.draft["stt_capture_profiles"] = others + [selected]
                return await self.async_step_init()
        choices = [{"value": row["satellite_device_id"], "label": row.get("satellite_entity_id") or row["room_id"] + " voice device"} for row in self.draft["satellites"]]
        return self._form("capture", {
            vol.Required("profile_id"): TextSelector(),
            vol.Required("source_device_id"): SelectSelector(SelectSelectorConfig(options=choices, mode="dropdown")),
            vol.Required("provider_entity_id"): EntitySelector(EntitySelectorConfig(domain="stt")),
        }, errors, user_input)

    async def async_step_test_pairing(self, user_input=None):
        self._ensure_draft()
        errors = {}
        saved = self.config_entry.options
        choices = [{"value": row["satellite_device_id"], "label": row.get("satellite_entity_id") or row["room_id"] + " voice device"} for row in saved.get("satellites", [])]
        if user_input is not None:
            device = user_input.get("satellite_device_id")
            if not user_input.get("confirm_test") or device not in {row["value"] for row in choices}:
                errors["base"] = "confirm_pairing_test"
            elif saved.get("live_actions_enabled") is not True or saved.get("satellite_audio_enabled") is not True:
                errors["base"] = "pairing_disabled"
            elif not self.hass.services.has_service("household_voice", "test_pairing"):
                errors["base"] = "pairing_unavailable"
            else:
                try:
                    result = await self.hass.services.async_call("household_voice", "test_pairing",
                        {"config_entry_id": self.config_entry.entry_id, "satellite_device_id": device},
                        blocking=True, return_response=True)
                except Exception:
                    errors["base"] = "pairing_failed"
                else:
                    # Never interpolate arbitrary adapter result/URL/transcript into UI.
                    self.pairing_status = "Test action accepted; listen for the reply." if isinstance(result, dict) and result.get("status") in ("action_accepted", "completed") else "Test did not confirm an accepted action. Check the saved mapping and device status."
                    return await self.async_step_pairing_result()
        return self._form("test_pairing", {
            vol.Required("satellite_device_id"): SelectSelector(SelectSelectorConfig(options=choices, mode="dropdown")),
            vol.Required("confirm_test", default=False): BooleanSelector(),
        }, errors, user_input)

    async def async_step_pairing_result(self, user_input=None):
        if user_input is not None:
            return await self.async_step_init()
        return self._form("pairing_result", {}, placeholders={"result": self.pairing_status})

    async def async_step_media(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                selected = media_room(user_input, self._registry()[0])
                other = [item for item in self.draft['media_rooms'] if item['room_id'] != selected['room_id']]
                if any(item['music_player']==selected['music_player'] for item in other):
                    raise RoutingError('duplicate_mapping')
            except RoutingError as exc:
                errors['base'] = str(exc)
            else:
                self.draft['media_rooms'] = other + [selected]
                return await self.async_step_init()
        return self._form('media', {
            vol.Required('room_id'): self._rooms(),
            vol.Required('music_player'): EntitySelector(EntitySelectorConfig(domain='media_player',integration='music_assistant')),
            vol.Required('username'): TextSelector(),
            vol.Required('default_sleep_playlist'): TextSelector(),
        }, errors, user_input)

    async def async_step_announcements(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                selected = speakers(user_input.get("default_announcement_speakers", []), self._registry()[0])
                level = volume(user_input.get("announcement_volume", self.draft["announcement_volume"]))
                chime = user_input.get("announcement_chime", self.draft["announcement_chime"])
                if chime not in ("none", "default"):
                    raise RoutingError("invalid_chime")
                policy=user_input.get('spoken_media_policy',self.draft['spoken_media_policy'])
                if policy not in ('provider_default','pause_and_resume','pause_and_hold'):
                    raise RoutingError('invalid_spoken_media_policy')
                presence = user_input.get('phone_presence_policy', self.draft['phone_presence_policy'])
                if presence not in ('current_state','recent_hour','recent_five_minutes'):
                    raise RoutingError('invalid_phone_presence_policy')
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                self.draft.update(default_announcement_speakers=selected, announcement_volume=level, announcement_chime=chime, spoken_media_policy=policy, phone_presence_policy=presence)
                return await self.async_step_init()
        return self._form("announcements", {
            vol.Optional("default_announcement_speakers", default=self.draft["default_announcement_speakers"]): EntitySelector(EntitySelectorConfig(domain="media_player", integration="music_assistant", multiple=True)),
            vol.Optional("announcement_volume", default=self.draft["announcement_volume"]): self._volume(),
            vol.Optional('phone_presence_policy', default=self.draft['phone_presence_policy']): SelectSelector(SelectSelectorConfig(options=[{'value':'recent_five_minutes','label':'Home; reported within five minutes (existing behavior)'}, {'value':'recent_hour','label':'Home; reported within one hour'}, {'value':'current_state','label':'Home according to current Home Assistant state'}],mode='dropdown')),
            vol.Optional("announcement_chime", default=self.draft["announcement_chime"]): SelectSelector(SelectSelectorConfig(options=[{"value":"none", "label":"No chime"}, {"value":"default", "label":"Music Assistant announcement chime"}], mode="dropdown")),
            vol.Optional('spoken_media_policy', default=self.draft['spoken_media_policy']): SelectSelector(SelectSelectorConfig(options=[{'value':'pause_and_resume','label':'Pause spoken media, then resume when unchanged'}, {'value':'provider_default','label':'Follow Music Assistant playback behavior'}, {'value':'pause_and_hold','label':'Pause recognized spoken media; require explicit Resume'}],mode='dropdown')),
        }, errors, user_input or self.draft)

    async def async_step_phone(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            from .options import entity
            try:
                self.phone_tracker = entity(user_input.get("device_tracker"), "device_tracker", self._registry()[0])
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                return await self.async_step_phone_details()
        return self._form("phone", {vol.Required("device_tracker"): EntitySelector(EntitySelectorConfig(domain="device_tracker"))}, errors, user_input)

    async def async_step_phone_details(self, user_input=None):
        errors = {}
        entities, _, services = self._registry()
        current = next((item for item in self.draft["phones"] if item["device_tracker"] == self.phone_tracker), {"enabled": True})
        if user_input is not None:
            try:
                selected = phone({**user_input, "device_tracker": self.phone_tracker}, entities, services)
                others = [item for item in self.draft["phones"] if item["device_tracker"] != self.phone_tracker]
                if any(item["notify_service"] == selected["notify_service"] for item in others):
                    raise RoutingError("duplicate_mapping")
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                self.draft["phones"] = others + [selected]
                return await self.async_step_init()
        choices = sorted(name for name in services if name.startswith("notify.mobile_app_"))
        return self._form("phone_details", {
            vol.Required("notify_service"): SelectSelector(SelectSelectorConfig(options=choices, mode="dropdown")),
            vol.Required("enabled", default=True): BooleanSelector(),
        }, errors, user_input or current, {"tracker": self.phone_tracker})

    async def async_step_satellite(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                entities, devices, _ = self._registry()
                self.selected_satellite = resolve_satellite(user_input, entities, devices)
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                return await self.async_step_satellite_details()
        return self._form("satellite", {
            vol.Optional("satellite_entity_id"): EntitySelector(EntitySelectorConfig(domain="assist_satellite")),
            vol.Optional("satellite_device_id"): DeviceSelector(DeviceSelectorConfig(integration="esphome")),
        }, errors, user_input)

    async def async_step_satellite_details(self, user_input=None):
        errors = {}
        entities, devices, services = self._registry()
        device = self.selected_satellite["satellite_device_id"]
        current = next((item for item in self.draft["satellites"] if item["satellite_device_id"] == device), {})
        if not current:
            from .area_registry import collect_pairings
            recommended=collect_pairings(self.hass,self.draft)['assistants'].get(self.selected_satellite.get('satellite_entity_id'))
            if recommended and recommended['status']=='ready':
                current={'room_id':recommended['area_id'],'tts_media_player':recommended['speaker_id'],
                    'duck_media_players':recommended['duck_speakers']}
        if user_input is not None:
            try:
                selected = satellite({**user_input, **self.selected_satellite}, entities, devices, services)
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                self.draft["satellites"] = [item for item in self.draft["satellites"] if item["satellite_device_id"] != device] + [selected]
                return await self.async_step_init()
        completion = [{"value": "", "label": "None — routing only"}] + [
            {"value": name, "label": name} for name in sorted(services)
            if name.startswith("esphome.") and name.endswith("_external_reply_complete")]
        return self._form("satellite_details", {
            vol.Required("room_id"): self._rooms(),
            vol.Required("tts_media_player"): EntitySelector(EntitySelectorConfig(domain="media_player", integration="music_assistant")),
            vol.Optional("duck_media_players", default=[]): self._media(True),
            vol.Optional("completion_service", default=""): SelectSelector(SelectSelectorConfig(options=completion, mode="dropdown")),
            vol.Optional("reply_volume", default=current.get("reply_volume", 0.5)): self._volume(),
            vol.Optional("duck_factor", default=current.get("duck_factor", 0.25)): NumberSelector(NumberSelectorConfig(min=0.05, max=1, step=0.05, mode="slider")),
        }, errors, user_input or current, {"satellite": self.selected_satellite["satellite_entity_id"] or device})

    async def async_step_remove(self, user_input=None):
        self._ensure_draft()
        choices = [{"value": "phone:" + item["device_tracker"], "label": "Phone: " + item["device_tracker"]} for item in self.draft["phones"]]
        if not self.draft['assist_tools_enabled']:
            choices += [{"value": "satellite:" + item["satellite_device_id"], "label": "Satellite: " + (item["satellite_entity_id"] or item["satellite_device_id"])} for item in self.draft["satellites"]]
        choices += [{'value':'media:'+item['room_id'],'label':'Music room: '+item['room_id']} for item in self.draft['media_rooms']]
        choices += [{"value":"alarm:"+item["room_id"], "label":"Alarm room: "+item["room_id"]} for item in self.draft["alarm_rooms"]]
        if not self.draft['assist_tools_enabled']:
            choices += [{"value":"capture:"+item["profile_id"], "label":"Capture profile: "+item["profile_id"]} for item in self.draft["stt_capture_profiles"]]
        errors = {}
        if user_input is not None:
            selection = user_input.get("mapping")
            if selection not in {item["value"] for item in choices}:
                errors["base"] = "invalid_mapping"
            else:
                kind, value = selection.split(":", 1)
                key, field = {'phone':('phones','device_tracker'),'satellite':('satellites','satellite_device_id'),'media':('media_rooms','room_id'), 'alarm':('alarm_rooms','room_id'), 'capture':('stt_capture_profiles','profile_id')}[kind]
                self.draft[key] = [item for item in self.draft[key] if item[field] != value]
                if kind == "satellite":
                    self.draft["stt_capture_profiles"] = [item for item in self.draft["stt_capture_profiles"] if item["source_device_id"] != value]
                return await self.async_step_init()
        return self._form("remove", {vol.Required("mapping"): SelectSelector(SelectSelectorConfig(options=choices, mode="dropdown"))}, errors)

    async def async_step_review(self, user_input=None):
        self._ensure_draft()
        errors = {}
        if user_input is not None:
            try:
                if self.draft.get('native_recording_enabled'):
                    if self.config_entry.data.get('mode')!='embedded':
                        raise RoutingError('native_recording_embedded_required')
                    entries=[item for item in self.hass.config_entries.async_entries('household_voice') if item.disabled_by is None]
                    if len(entries)!=1 or entries[0].entry_id!=self.config_entry.entry_id:
                        raise RoutingError('native_recording_one_authority_required')
                result = validate_options(self.draft, *self._registry())
            except RoutingError as exc:
                errors["base"] = str(exc)
            else:
                return self.async_create_entry(title="", data=result)
        return self._form("review", {}, errors, placeholders={
            "speakers": str(len(self.draft["default_announcement_speakers"])),
            "phones": str(len(self.draft["phones"])), "satellites": str(len(self.draft["satellites"])),
            "live": "enabled" if self.draft["live_actions_enabled"] else "disabled",
        })
