from datetime import timedelta
import logging
from aiohttp import ClientError
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .api import CoordinatorAuthError, CoordinatorError


class VoiceCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client):
        super().__init__(hass, logging.getLogger(__name__), name="Household Voice",
                         config_entry=entry, update_interval=timedelta(seconds=15), always_update=False)
        self.client = client
        self.entry_id = entry.entry_id
        self.entry = entry

    async def _async_update_data(self):
        try:
            from .runtime_config import async_configure
            discovered=await async_configure(self.client,self.entry.options,hass=self.hass)
            result=await self.client.schedules()
            if discovered is not None: result['area_pairings']=discovered
            return result
        except CoordinatorAuthError as error:
            raise ConfigEntryAuthFailed("Coordinator authentication failed") from error
        except (CoordinatorError, ClientError, TimeoutError) as error:
            raise UpdateFailed("Unable to retrieve household schedules") from error
