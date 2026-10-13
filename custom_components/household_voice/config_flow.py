from aiohttp import ClientError
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
from .api import CoordinatorAuthError, CoordinatorError, VoiceClient
from .const import CONF_TOKEN, CONF_URL, DOMAIN
from .contracts import normalize_url


class HouseholdVoiceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import HouseholdVoiceOptionsFlow
        return HouseholdVoiceOptionsFlow()

    async def async_step_user(self, user_input=None):
        # Preserve API callers using the original external coordinator form.
        if user_input is not None and CONF_URL in user_input:
            return await self.async_step_external(user_input)
        return self.async_show_menu(step_id="user", menu_options=["embedded", "external"])

    async def async_step_embedded(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                from .local_setup import audio_base_url
                url = audio_base_url(self.hass, user_input.get("audio_base_url"))
            except ValueError:
                errors["base"] = "invalid_audio_url"
            else:
                # A single local database authority, including concurrent flows.
                await self.async_set_unique_id("household_voice_embedded")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Household Voice", data={
                    "mode": "embedded", "audio_base_url": url})
        return self.async_show_form(step_id="embedded", data_schema=vol.Schema({
            vol.Optional("audio_base_url"): str,
        }), errors=errors)

    async def async_step_external(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_URL])
                client = VoiceClient(async_get_clientsession(self.hass), url, user_input[CONF_TOKEN])
                await client.schedules()
            except CoordinatorAuthError:
                errors["base"] = "invalid_auth"
            except (CoordinatorError, ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Household Voice", data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]})
        return self.async_show_form(step_id="external", data_schema=vol.Schema({
            vol.Required(CONF_URL): str,
            vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }), errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        if entry.data.get('mode') != 'embedded':
            return self.async_abort(reason='external_reconfigure')
        errors = {}
        if user_input is not None:
            try:
                from .local_setup import audio_base_url
                url = audio_base_url(self.hass, user_input.get('audio_base_url'))
            except ValueError:
                errors['base'] = 'invalid_audio_url'
            else:
                return self.async_update_reload_and_abort(entry, data_updates={'audio_base_url': url})
        return self.async_show_form(step_id='reconfigure', data_schema=vol.Schema({
            vol.Optional('audio_base_url', default=entry.data.get('audio_base_url', '')): str,
        }), errors=errors)

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input:
            try:
                client = VoiceClient(async_get_clientsession(self.hass), entry.data[CONF_URL], user_input[CONF_TOKEN])
                await client.schedules()
            except (CoordinatorAuthError, ValueError):
                errors["base"] = "invalid_auth"
            except (CoordinatorError, ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]})
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({
            vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }), errors=errors)
